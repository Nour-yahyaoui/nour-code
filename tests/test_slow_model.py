"""A slow/absent model must not look like a frozen page.

Covers the two guarantees added for cold starts:
  * while no token has arrived, `status`/`waiting` heartbeats keep flowing;
  * if the first token never arrives, the turn ends with an actionable error.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nour_code.config import Settings
from nour_code.ollama_client import StreamChunk


class SlowModel:
    """Yields nothing for `delay` seconds, then streams a short answer."""

    def __init__(self, delay: float, *, answer: str = "hello", fail: Exception | None = None) -> None:
        self.delay = delay
        self.answer = answer
        self.fail = fail
        self.started = 0

    async def stream_chat(self, messages, tools=None, *, temperature=None, num_ctx=None, model=None, options=None) -> AsyncIterator[StreamChunk]:
        self.started += 1
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        for word in self.answer.split(" "):
            yield StreamChunk(kind="token", text=word + " ")

    async def aclose(self) -> None:  # pragma: no cover - interface parity
        return None


def _settings(settings: Settings, **overrides: Any) -> Settings:
    base = settings.model_dump()
    base.update(overrides)
    return Settings(**base)


async def collect(container, message: str = "hi") -> list[tuple[str, dict[str, Any]]]:
    session = await container.store.get_or_create()
    events: list[tuple[str, dict[str, Any]]] = []
    async for event in container.agent.run_stream(session, message):
        events.append((event.type, event.data))
    return events


async def test_heartbeat_flows_while_the_model_is_silent(container) -> None:
    container.agent.client = SlowModel(delay=0.35, answer="hi there")
    container.agent.settings = _settings(
        container.settings, wait_heartbeat_seconds=0.1, first_token_timeout_seconds=30
    )

    events = await collect(container)
    waiting = [data for kind, data in events if kind == "status" and data.get("phase") == "waiting"]

    assert waiting, "no heartbeat while the model was silent"
    assert all(data["awaiting_first_token"] is True for data in waiting)
    assert waiting[0]["waited_ms"] < waiting[-1]["waited_ms"]  # time actually advances

    # the answer still arrives, and streaming is announced once
    tokens = "".join(data["text"] for kind, data in events if kind == "token")
    assert tokens.strip() == "hi there"
    streaming = [data for kind, data in events if kind == "status" and data.get("phase") == "model_streaming"]
    assert len(streaming) == 1
    assert streaming[0]["wait_ms"] >= 300


async def test_first_token_timeout_fails_with_a_useful_message(container) -> None:
    container.agent.client = SlowModel(delay=5, answer="never mind")
    container.agent.settings = _settings(
        container.settings, wait_heartbeat_seconds=0.05, first_token_timeout_seconds=0.2
    )

    events = await collect(container)
    errors = [data for kind, data in events if kind == "error"]

    assert errors, "a model that never answers must end the turn"
    message = errors[0]["message"]
    assert "still loading" in message
    assert "/ollama/warmup" in message
    assert "NOUR_CODE_FIRST_TOKEN_TIMEOUT_SECONDS" in message


async def test_a_failure_inside_the_stream_is_re_raised(container) -> None:
    container.agent.client = SlowModel(delay=0.05, fail=RuntimeError("connection reset"))
    container.agent.settings = _settings(container.settings, wait_heartbeat_seconds=0.05)

    events = await collect(container)
    errors = [data for kind, data in events if kind == "error"]
    assert errors and "connection reset" in errors[0]["message"]


async def test_call_cancellation_stops_the_model_task(container) -> None:
    """Aborting the client (Esc in the UI) must not leave the model running."""
    model = SlowModel(delay=10, answer="too late")
    container.agent.client = model
    container.agent.settings = _settings(container.settings, wait_heartbeat_seconds=0.05)
    session = await container.store.get_or_create()

    async def consume() -> None:
        async for _event in container.agent.run_stream(session, "hi"):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation propagates: give the loop a couple of turns to unwind, then
    # no task from this turn may still be alive.
    def turn_tasks() -> list[asyncio.Task]:
        return [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and not t.done()
            and t.get_name() != "nour-code-session-sweeper"  # container background task
        ]

    for _ in range(20):
        remaining = turn_tasks()
        if not remaining:
            break
        await asyncio.sleep(0.05)
    assert remaining == [], f"leaked tasks: {remaining}"
