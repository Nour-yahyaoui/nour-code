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
class ModelOptions:
    """Per-turn sampling overrides.

    Every field defaults to ``None``, which means "keep the process-wide
    :class:`~nour_code.config.Settings` value".  A turn can therefore ask for a
    different temperature, a different model, or Qwen3 thinking mode without
    mutating any global state.
    """

    model: str | None = None
    temperature: float | None = None
    num_ctx: int | None = None
    num_predict: int | None = None
    seed: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    stop: list[str] | None = None
    enable_thinking: bool | None = None


@dataclass(slots=True)
class ChatResult:
    """A complete (non-streamed) chat completion."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamChunk:
    """One piece of a streamed completion."""

    kind: str  # "token" | "thinking" | "tool_calls" | "usage" | "error"
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
        model: str | None = None,
        options: ModelOptions | None = None,
    ) -> dict[str, Any]:
        """Build the request body.

        ``options`` carries per-turn sampling overrides (1.2.0); anything left as
        ``None`` falls back to the process-wide :class:`Settings`.
        """
        overrides = options or ModelOptions()
        serialised: list[dict[str, Any]] = [
            m.to_payload() if isinstance(m, ChatMessage) else dict(m) for m in messages
        ]
        payload: dict[str, Any] = {
            "model": model or self.settings.model,
            "messages": serialised,
            "stream": stream,
            "options": {
                "num_ctx": overrides.num_ctx if overrides.num_ctx is not None else (num_ctx if num_ctx is not None else self.settings.num_ctx),
                "temperature": overrides.temperature if overrides.temperature is not None else (self.settings.temperature if temperature is None else temperature),
            },
        }

        def _set(key: str, value: Any, fallback: Any) -> None:
            chosen = fallback if value is None else value
            if chosen is not None:
                payload["options"][key] = chosen

        _set("num_predict", overrides.num_predict, self.settings.num_predict)
        _set("seed", overrides.seed, self.settings.seed)
        _set("top_p", overrides.top_p, self.settings.top_p)
        _set("top_k", overrides.top_k, self.settings.top_k)
        _set("repeat_penalty", overrides.repeat_penalty, self.settings.repeat_penalty)
        _set("presence_penalty", overrides.presence_penalty, self.settings.presence_penalty)
        _set("frequency_penalty", overrides.frequency_penalty, self.settings.frequency_penalty)
        _set("enable_thinking", overrides.enable_thinking, self.settings.enable_thinking)

        stop = overrides.stop if overrides.stop is not None else self.settings.stop_sequences
        if stop:
            payload["options"]["stop"] = list(stop)

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

    async def _post_with_retry(
        self, url: str, payload: dict[str, Any], *, track_metrics: bool = True
    ) -> dict[str, Any]:
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
                if self.metrics and track_metrics:
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
                if isinstance(last, OllamaHTTPError | OllamaResponseError):
                    if self.metrics and track_metrics:
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
        if self.metrics and track_metrics:
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

    @staticmethod
    def _extract_reasoning(message: dict[str, Any]) -> str:
        """Pull the model's reasoning out of a completion message (may be empty)."""
        for key in ("reasoning", "reasoning_content", "thinking"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    # -- blocking chat -----------------------------------------------------
    async def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        num_ctx: int | None = None,
        model: str | None = None,
        options: ModelOptions | None = None,
    ) -> ChatResult:
        payload = self.build_payload(
            messages, tools, stream=False, temperature=temperature, num_ctx=num_ctx, model=model, options=options
        )
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
            thinking=self._extract_reasoning(message),
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
        model: str | None = None,
        options: ModelOptions | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = self.build_payload(
            messages, tools, stream=True, temperature=temperature, num_ctx=num_ctx, model=model, options=options
        )
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

        # Qwen3 (and other reasoning models) stream their chain of thought in
        # ``reasoning`` (OpenAI-compatible) or ``reasoning_content`` (Ollama).
        reasoning = delta.get("reasoning")
        if reasoning is None:
            reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("thinking")
        if isinstance(reasoning, str) and reasoning:
            yield StreamChunk(kind="thinking", text=reasoning)

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

    # -- model residency ---------------------------------------------------
    async def ps(self, model: str | None = None) -> dict[str, Any]:
        """List the models Ollama holds in memory (native ``GET /api/ps``).

        Ollama loads a model on demand and unloads it again after ``keep_alive``
        (five minutes by default), so an empty answer here does **not** mean the
        model is missing — use :meth:`health` for the *pulled* check and this
        method for the *resident* check.
        """
        url = self.settings.native_ps_url
        timeout = httpx.Timeout(self.settings.health_timeout_seconds, connect=self.settings.connect_timeout_seconds)
        try:
            response = await self._client.get(url, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # narrowed by _to_error
            raise self._to_error(exc, endpoint=url) from exc

        models: list[dict[str, Any]] = []
        for item in (payload or {}).get("models", []) if isinstance(payload, dict) else []:
            name = item.get("name") or item.get("model")
            if not name:
                continue
            models.append(
                {
                    "name": str(name),
                    "expires_at": item.get("expires_at"),
                    "size": int(item.get("size") or 0),
                    "size_vram": int(item.get("size_vram") or 0),
                }
            )
        target = model or self.settings.model
        match = next((entry for entry in models if self._same_model(entry["name"], target)), None)
        return {
            "model": target,
            "running": match is not None,
            "expires_at": (match or {}).get("expires_at"),
            "models": models,
        }

    async def load_model(self, *, model: str | None = None, keep_alive: str | None = None) -> dict[str, Any]:
        """Preload a model through the native API and report its residency.

        ``/api/generate`` with no prompt only loads the weights, which is the
        documented way to control residency.  Ollama's OpenAI-compatible
        ``/v1/chat/completions`` endpoint ignores ``keep_alive``, so warm-up
        must go through the native endpoint.
        """
        target = model or self.settings.model
        chosen = keep_alive if keep_alive is not None else self.settings.keep_alive
        payload: dict[str, Any] = {"model": target, "stream": False}
        if chosen is not None:
            payload["keep_alive"] = chosen
        await self._post_with_retry(self.settings.native_generate_url, payload, track_metrics=False)
        return await self._residency(target, keep_alive=chosen)

    async def unload_model(self, *, model: str | None = None) -> dict[str, Any]:
        """Unload a model (``keep_alive: 0``) and report its residency."""
        target = model or self.settings.model
        payload = {"model": target, "stream": False, "keep_alive": 0}
        await self._post_with_retry(self.settings.native_generate_url, payload, track_metrics=False)
        return await self._residency(target, keep_alive="0")

    async def _residency(self, model: str, *, keep_alive: str | None) -> dict[str, Any]:
        try:
            state = await self.ps(model)
        except OllamaError as exc:
            return {
                "model": model,
                "keep_alive": keep_alive,
                "running": False,
                "expires_at": None,
                "detail": f"model list probe failed: {exc}",
            }
        return {
            "model": model,
            "keep_alive": keep_alive,
            "running": bool(state["running"]),
            "expires_at": state["expires_at"],
            "detail": None,
        }

    @staticmethod
    def _same_model(name: str, model: str) -> bool:
        """Strict match for *running* models (unlike the tolerant family check).

        ``qwen3`` matches ``qwen3:8b`` because one side carries no tag, but
        ``qwen3:8b`` does not match ``qwen3:14b``: those are different weights.
        """
        if name == model:
            return True
        name_family, _, name_tag = name.partition(":")
        model_family, _, model_tag = model.partition(":")
        if name_family != model_family:
            return False
        return not name_tag or not model_tag

    # -- health ------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Probe Ollama: version, pulled models, and which of them are resident."""
        info: dict[str, Any] = {
            "reachable": False,
            "url": self.settings.ollama_base_url,
            "version": None,
            "models": [],
            "model_loaded": False,
            "model_running": False,
            "model_expires_at": None,
            "running_models": [],
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

        # Second, different question: which models does Ollama hold in memory
        # right now?  A model can be pulled (``/api/tags``) and idle (absent
        # from ``/api/ps``) at the same time - that is the normal state.
        try:
            state = await self.ps()
            info["running_models"] = [entry["name"] for entry in state["models"]]
            info["model_running"] = bool(state["running"])
            info["model_expires_at"] = state["expires_at"]
        except Exception as exc:  # the residency probe must never fail /health
            logger.info("ollama running-model probe failed: %s", exc)
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
