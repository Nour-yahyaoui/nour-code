"""Shell execution tool.

The command is validated *before* a process is spawned:

1. ``tools.run_shell`` must be enabled in ``permissions.yaml``
2. the executable must appear in ``shell.allowed_commands``
3. the whole command line must not match any ``shell.denied_patterns`` regex
4. ``cwd`` must resolve inside an allowed root
5. the timeout is clamped to ``shell.max_timeout_seconds``

and while running:

* stdin is closed, stdout/stderr are captured and size-capped
* on timeout the whole process **group** is SIGTERM'd then SIGKILL'd
  (``taskkill /F /T`` on Windows)
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..permissions import PermissionDeniedError
from . import ToolContext, ToolResult, ToolSpec

MAX_COMMAND_CHARS = 4096


class RunShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str | None = Field(default=None, description="Defaults to the session working directory.")
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    env: dict[str, str] = Field(default_factory=dict)
    max_output_chars: int = Field(default=50_000, ge=1_000, le=1_000_000)


def _build_env(extra: dict[str, str], session_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "NOUR_CODE_SESSION_ID": session_id,
            "PYTHONUNBUFFERED": "1",
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
            "CI": env.get("CI", ""),
        }
    )
    for key, value in (extra or {}).items():
        if not key or "=" in key or "\x00" in key:
            raise PermissionDeniedError(f"invalid environment variable name {key!r}", code="invalid_env")
        env[str(key)] = str(value)
    return env


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group, escalate to SIGKILL, never raise."""
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()
    except (ProcessLookupError, OSError):  # pragma: no cover - already gone
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            asyncio.create_task(_windows_kill(process))
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
        process.kill()


async def _windows_kill(process: asyncio.subprocess.Process) -> None:  # pragma: no cover
    killer = await asyncio.create_subprocess_exec(
        "taskkill", "/F", "/T", "/PID", str(process.pid),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await killer.wait()


def _decode(raw: bytes | None, limit: int) -> tuple[str, bool]:
    if not raw:
        return "", False
    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]...", True
    return text, False


async def run_shell(args: RunShellArgs, ctx: ToolContext) -> ToolResult:
    perms = ctx.permissions
    assert perms is not None

    executable = perms.check_shell(args.command)
    timeout = perms.clamp_timeout(args.timeout_seconds)

    if args.cwd:
        cwd = perms.resolve_path(args.cwd, must_exist=True, kind="dir")
    else:
        cwd = perms.resolve_path(str(ctx.working_dir), must_exist=True, kind="dir")

    env = _build_env(args.env, ctx.session_id)
    half = max(1, args.max_output_chars // 2)

    ctx.logger.info(
        "shell start",
        extra={"executable": executable, "cwd": str(cwd), "timeout": timeout, "session_id": ctx.session_id},
    )

    popen_kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "stdin": asyncio.subprocess.DEVNULL,
        "cwd": str(cwd),
        "env": env,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows-only branch
        # Own process group so the whole tree can be killed on timeout.
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    try:
        process = await asyncio.create_subprocess_shell(args.command, **popen_kwargs)
    except (OSError, ValueError) as exc:
        return ToolResult.failure(f"could not start '{executable}': {exc}")

    timed_out = False
    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate(process)
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError):  # pragma: no cover - stubborn process
            raw_out, raw_err = b"", b""
    except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
        await _terminate(process)
        raise

    stdout, out_truncated = _decode(raw_out, half)
    stderr, err_truncated = _decode(raw_err, half)
    exit_code = process.returncode if process.returncode is not None else -1

    payload = {
        "command": args.command,
        "cwd": str(cwd),
        "executable": executable,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_truncated or err_truncated,
    }
    if timed_out:
        return ToolResult.failure(
            f"command timed out after {timeout}s (exit_code={exit_code})", **payload
        )
    if exit_code != 0:
        return ToolResult.failure(f"command exited with {exit_code}", **payload)
    return ToolResult.success(payload, exit_code=exit_code, cwd=str(cwd))


def build_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="run_shell",
            description=(
                "Run an allow-listed shell command (npm, npx, node, cargo, git, python, pip, docker) "
                "inside an allowed root with a hard timeout. Returns stdout, stderr and exit_code. "
                "Pipes and && are allowed; multi-line scripts are not."
            ),
            args_model=RunShellArgs,
            handler=run_shell,  # type: ignore[arg-type]
        )
    ]
