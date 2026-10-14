"""Pydantic v2 schemas shared by the API layer, the agent loop and the tools.

Keeping every payload in one module means the OpenAPI docs, the internal
plumbing and the SSE stream can never drift apart.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model: forbid unknown fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def new_id(prefix: str = "") -> str:
    """Return a short, collision-resistant identifier."""
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}{uid}" if prefix else uid


def now_ms() -> float:
    return time.time() * 1000.0


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    """Body of ``POST /chat`` and ``POST /chat/sync``."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=200_000)
    session_id: str | None = Field(
        default=None,
        description="Resume an existing session; a new one is created when omitted.",
    )
    working_dir: str | None = Field(
        default=None, description="Override the session working directory."
    )
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    production_ready: bool | None = Field(
        default=None,
        description="Force production-ready mode on/off instead of auto-detecting it.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ToolCallRecord(BaseModel):
    """One executed tool call, as returned to the client."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "denied"] = "ok"
    result: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    iteration: int = 0
    source: Literal["builtin", "skill", "unknown"] = "builtin"
    skill: str | None = None


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    total_latency_ms: float = 0.0


class ChatResponse(BaseModel):
    """Body returned by ``POST /chat/sync`` (and by the final ``done`` event)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    content: str
    iterations: int = 0
    finished: bool = True
    finish_reason: str = "stop"
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    production_ready: bool = False
    production_checklist: list[dict[str, Any]] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Model plumbing (OpenAI-compatible shapes)
# ---------------------------------------------------------------------------
class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = ""
    type: Literal["function"] = "function"
    function: FunctionCall
    index: int = 0


class ChatMessage(BaseModel):
    """A single message in a session transcript."""

    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None, tool_calls: list[ToolCall] | None = None) -> ChatMessage:
        return cls(role="assistant", content=content, tool_calls=tool_calls or None)

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> ChatMessage:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    def to_payload(self) -> dict[str, Any]:
        """Serialize in the OpenAI wire format (``None`` fields dropped)."""
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.name:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
EventType = Literal[
    "token",
    "tool_call",
    "tool_result",
    "skill_loaded",
    "error",
    "done",
]


class StreamEvent(BaseModel):
    """An SSE frame. ``event:`` carries :attr:`type`, ``data:`` carries the JSON."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    ts: float = Field(default_factory=lambda: time.time())
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        """Render as an SSE frame: the event name carries ``type``, ``data`` the payload."""
        import json

        payload: dict[str, Any] = dict(self.data)
        payload.setdefault("type", self.type)
        payload.setdefault("ts", self.ts)
        return f"event: {self.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
class SkillInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    source: str = ""
    version: str = "0.1.0"
    loaded_at: float = Field(default_factory=lambda: time.time())


class SkillListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    skills: list[SkillInfo] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)


class SkillError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    error: str
    detail: str = ""


class ReloadSkillsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    loaded: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[SkillError] = Field(default_factory=list)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class SessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    created_at: float
    last_active: float
    message_count: int = 0
    tool_call_count: int = 0
    working_dir: str = ""
    production_ready: bool = False
    age_seconds: float = 0.0
    idle_seconds: float = 0.0


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    ttl_seconds: int = 0
    sessions: list[SessionInfo] = Field(default_factory=list)


class DeleteSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    session_id: str
    deleted: bool = False
    detail: str = ""


# ---------------------------------------------------------------------------
# Health / audit / errors
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "down"] = "ok"
    app: str = "nour-code"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0
    ollama_reachable: bool = False
    ollama_url: str = ""
    ollama_version: str | None = None
    model: str = ""
    model_loaded: bool = False
    available_models: list[str] = Field(default_factory=list)
    skills_loaded: int = 0
    active_sessions: int = 0
    tools: int = 0
    detail: str | None = None


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str = ""
    session_id: str = ""
    tool: str = ""
    args_hash: str = ""
    result_status: str = ""
    duration_ms: float = 0.0
    error: str | None = None


class AuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    path: str = ""
    entries: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    code: str = "internal_error"
    detail: str | None = None
