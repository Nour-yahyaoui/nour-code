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

from .approvals import ApprovalRegistry, is_mutating
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
from .ollama_client import ChatResult, ModelOptions, OllamaClient, OllamaError, Usage
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

#: Tools whose success changes a file on disk (used for ``file_change`` events).
FILE_MUTATING_TOOLS = frozenset({"write_file", "apply_patch", "delete_file"})


#: Command candidates per project marker, in priority order.
VERIFY_COMMANDS: tuple[tuple[str, str], ...] = (
    # `python -m pytest` also works when pytest is only installed in a venv.
    ("pyproject.toml", "python -m pytest -q"),
    ("pytest.ini", "python -m pytest -q"),
    ("setup.py", "python -m pytest -q"),
    ("requirements.txt", "python -m pytest -q"),
    ("package.json", "npm test --if-present"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Makefile", "make test"),
)


def detect_verify_command(root: Path) -> str | None:
    """Pick the project's test command from its manifest files (best effort)."""
    for marker, command in VERIFY_COMMANDS:
        if (root / marker).is_file():
            if marker == "package.json":
                text = (root / marker).read_text(encoding="utf-8", errors="replace")
                if '"test"' not in text:
                    continue
            return command
    return None


def _first_path(arguments: dict[str, Any] | None) -> str | None:
    """Best-effort target path of a filesystem tool call."""
    if not isinstance(arguments, dict):
        return None
    for key in PRODUCTION_FILE_ARGS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass
class TurnState:
    """Mutable bookkeeping for one user turn."""

    session_id: str = "-"
    usage: Usage = field(default_factory=Usage)
    model_calls: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    approval_mode: str = "off"
    verifications: list[dict[str, Any]] = field(default_factory=list)
    recent_signatures: list[str] = field(default_factory=list)
    approvals_requested: int = 0
    approvals_rejected: list[str] = field(default_factory=list)
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
        approvals: ApprovalRegistry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.registry = registry
        self.store = store
        self.permissions = permissions
        self.skills = skills
        self.approvals = approvals or ApprovalRegistry()
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

    def _system_message(self, session: Session, production_mode: bool, extra: str | None = None) -> ChatMessage:
        listing = self._shallow_listing(session.working_dir)
        prompt = build_system_prompt(
            cwd=str(session.working_dir),
            files=listing,
            skills=[s.model_dump(mode="json") for s in self.skills.info().skills],
            permissions=self.permissions.describe(),
            tools=self.registry.describe(),
            production_mode=production_mode,
            extra_context=extra,
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

            if self._needs_approval(name, state):
                state.approvals_requested += 1
                decision = await self._request_approval(
                    session, state, tool=name, arguments=arguments or {}, iteration=iteration, skill=self.skills.owner_of(name), emit=emit
                )
                if decision != "approved":
                    reason = {
                        "rejected": "the user rejected this action",
                        "timeout": f"no decision within {self.approvals.timeout_seconds:.0f}s",
                        "cancelled": "the turn was cancelled",
                    }.get(decision, decision)
                    state.approvals_rejected.append(f"{name}: {reason}")
                    result = ToolResult.denied(f"DENIED: {reason}")
                    rendered = (
                        f"DENIED: {reason}. Do not retry this call; either propose something else "
                        "or explain to the user what you would do instead."
                    )
                    session.tool_call_count += 1
                    record = ToolCallRecord(
                        id=call_id,
                        tool=name,
                        arguments=arguments or {},
                        status=result.status,
                        result="",
                        error=reason,
                        duration_ms=0.0,
                        iteration=iteration,
                        source="builtin",
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
                                    "result": "",
                                    "error": reason,
                                    "duration_ms": 0.0,
                                    "iteration": iteration,
                                    "skill": record.skill,
                                    "rejected": True,
                                },
                            )
                        )
                    session.add(ChatMessage.tool_result(call_id, name, rendered))

                    continue

            pre_existing: bool | None = None
            if name in FILE_MUTATING_TOOLS:
                target_before = _first_path(arguments)
                if target_before:
                    try:
                        pre_existing = self.permissions.resolve_path(target_before, kind="any").exists()
                    except PermissionDeniedError:
                        pre_existing = None

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
            if emit is not None and result.status == "ok" and name in FILE_MUTATING_TOOLS:
                target = _first_path(arguments)
                if target:
                    try:
                        resolved = self.permissions.resolve_path(target, kind="any")
                    except PermissionDeniedError:
                        resolved = None
                    if resolved is not None:
                        if name == "delete_file":
                            action = "deleted"
                        else:
                            action = "modified" if pre_existing else "created"
                        await emit(
                            StreamEvent(
                                type="file_change",
                                data={
                                    "tool": name,
                                    "path": str(resolved),
                                    "action": action,
                                    "iteration": iteration,
                                    "call_id": call_id,
                                },
                            )
                        )

    # -- loop guards --------------------------------------------------------
    def _repeated_call(self, state: TurnState, calls: list[ToolCall]) -> bool:
        """True when the model keeps issuing the identical call (a stuck loop)."""
        if not calls:
            return False
        threshold = self.settings.loop_detection_threshold
        signature = json.dumps(
            [{"tool": call.function.name, "arguments": call.function.arguments} for call in calls],
            sort_keys=True,
        )
        state.recent_signatures.append(signature)
        if len(state.recent_signatures) < threshold:
            return False
        tail = state.recent_signatures[-threshold:]
        return len(set(tail)) == 1

    # -- verification -------------------------------------------------------
    async def _verify(
        self,
        session: Session,
        state: TurnState,
        ctx: ToolContext,
        emit: Callable[[StreamEvent], Awaitable[None]],
    ) -> None:
        """Run the project's own checks and report the result."""
        command = detect_verify_command(session.working_dir)
        if not command:
            state.warnings.append("verification: no test command detected for this project")
            return

        result = await self.registry.call(
            "run_shell",
            {"command": command, "timeout_seconds": self.settings.verify_timeout_seconds},
            ctx,
            iteration=state.iterations,
        )
        if result.status == "denied":
            state.warnings.append(f"verification: `{command}` was denied: {result.error}")
            return

        output = result.output if isinstance(result.output, dict) else {}
        payload = {
            "command": command,
            "exit_code": output.get("exit_code", -1),
            "stdout": (output.get("stdout") or "")[: self.settings.max_tool_output_chars],
            "stderr": (output.get("stderr") or "")[: self.settings.max_tool_output_chars],
            "status": result.status,
            "output": (output.get("stdout") or output.get("stderr") or "")[:4000],
        }
        state.verifications.append(payload)
        if payload["exit_code"] != 0:
            state.warnings.append(f"verification failed: `{command}` exited with {payload['exit_code']}")
        await emit(StreamEvent(type="verification", data=payload))

    # -- approvals ----------------------------------------------------------
    def _needs_approval(self, tool: str, state: TurnState) -> bool:
        """Whether this call must be confirmed by a human before it runs."""
        mode = state.approval_mode
        if mode == "off":
            return False
        if mode == "all":
            return True
        return is_mutating(tool, skill=self.skills.owner_of(tool))

    async def _request_approval(
        self,
        session: Session,
        state: TurnState,
        *,
        tool: str,
        arguments: dict[str, Any],
        iteration: int,
        skill: str | None,
        emit: Callable[[StreamEvent], Awaitable[None]] | None,
    ) -> str:
        gate = self.approvals.get(session.session_id)
        if gate is None:  # no gate (e.g. unit test without the HTTP layer)
            return "approved"
        return await gate.request(tool=tool, arguments=arguments, iteration=iteration, skill=skill, emit=emit)

    # -- event helpers ------------------------------------------------------
    async def _drain(self, queue: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
        while queue:
            yield queue.pop(0)

    async def _emit_skill_events(self, session: Session, emit: Callable[[StreamEvent], Awaitable[None]]) -> None:
        """Announce loaded skills once per (re)load so clients can display them."""
        version = self.skills.version
        if session.metadata.get("skills_version") == version:
            return
        session.metadata["skills_version"] = version
        for info in self.skills.info().skills:
            await emit(StreamEvent(type="skill_loaded", data=info.model_dump(mode="json")))

    # -- model iterations ---------------------------------------------------
    def _log_model_call(self, state: TurnState, usage: Usage, latency_ms: float, iteration: int, outcome: str) -> None:
        self.logger.info(
            "model call",
            extra={
                "session_id": getattr(state, "session_id", "-"),
                "iteration": iteration,
                "latency_ms": round(latency_ms, 2),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "model": self.settings.model,
                "outcome": outcome,
            },
        )

    async def _stream_iteration(
        self,
        messages: list[ChatMessage],
        emit: Callable[[StreamEvent], Awaitable[None]],
        state: TurnState,
        iteration: int,
        options: ModelOptions | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str, list[ToolCall], Usage, str]:
        content: list[str] = []
        thinking: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage()
        finish = "stop"
        started = time.perf_counter()
        schemas = tools if tools is not None else self.registry.schemas()
        first_chunk = True
        await emit(
            StreamEvent(
                type="status",
                data={
                    "phase": "model_call",
                    "iteration": iteration,
                    "model": (options.model if options and options.model else self.settings.model),
                    "prompt_chars": sum(len(message.content or "") for message in messages),
                    "tools": len(schemas),
                },
            )
        )
        async for chunk in self.client.stream_chat(messages, schemas, options=options):
            if first_chunk:
                first_chunk = False
                await emit(
                    StreamEvent(
                        type="status",
                        data={
                            "phase": "model_streaming",
                            "iteration": iteration,
                            "wait_ms": round((time.perf_counter() - started) * 1000, 2),
                        },
                    )
                )
            if chunk.kind == "thinking":
                thinking.append(chunk.text)
                await emit(StreamEvent(type="thinking", data={"text": chunk.text, "iteration": iteration}))
            elif chunk.kind == "token":
                content.append(chunk.text)
                await emit(StreamEvent(type="token", data={"text": chunk.text, "iteration": iteration}))
            elif chunk.kind == "tool_calls":
                calls = chunk.tool_calls
                finish = chunk.finish_reason or "tool_calls"
                if chunk.usage:
                    usage = usage + chunk.usage
            elif chunk.kind == "usage":
                finish = chunk.finish_reason or "stop"
                if chunk.usage:
                    usage = usage + chunk.usage
            elif chunk.kind == "error":
                state.warnings.append(f"model stream error: {chunk.error}")
        latency_ms = (time.perf_counter() - started) * 1000.0
        state.usage = state.usage + usage
        state.model_calls += 1
        self._log_model_call(state, usage, latency_ms, iteration, "ok")
        return "".join(content), "".join(thinking), calls, usage, finish

    async def _sync_iteration(
        self,
        messages: list[ChatMessage],
        state: TurnState,
        iteration: int,
        options: ModelOptions | None = None,
        tools: list[dict[str, Any]] | None = None,
        emit: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> tuple[str, str, list[ToolCall], Usage, str]:
        schemas = tools if tools is not None else self.registry.schemas()
        if emit is not None:
            await emit(
                StreamEvent(
                    type="status",
                    data={
                        "phase": "model_call",
                        "iteration": iteration,
                        "model": (options.model if options and options.model else self.settings.model),
                        "prompt_chars": sum(len(message.content or "") for message in messages),
                        "tools": len(schemas),
                    },
                )
            )
        result: ChatResult = await self.client.chat(messages, schemas, options=options)
        state.usage = state.usage + result.usage
        state.model_calls += 1
        self._log_model_call(state, result.usage, result.latency_ms, iteration, "ok")
        return result.content, result.thinking, result.tool_calls, result.usage, result.finish_reason

    # -- production-ready mode ---------------------------------------------
    async def _production_candidates(self, session: Session, state: TurnState) -> list[Path]:
        """Files the model touched, or the project's top-level sources as a fallback."""
        candidates: list[Path] = []
        for raw in dict.fromkeys(state.touched_files):
            try:
                path = self.permissions.resolve_path(raw, must_exist=True, kind="file")
            except PermissionDeniedError:
                continue
            if path.suffix.lower() in SOURCE_SUFFIXES and path not in candidates:
                candidates.append(path)
        if candidates:
            return candidates[: self.settings.production_max_files]

        try:
            root = self.permissions.resolve_path(str(session.working_dir), must_exist=True, kind="dir")
        except PermissionDeniedError:
            return []
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for child in children:
            if child.is_file() and child.suffix.lower() in SOURCE_SUFFIXES:
                candidates.append(child)
            if len(candidates) >= 5:
                break
        return candidates

    async def _production_harden(
        self,
        session: Session,
        state: TurnState,
        ctx: ToolContext,
        emit: Callable[[StreamEvent], Awaitable[None]],
        *,
        round_index: int,
        user_message: str,
    ) -> None:
        """Run the production skill's hardening tools on the files that changed."""
        if not self.settings.production_auto_harden or round_index > 0:
            return

        candidates = await self._production_candidates(session, state)
        if not candidates:
            state.warnings.append("production mode: no source files were touched, nothing to harden")
            return

        hardening_tools = [
            name
            for name in ("add_logging", "add_error_handling", "add_input_validation")
            if name in self.registry.names()
        ]
        if not hardening_tools:
            state.warnings.append("production mode: production_skill is not loaded, skipping hardening")
            return

        for path in candidates:
            for tool_name in hardening_tools:
                result = await self.registry.call(
                    tool_name, {"file_path": str(path)}, ctx, iteration=state.iterations
                )
                state.tool_calls.append(
                    ToolCallRecord(
                        id=new_id("call_"),
                        tool=tool_name,
                        arguments={"file_path": str(path)},
                        status=result.status,
                        result=result.output if isinstance(result.output, str) else json.dumps(result.output, default=str)[:2000],
                        error=result.error,
                        duration_ms=round(result.duration_ms, 2),
                        iteration=state.iterations,
                        source="skill",
                        skill=self.skills.owner_of(tool_name),
                    )
                )
                await emit(
                    StreamEvent(
                        type="tool_result",
                        data={
                            "id": new_id("call_"),
                            "tool": tool_name,
                            "status": result.status,
                            "result": (result.output if isinstance(result.output, str) else json.dumps(result.output, default=str))[:TOOL_EVENT_PREVIEW_CHARS],
                            "error": result.error,
                            "duration_ms": round(result.duration_ms, 2),
                            "iteration": state.iterations,
                            "skill": self.skills.owner_of(tool_name),
                            "auto": "production_ready",
                        },
                    )
                )

        lowered = user_message.lower()
        if any(token in lowered for token in ("docker", "container", "image")) and "generate_dockerfile" in self.registry.names():
            result = await self.registry.call(
                "generate_dockerfile", {"project_root": str(session.working_dir)}, ctx, iteration=state.iterations
            )
            state.tool_calls.append(
                ToolCallRecord(
                    id=new_id("call_"),
                    tool="generate_dockerfile",
                    arguments={"project_root": str(session.working_dir)},
                    status=result.status,
                    result=json.dumps(result.output, default=str)[:2000],
                    error=result.error,
                    duration_ms=round(result.duration_ms, 2),
                    iteration=state.iterations,
                    source="skill",
                    skill=self.skills.owner_of("generate_dockerfile"),
                )
            )
            await emit(
                StreamEvent(
                    type="tool_result",
                    data={
                        "id": new_id("call_"),
                        "tool": "generate_dockerfile",
                        "status": result.status,
                        "result": json.dumps(result.output, default=str)[:TOOL_EVENT_PREVIEW_CHARS],
                        "error": result.error,
                        "auto": "production_ready",
                    },
                )
            )
        if any(token in lowered for token in (" ci", "pipeline", "github action", "workflow")) and "generate_ci_pipeline" in self.registry.names():
            result = await self.registry.call(
                "generate_ci_pipeline",
                {"project_root": str(session.working_dir), "target": "github"},
                ctx,
                iteration=state.iterations,
            )
            state.tool_calls.append(
                ToolCallRecord(
                    id=new_id("call_"),
                    tool="generate_ci_pipeline",
                    arguments={"project_root": str(session.working_dir), "target": "github"},
                    status=result.status,
                    result=json.dumps(result.output, default=str)[:2000],
                    error=result.error,
                    duration_ms=round(result.duration_ms, 2),
                    iteration=state.iterations,
                    source="skill",
                    skill=self.skills.owner_of("generate_ci_pipeline"),
                )
            )

    async def _production_scan(self, session: Session, state: TurnState) -> dict[str, Any]:
        """Look for placeholder stubs left behind in the files that changed."""
        offenders: list[str] = []
        for path in await self._production_candidates(session, state):
            try:
                resolved = self.permissions.resolve_path(str(path), must_exist=True, kind="file")
            except PermissionDeniedError:
                continue
            try:
                text = await anyio.Path(resolved).read_text(errors="replace")
            except OSError:
                continue
            for match in PLACEHOLDER_REGEX.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{resolved}:{line}: {match.group(0)}")
                break
        return {"placeholders": bool(offenders), "offenders": offenders[:40], "count": len(offenders)}

    async def _scan_signals(self, root: Path) -> dict[str, Any]:
        """One bounded pass over the project collecting production signals."""
        signals: dict[str, Any] = {key: [] for key in SIGNAL_PATTERNS}
        scanned = 0
        placeholder_hits: list[str] = []
        test_files: list[str] = []
        try:
            resolved_root = self.permissions.resolve_path(str(root), must_exist=True, kind="dir")
        except PermissionDeniedError:
            return {"signals": signals, "scanned": 0, "placeholders": [], "test_files": []}

        async for path in anyio.Path(resolved_root).rglob("*"):
            if scanned >= MAX_SCAN_FILES:
                break
            try:
                if not await path.is_file():
                    continue
            except OSError:
                continue
            if any(part in IGNORED_SCAN_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in {"Dockerfile", "Makefile"}:
                continue
            try:
                if (await path.stat()).st_size > MAX_SCAN_BYTES:
                    continue
            except OSError:
                continue
            try:
                text = await path.read_text(errors="replace")
            except OSError:
                continue
            scanned += 1
            lowered_name = path.name.lower()
            if (
                "test" in lowered_name
                or "spec" in lowered_name
                or any(part in {"tests", "test", "__tests__"} for part in path.parts)
            ):
                test_files.append(str(path))
            for key, pattern in SIGNAL_PATTERNS.items():
                if key == "tests":
                    continue
                if pattern.search(text):
                    signals[key].append(str(path))
            match = PLACEHOLDER_REGEX.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                placeholder_hits.append(f"{path}:{line}: {match.group(0)}")
        return {
            "signals": signals,
            "scanned": scanned,
            "placeholders": placeholder_hits,
            "test_files": test_files,
        }

    async def _production_status(self, session: Session, state: TurnState) -> dict[str, tuple[bool, str]]:
        """Evaluate each checklist item against the real project state."""
        root = session.working_dir
        scan = await self._scan_signals(root)
        signals = scan["signals"]
        status: dict[str, tuple[bool, str]] = {}

        def _mark(key: str, ok: bool, detail: str) -> None:
            status[key] = (ok, detail)

        _mark(
            "logging",
            bool(signals["logging"]),
            f"found in {signals['logging'][0]}" if signals["logging"] else "no logger usage found",
        )
        _mark(
            "error_handling",
            bool(signals["error_handling"]),
            f"found in {signals['error_handling'][0]}" if signals["error_handling"] else "no try/except or catch blocks",
        )
        _mark(
            "input_validation",
            bool(signals["input_validation"]),
            f"found in {signals['input_validation'][0]}" if signals["input_validation"] else "no validation found",
        )
        _mark(
            "config",
            bool(signals["config"]),
            f"found in {signals['config'][0]}" if signals["config"] else "no environment configuration found",
        )
        _mark(
            "health",
            bool(signals["health"]),
            f"found in {signals['health'][0]}" if signals["health"] else "no /health endpoint found",
        )
        _mark(
            "tests",
            bool(scan["test_files"]) or bool(signals["tests"]),
            f"{len(scan['test_files'])} test file(s)" if scan["test_files"] else "no test files found",
        )
        placeholders = scan["placeholders"]
        _mark(
            "no_placeholders",
            not placeholders,
            "clean" if not placeholders else f"{len(placeholders)} placeholder(s): {placeholders[0]}",
        )
        dockerfile = root / "Dockerfile"
        _mark("dockerfile", dockerfile.is_file(), str(dockerfile) if dockerfile.is_file() else "no Dockerfile")
        workflows = root / ".github" / "workflows"
        has_ci = workflows.is_dir() and any(workflows.glob("*.y*ml"))
        _mark("ci", bool(has_ci), str(workflows) if has_ci else "no .github/workflows found")
        return status

    # -- the loop -----------------------------------------------------------
    async def _turn(
        self,
        session: Session,
        user_message: str,
        *,
        max_iterations: int | None,
        production_mode: bool | None,
        audit: Any,
        stream: bool,
        approval_mode: str | None = None,
        verify: bool | None = None,
        system_extra: str | None = None,
        tools: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        enable_thinking: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        queue: list[StreamEvent] = []

        async def emit(event: StreamEvent) -> None:
            queue.append(event)

        state = TurnState()
        state.session_id = session.session_id  # type: ignore[attr-defined]
        limit = max_iterations or self.settings.max_iterations
        production = detect_production_mode(user_message) if production_mode is None else production_mode
        if production:
            session.production_ready = True
        state.approval_mode = (approval_mode or self.settings.approval_mode or "off").lower()
        options = ModelOptions(
            model=model,
            temperature=temperature,
            seed=seed,
            enable_thinking=enable_thinking,
        )
        if tools:
            allowed = set(tools)
            schemas = [
                schema
                for schema in self.registry.schemas()
                if str((schema.get("function") or {}).get("name") or schema.get("name") or "") in allowed
            ]
        else:
            schemas = None
        # Registered on the container so POST /sessions/{id}/approve can reach it.
        self.approvals.create(session.session_id, emit=emit)

        session.add(ChatMessage.user(user_message))
        system = self._system_message(session, production, extra=system_extra)
        ctx = self._tool_context(session, emit, audit=audit)
        await self._emit_skill_events(session, emit)
        async for event in self._drain(queue):
            yield event

        rounds = 1 + (self.settings.production_max_remediation_rounds if production else 0)
        final_text = ""
        finished = False
        finish_reason = "stop"

        try:
            deadline = time.monotonic() + self.settings.max_turn_seconds
            budget_exhausted: str | None = None

            for round_index in range(rounds):
                for iteration in range(1, limit + 1):
                    if time.monotonic() > deadline:
                        budget_exhausted = (
                            f"stopped after {self.settings.max_turn_seconds:.0f}s (time budget exhausted)"
                        )
                        break
                    if self.settings.max_turn_tokens and state.usage.total_tokens >= self.settings.max_turn_tokens:
                        budget_exhausted = (
                            f"stopped after {state.usage.total_tokens} tokens (token budget exhausted)"
                        )
                        break
                    state.iterations = iteration
                    messages = self._messages_for_model(session, system)
                    if stream:
                        content, thought, calls, _usage, finish = await self._stream_iteration(
                            messages, emit, state, iteration, options=options, tools=schemas
                        )
                    else:
                        content, thought, calls, _usage, finish = await self._sync_iteration(
                            messages, state, iteration, options=options, tools=schemas, emit=emit
                        )
                    if thought:
                        state.thinking_parts.append(thought)
                    await emit(
                        StreamEvent(
                            type="usage",
                            data={
                                "iteration": iteration,
                                "prompt_tokens": _usage.prompt_tokens,
                                "completion_tokens": _usage.completion_tokens,
                                "total_tokens": _usage.total_tokens,
                                "elapsed_ms": round(state.duration_ms, 2),
                            },
                        )
                    )

                    if calls:
                        session.add(ChatMessage.assistant(content or None, calls))
                        await self._execute_tool_calls(
                            session, calls, state=state, iteration=iteration, ctx=ctx, emit=emit
                        )
                        async for event in self._drain(queue):
                            yield event
                        if self._repeated_call(state, calls):
                            budget_exhausted = (
                                f"stopped: `{calls[0].function.name}` was called "
                                f"{self.settings.loop_detection_threshold} times in a row with the same arguments"
                            )
                            break
                        continue

                    final_text = content
                    finish_reason = finish
                    finished = True
                    session.add(ChatMessage.assistant(content))
                    async for event in self._drain(queue):
                        yield event
                    break
                else:
                    state.warnings.append(
                        f"stopped after {limit} iterations without a final answer (partial state returned)"
                    )
                    finish_reason = "max_iterations"
                    finished = False

                if budget_exhausted:
                    state.warnings.append(budget_exhausted)
                    finish_reason = (
                        "loop_detected"
                        if "in a row" in budget_exhausted
                        else ("token_budget" if "token budget" in budget_exhausted else "time_budget")
                    )
                    finished = False
                    break

                if not production:
                    break

                await self._production_harden(
                    session, state, ctx, emit, round_index=round_index, user_message=user_message
                )
                async for event in self._drain(queue):
                    yield event

                scan = await self._production_scan(session, state)
                if not scan["placeholders"]:
                    break
                state.warnings.append(
                    f"found {scan['count']} placeholder(s) in the files that changed"
                )
                if round_index == rounds - 1:
                    state.warnings.append("placeholders remain after the remediation rounds")
                    break
                offenders = "\n".join(f"- {item}" for item in scan["offenders"])
                session.add(
                    ChatMessage.user(
                        "Production-ready check failed. Replace every placeholder stub you left "
                        "with a real implementation and re-emit the affected files:\n"
                        f"{offenders}\n"
                        "Do not answer with an explanation only — call the tools and fix the code."
                    )
                )
        except asyncio.CancelledError:
            raise
        except OllamaError as exc:
            self.logger.error("ollama failure: %s", exc)
            state.warnings.append(str(exc))
            yield StreamEvent(type="error", data={"message": str(exc), "code": "ollama_error", "fatal": True})
        except Exception as exc:  # noqa: BLE001 - the stream must still end cleanly
            self.logger.exception("agent loop failed")
            message = f"{exc.__class__.__name__}: {exc}"
            state.warnings.append(message)
            yield StreamEvent(type="error", data={"message": message, "code": "agent_error", "fatal": True})

        checklist: list[dict[str, Any]] = []
        if production:
            status = await self._production_status(session, state)
            checklist = build_production_checklist(status)
            final_text = (final_text or "") + render_checklist_markdown(checklist)
            failed = [row for row in checklist if row["status"] != "ok"]
            if failed and finished:
                state.warnings.append(
                    f"{len(failed)} production-readiness item(s) still open: "
                    + ", ".join(row["item"] for row in failed)
                )

        if verify or (verify is None and self.settings.verify_default):
            try:
                await self._verify(session, state, ctx, emit)
                async for event in self._drain(queue):
                    yield event
            except Exception as exc:  # noqa: BLE001 - verification must never break a turn
                self.logger.warning("verification failed: %s", exc)
                state.warnings.append(f"verification error: {exc}")

        self.approvals.discard(session.session_id)
        session.trim(self.settings.max_history_messages)
        response = ChatResponse(
            session_id=session.session_id,
            content=final_text,
            thinking="\n\n".join(part for part in state.thinking_parts if part),
            iterations=state.iterations,
            finished=finished,
            finish_reason=finish_reason,
            tool_calls=state.tool_calls,
            warnings=state.warnings,
            production_ready=production,
            production_checklist=checklist,
            verifications=state.verifications,
            usage=ModelUsage(
                prompt_tokens=state.usage.prompt_tokens,
                completion_tokens=state.usage.completion_tokens,
                total_tokens=state.usage.total_tokens,
                calls=state.model_calls,
                total_latency_ms=round(state.usage.total_duration_ns / 1_000_000, 2),
            ),
            duration_ms=round(state.duration_ms, 2),
        )
        self.logger.info(
            "turn complete",
            extra={
                "session_id": session.session_id,
                "iterations": state.iterations,
                "tool_calls": len(state.tool_calls),
                "finished": finished,
                "production_ready": production,
                "duration_ms": round(state.duration_ms, 2),
            },
        )
        yield StreamEvent(type="done", data={"response": response.model_dump(mode="json")})

    # -- public API ---------------------------------------------------------
    async def run_stream(
        self,
        session: Session,
        user_message: str,
        *,
        max_iterations: int | None = None,
        production_mode: bool | None = None,
        audit: Any = None,
        approval_mode: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        enable_thinking: bool | None = None,
        tools: list[str] | None = None,
        system_extra: str | None = None,
        verify: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the turn as SSE events (tokens arrive as the model emits them)."""
        async for event in self._turn(
            session,
            user_message,
            max_iterations=max_iterations,
            production_mode=production_mode,
            audit=audit,
            stream=True,
            approval_mode=approval_mode,
            model=model,
            temperature=temperature,
            seed=seed,
            enable_thinking=enable_thinking,
            tools=tools,
            system_extra=system_extra,
            verify=verify,
        ):
            yield event

    async def run_sync(
        self,
        session: Session,
        user_message: str,
        *,
        max_iterations: int | None = None,
        production_mode: bool | None = None,
        audit: Any = None,
        approval_mode: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        enable_thinking: bool | None = None,
        tools: list[str] | None = None,
        system_extra: str | None = None,
        verify: bool | None = None,
    ) -> ChatResponse:
        """Run the turn to completion and return the whole response."""
        fallback = ChatResponse(session_id=session.session_id, content="", finished=False, finish_reason="error")
        async for event in self._turn(
            session,
            user_message,
            max_iterations=max_iterations,
            production_mode=production_mode,
            audit=audit,
            stream=False,
            approval_mode=approval_mode,
            model=model,
            temperature=temperature,
            seed=seed,
            enable_thinking=enable_thinking,
            tools=tools,
            system_extra=system_extra,
            verify=verify,
        ):
            if event.type == "done":
                return ChatResponse.model_validate(event.data["response"])
            if event.type == "error":
                fallback.warnings.append(str(event.data.get("message", "unknown error")))
                fallback.content = ""
        fallback.finish_reason = "error"
        return fallback
