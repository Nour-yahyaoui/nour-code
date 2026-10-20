"""Terminal chat: talk to the agent without the HTTP layer.

Two ways in, both from a checkout (no server, no browser)::

    python -m nour_code --chat              # interactive prompt
    python -m nour_code --chat "list the files here"

    python -m nour_code.agent               # same thing
    python agent.py                         # same thing, from inside nour_code/

The REPL streams tokens as they arrive, prints every tool call with its result,
and keeps one session for the whole conversation, so ``/reset`` is the only way
to forget context.  Slash commands: ``/help``, ``/tools``, ``/reset``, ``/exit``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from . import __version__
from .config import APP_NAME, Settings, load_settings

if TYPE_CHECKING:
    from .main import Container
    from .sessions import Session

RESET, DIM, BOLD, RED, GREEN, CYAN = "\033[0m", "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[36m"
MAX_ARGS_CHARS = 160

BANNER = f"""{APP_NAME} {__version__} - local agent on Ollama
  model    {{model}}
  workdir  {{workdir}}
  /help for commands, /exit to quit (Ctrl+C stops the current turn)
"""

HELP = """commands
  /tools          list the tools the model may call
  /reset          start a fresh session (forget the conversation)
  /exit, /quit    leave
  Ctrl+C          stop the current turn, keep the session"""


# ---------------------------------------------------------------------------
# terminal output
# ---------------------------------------------------------------------------
@dataclass
class Terminal:
    """Tiny write helper: colour only when the stream is a real tty."""

    stream: TextIO | None = None
    color: bool | None = None
    show_thinking: bool = False
    show_usage: bool = False
    _buffer: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stream = self.stream or sys.stdout
        if self.color is None:
            is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
            self.color = is_tty and os.environ.get("NO_COLOR") is None

    # -- primitives --------------------------------------------------------
    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def write(self, text: str) -> None:
        assert self.stream is not None
        self.stream.write(text)
        self.stream.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def dim(self, text: str) -> None:
        self.write(self._paint(text, DIM))

    def prompt(self, text: str) -> str:
        return self._paint(text, CYAN)

    # -- events ------------------------------------------------------------
    def token(self, text: str) -> None:
        self._buffer.append(text)
        self.write(text)

    def thinking(self, text: str) -> None:
        if self.show_thinking:
            self.dim(text)

    def tool_call(self, tool: str, arguments: Any) -> None:
        self.line()
        self.line(self._paint(f"  -> {tool}({_compact(arguments)})", CYAN))

    def tool_result(self, tool: str, status: str, duration_ms: Any, error: str | None) -> None:
        minutes = f" ({_duration(duration_ms)})" if duration_ms else ""
        if status == "ok":
            self.line(self._paint(f"     ok  {tool}{minutes}", GREEN))
        else:
            reason = f": {error}" if error else ""
            self.line(self._paint(f"     {status}  {tool}{minutes}{reason}", RED))

    def file_change(self, action: str, path: str) -> None:
        self.line(self._paint(f"     {action} {path}", GREEN))

    def usage(self, data: dict[str, Any]) -> None:
        if self.show_usage:
            self.dim(
                f"     [{data.get('prompt_tokens', 0)} prompt + "
                f"{data.get('completion_tokens', 0)} completion tokens]\n"
            )

    def error(self, message: str) -> None:
        self.line()
        self.line(self._paint(f"  error: {message}", RED))

    def clear_buffer(self) -> None:
        self._buffer.clear()


def _compact(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        text = str(arguments)
    else:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= MAX_ARGS_CHARS else text[: MAX_ARGS_CHARS - 1] + "…"


def _duration(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


# ---------------------------------------------------------------------------
# one turn
# ---------------------------------------------------------------------------
async def run_turn(
    container: Container,
    session: Session,
    message: str,
    *,
    terminal: Terminal,
    max_iterations: int | None = None,
) -> str:
    """Stream one user message to the terminal; return the final answer text."""
    answer: list[str] = []
    failed = False
    async for event in container.agent.run_stream(
        session,
        message,
        max_iterations=max_iterations,
        audit=container.audit,
    ):
        data = event.data
        if event.type == "token":
            text = str(data.get("text", ""))
            answer.append(text)
            terminal.token(text)
        elif event.type == "thinking":
            terminal.thinking(str(data.get("text", "")))
        elif event.type == "tool_call":
            terminal.tool_call(str(data.get("tool", "?")), data.get("arguments"))
        elif event.type == "tool_result":
            terminal.tool_result(
                str(data.get("tool", "?")),
                str(data.get("status", "error")),
                data.get("duration_ms"),
                data.get("error"),
            )
        elif event.type == "file_change":
            terminal.file_change(str(data.get("action", "changed")), str(data.get("path", "")))
        elif event.type == "usage":
            terminal.usage(data)
        elif event.type == "error":
            failed = True
            terminal.error(str(data.get("message", "unknown error")))
    terminal.line()
    terminal.clear_buffer()
    return "" if failed and not answer else "".join(answer)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
async def repl(
    container: Container,
    session: Session,
    *,
    terminal: Terminal,
    ask: Callable[[str], str] | None = None,
    max_iterations: int | None = None,
) -> int:
    """Interactive loop. ``ask`` is injectable so tests never touch stdin."""
    reader = ask or input
    terminal.line(
        BANNER.format(model=container.settings.model, workdir=container.settings.working_dir)
    )
    current = session
    while True:
        try:
            raw = await asyncio.to_thread(reader, terminal.prompt("you> "))
        except (EOFError, KeyboardInterrupt):
            terminal.line()
            return 0
        message = (raw or "").strip()
        if not message:
            continue
        lowered = message.lower()
        if lowered in {"/exit", "/quit"}:
            return 0
        if lowered in {"/help", "?"}:
            terminal.line(HELP)
            continue
        if lowered == "/tools":
            terminal.line("\n".join(f"  {name}" for name in container.registry.names()))
            continue
        if lowered == "/reset":
            current = await container.store.get_or_create()
            terminal.dim(f"new session {current.session_id}\n")
            continue
        try:
            await run_turn(container, current, message, terminal=terminal, max_iterations=max_iterations)
        except KeyboardInterrupt:
            terminal.line()
            terminal.error("turn cancelled")
        except Exception as exc:  # noqa: BLE001 - the REPL must survive a bad turn
            terminal.error(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{APP_NAME} chat",
        description="Talk to the local agent in the terminal (same agent as the HTTP API).",
    )
    parser.add_argument("message", nargs="*", help="one-shot prompt; omit for an interactive session")
    parser.add_argument("--model", default=None, help="Ollama model (NOUR_CODE_MODEL)")
    parser.add_argument("--working-dir", default=None, help="directory the agent works in")
    parser.add_argument("--session", default=None, help="resume a session id")
    parser.add_argument("--max-iterations", type=int, default=None, help="cap the tool loop")
    parser.add_argument("--think", action="store_true", help="print Qwen3's reasoning as it streams")
    parser.add_argument("--usage", action="store_true", help="print token counts after each model call")
    parser.add_argument("--no-color", action="store_true", help="never emit ANSI colour")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


async def _make_container(settings: Settings) -> Container:
    """Imported lazily: the HTTP layer imports this package, so avoid a cycle."""
    from .main import Container

    return await Container.create(settings)


async def _serve(
    settings: Settings,
    arguments: argparse.Namespace,
    *,
    terminal: Terminal,
    ask: Callable[[str], str] | None = None,
) -> int:
    container = await _make_container(settings)
    try:
        session = await container.store.get_or_create(arguments.session)
        if arguments.message:
            await run_turn(
                container,
                session,
                " ".join(arguments.message),
                terminal=terminal,
                max_iterations=arguments.max_iterations,
            )
            return 0
        return await repl(
            container,
            session,
            terminal=terminal,
            ask=ask,
            max_iterations=arguments.max_iterations,
        )
    finally:
        with contextlib.suppress(Exception):
            await container.aclose()


def main(
    argv: list[str] | None = None,
    *,
    terminal: Terminal | None = None,
    ask: Callable[[str], str] | None = None,
) -> int:
    """``python -m nour_code --chat`` / ``python -m nour_code.agent``."""
    arguments = build_parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if arguments.model:
        overrides["model"] = arguments.model
    if arguments.working_dir:
        overrides["working_dir"] = arguments.working_dir
    settings = load_settings(**overrides)

    output = terminal or Terminal(color=False if arguments.no_color else None)
    output.show_thinking = bool(arguments.think)
    output.show_usage = bool(arguments.usage)

    try:
        return asyncio.run(_serve(settings, arguments, terminal=output, ask=ask))
    except KeyboardInterrupt:
        output.line()
        output.dim("stopped.")
        return 130


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main())
