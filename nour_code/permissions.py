"""Permission scope for nour-code.

The scope is declared in ``permissions.yaml`` (path overridable with
``NOUR_CODE_PERMISSIONS_FILE``).  Every filesystem, shell and patch operation
goes through :class:`PermissionManager` *before* anything is touched, and every
refusal carries a human readable reason that is surfaced straight to the model
so it can correct itself instead of retrying blindly.

Threat model covered here
-------------------------
* path traversal (``../../etc/passwd``) and absolute escapes
* symlink escapes (a link inside a root that points outside of it)
* Windows drive-letter paths leaking into a POSIX root and vice versa
* shell command allow-listing + deny-list regexes + timeouts
* secret-ish files (``*.pem``, ``.env``, ssh keys) via ``denied_globs``
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_YAML = """\
version: 1
allowed_roots:
  - .
shell:
  allowed_commands: [npm, npx, node, cargo, git, python, pip, docker]
  denied_patterns: ["rm -rf", "format", "del /f /s /q C:"]
  default_timeout_seconds: 60
  max_timeout_seconds: 600
network:
  allow_outbound: true
tools:
  delete_file: false
  run_shell: true
"""

#: Commands that reach the internet; refused when ``network.allow_outbound`` is false.
NETWORK_COMMANDS = frozenset(
    {"npm", "npx", "yarn", "pnpm", "pip", "pip3", "docker", "curl", "wget", "cargo", "go", "uv"}
)

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class PermissionDeniedError(Exception):
    """Raised when an operation is outside the configured permission scope."""

    def __init__(self, reason: str, *, code: str = "permission_denied", detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.detail = detail or ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.reason} ({self.detail})"
        return self.reason


# ---------------------------------------------------------------------------
# Configuration schema
# ---------------------------------------------------------------------------
class FilesystemPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_read_bytes: int = Field(default=1_000_000, ge=1)
    max_write_bytes: int = Field(default=2_000_000, ge=1)
    denied_globs: list[str] = Field(default_factory=list)
    ignore_dirs: list[str] = Field(
        default_factory=lambda: [
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            "target",
            ".next",
            ".mypy_cache",
            ".pytest_cache",
        ]
    )


class ShellPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    allowed_commands: list[str] = Field(default_factory=list)
    denied_patterns: list[str] = Field(default_factory=list)
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_timeout_seconds: int = Field(default=600, ge=1)

    @field_validator("allowed_commands", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [c.strip() for c in value.split(",") if c.strip()]
        if isinstance(value, list):
            return [str(c).strip().lower() for c in value if str(c).strip()]
        return value

    @field_validator("max_timeout_seconds")
    @classmethod
    def _max_ge_default(cls, value: int, info: Any) -> int:
        default = (info.data or {}).get("default_timeout_seconds", 60)
        return max(value, default)


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    allow_outbound: bool = True


class PermissionConfig(BaseModel):
    """Parsed ``permissions.yaml``."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    allowed_roots: list[str] = Field(default_factory=list)
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    shell: ShellPolicy = Field(default_factory=ShellPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    tools: dict[str, bool] = Field(default_factory=dict)

    @field_validator("tools", mode="before")
    @classmethod
    def _boolify(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {str(k): bool(v) for k, v in value.items()}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class PermissionManager:
    """Loads the scope and answers *"may this operation happen?"* questions."""

    def __init__(
        self,
        config: PermissionConfig,
        *,
        working_dir: Path,
        warnings: Sequence[str] | None = None,
        source: Path | None = None,
    ) -> None:
        self.config = config
        self.working_dir = working_dir
        self.source = source
        self.warnings: list[str] = list(warnings or [])
        self.roots: list[Path] = [p for p in self._resolve_roots(working_dir)]
        self._denied: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in self.config.shell.denied_patterns
        ]

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: Path, *, working_dir: Path, root_overrides: Iterable[Path] | None = None) -> "PermissionManager":
        """Load ``permissions.yaml``, falling back to a safe default scope."""
        warnings: list[str] = []
        raw: dict[str, Any] | None = None
        using_defaults = False
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                loaded = yaml.safe_load(text) or {}
                if not isinstance(loaded, dict):
                    raise TypeError("permissions.yaml must contain a mapping")
                raw = loaded
            else:
                warnings.append(f"permissions file not found at {path}; using built-in defaults")
                raw = yaml.safe_load(DEFAULT_YAML)
                using_defaults = True
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"could not read {path}: {exc}; using built-in defaults")
            raw = yaml.safe_load(DEFAULT_YAML)
            using_defaults = True
        except (yaml.YAMLError, TypeError) as exc:
            warnings.append(f"invalid permissions file {path}: {exc}; using built-in defaults")
            raw = yaml.safe_load(DEFAULT_YAML)
            using_defaults = True

        assert raw is not None
        overrides = list(root_overrides or [])
        if overrides:
            raw["allowed_roots"] = [str(p) for p in overrides]
        try:
            config = PermissionConfig.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            warnings.append(f"permissions file rejected ({exc}); using built-in defaults")
            config = PermissionConfig.model_validate(yaml.safe_load(DEFAULT_YAML))
            config.allowed_roots = [str(working_dir)]
        if using_defaults:
            # Fail safe: the built-in scope is the working directory, nothing else.
            raw["allowed_roots"] = [str(working_dir)]
            config = PermissionConfig.model_validate(raw)
        return cls(config, working_dir=working_dir, warnings=warnings, source=path)

    def _resolve_roots(self, working_dir: Path) -> list[Path]:
        """Normalise configured roots, dropping ones that cannot be used here."""
        candidates: list[Path] = []
        for raw in self.config.allowed_roots:
            raw = str(raw).strip()
            if not raw:
                continue
            if _WIN_DRIVE.match(raw) and os.name != "nt":
                self.warnings.append(
                    f"ignoring Windows root {raw!r}: this host is not Windows "
                    "(set NOUR_CODE_ALLOWED_ROOTS to override)"
                )
                continue
            try:
                p = Path(os.path.expanduser(raw))
            except (RuntimeError, ValueError):
                self.warnings.append(f"ignoring malformed root {raw!r}")
                continue
            if not p.is_absolute():
                p = (self.source.parent if self.source else working_dir) / p
            candidates.append(p)

        existing: list[Path] = []
        for p in candidates:
            try:
                resolved = p.resolve()
            except OSError as exc:
                self.warnings.append(f"ignoring root {str(p)!r}: {exc}")
                continue
            if resolved.is_dir():
                existing.append(resolved)
            else:
                self.warnings.append(f"ignoring root {str(p)!r}: directory does not exist")

        if not existing:
            fallback = working_dir if working_dir.is_dir() else Path.cwd()
            sandbox = fallback / "sandbox"
            try:
                sandbox.mkdir(parents=True, exist_ok=True)
            except OSError:
                sandbox = fallback
            existing = [fallback.resolve(), sandbox.resolve()]
            self.warnings.append(
                "no usable allowed_roots configured; falling back to "
                f"{existing[0]} and {existing[1]}"
            )
        # de-duplicate, keep order
        seen: set[str] = set()
        unique: list[Path] = []
        for p in existing:
            key = os.path.normcase(str(p))
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _matches_any(path_str: str, patterns: Sequence[str]) -> str | None:
        """Match a relative path against glob patterns, also trying its basename."""
        base = path_str.rsplit("/", 1)[-1]
        for pattern in patterns:
            if (
                fnmatch.fnmatch(path_str, pattern)
                or fnmatch.fnmatch(path_str, f"**/{pattern}")
                or fnmatch.fnmatch(base, pattern)
            ):
                return pattern
        return None

    def _is_within(self, candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    # -- filesystem --------------------------------------------------------
    def check_tool(self, tool_name: str) -> None:
        """Raise if ``tool_name`` is switched off in the ``tools:`` section."""
        allowed = self.config.tools.get(tool_name, True)
        if not allowed:
            raise PermissionDeniedError(
                f"tool '{tool_name}' is disabled in permissions.yaml (tools.{tool_name}: false)",
                code="tool_disabled",
            )

    def resolve_path(
        self,
        raw: str | os.PathLike[str],
        *,
        must_exist: bool = False,
        kind: str = "file",
        for_write: bool = False,
        allow_missing_parent: bool = True,
    ) -> Path:
        """Validate ``raw`` and return the fully resolved :class:`Path`.

        Raises :class:`PermissionDeniedError` for anything outside the scope.
        """
        if raw is None:
            raise PermissionDeniedError("path is required", code="invalid_path")
        text = os.fspath(raw)
        if not isinstance(text, str) or not text.strip():
            raise PermissionDeniedError("path must be a non-empty string", code="invalid_path")
        if _CONTROL_CHARS.search(text):
            raise PermissionDeniedError(
                "path contains control characters", code="invalid_path", detail=repr(text[:64])
            )
        if _WIN_DRIVE.match(text) and os.name != "nt":
            raise PermissionDeniedError(
                f"Windows path {text!r} cannot be used on this host", code="outside_root"
            )

        try:
            expanded = Path(os.path.expanduser(text.strip()))
        except (RuntimeError, ValueError) as exc:
            raise PermissionDeniedError(f"unusable path {text!r}: {exc}", code="invalid_path") from exc

        candidate = expanded if expanded.is_absolute() else (self.default_root() / expanded)
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise PermissionDeniedError(f"cannot resolve {text!r}: {exc}", code="invalid_path") from exc

        root = next((r for r in self.roots if self._is_within(resolved, r)), None)
        if root is None:
            raise PermissionDeniedError(
                f"path {str(resolved)!r} is outside the allowed roots "
                f"({', '.join(str(r) for r in self.roots)})",
                code="outside_root",
            )

        # Symlink escape: every existing component must itself stay in scope.
        try:
            rel_parts = candidate.relative_to(candidate.anchor).parts
        except ValueError:  # pragma: no cover - defensive
            rel_parts = ()
        current = Path(candidate.anchor)
        for part in rel_parts:
            current = current / part
            try:
                is_link = current.is_symlink()
            except OSError as exc:
                raise PermissionDeniedError(
                    f"cannot inspect {str(current)!r}: {exc}", code="invalid_path"
                ) from exc
            if is_link:
                try:
                    target = current.resolve()
                except OSError as exc:
                    raise PermissionDeniedError(
                        f"cannot resolve symlink {str(current)!r}: {exc}", code="symlink_escape"
                    ) from exc
                if not any(self._is_within(target, r) for r in self.roots):
                    raise PermissionDeniedError(
                        f"symlink {str(current)!r} escapes the allowed roots (target {str(target)!r})",
                        code="symlink_escape",
                    )

        # Denied globs (secrets, keys, env files).
        try:
            rel_for_match = resolved.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - guarded above
            rel_for_match = resolved.as_posix()
        match = self._matches_any(rel_for_match, self.config.filesystem.denied_globs)
        if match is None:
            match = self._matches_any(resolved.name, self.config.filesystem.denied_globs)
        if match:
            raise PermissionDeniedError(
                f"path {str(resolved)!r} matches denied pattern {match!r}", code="denied_pattern"
            )

        if must_exist:
            if not resolved.exists():
                raise PermissionDeniedError(f"path does not exist: {str(resolved)}", code="not_found")
            if kind == "file" and not resolved.is_file():
                raise PermissionDeniedError(f"not a file: {str(resolved)}", code="not_a_file")
            if kind == "dir" and not resolved.is_dir():
                raise PermissionDeniedError(f"not a directory: {str(resolved)}", code="not_a_dir")
        elif not allow_missing_parent and not resolved.parent.is_dir():
            raise PermissionDeniedError(
                f"parent directory does not exist: {str(resolved.parent)}", code="not_found"
            )
        if for_write and resolved.is_dir() and kind == "file":
            raise PermissionDeniedError(f"path is a directory: {str(resolved)}", code="not_a_file")
        return resolved

    def default_root(self) -> Path:
        """Root used to resolve relative paths."""
        wd = self.working_dir
        for root in self.roots:
            if self._is_within(wd, root):
                return wd if wd.is_dir() else root
        return self.roots[0]

    def check_read_size(self, size: int) -> None:
        if size > self.config.filesystem.max_read_bytes:
            raise PermissionDeniedError(
                f"file too large to read ({size} bytes > "
                f"{self.config.filesystem.max_read_bytes} bytes limit)",
                code="too_large",
            )

    def check_write_size(self, size: int) -> None:
        if size > self.config.filesystem.max_write_bytes:
            raise PermissionDeniedError(
                f"content too large to write ({size} chars > "
                f"{self.config.filesystem.max_write_bytes} chars limit)",
                code="too_large",
            )

    def check_network(self, tool_or_command: str) -> None:
        if not self.config.network.allow_outbound:
            raise PermissionDeniedError(
                f"outbound network access is disabled, so '{tool_or_command}' cannot run "
                "(network.allow_outbound: false)",
                code="network_denied",
            )

    # -- shell -------------------------------------------------------------
    def check_shell(self, command: str) -> str:
        """Validate a shell command; returns the executable that will run."""
        if not isinstance(command, str) or not command.strip():
            raise PermissionDeniedError("shell command must be a non-empty string", code="invalid_command")
        cleaned = command.strip()
        if len(cleaned) > 4096:
            raise PermissionDeniedError("shell command exceeds 4096 characters", code="too_long")
        if "\x00" in cleaned:
            raise PermissionDeniedError("shell command contains a NUL byte", code="invalid_command")
        if "\n" in cleaned or "\r" in cleaned:
            raise PermissionDeniedError(
                "multi-line shell commands are rejected; use && or ; instead", code="invalid_command"
            )
        for pattern in self._denied:
            if pattern.search(cleaned):
                raise PermissionDeniedError(
                    f"command matches denied pattern /{pattern.pattern}/", code="denied_pattern"
                )
        try:
            tokens = shlex.split(cleaned, posix=(os.name != "nt"))
        except ValueError as exc:
            raise PermissionDeniedError(f"cannot parse command: {exc}", code="invalid_command") from exc
        if not tokens:
            raise PermissionDeniedError("empty command after parsing", code="invalid_command")

        # Skip leading VAR=value assignments to find the real executable.
        idx = 0
        while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            raise PermissionDeniedError("command has no executable", code="invalid_command")
        executable = Path(tokens[idx]).name.lower()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        if executable in {"sudo", "su", "doas"}:
            raise PermissionDeniedError(
                f"privilege escalation via '{executable}' is not permitted", code="denied_pattern"
            )
        allowed = self.config.shell.allowed_commands
        if allowed and executable not in allowed:
            raise PermissionDeniedError(
                f"'{executable}' is not in shell.allowed_commands "
                f"({', '.join(allowed)})",
                code="command_not_allowed",
            )
        if executable in NETWORK_COMMANDS:
            self.check_network(executable)
        return executable

    def clamp_timeout(self, timeout_seconds: int | None) -> int:
        default = self.config.shell.default_timeout_seconds
        maximum = self.config.shell.max_timeout_seconds
        if timeout_seconds is None:
            return default
        try:
            value = int(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise PermissionDeniedError(f"invalid timeout {timeout_seconds!r}", code="invalid_timeout") from exc
        return max(1, min(value, maximum))

    # -- introspection -----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Scope summary injected into the system prompt and shown by the API."""
        return {
            "allowed_roots": [str(r) for r in self.roots],
            "working_dir": str(self.working_dir),
            "shell_allowed_commands": list(self.config.shell.allowed_commands),
            "shell_denied_patterns": list(self.config.shell.denied_patterns),
            "shell_timeout": {
                "default": self.config.shell.default_timeout_seconds,
                "max": self.config.shell.max_timeout_seconds,
            },
            "network_allow_outbound": self.config.network.allow_outbound,
            "tools": dict(self.config.tools),
            "denied_globs": list(self.config.filesystem.denied_globs),
            "warnings": list(self.warnings),
            "source": str(self.source) if self.source else None,
        }
