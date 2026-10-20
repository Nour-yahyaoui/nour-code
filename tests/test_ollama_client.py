"""Ollama client: payload building, response parsing, streaming and health."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nour_code.models import ChatMessage
from nour_code.ollama_client import (
    ChatResult,
    ModelOptions,
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
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3:8b", "expires_at": "2026-01-01T00:05:00Z", "size_vram": 5_000}]},
            )
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    info = await client.health()
    assert info["reachable"] is True
    assert info["version"] == "0.5.1"
    assert info["model_loaded"] is True
    assert info["models"] == ["llama3.2:1b", "qwen3:8b"]
    assert info["model_running"] is True
    assert info["running_models"] == ["qwen3:8b"]
    assert info["model_expires_at"] == "2026-01-01T00:05:00Z"


async def test_health_distinguishes_pulled_from_running() -> None:
    """A pulled model with nothing in ``/api/ps`` is idle, not missing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    info = await client.health()
    assert info["model_loaded"] is True
    assert info["model_running"] is False
    assert info["model_expires_at"] is None
    assert info["detail"] is None


async def test_health_survives_a_failing_residency_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    info = await client.health()
    assert info["model_loaded"] is True  # /health must survive an old daemon
    assert info["model_running"] is False


async def test_ps_does_not_match_a_different_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})

    client = make_client(handler)
    state = await client.ps()
    assert state["running"] is False
    assert state["models"][0]["name"] == "qwen3:14b"


def test_same_model_is_strict_about_tags() -> None:
    assert OllamaClient._same_model("qwen3:8b", "qwen3:8b") is True
    assert OllamaClient._same_model("qwen3:8b", "qwen3") is True  # untagged request
    assert OllamaClient._same_model("qwen3", "qwen3:8b") is True  # untagged resident
    assert OllamaClient._same_model("qwen3:14b", "qwen3:8b") is False
    assert OllamaClient._same_model("llama3.2:1b", "qwen3:8b") is False


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
# residency control
# ---------------------------------------------------------------------------
async def test_load_model_uses_native_generate_with_keep_alive() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"model": "qwen3:8b", "done": True, "done_reason": "load"})
        if request.url.path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3:8b", "expires_at": "2026-01-01T00:30:00Z"}]}
            )
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    state = await client.load_model(keep_alive="30m")
    assert state["running"] is True
    assert state["keep_alive"] == "30m"
    assert state["expires_at"] == "2026-01-01T00:30:00Z"

    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/generate"
    assert body == {"model": "qwen3:8b", "stream": False, "keep_alive": "30m"}
    assert "prompt" not in body  # no prompt = load only


async def test_unload_model_sends_keep_alive_zero() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"model": "qwen3:8b", "done": True, "done_reason": "unload"})
        return httpx.Response(200, json={"models": []})

    client = make_client(handler)
    state = await client.unload_model()
    assert state["running"] is False
    assert state["keep_alive"] == "0"
    assert json.loads(seen[0].content)["keep_alive"] == 0


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


# ---------------------------------------------------------------------------
# reasoning (Qwen3 thinking mode)
# ---------------------------------------------------------------------------
async def test_stream_yields_thinking_chunks() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning": "First I should look "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning": "at the folder."}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "It is empty."}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 9, "total_tokens": 13}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("hi")])]
    thinking = [c.text for c in chunks if c.kind == "thinking"]
    tokens = [c.text for c in chunks if c.kind == "token"]
    assert thinking == ["First I should look ", "at the folder."]
    assert tokens == ["It is empty."]


async def test_stream_accepts_alternative_reasoning_keys() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "hmm"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"thinking": "ok"}, "finish_reason": None}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream_chat([ChatMessage.user("hi")])]
    assert [c.text for c in chunks if c.kind == "thinking"] == ["hmm", "ok"]


async def test_chat_captures_reasoning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = completion("done")
        payload["choices"][0]["message"]["reasoning"] = "I need to read the file first."
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    result = await client.chat([ChatMessage.user("read it")])
    assert result.thinking == "I need to read the file first."
    assert result.content == "done"


def test_extract_reasoning_tolerates_absent_fields() -> None:
    assert OllamaClient._extract_reasoning({"content": "hi"}) == ""
    assert OllamaClient._extract_reasoning({"reasoning": "a", "thinking": "b"}) == "a"


def test_model_options_are_forwarded_into_the_payload() -> None:
    client = make_client(lambda request: httpx.Response(200, json=completion()))
    payload = client.build_payload(
        [ChatMessage.user("x")],
        None,
        stream=True,
        model="qwen3:14b",
        options=ModelOptions(temperature=0.9, top_p=0.8, top_k=40, enable_thinking=True, stop=["###"]),
    )
    assert payload["model"] == "qwen3:14b"
    assert payload["options"]["temperature"] == 0.9
    assert payload["options"]["top_p"] == 0.8
    assert payload["options"]["top_k"] == 40
    assert payload["options"]["enable_thinking"] is True
    assert payload["options"]["stop"] == ["###"]
    assert payload["options"]["num_ctx"] == 16384


def test_model_options_fall_back_to_settings() -> None:
    settings = pytest.importorskip("nour_code.config").Settings(
        metrics_enabled=False, top_p=0.5, repeat_penalty=1.2, enable_thinking=False, stop="END"
    )
    client = OllamaClient(settings, client=httpx.AsyncClient(), owns_client=False)
    payload = client.build_payload([ChatMessage.user("x")], None, stream=False, options=ModelOptions())
    assert payload["options"]["top_p"] == 0.5
    assert payload["options"]["repeat_penalty"] == 1.2
    assert payload["options"]["enable_thinking"] is False
    assert payload["options"]["stop"] == ["END"]
