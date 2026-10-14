"""Ollama client: payload building, response parsing, streaming and health."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nour_code.models import ChatMessage
from nour_code.ollama_client import (
    ChatResult,
    OllamaClient,
    OllamaError,
    OllamaHTTPError,
    OllamaResponseError,
    OllamaUnavailableError,
    StreamChunk,
    Usage,
)


def make_client(handler) -> OllamaClient:
    """Build a client wired to an in-process httpx transport."""
    settings = pytest.importorskip("nour_code.config").Settings(metrics_enabled=False, model="qwen3:8b")
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=settings.ollama_base_url)
    return OllamaClient(settings, client=http_client, owns_client=False)


def completion(content: str = "hi", tool_calls: list[dict[str, Any]] | None = None, finish: str = "stop") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "qwen3:8b",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def test_build_payload_defaults(settings) -> None:
    client = OllamaClient(settings, client=httpx.AsyncClient())
    payload = client.build_payload([ChatMessage.user("hi")], None, stream=False)
    assert payload["model"] == "qwen3:8b"
    assert payload["stream"] is False
    assert payload["options"]["num_ctx"] == settings.num_ctx
    assert payload["options"]["temperature"] == settings.temperature
    assert "tools" not in payload


def test_build_payload_includes_tools_and_overrides(settings) -> None:
    client = OllamaClient(settings, client=httpx.AsyncClient())
    tools = [{"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}]
    payload = client.build_payload(
        [ChatMessage.user("hi")], tools, stream=True, temperature=0.7, num_ctx=4096
    )
    assert payload["tools"] == tools
    assert payload["stream"] is True
    assert payload["options"]["temperature"] == 0.7
    assert payload["options"]["num_ctx"] == 4096


def test_build_payload_serialises_messages(settings) -> None:
    client = OllamaClient(settings, client=httpx.AsyncClient())
    payload = client.build_payload(
        [ChatMessage.user("hi"), ChatMessage.assistant("yo"), ChatMessage.tool_result("c1", "read_file", "{}")],
        None,
        stream=False,
    )
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "tool"]
    assert payload["messages"][2]["tool_call_id"] == "c1"


# ---------------------------------------------------------------------------
# blocking chat
# ---------------------------------------------------------------------------
async def test_chat_returns_content_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion("hello there"))

    client = make_client(handler)
    result = await client.chat([ChatMessage.user("hi")])
    assert isinstance(result, ChatResult)
    assert result.content == "hello there"
    assert result.tool_calls == []
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 20
    assert result.finish_reason == "stop"


async def test_chat_parses_tool_calls() -> None:
    calls = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]
    client = make_client(lambda request: httpx.Response(200, json=completion("", calls, "tool_calls")))
    result = await client.chat([ChatMessage.user("read a.py")])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].function.name == "read_file"
    assert json.loads(result.tool_calls[0].function.arguments) == {"path": "a.py"}
    assert result.finish_reason == "tool_calls"


async def test_chat_tolerates_arguments_as_object() -> None:
    calls = [{"id": "c", "type": "function", "function": {"name": "read_file", "arguments": {"path": "a.py"}}}]
    client = make_client(lambda request: httpx.Response(200, json=completion("", calls, "tool_calls")))
    result = await client.chat([ChatMessage.user("x")])
    assert json.loads(result.tool_calls[0].function.arguments) == {"path": "a.py"}


async def test_chat_ignores_nameless_tool_calls() -> None:
    calls = [{"id": "c", "type": "function", "function": {"name": "", "arguments": "{}"}}]
    client = make_client(lambda request: httpx.Response(200, json=completion("", calls, "tool_calls")))
    result = await client.chat([ChatMessage.user("x")])
    assert result.tool_calls == []


async def test_chat_raises_on_http_error() -> None:
    client = make_client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(OllamaHTTPError) as excinfo:
        await client.chat([ChatMessage.user("x")])
    assert excinfo.value.status_code == 500


async def test_chat_raises_on_malformed_payload() -> None:
    client = make_client(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(OllamaResponseError):
        await client.chat([ChatMessage.user("x")])


async def test_chat_raises_when_ollama_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    settings = pytest.importorskip("nour_code.config").Settings(metrics_enabled=False, max_model_retries=0)
    client = OllamaClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(OllamaUnavailableError):
        await client.chat([ChatMessage.user("x")])


async def test_chat_retries_transient_failures() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("flaky", request=request)
        return httpx.Response(200, json=completion("recovered"))

    settings = pytest.importorskip("nour_code.config").Settings(
        metrics_enabled=False, max_model_retries=2, retry_backoff_seconds=0
    )
    client = OllamaClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await client.chat([ChatMessage.user("x")])
    assert result.content == "recovered"
    assert attempts["count"] == 2


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------
async def test_stream_yields_tokens_and_usage() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("hi")])]
    assert [c.kind for c in chunks] == ["token", "token", "usage"]
    assert "".join(c.text for c in chunks) == "Hello"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 7


async def test_stream_accumulates_tool_call_arguments() -> None:
    frames = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_9", "type": "function",
                             "function": {"name": "read_file", "arguments": ""}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"pa'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("read a.py")])]
    calls = [c for c in chunks if c.kind == "tool_calls"]
    assert len(calls) == 1
    assert calls[0].tool_calls[0].id == "call_9"
    assert json.loads(calls[0].tool_calls[0].function.arguments) == {"path": "a.py"}


async def test_stream_reports_model_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"error": "model overloaded"}\n\n')

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("hi")])]
    assert chunks[0].kind == "error"
    assert "model overloaded" in chunks[0].error


async def test_stream_raises_on_http_error() -> None:
    client = make_client(lambda request: httpx.Response(404, text="nope"))
    with pytest.raises(OllamaHTTPError):
        async for _ in client.stream_chat([ChatMessage.user("hi")]):
            pass


async def test_stream_tolerates_non_json_lines() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = ": ping\n\ndata: not-json\n\ndata: {\"choices\": [{\"index\": 0, \"delta\": {\"content\": \"ok\"}, \"finish_reason\": null}]}\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("hi")])]
    assert [c.text for c in chunks if c.kind == "token"] == ["ok"]


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
async def test_health_reports_model_loaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:1b"}]})
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    info = await client.health()
    assert info["reachable"] is True
    assert info["version"] == "0.5.1"
    assert info["model_loaded"] is True
    assert info["models"] == ["llama3.2:1b", "qwen3:8b"]


async def test_health_flags_missing_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        return httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]})

    client = make_client(handler)
    info = await client.health()
    assert info["model_loaded"] is False
    assert "ollama pull" in (info["detail"] or "")


async def test_health_reports_unreachable_ollama() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    info = await client.health()
    assert info["reachable"] is False
    assert "version probe failed" in (info["detail"] or "")


def test_model_available_matches_family() -> None:
    assert OllamaClient._model_available(["qwen3:8b"], "qwen3:8b") is True
    assert OllamaClient._model_available(["qwen3:latest"], "qwen3:8b") is True
    assert OllamaClient._model_available(["llama3.2:1b"], "qwen3:8b") is False
    assert OllamaClient._model_available([], "qwen3:8b") is False


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
def test_usage_addition() -> None:
    total = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3) + Usage(
        prompt_tokens=4, completion_tokens=5, total_tokens=9
    )
    assert (total.prompt_tokens, total.completion_tokens, total.total_tokens) == (5, 7, 12)


def test_stream_chunk_defaults() -> None:
    chunk = StreamChunk(kind="token", text="x")
    assert chunk.tool_calls == [] and chunk.usage is None


def test_error_hierarchy() -> None:
    assert issubclass(OllamaUnavailableError, OllamaError)
    assert issubclass(OllamaHTTPError, OllamaError)
    assert issubclass(OllamaResponseError, OllamaError)


async def test_aclose_is_idempotent() -> None:
    client = make_client(lambda request: httpx.Response(200, json=completion()))
    await client.aclose()
    await client.aclose()
