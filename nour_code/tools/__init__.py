"""Tool framework: Pydantic-validated arguments, permission checks, auditing.

Every tool is a :class:`ToolSpec` — a name, a JSON schema derived from a
Pydantic model, a permission scope and an async handler.  :class:`ToolRegistry`
is the single choke point through which all tool calls (built-in *and* skill
provided) pass, which is why auditing, timing, permission enforcement and
output truncation are guaranteed everywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..models import StreamEvent
from ..observability import AuditLog, Metrics
from ..permissions import PermissionDeniedError, PermissionManager

logger = logging.getLogger("nour_code.tools")

TRUNCATION_MARKER = "\n...[nour-code: output truncated, {removed} chars removed]...\n"


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Cap ``text`` at ``limit`` characters, keeping head and tail."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    overhead = len(TRUNCATION_MARKER) + 16
    keep = max(0, limit - overhead)
    head = keep // 2
    tail = keep - head
    removed = len(text) - keep
    return text[:head] + TRUNCATION_MARKER.format(removed=removed) + text[-tail:], True


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about the current request."""

    session_id: str = "-"
    working_dir: Path = field(default_factory=Path.cwd)
    settings: Settings | None = None
    permissions: PermissionManager | None = None
    audit: AuditLog | None = None
    logger: logging.Logger = logger
    emit: Callable[[StreamEvent], Awaitable[None]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve(self, path: str, **kwargs: Any) -> Path:
        assert self.permissions is not None, "ToolContext.permissions is required"
        return self.permissions.resolve_path(path, **kwargs)


@dataclass
class ToolResult:
    """Outcome of a tool call.  Tools raise nothing; they return this."""

    ok: bool = True
    output: Any = ""
    error: str | None = None
    status: Literal["ok", "error", "denied"] = "ok"
    duration_ms: float = 0.0
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: Any = "", **metadata: Any) -> "ToolResult":
        return cls(ok=True, status="ok", output=output, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> "ToolResult":
        return cls(ok=False, status="error", output="", error=error, metadata=metadata)

    @classmethod
    def denied(cls, reason: str, **metadata: Any) -> "ToolResult":
        return cls(ok=False, status="denied", output="", error=reason, metadata=metadata)

    def render(self, max_chars: int) -> str:
        """String form handed back to the model (always size-capped)."""
        if self.status == "denied":
            text = f"DENIED: {self.error}"
        elif not self.ok:
            text = f"ERROR: {self.error}"
        elif isinstance(self.output, str):
            text = self.output
        else:
            try:
                text = json.dumps(self.output, indent=2, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = repr(self.output)
        text, self.truncated = truncate_output(text, max_chars)
        return text

    def to_record(
        self, *, tool: str, arguments: dict[str, Any], iteration: int, source: str, skill: str | None, call_id: str
    ) -> dict[str, Any]:
        return {
            "id": call_id,
            "tool": tool,
            "arguments": arguments,
            "status": self.status,
            "result": self.output if isinstance(self.output, str) else json.dumps(self.output, default=str)[:4000],
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "iteration": iteration,
            "source": source,
            "skill": skill,
        }


@dataclass
class ToolSpec:
    """Declarative description of one callable tool."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[Any]]
    scope: str = ""  # permission key in permissions.yaml -> defaults to name
    kind: Literal["builtin", "skill"] = "builtin"
    skill: str | None = None
    schema_override: dict[str, Any] | None = None

    @property
    def permission_key(self) -> str:
        return self.scope or self.name

    def openai_schema(self) -> dict[str, Any]:
        if self.schema_override is not None:
            return self.schema_override
        parameters = self.args_model.model_json_schema()
        parameters.pop("title", None)
        if not parameters.get("properties"):
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        else:
            parameters.setdefault("type", "object")
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class ToolRegistry:
    """Holds every tool (built-in + skill provided) and executes them safely."""

    def __init__(
        self,
        *,
        settings: Settings,
        permissions: PermissionManager,
        audit: AuditLog | None = None,
        metrics: Metrics | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.permissions = permissions
        self.audit = audit
        self.metrics = metrics
        self.logger = logger or logging.getLogger("nour_code.tools")
        self._tools: dict[str, ToolSpec] = {}
        self._skill_dispatcher: Callable[[str, dict[str, Any], ToolContext], Awaitable[dict[str, Any]]] | None = None
        self._audit_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _log_audit_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():  # pragma: no cover - shutdown race
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logging.getLogger("nour_code.tools").warning("audit write failed: %s", exc)

    async def drain(self) -> None:
        """Await pending audit writes (called on shutdown and in tests)."""
        if self._audit_tasks:
            await asyncio.gather(*list(self._audit_tasks), return_exceptions=True)

    # -- registration ------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools and self._tools[spec.name].kind == "builtin" and spec.kind != "builtin":
            self.logger.warning(
                "skill %r tried to override built-in tool %r; keeping the built-in",
                spec.skill, spec.name,
            )
            return
        self._tools[spec.name] = spec
        self.logger.debug("registered tool %s (%s)", spec.name, spec.kind)

    def register_many(self, specs: Sequence[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_skill(self, skill_name: str) -> list[str]:
        removed = [n for n, s in self._tools.items() if s.skill == skill_name]
        for name in removed:
            del self._tools[name]
        return removed

    def set_skill_dispatcher(
        self, dispatcher: Callable[[str, dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]
    ) -> None:
        self._skill_dispatcher = dispatcher

    # -- introspection -----------------------------------------------------
    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].openai_schema() for name in sorted(self._tools)]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": self._tools[name].description,
                "kind": self._tools[name].kind,
                "skill": self._tools[name].skill,
                "scope": self._tools[name].permission_key,
            }
            for name in sorted(self._tools)
        ]

    # -- execution ---------------------------------------------------------
    async def call(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext,
        *,
        iteration: int = 0,
        call_id: str = "",
    ) -> ToolResult:
        """Validate -> authorise -> execute -> audit.  Never raises."""
        started = time.perf_counter()
        args = arguments or {}
        spec = self._tools.get(name)

        if spec is None:
            result = ToolResult.failure(
                f"unknown tool '{name}'. Available tools: {', '.join(self.names())}",
            )
            self._finish(result, started, name, args, ctx, iteration, call_id)
            return result

        try:
            # 1. permission scope
            try:
                self.permissions.check_tool(spec.permission_key)
            except PermissionDeniedError as exc:
                result = ToolResult.denied(str(exc))
                self._finish(result, started, name, args, ctx, iteration, call_id)
                return result

            # 2. argument validation (Pydantic v2)
            try:
                parsed = spec.args_model.model_validate(args)
            except ValidationError as exc:
                result = ToolResult.failure(
                    f"invalid arguments for {name}: "
                    + "; ".join(
                        f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}"
                        for e in exc.errors()[:5]
                    ),
                )
                self._finish(result, started, name, args, ctx, iteration, call_id)
                return result

            # 3. execute
            if spec.kind == "skill":
                if self._skill_dispatcher is None:  # pragma: no cover - defensive
                    result = ToolResult.failure("skill dispatcher is not configured")
                else:
                    payload = await asyncio.wait_for(
                        self._skill_dispatcher(name, args, ctx),
                        timeout=self.settings.skill_tool_timeout_seconds,
                    )
                    if isinstance(payload, dict) and payload.get("ok") is False:
                        # The skill ran but reports a failure: surface it as a tool error
                        # while keeping the structured payload for the model.
                        result = ToolResult(
                            ok=False,
                            status="error",
                            output=payload,
                            error=str(payload.get("error") or "skill reported a failure"),
                        )
                    else:
                        result = self._coerce_payload(payload)
            else:
                payload = await asyncio.wait_for(
                    spec.handler(parsed, ctx),
                    timeout=self.settings.skill_tool_timeout_seconds,
                )
                result = self._coerce_payload(payload)

        except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
            raise
        except asyncio.TimeoutError:
            result = ToolResult.failure(
                f"tool '{name}' timed out after {self.settings.skill_tool_timeout_seconds}s",
            )
        except PermissionDeniedError as exc:
            result = ToolResult.denied(str(exc))
        except FileNotFoundError as exc:
            result = ToolResult.failure(f"{name}: {exc.strerror or 'not found'}: {exc.filename}")
        except OSError as exc:
            result = ToolResult.failure(f"{name} failed with OS error: {exc.strerror or exc}")
        except Exception as exc:  # last resort: never crash the agent loop
            self.logger.exception("tool %s raised an unexpected error", name)
            result = ToolResult.failure(f"{name} raised {exc.__class__.__name__}: {exc}")

        self._finish(result, started, name, args, ctx, iteration, call_id)
        return result

    @staticmethod
    def _coerce_payload(payload: Any) -> ToolResult:
        """Normalise whatever a handler returned into a :class:`ToolResult`."""
        if isinstance(payload, ToolResult):
            return payload
        if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], bool):
            ok, output = payload
            return ToolResult.success(output) if ok else ToolResult.failure(str(output))
        return ToolResult.success(payload)

    def _finish(
        self,
        result: ToolResult,
        started: float,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        iteration: int,
        call_id: str,
    ) -> None:
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        spec = self._tools.get(name)
        result.metadata.setdefault("source", spec.kind if spec else "unknown")
        result.metadata.setdefault("skill", spec.skill if spec else None)
        result.metadata.setdefault("iteration", iteration)
        result.metadata.setdefault("call_id", call_id)

        self.logger.info(
            "tool call",
            extra={
                "tool": name,
                "status": result.status,
                "duration_ms": round(result.duration_ms, 2),
                "session_id": ctx.session_id,
                "iteration": iteration,
            },
        )
        if self.metrics:
            self.metrics.observe_tool(tool=name, status=result.status, latency_s=result.duration_ms / 1000.0)
        if ctx.audit is not None:
            task = asyncio.create_task(
                ctx.audit.tool_call(
                    session_id=ctx.session_id,
                    tool=name,
                    args=args,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    error=result.error,
                    extra={
                        "iteration": iteration,
                        "source": spec.kind if spec else "unknown",
                        "skill": spec.skill if spec else None,
                    },
                )
            )
            self._audit_tasks.add(task)
            task.add_done_callback(self._audit_tasks.discard)
            task.add_done_callback(self._log_audit_failure)
