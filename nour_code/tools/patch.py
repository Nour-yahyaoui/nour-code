"""Unified-diff patching.

``apply_patch`` tries three strategies, in order, so it works whether or not
the host has git / patch installed and whether or not the project is a repo:

1. ``git apply`` (``-p1``, then ``-p0``, then ``-3``)
2. ``patch`` (``-p1``, then ``-p0``)
3. a self-contained pure-Python unified diff applier with fuzzy hunk matching

External binaries are only used when ``permissions.yaml`` allows them
(same allow-list as ``run_shell``), every target path is re-validated against
the allowed roots, and an optional timestamped backup is kept under
``logs/backups/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field

from ..permissions import PermissionDeniedError
from . import ToolContext, ToolResult, ToolSpec

MAX_DIFF_CHARS = 2_000_000
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ApplyPatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "File to patch, or the directory to apply a multi-file diff inside. "
            "Must be inside an allowed root."
        )
    )
    diff: str = Field(min_length=1, max_length=MAX_DIFF_CHARS, description="Unified diff text.")
    create_backup: bool = Field(default=False, description="Keep the original file under logs/backups/.")
    dry_run: bool = Field(default=False, description="Validate/report only, do not modify files.")
    timeout_seconds: int = Field(default=60, ge=1, le=600)


# ---------------------------------------------------------------------------
# Pure-Python unified diff engine
# ---------------------------------------------------------------------------
@dataclass
class Hunk:
    start_old: int
    len_old: int
    start_new: int
    len_new: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_delete: bool = False


def parse_unified_diff(diff: str) -> list[FilePatch]:
    """Parse a unified diff into :class:`FilePatch` objects.

    Raises :class:`ValueError` when the text is not a usable unified diff.
    """
    lines = diff.splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    index = 0
    saw_hunk = False

    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            old = line[4:].strip()
            # Peek for the +++ line
            if index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
                new = lines[index + 1][4:].strip()
                current = FilePatch(
                    old_path=_strip_quoted(old),
                    new_path=_strip_quoted(new),
                    is_new=(_strip_quoted(old) in {"/dev/null", "a/dev/null"}),
                    is_delete=(_strip_quoted(new) in {"/dev/null", "b/dev/null"}),
                )
                patches.append(current)
                index += 2
                continue
            index += 1
            continue
        match = _HUNK_RE.match(line)
        if match:
            saw_hunk = True
            if current is None:
                current = FilePatch(old_path=None, new_path="")
                patches.append(current)
            hunk = Hunk(
                start_old=int(match.group(1)),
                len_old=int(match.group(2) or "1"),
                start_new=int(match.group(3)),
                len_new=int(match.group(4) or "1"),
            )
            current.hunks.append(hunk)
            index += 1
            continue
        if hunk is not None and line[:1] in {" ", "+", "-", "\\"} and not line.startswith(("--- ", "+++ ")):
            if not line.startswith("\\"):
                hunk.lines.append(line)
            index += 1
            continue
        if line.startswith(("diff ", "index ", "new file", "deleted file", "similarity", "rename ")):
            hunk = None
            index += 1
            continue
        index += 1

    if not patches or not saw_hunk:
        raise ValueError(
            "diff does not look like a unified diff (expected '--- a/file', '+++ b/file' and '@@ ... @@' hunks)"
        )
    return patches


def _strip_quoted(value: str) -> str:
    """Strip a trailing timestamp from a diff header path."""
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def strip_level(path: str, level: int) -> str:
    """Apply ``-p<level>`` to a diff header path."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= level:
        return parts[-1] if parts else path
    return "/".join(parts[level:])


def _match_hunk(source: list[str], hunk: Hunk, *, fuzz: int = 200) -> int | None:
    """Find the source line index where ``hunk`` applies, or ``None``."""
    expected = [line[1:] for line in hunk.lines if line[:1] in {" ", "-"}]
    if not expected:
        # Pure insertion: clamp to the declared position.
        return max(0, min(hunk.start_old - 1, len(source)))
    anchor = max(0, hunk.start_old - 1)
    for offset in range(0, fuzz + 1):
        for candidate in ({anchor + offset, anchor - offset} if offset else {anchor}):
            if candidate < 0 or candidate + len(expected) > len(source):
                continue
            if all(
                source[candidate + i].rstrip("\r\n") == expected[i].rstrip("\r\n")
                for i in range(len(expected))
            ):
                return candidate
    return None


def apply_hunks(source: list[str], hunks: list[Hunk]) -> list[str]:
    """Apply every hunk to ``source`` (list of lines, newlines kept).

    Raises :class:`ValueError` with a helpful message when a hunk does not match.
    """
    result: list[str] = []
    cursor = 0
    for number, hunk in enumerate(hunks, start=1):
        position = _match_hunk(source, hunk)
        if position is None:
            raise ValueError(
                f"hunk {number} (@@ -{hunk.start_old},{hunk.len_old} +{hunk.start_new},{hunk.len_new} @@) "
                "does not match the target file"
            )
        result.extend(source[cursor:position])
        cursor = position
        for line in hunk.lines:
            prefix, content = line[:1], line[1:]
            if prefix == " ":
                result.append(source[cursor])
                cursor += 1
            elif prefix == "-":
                cursor += 1
            elif prefix == "+":
                result.append(content)
            else:  # pragma: no cover - parser filters these
                raise ValueError(f"unexpected diff line: {line!r}")
    result.extend(source[cursor:])
    return result


def _read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines(keepends=True)


# ---------------------------------------------------------------------------
# External helpers
# ---------------------------------------------------------------------------
async def _run(
    argv: list[str], *, cwd: Path, timeout: int
) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return 127, "", f"{argv[0]} unavailable: {exc}"
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:  # pragma: no cover
            out, err = b"", b""
        return 124, out.decode("utf-8", "replace"), f"timeout after {timeout}s"
    return (
        process.returncode if process.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _tool_allowed(ctx: ToolContext, binary: str) -> bool:
    perms = ctx.permissions
    if perms is None:  # pragma: no cover - defensive
        return False
    try:
        perms.check_shell(binary)
        return True
    except PermissionDeniedError:
        return False


async def _backup(ctx: ToolContext, path: Path) -> str | None:
    settings = ctx.settings
    if settings is None or not await anyio.Path(path).exists():
        return None
    target_dir = settings.log_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = target_dir / f"{stamp}-{path.name}"
    try:
        await anyio.to_thread.run_sync(lambda: shutil.copy2(path, destination))
    except OSError as exc:  # pragma: no cover - best effort only
        ctx.logger.warning("backup failed for %s: %s", path, exc)
        return None
    return str(destination)


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------
async def _strategy_git(ctx: ToolContext, base: Path, diff_file: Path, diff: str, dry_run: bool, timeout: int) -> tuple[bool, str, str]:
    if shutil.which("git") is None or not await _tool_allowed(ctx, "git apply"):
        return False, "", "git not usable"
    flags = ["apply", "--whitespace=nowarn", "--verbose"]
    if dry_run:
        flags.append("--check")
    attempts = [["-p1"], ["-p0"], ["-p1", "--3way"], ["-p1", "--ignore-whitespace"]]
    for extra in attempts:
        code, out, err = await _run(["git", *flags, *extra, str(diff_file)], cwd=base, timeout=timeout)
        if code == 0:
            return True, f"git apply {' '.join(extra)}", (out or err).strip()[:2000]
    return False, "", "git apply failed"


async def _strategy_patch(ctx: ToolContext, base: Path, diff_file: Path, diff: str, dry_run: bool, timeout: int) -> tuple[bool, str, str]:
    if shutil.which("patch") is None or not await _tool_allowed(ctx, "patch"):
        return False, "", "patch not usable"
    for level in ("-p1", "-p0"):
        argv = ["patch", level, "--forward", "--batch", "-d", str(base), "-i", str(diff_file)]
        if dry_run:
            argv.insert(2, "--dry-run")
        code, out, err = await _run(argv, cwd=base, timeout=timeout)
        if code == 0:
            return True, f"patch {level}", (out or err).strip()[:2000]
    return False, "", "patch failed"


async def _strategy_python(
    ctx: ToolContext, base: Path, diff: str, dry_run: bool
) -> tuple[bool, str, str]:
    perms = ctx.permissions
    assert perms is not None
    patches = parse_unified_diff(diff)
    touched: list[str] = []

    for file_patch in patches:
        candidates: list[str] = []
        for level in (1, 0):
            for candidate in (file_patch.new_path, file_patch.old_path):
                if candidate and candidate not in {"/dev/null"}:
                    stripped = strip_level(candidate, level)
                    if stripped and stripped not in candidates:
                        candidates.append(stripped)
        if not candidates:
            return False, "", "diff has no target path"

        target: Path | None = None
        for candidate in candidates:
            resolved = (base / candidate).resolve()
            if any(_within(resolved, root) for root in perms.roots):
                target = resolved
                break
        if target is None:
            return False, "", f"diff target {candidates[0]!r} escapes the allowed roots"

        if file_patch.is_delete:
            if not dry_run and await anyio.Path(target).exists():
                await anyio.Path(target).unlink()
            touched.append(str(target))
            continue

        source = _read_lines(target) if await anyio.Path(target).exists() else []
        if source and file_patch.is_new:
            # Re-creating an existing file: overwrite is safer than failing.
            source = []
        try:
            patched = apply_hunks(source, file_patch.hunks)
        except ValueError as exc:
            return False, "", f"{target.name}: {exc}"
        if not dry_run:
            await anyio.Path(target).parent.mkdir(parents=True, exist_ok=True)
            await anyio.Path(target).write_text("".join(patched), encoding="utf-8")
        touched.append(str(target))

    return True, "python-diff-engine", ", ".join(touched)


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
async def apply_patch(args: ApplyPatchArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    diff = args.diff
    if "\x00" in diff:
        return ToolResult.failure("diff contains a NUL byte")

    try:
        patches = parse_unified_diff(diff)
    except ValueError as exc:
        return ToolResult.failure(str(exc))

    path = perms.resolve_path(args.path, must_exist=False, allow_missing_parent=False)
    base = path if await anyio.Path(path).is_dir() else path.parent
    if not await anyio.Path(base).is_dir():
        return ToolResult.failure(f"patch target directory does not exist: {base}")

    backups: list[str] = []
    if args.create_backup and not args.dry_run:
        for file_patch in patches:
            for level in (1, 0):
                candidate = (base / strip_level(file_patch.new_path, level)).resolve()
                if any(_within(candidate, root) for root in perms.roots) and await anyio.Path(candidate).exists():
                    stored = await _backup(ctx, candidate)
                    if stored:
                        backups.append(stored)
                    break

    notes: list[str] = []
    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(diff if diff.endswith("\n") else diff + "\n")
        diff_file = Path(handle.name)

    try:
        success, strategy, detail = await _strategy_git(
            ctx, base, diff_file, diff, args.dry_run, args.timeout_seconds
        )
        notes.append(f"git: {detail or ('ok' if success else 'failed')}")
        if not success:
            success, strategy, detail = await _strategy_patch(
                ctx, base, diff_file, diff, args.dry_run, args.timeout_seconds
            )
            notes.append(f"patch: {detail or ('ok' if success else 'failed')}")
        if not success:
            success, strategy, detail = await _strategy_python(ctx, base, diff, args.dry_run)
            notes.append(f"python: {detail or ('ok' if success else 'failed')}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        return ToolResult.failure(f"apply_patch failed with {exc.__class__.__name__}: {exc}", notes=notes)
    finally:
        with contextlib.suppress(OSError):
            diff_file.unlink(missing_ok=True)

    if not success:
        return ToolResult.failure(
            "could not apply the diff with any strategy (git apply, patch, built-in engine). "
            + " | ".join(notes),
            dry_run=args.dry_run,
        )

    targets = [strip_level(p.new_path, 1) or p.new_path for p in patches]
    payload: dict[str, Any] = {
        "applied": not args.dry_run,
        "dry_run": args.dry_run,
        "strategy": strategy,
        "files": detail.split(", ") if strategy == "python-diff-engine" else targets,
        "base": str(base),
        "backups": backups,
        "notes": notes,
    }
    return ToolResult.success(payload, path=str(path), strategy=strategy)


def build_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="apply_patch",
            description=(
                "Apply a unified diff to a file or directory inside an allowed root. "
                "Uses git apply, then patch, then a built-in diff engine. The diff must include "
                "'--- a/path', '+++ b/path' and '@@ -a,b +c,d @@' hunks. Set dry_run=true to validate first."
            ),
            args_model=ApplyPatchArgs,
            handler=apply_patch,  # type: ignore[arg-type]
        )
    ]
