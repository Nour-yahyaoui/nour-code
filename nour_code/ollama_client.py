"""Async HTTP wrapper around Ollama's OpenAI-compatible API.

Chat completions are always requested with ``tools`` attached (function
calling), ``num_ctx`` and a low ``temperature`` for deterministic coding work.
Both blocking and streaming (SSE) modes are supported; streaming is used to
push tokens to the client, blocking is used inside the agentic tool loop where
we need the complete tool-call payload before continuing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .models import ChatMessage, FunctionCall, ToolCall

logger = logging.getLogger("nour_code.ollama")


class OllamaError(Exception):
    """Base class for every Ollama failure."""


class OllamaUnavailableError(OllamaError):
    """Ollama is not reachable (connection refused / DNS / timeout)."""


class OllamaHTTPError(OllamaError):
    """Ollama answered with a non-2xx status."""

    def __init__(self, status_code: int, body: str, *, endpoint: str = "") -> None:
        self.status_code = status_code
        self.body = body[:2000]
        self.endpoint = endpoint
        super().__init__(f"Ollama returned HTTP {status_code} on {endpoint or 'request'}: {self.body}")


class OllamaResponseError(OllamaError):
    """Ollama answered, but the payload could not be understood."""


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_duration_ns: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            total_duration_ns=self.total_duration_ns + other.total_duration_ns,
        )


@dataclass(slots=True)
class ChatResult:
    """A complete (non-streamed) chat completion."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamChunk:
    """One piece of a streamed completion."""

    kind: str  # "token" | "tool_calls" | "usage" | "error"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str = ""
    error: str = ""


class OllamaClient:
    """Thin, dependency-injected client for a local Ollama daemon."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        metrics: Any | None = None,
        owns_client: bool = True,
    ) -> None:
        self.settings = settings
        self.metrics = metrics
        self._owns_client = owns_client
        self._client = client or self._build_client(settings)

    # -- lifecycle ---------------------------------------------------------
    @staticmethod
    def _build_client(settings: Settings) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.request_timeout_seconds,
            write=30.0,
            pool=10.0,
        )
        return httpx.AsyncClient(
            base_url=settings.ollama_base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {settings.ollama_api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"nour-code/{settings.version}",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    # -- payload -----------------------------------------------------------
    def build_payload(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        stream: bool,
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        serialised: list[dict[str, Any]] = [
            m.to_payload() if isinstance(m, ChatMessage) else dict(m) for m in messages
        ]
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": serialised,
            "stream": stream,
            "options": {
                "num_ctx": num_ctx if num_ctx is not None else self.settings.num_ctx,
                "temperature": self.settings.temperature if temperature is None else temperature,
            },
        }
        if self.settings.num_predict is not None:
            payload["options"]["num_predict"] = self.settings.num_predict
        if self.settings.seed is not None:
            payload["options"]["seed"] = self.settings.seed
        tool_list = list(tools or [])
        if tool_list:
            payload["tools"] = tool_list
            if self.settings.parallel_tool_calls:
                payload["parallel_tool_calls"] = True
        if max_iterations is not None:  # documented for subclasses / future backends
            payload["options"]["num_iterations"] = max_iterations
        return payload

    # -- errors ------------------------------------------------------------
    @staticmethod
    def _to_error(exc: Exception, *, endpoint: str) -> OllamaError:
        if isinstance(exc, OllamaError):
            return exc
        if isinstance(exc, httpx.ConnectError):
            return OllamaUnavailableError(f"cannot reach Ollama at {endpoint}: {exc}")
        if isinstance(exc, httpx.TimeoutException):
            return OllamaUnavailableError(f"Ollama request timed out ({exc.__class__.__name__})")
        if isinstance(exc, httpx.HTTPStatusError):
            return OllamaHTTPError(exc.response.status_code, exc.response.text, endpoint=endpoint)
        if isinstance(exc, httpx.HTTPError):
            return OllamaUnavailableError(f"Ollama transport error: {exc}")
        if isinstance(exc, json.JSONDecodeError):
            return OllamaResponseError(f"Ollama returned invalid JSON: {exc}")
        return OllamaError(f"{exc.__class__.__name__}: {exc}")

    async def _post_with_retry(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = max(1, self.settings.max_model_retries + 1)
        last: OllamaError | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise OllamaResponseError("expected a JSON object from Ollama")
                if self.metrics:
                    self.metrics.observe_model(
                        model=payload.get("model", self.settings.model),
                        latency_s=time.perf_counter() - started,
                        prompt_tokens=0,
                        completion_tokens=0,
                        outcome="ok",
                    )
                return data
            except Exception as exc:  # narrowed immediately below
                last = self._to_error(exc, endpoint=url)
                if isinstance(last, (OllamaHTTPError, OllamaResponseError)):
                    if self.metrics:
                        self.metrics.observe_model(
                            model=self.settings.model,
                            latency_s=time.perf_counter() - started,
                            prompt_tokens=0,
                            completion_tokens=0,
                            outcome="error",
                        )
                    raise last from exc
                logger.warning(
                    "ollama call failed (attempt %d/%d): %s", attempt, attempts, last
                )
                if attempt < attempts:
                    await asyncio.sleep(self.settings.retry_backoff_seconds * attempt)
        assert last is not None
        if self.metrics:
            self.metrics.observe_model(
                model=self.settings.model, latency_s=0.0, prompt_tokens=0, completion_tokens=0, outcome="error"
            )
        raise last

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _parse_usage(data: dict[str, Any]) -> Usage:
        usage = data.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None:
            prompt = data.get("prompt_eval_count") or 0
        if completion is None:
            completion = data.get("eval_count") or 0
        prompt_i = int(prompt or 0)
        completion_i = int(completion or 0)
        return Usage(
            prompt_tokens=prompt_i,
            completion_tokens=completion_i,
            total_tokens=int(usage.get("total_tokens") or (prompt_i + completion_i)),
            total_duration_ns=int(data.get("total_duration") or 0),
        )

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[ToolCall]:
        """Normalise OpenAI-style tool calls, tolerating partial/missing fields."""
        calls: list[ToolCall] = []
        if not isinstance(raw, list):
            return calls
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                args_str = arguments
            elif arguments is None:
                args_str = "{}"
            else:
                args_str = json.dumps(arguments)
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}_{time.time_ns()}"),
                    type="function",
                    index=int(item.get("index", index) or index),
                    function=FunctionCall(name=name, arguments=args_str),
                )
            )
        return calls

    # -- blocking chat -----------------------------------------------------
    async def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        payload = self.build_payload(messages, tools, stream=False, temperature=temperature, num_ctx=num_ctx)
        started = time.perf_counter()
        data = await self._post_with_retry(self.settings.chat_completions_url, payload)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._to_result(data, latency_ms)

    def _to_result(self, data: dict[str, Any], latency_ms: float) -> ChatResult:
        try:
            choices = data.get("choices") or []
            if not choices:
                raise OllamaResponseError(f"no choices in response: {json.dumps(data)[:400]}")
            choice = choices[0] or {}
            message = choice.get("message") or {}
        except (AttributeError, IndexError, TypeError) as exc:
            raise OllamaResponseError(f"malformed completion payload: {exc}") from exc

        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        usage = self._parse_usage(data)
        if self.metrics:
            self.metrics.observe_model(
                model=data.get("model", self.settings.model),
                latency_s=latency_ms / 1000.0,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                outcome="ok",
            )
        return ChatResult(
            content=content,
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=usage,
            latency_ms=latency_ms,
            model=str(data.get("model") or self.settings.model),
            raw=data,
        )

    # -- streaming chat ----------------------------------------------------
    async def stream_chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        num_ctx: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = self.build_payload(messages, tools, stream=True, temperature=temperature, num_ctx=num_ctx)
        url = self.settings.chat_completions_url
        started = time.perf_counter()
        pending: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", url, json=payload) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise OllamaHTTPError(exc.response.status_code, exc.response.text, endpoint=url) from exc
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("skipping non-JSON stream line: %r", line[:120])
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        yield StreamChunk(kind="error", error=str(chunk["error"])[:500])
                        return
                    for item in self._iter_chunks(chunk, pending):
                        yield item
        except httpx.HTTPError as exc:
            raise self._to_error(exc, endpoint=url) from exc

        if self.metrics:
            self.metrics.observe_model(
                model=self.settings.model,
                latency_s=time.perf_counter() - started,
                prompt_tokens=0,
                completion_tokens=0,
                outcome="ok",
            )

    def _iter_chunks(self, chunk: dict[str, Any], pending: dict[int, dict[str, Any]]) -> Iterable[StreamChunk]:
        """Turn one SSE payload into zero or more :class:`StreamChunk` objects."""
        choices = chunk.get("choices") or []
        if not choices:
            # Ollama emits a final message carrying usage stats.
            if chunk.get("usage") or chunk.get("prompt_eval_count") is not None:
                yield StreamChunk(kind="usage", usage=self._parse_usage(chunk), finish_reason="stop")
            return
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        finish = str(choice.get("finish_reason") or "")

        text = delta.get("content")
        if isinstance(text, str) and text:
            yield StreamChunk(kind="token", text=text)

        for raw_call in delta.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                continue
            index = int(raw_call.get("index", 0) or 0)
            slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if raw_call.get("id"):
                slot["id"] = str(raw_call["id"])
            function = raw_call.get("function") or {}
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += function["arguments"] if isinstance(function["arguments"], str) else json.dumps(function["arguments"])

        if finish in {"tool_calls", "stop", "length"} and pending:
            calls = [
                ToolCall(
                    id=slot["id"] or f"call_{idx}_{time.time_ns()}",
                    type="function",
                    index=idx,
                    function=FunctionCall(name=slot["name"], arguments=slot["arguments"] or "{}"),
                )
                for idx, slot in sorted(pending.items())
            ]
            pending.clear()
            usage = self._parse_usage(chunk) if (chunk.get("usage") or chunk.get("eval_count")) else None
            yield StreamChunk(kind="tool_calls", tool_calls=calls, finish_reason=finish, usage=usage)
        elif finish in {"stop", "length"}:
            usage = self._parse_usage(chunk) if (chunk.get("usage") or chunk.get("eval_count")) else None
            yield StreamChunk(kind="usage", finish_reason=finish, usage=usage)

    # -- health ------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Probe Ollama: version, reachable models and whether our model is pulled."""
        info: dict[str, Any] = {
            "reachable": False,
            "url": self.settings.ollama_base_url,
            "version": None,
            "models": [],
            "model_loaded": False,
            "detail": None,
        }
        timeout = httpx.Timeout(self.settings.health_timeout_seconds, connect=self.settings.connect_timeout_seconds)
        try:
            response = await self._client.get(self.settings.ollama_version_url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            info["reachable"] = True
            info["version"] = str(data.get("version")) if isinstance(data, dict) else None
        except Exception as exc:
            info["detail"] = f"version probe failed: {exc}"
            logger.info("ollama health check failed: %s", exc)
            return info

        try:
            response = await self._client.get(self.settings.native_tags_url, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            models: list[str] = []
            for item in (payload or {}).get("models", []) if isinstance(payload, dict) else []:
                name = item.get("name") or item.get("model")
                if name:
                    models.append(str(name))
            info["models"] = sorted(models)
            info["model_loaded"] = self._model_available(models, self.settings.model)
            if not info["model_loaded"]:
                info["detail"] = (
                    f"model '{self.settings.model}' is not pulled; run: ollama pull {self.settings.model}"
                )
        except Exception as exc:
            info["detail"] = f"model list probe failed: {exc}"
        return info

    @staticmethod
    def _model_available(models: Sequence[str], model: str) -> bool:
        if not models:
            return False
        if model in models:
            return True
        # tolerate "qwen3" matching "qwen3:8b" and "qwen3:latest"
        family = model.split(":", 1)[0]
        return any(m == family or m.split(":", 1)[0] == family for m in models)
