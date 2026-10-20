"""Terminal chat: streaming output, tool lines, slash commands and the CLI."""

from __future__ import annotations

import io
from typing import Any

import pytest
from nour_code.chat import HELP, Terminal, build_parser, main, repl, run_turn
from nour_code.config import Settings


def terminal(**kwargs: Any) -> tuple[Terminal, io.StringIO]:
    stream = io.StringIO()
    return Terminal(stream=stream, color=False, **kwargs), stream


async def test_run_turn_streams_the_answer(container) -> None:
    container.agent.client.add_text("Hello from qwen.")
    out, buffer = terminal()
    session = await container.store.get_or_create()
    content = await run_turn(container, session, "hi", terminal=out)
    assert content == "Hello from qwen."
    assert "Hello from qwen." in buffer.getvalue()
    assert buffer.getvalue().endswith("\n")


async def test_run_turn_prints_tool_calls_and_results(container) -> None:
    container.agent.client.add_tool_call("list_directory", path=".")
    container.agent.client.add_text("done")
    out, buffer = terminal()
    session = await container.store.get_or_create()
    await run_turn(container, session, "what is here?", terminal=out)
    text = buffer.getvalue()
    assert "-> list_directory(" in text
    assert "ok  list_directory" in text
    assert "done" in text


async def test_thinking_is_hidden_unless_requested(container) -> None:
    container.agent.client.script = [{"thinking": "secret reasoning", "content": "answer"}]
    quiet, quiet_buffer = terminal()
    session = await container.store.get_or_create()
    await run_turn(container, session, "hi", terminal=quiet)
    assert "secret reasoning" not in quiet_buffer.getvalue()

    container.agent.client.script = [{"thinking": "secret reasoning", "content": "answer"}]
    loud, loud_buffer = terminal(show_thinking=True)
    await run_turn(container, session, "hi", terminal=loud)
    assert "secret reasoning" in loud_buffer.getvalue()


async def test_errors_are_reported_not_raised(container) -> None:
    class Boom:
        async def stream_chat(self, *args: Any, **kwargs: Any):
            raise RuntimeError("ollama is down")
            yield  # pragma: no cover - keeps this an async generator

        async def aclose(self) -> None:
            return None

    container.agent.client = Boom()
    out, buffer = terminal()
    session = await container.store.get_or_create()
    content = await run_turn(container, session, "hi", terminal=out)
    assert content == ""
    assert "error:" in buffer.getvalue()


async def test_repl_answers_help_and_exits(container) -> None:
    container.agent.client.add_text("hi there")
    answers = iter(["", "/help", "hello", "/tools", "/exit"])
    out, buffer = terminal()
    session = await container.store.get_or_create()
    code = await repl(container, session, terminal=out, ask=lambda _prompt: next(answers))
    text = buffer.getvalue()
    assert code == 0
    assert "hi there" in text
    assert HELP.splitlines()[0] in text
    assert "read_file" in text  # /tools listing


async def test_repl_reset_starts_a_new_session(container) -> None:
    answers = iter(["/reset", "/exit"])
    out, buffer = terminal()
    session = await container.store.get_or_create()
    await repl(container, session, terminal=out, ask=lambda _prompt: next(answers))
    assert "new session" in buffer.getvalue()


async def test_repl_survives_eof(container) -> None:
    def reader(_prompt: str) -> str:
        raise EOFError

    out, _ = terminal()
    session = await container.store.get_or_create()
    assert await repl(container, session, terminal=out, ask=reader) == 0


def test_parser_defaults() -> None:
    args = build_parser().parse_args(["list", "files"])
    assert args.message == ["list", "files"]
    assert args.model is None and args.max_iterations is None
    assert args.think is False and args.usage is False


def test_main_one_shot(monkeypatch: pytest.MonkeyPatch, container, settings: Settings) -> None:
    container.agent.client.add_text("one shot answer")

    async def fake_make_container(_settings: Settings):
        return container

    monkeypatch.setattr("nour_code.chat._make_container", fake_make_container)
    monkeypatch.setattr("nour_code.chat.load_settings", lambda **_: settings)

    stream = io.StringIO()
    code = main(["what", "is", "up?"], terminal=Terminal(stream=stream, color=False))
    assert code == 0
    assert "one shot answer" in stream.getvalue()


def test_terminal_colour_is_off_for_plain_streams() -> None:
    plain, _ = terminal()
    assert plain.color is False
    tty_like = Terminal(stream=io.StringIO(), color=True)
    assert tty_like._paint("x", "\033[31m").startswith("\033[31m")
    plain.line("x")
