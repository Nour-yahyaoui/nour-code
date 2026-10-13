"""Filesystem tools: read_file, write_file, list_directory, search_files, delete_file.

Rules enforced for every call:

* the path must resolve **inside** an allowed root (no traversal, no symlink escapes)
* secret-ish files (``*.pem``, ``.env``, ssh keys) are refused via ``denied_globs``
* reads and writes are size-capped, listings are entry-capped
* every call is audited (see :mod:`nour_code.observability`)

All filesystem access goes through :mod:`pathlib` / :class:`anyio.Path` — never
``os.path`` string munging.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..permissions import PermissionDeniedError
from . import ToolContext, ToolResult, ToolSpec

IGNORE_DIRS_DEFAULT = (
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
    ".ruff_cache",
)


def _ignore_dirs(ctx: ToolContext) -> frozenset[str]:
    perms = ctx.permissions
    if perms is None:
        return frozenset(IGNORE_DIRS_DEFAULT)
    configured = perms.config.filesystem.ignore_dirs
    return frozenset(configured or IGNORE_DIRS_DEFAULT)


async def _exists(path: Path) -> bool:
    return await anyio.Path(path).exists()


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Absolute path, or relative to the session working directory.")
    max_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    encoding: str = "utf-8"


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    create_parents: bool = True
    append: bool = False
    encoding: str = "utf-8"


class ListDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=500, ge=1, le=10_000)
    include_ignored: bool = False


class SearchFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = "."
    pattern: str = Field(description="Glob pattern, e.g. '*.py', '**/*.tsx', 'src/**/*.rs'.")
    content_regex: str | None = Field(
        default=None, description="Optional regex; when set, matching lines are returned as well."
    )
    max_results: int = Field(default=200, ge=1, le=5_000)
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=20_000_000)
    include_ignored: bool = False

    @field_validator("content_regex")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return value


class DeleteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    recursive: bool = Field(
        default=False, description="Allow deleting a non-empty directory (still refused for allowed roots)."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _read_bytes(path: Path, ctx: ToolContext, max_bytes: int) -> bytes:
    perms = ctx.permissions
    assert perms is not None
    info = await anyio.Path(path).stat()
    if info.st_size > max_bytes:
        raise PermissionDeniedError(
            f"file is {info.st_size} bytes, above the {max_bytes} byte read limit "
            "(raise filesystem.max_read_bytes in permissions.yaml)",
            code="too_large",
        )
    return await anyio.Path(path).read_bytes()


async def _iter_entries(root: Path, include_ignored: bool, ignore: frozenset[str], limit: int):
    """Yield directory entries, skipping ignored directories. Bounded by ``limit``."""
    anyio_root = anyio.Path(root)
    try:
        entries = [entry async for entry in anyio_root.iterdir()]
    except NotADirectoryError as exc:
        raise PermissionDeniedError(f"not a directory: {root}", code="not_a_dir") from exc
    entries.sort(key=lambda p: p.name)
    for entry in entries:
        if len(entries) and not include_ignored and entry.name in ignore:
            continue
        yield entry


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def read_file(args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    path = perms.resolve_path(args.path, must_exist=True, kind="file")
    max_bytes = min(perms.config.filesystem.max_read_bytes, max(args.max_chars, 1) * 4)
    try:
        raw = await _read_bytes(path, ctx, max_bytes)
    except PermissionDeniedError:
        raise
    if b"\x00" in raw[:8192]:
        return ToolResult.failure(
            f"{path} looks like a binary file (NUL byte in the first 8 KB); refusing to read it as text"
        )
    try:
        text = raw.decode(args.encoding, errors="replace")
    except LookupError as exc:
        return ToolResult.failure(f"unknown encoding {args.encoding!r}: {exc}")
    truncated = len(text) > args.max_chars
    if truncated:
        text = text[: args.max_chars] + f"\n...[truncated at {args.max_chars} chars]..."
    return ToolResult.success(
        text,
        path=str(path),
        bytes=len(raw),
        truncated=truncated,
        encoding=args.encoding,
    )


async def write_file(args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    perms.check_write_size(len(args.content))
    path = perms.resolve_path(args.path, for_write=True, allow_missing_parent=args.create_parents)

    if await _exists(path) and (await anyio.Path(path).is_dir()):
        return ToolResult.failure(f"{path} is a directory; refusing to write a file over it")

    try:
        if args.create_parents:
            await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        elif not await anyio.Path(path.parent).is_dir():
            return ToolResult.failure(f"parent directory does not exist: {path.parent}")

        if args.append and await _exists(path):
            async with await anyio.open_file(path, "a", encoding=args.encoding) as handle:
                await handle.write(args.content)
        else:
            await _atomic_write(path, args.content, args.encoding)
    except UnicodeEncodeError as exc:
        return ToolResult.failure(f"cannot encode content with {args.encoding!r}: {exc}")

    size = (await anyio.Path(path).stat()).st_size
    return ToolResult.success(
        {"written": True, "path": str(path), "bytes": size, "appended": args.append},
        path=str(path),
        bytes=size,
        action="append" if args.append else "write",
    )


async def _atomic_write(path: Path, content: str, encoding: str) -> None:
    """Write via a temp file in the same directory, then atomically replace."""
    directory = path.parent

    def _sync() -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=".nour-code-", suffix=".tmp", dir=str(directory))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    await anyio.to_thread.run_sync(_sync)


async def list_directory(args: ListDirectoryArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    root = perms.resolve_path(args.path, must_exist=True, kind="dir")
    ignore = _ignore_dirs(ctx)
    state = {"count": 0, "truncated": False}

    async def _walk(directory: Path, depth: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if depth > 12:
            return out
        async for entry in _iter_entries(directory, args.include_ignored, ignore, args.max_entries):
            if state["count"] >= args.max_entries:
                state["truncated"] = True
                break
            try:
                info = await entry.stat()
                is_dir = stat.S_ISDIR(info.st_mode)
                is_link = stat.S_ISLNK(info.st_mode)
            except OSError:
                continue
            state["count"] += 1
            node: dict[str, Any] = {
                "name": entry.name,
                "path": str(entry),
                "type": "dir" if is_dir else ("link" if is_link else "file"),
                "size": 0 if is_dir else info.st_size,
            }
            if args.recursive and is_dir and not is_link:
                node["children"] = await _walk(Path(entry), depth + 1)
            out.append(node)
        return out

    entries = await _walk(root, 0)
    return ToolResult.success(
        {
            "path": str(root),
            "recursive": args.recursive,
            "count": len(entries),
            "truncated": state["truncated"],
            "entries": entries,
        },
        path=str(root),
        count=len(entries),
    )


async def search_files(args: SearchFilesArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    root = perms.resolve_path(args.root, must_exist=True, kind="dir")
    ignore = _ignore_dirs(ctx)
    matcher = re.compile(args.content_regex) if args.content_regex else None
    anyio_root = anyio.Path(root)

    matches: list[dict[str, Any]] = []
    truncated = False
    scanned = 0

    # ``rglob`` handles '**' patterns; a bare '*.py' should search recursively too.
    pattern = args.pattern if args.pattern.startswith("**") else f"**/{args.pattern}"
    async for candidate in anyio_root.rglob(pattern.lstrip("/")):
        if len(matches) >= args.max_results:
            truncated = True
            break
        try:
            if not await candidate.is_file():
                continue
        except OSError:
            continue
        rel_parts = candidate.relative_to(root).parts[:-1]
        if not args.include_ignored and any(part in ignore for part in rel_parts):
            continue
        try:
            resolved = perms.resolve_path(str(candidate), must_exist=True, kind="file")
        except PermissionDeniedError:
            continue
        scanned += 1
        if matcher is None:
            try:
                size = (await candidate.stat()).st_size
            except OSError:
                size = 0
            matches.append({"path": str(resolved), "size": size})
            continue

        try:
            size = (await candidate.stat()).st_size
        except OSError:
            continue
        if size > args.max_file_bytes:
            continue
        try:
            text = await candidate.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if "\x00" in text[:4096]:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                matches.append(
                    {
                        "path": str(resolved),
                        "line": lineno,
                        "text": line.strip()[:300],
                    }
                )
                break
        if len(matches) >= args.max_results:
            truncated = True
            break

    return ToolResult.success(
        {
            "root": str(root),
            "pattern": args.pattern,
            "count": len(matches),
            "scanned": scanned,
            "truncated": truncated,
            "matches": matches,
        },
        root=str(root),
        count=len(matches),
    )


async def delete_file(args: DeleteFileArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None
    path = perms.resolve_path(args.path, must_exist=True)

    for root in perms.roots:
        if path == root:
            return ToolResult.denied(f"refusing to delete an allowed root: {path}")

    target = anyio.Path(path)
    removed = 1
    if await target.is_dir():
        if not args.recursive:
            return ToolResult.failure(
                f"{path} is a directory; pass recursive=true to delete it (files inside are not recoverable)"
            )
        removed = await _rmtree(path)
        action = "deleted_directory"
    else:
        await target.unlink()
        action = "deleted_file"

    return ToolResult.success(
        {"deleted": True, "path": str(path), "action": action, "entries_removed": removed},
        path=str(path),
    )


async def _rmtree(path: Path) -> int:
    """Recursively delete ``path`` without following symlinks. Returns count removed."""
    removed = 0
    directory = anyio.Path(path)
    try:
        entries = [entry async for entry in directory.iterdir()]
    except NotADirectoryError:  # pragma: no cover - guarded by the caller
        return 0
    for entry in entries:
        try:
            is_dir = await entry.is_dir()
            is_link = await entry.is_symlink()
        except OSError:
            continue
        if is_dir and not is_link:
            removed += await _rmtree(Path(entry))
        else:
            try:
                await entry.unlink()
            except OSError:
                continue
            removed += 1
    try:
        await directory.rmdir()
    except OSError:  # pragma: no cover - already gone
        pass
    return removed


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
def build_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read_file",
            description=(
                "Read a UTF-8 text file inside an allowed root. Returns the file contents "
                "(truncated at max_chars). Use list_directory/search_files to find paths first."
            ),
            args_model=ReadFileArgs,
            handler=read_file,  # type: ignore[arg-type]
        ),
        ToolSpec(
            name="write_file",
            description=(
                "Create or overwrite a file with the given content. Parent directories are created "
                "automatically. Prefer apply_patch when editing an existing file."
            ),
            args_model=WriteFileArgs,
            handler=write_file,  # type: ignore[arg-type]
        ),
        ToolSpec(
            name="list_directory",
            description="List the entries of a directory (optionally recursive).",
            args_model=ListDirectoryArgs,
            handler=list_directory,  # type: ignore[arg-type]
        ),
        ToolSpec(
            name="search_files",
            description=(
                "Find files by glob pattern (e.g. '*.ts', 'src/**/*.rs'), optionally filtered by a "
                "content regex. Ignored directories (node_modules, .git, target, ...) are skipped."
            ),
            args_model=SearchFilesArgs,
            handler=search_files,  # type: ignore[arg-type]
        ),
        ToolSpec(
            name="delete_file",
            description=(
                "Delete a file (or a directory with recursive=true). Disabled by default in "
                "permissions.yaml (tools.delete_file: false)."
            ),
            args_model=DeleteFileArgs,
            handler=delete_file,  # type: ignore[arg-type]
        ),
    ]
