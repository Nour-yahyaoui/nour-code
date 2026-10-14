"""Logging, audit trail and (optional) Prometheus metrics.

Three artefacts are produced:

``logs/nour-code.log``
    Structured (JSON lines) rotating application log — 10 MB x 5 files.
``logs/nour-code-audit.jsonl``
    One JSON object per tool call: ``ts``, ``session_id``, ``tool``,
    ``args_hash``, ``result_status``, ``duration_ms`` (+ error when it failed).
``GET /metrics``
    Prometheus exposition format, enabled when ``prometheus-client`` is
    installed and ``NOUR_CODE_METRICS_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import logging.handlers
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import Settings

try:  # optional dependency
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment
    PROMETHEUS_AVAILABLE = False


_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object (one per line)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.args and isinstance(record.args, Mapping):
            for key, value in record.args.items():
                if key not in _RESERVED:
                    payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return json.dumps({"ts": payload["ts"], "level": record.levelname, "message": "<unserialisable>"})


def setup_logging(settings: Settings) -> logging.Logger:
    """Configure the root logger; return the ``nour_code`` logger."""
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        settings.app_log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)-22s %(message)s", "%H:%M:%S")
    )
    stream.setLevel(getattr(logging, settings.log_level))
    root.addHandler(stream)

    # Keep third-party noise out of the rotating file at DEBUG level.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("nour_code")
    logger.setLevel(getattr(logging, settings.log_level))
    logger.debug("logging initialised", extra={"log_file": str(settings.app_log_file)})
    return logger


def hash_args(args: Any) -> str:
    """Stable, non-reversible fingerprint of a tool call's arguments."""
    try:
        canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class AuditLog:
    """Append-only JSONL audit trail (thread-safe, off the event loop)."""

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writes ------------------------------------------------------------
    def _write_sync(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    async def write(self, entry: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._write_sync, entry)
        except OSError as exc:  # pragma: no cover - disk full / perms
            logging.getLogger("nour_code.audit").error("audit write failed: %s", exc)

    async def tool_call(
        self,
        *,
        session_id: str,
        tool: str,
        args: Any,
        status: str,
        duration_ms: float,
        error: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ts_epoch": round(time.time(), 3),
            "session_id": session_id,
            "tool": tool,
            "args_hash": hash_args(args),
            "result_status": status,
            "duration_ms": round(duration_ms, 2),
        }
        if error:
            entry["error"] = error[:2000]
        if extra:
            entry.update(dict(extra))
        await self.write(entry)

    # -- reads -------------------------------------------------------------
    def _tail_sync(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-max(0, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"ts": "", "tool": "<unparsable>", "raw": line[:500]})
        return entries

    async def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._tail_sync, limit)
        except OSError as exc:  # pragma: no cover
            logging.getLogger("nour_code.audit").error("audit read failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class _NoopMetric:
    def labels(self, *_args: Any, **_kwargs: Any) -> _NoopMetric:
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def dec(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class Metrics:
    """Thin wrapper that degrades gracefully when prometheus-client is absent."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and PROMETHEUS_AVAILABLE
        if not self.enabled:
            self.requests = _NoopMetric()
            self.model_calls = _NoopMetric()
            self.model_latency = _NoopMetric()
            self.model_tokens = _NoopMetric()
            self.tool_calls = _NoopMetric()
            self.tool_latency = _NoopMetric()
            self.iterations = _NoopMetric()
            self.sessions = _NoopMetric()
            self.errors = _NoopMetric()
            return

        self.requests = Counter(
            "nour_code_requests_total", "HTTP requests", ["endpoint", "status"]
        )
        self.model_calls = Counter(
            "nour_code_model_calls_total", "Ollama chat completions", ["model", "outcome"]
        )
        self.model_latency = Histogram(
            "nour_code_model_latency_seconds", "Ollama chat completion latency", ["model"]
        )
        self.model_tokens = Counter(
            "nour_code_model_tokens_total", "Tokens processed", ["model", "direction"]
        )
        self.tool_calls = Counter(
            "nour_code_tool_calls_total", "Tool invocations", ["tool", "status"]
        )
        self.tool_latency = Histogram(
            "nour_code_tool_latency_seconds", "Tool execution latency", ["tool"]
        )
        self.iterations = Counter(
            "nour_code_agent_iterations_total", "Agent loop iterations", ["outcome"]
        )
        self.sessions = Gauge("nour_code_sessions_active", "Sessions currently in memory")
        self.errors = Counter("nour_code_errors_total", "Errors", ["where", "type"])

    def observe_model(
        self, *, model: str, latency_s: float, prompt_tokens: int, completion_tokens: int, outcome: str
    ) -> None:
        self.model_calls.labels(model=model, outcome=outcome).inc()
        self.model_latency.labels(model=model).observe(latency_s)
        if prompt_tokens:
            self.model_tokens.labels(model=model, direction="prompt").inc(prompt_tokens)
        if completion_tokens:
            self.model_tokens.labels(model=model, direction="completion").inc(completion_tokens)

    def observe_tool(self, *, tool: str, status: str, latency_s: float) -> None:
        self.tool_calls.labels(tool=tool, status=status).inc()
        self.tool_latency.labels(tool=tool).observe(latency_s)

    def render(self) -> tuple[bytes, str]:
        if not self.enabled:
            raise RuntimeError("metrics disabled")
        return generate_latest(), CONTENT_TYPE_LATEST
