"""The agentic loop: user message -> model -> tool call -> tool result -> answer.

Both transports share one implementation:

* :meth:`Agent.run_stream` yields :class:`~nour_code.models.StreamEvent` objects
  (``token``, ``tool_call``, ``tool_result``, ``skill_loaded``, ``error``, ``done``)
  as they happen, so the HTTP layer can push them over SSE without buffering.
* :meth:`Agent.run_sync` drives the same loop with blocking model calls and
  returns a single :class:`~nour_code.models.ChatResponse`.

Production-ready mode is handled by a preprocessor: when the user's message
matches a production trigger, an instruction block is injected into the system
prompt for that turn, the production skill's tools are invoked automatically on
the files the model touched, placeholders are scanned for (and retried), and a
checklist is appended to the final answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from .config import Settings
from .models import (
    ChatMessage,
    ChatResponse,
    ModelUsage,
    StreamEvent,
    ToolCall,
    ToolCallRecord,
    new_id,
)
from .ollama_client import ChatResult, OllamaClient, OllamaError, Usage
from .permissions import PermissionDeniedError, PermissionManager
from .prompts import (
    PLACEHOLDER_REGEX,
    build_production_checklist,
    build_system_prompt,
    detect_production_mode,
    render_checklist_markdown,
)
from .sessions import Session, SessionStore
from .skills import SkillsManager
from .tools import ToolContext, ToolRegistry, ToolResult

logger = logging.getLogger("nour_code.agent")

TOOL_EVENT_PREVIEW_CHARS = 4_000
SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go"})
IGNORED_SCAN_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".next", "logs"}
)
MAX_SCAN_FILES = 200
MAX_SCAN_BYTES = 400_000

SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "logging": re.compile(
        r"logging\.getLogger|logger\s*=|logger\.(?:info|debug|warning|error|exception)|"
        r"pino\(|winston\.createLogger|createLogger\(|log::|tracing::|structlog|getLogger\("
    ),
    "error_handling": re.compile(r"\btry\s*:|\bexcept\s+\w|try\s*\{|catch\s*\(|\.map_err\(|Result<|anyhow::"),
    "input_validation": re.compile(
        r"\braise\s+(?:ValueError|TypeError)\b|isinstance\(|throw new TypeError|zod|pydantic|validator\(|parse_obj"
    ),
    "config": re.compile(r"os\.environ|os\.getenv|process\.env|std::env::var|BaseSettings|getenv\(|Settings\("),
    "health": re.compile(r"""["'`]/(?:health|healthz|healthcheck|ready|readyz)["'`]|/health"""),
    "tests": re.compile(r"\bdef test_|\basync def test_|\bdescribe\(|\bit\(|#\[test\]|#\[tokio::test\]|pytest"),
}

PRODUCTION_FILE_ARGS = ("path", "file_path", "target", "source")


@dataclass
class TurnState:
    """Mutable bookkeeping for one user turn."""

    usage: Usage = field(default_factory=Usage)
    model_calls: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    iterations: int = 0
    started: float = field(default_factory=time.perf_counter)

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0


class Agent:
    """Drives the model <-> tools loop for one request at a time."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: OllamaClient,
        registry: ToolRegistry,
        store: SessionStore,
        permissions: PermissionManager,
        skills: SkillsManager,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.registry = registry
        self.store = store
        self.permissions = permissions
        self.skills = skills
        self.logger = logger or logging.getLogger("nour_code.agent")

    # -- context plumbing ---------------------------------------------------
    def _tool_context(
        self,
        session: Session,
        emit: Callable[[StreamEvent], Awaitable[None]] | None,
        *,
        audit: Any = None,
    ) -> ToolContext:
        return ToolContext(
            session_id=session.session_id,
            working_dir=session.working_dir,
            settings=self.settings,
            permissions=self.permissions,
            audit=audit,
            logger=self.logger,
            emit=emit,
            extra={"session": session},
        )

    def _system_message(self, session: Session, production_mode: bool) -> ChatMessage:
        listing = self._shallow_listing(session.working_dir)
        prompt = build_system_prompt(
            cwd=str(session.working_dir),
            files=listing,
            skills=[s.model_dump(mode="json") for s in self.skills.info().skills],
            permissions=self.permissions.describe(),
            tools=self.registry.describe(),
            production_mode=production_mode,
        )
        return ChatMessage.system(prompt)

    def _shallow_listing(self, directory: Path) -> list[dict[str, Any]]:
        """Shallow listing of the working directory (never raises)."""
        try:
            resolved = self.permissions.resolve_path(str(directory), must_exist=True, kind="dir")
        except PermissionDeniedError:
            return []
        entries: list[dict[str, Any]] = []
        try:
            for child in sorted(resolved.iterdir(), key=lambda p: p.name)[:60]:
                try:
                    is_dir = child.is_dir()
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    continue
                entries.append({"name": child.name, "type": "dir" if is_dir else "file", "size": size})
        except OSError as exc:
            self.logger.debug("cannot list %s: %s", directory, exc)
        return entries

    def _messages_for_model(self, session: Session, system: ChatMessage) -> list[ChatMessage]:
        return [system, *session.messages]

    # -- tool execution -----------------------------------------------------
    @staticmethod
    def _parse_arguments(raw: str) -> tuple[dict[str, Any] | None, str | None]:
        """Tolerant JSON parsing of a tool-call argument payload."""
        if not raw or not raw.strip():
            return {}, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None, f"tool arguments are not valid JSON: {raw[:200]}"
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            return None, f"tool arguments must be a JSON object, got {type(parsed).__name__}"
        return parsed, None

    async def _execute_tool_calls(
        self,
        session: Session,
        tool_calls: list[ToolCall],
        *,
        state: TurnState,
        iteration: int,
        ctx: ToolContext,
        emit: Callable[[StreamEvent], Awaitable[None]] | None,
    ) -> None:
        for call in tool_calls:
            arguments, parse_error = self._parse_arguments(call.function.arguments)
            name = call.function.name
            call_id = call.id or new_id("call_")

            if emit is not None:
                await emit(
                    StreamEvent(
                        type="tool_call",
                        data={
                            "id": call_id,
                            "tool": name,
                            "arguments": arguments or {"_raw": call.function.arguments[:500]},
                            "iteration": iteration,
                            "skill": self.skills.owner_of(name),
                        },
                    )
                )

            if parse_error is not None:
                result = ToolResult.failure(parse_error)
                rendered = f"ERROR: {parse_error}. Reply with a tool call whose arguments are valid JSON."
            else:
                assert arguments is not None
                result = await self.registry.call(
                    name, arguments, ctx, iteration=iteration, call_id=call_id
                )
                rendered = result.render(min(self.settings.max_tool_history_chars, self.settings.max_tool_output_chars))
                for key in PRODUCTION_FILE_ARGS:
                    value = arguments.get(key)
                    if isinstance(value, str) and value:
                        state.touched_files.append(value)
                        break

            session.tool_call_count += 1
            record = ToolCallRecord(
                id=call_id,
                tool=name,
                arguments=arguments or {},
                status=result.status,
                result=result.output if isinstance(result.output, str) else json.dumps(result.output, default=str)[:4000],
                error=result.error,
                duration_ms=round(result.duration_ms, 2),
                iteration=iteration,
                source=str(result.metadata.get("source", "builtin")),
                skill=self.skills.owner_of(name),
            )
            state.tool_calls.append(record)

            if emit is not None:
                await emit(
                    StreamEvent(
                        type="tool_result",
                        data={
                            "id": call_id,
                            "tool": name,
                            "status": result.status,
                            "result": record.result[:TOOL_EVENT_PREVIEW_CHARS],
                            "error": result.error,
                            "duration_ms": round(result.duration_ms, 2),
                            "iteration": iteration,
                            "skill": record.skill,
                        },
                    )
                )
            session.add(ChatMessage.tool_result(call_id, name, rendered))
