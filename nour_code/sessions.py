"""In-memory session store with TTL expiry and JSON persistence.

Sessions live in a plain dict (no global module state — the store is built once
by the application container and injected wherever it is needed).  They are
persisted to ``nour-code-sessions.json`` on shutdown and reloaded on startup;
anything idle for longer than ``NOUR_CODE_SESSION_TTL_SECONDS`` (default 2h) is
dropped by a background sweeper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ChatMessage, SessionInfo, SessionListResponse

logger = logging.getLogger("nour_code.sessions")

PERSIST_VERSION = 1


@dataclass
class Session:
    """Conversation state for one client session."""

    session_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    working_dir: Path = field(default_factory=Path.cwd)
    tool_call_count: int = 0
    production_ready: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- state --------------------------------------------------------------
    def touch(self) -> None:
        self.last_active = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active

    def expired(self, ttl_seconds: int) -> bool:
        return self.idle_seconds > ttl_seconds

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        self.touch()

    def trim(self, max_messages: int) -> None:
        """Drop the oldest non-system messages when history grows too large."""
        if len(self.messages) <= max_messages:
            return
        keep: list[ChatMessage] = []
        system: list[ChatMessage] = []
        for message in self.messages:
            (system if message.role == "system" else keep).append(message)
        overflow = len(keep) - max(2, max_messages)
        if overflow > 0:
            keep = keep[overflow:]
        self.messages = system[:1] + keep
        logger.debug("trimmed session %s to %d messages", self.session_id, len(self.messages))

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            created_at=self.created_at,
            last_active=self.last_active,
            message_count=len(self.messages),
            tool_call_count=self.tool_call_count,
            working_dir=str(self.working_dir),
            production_ready=self.production_ready,
            age_seconds=round(time.time() - self.created_at, 1),
            idle_seconds=round(self.idle_seconds, 1),
        )

    # -- (de)serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "working_dir": str(self.working_dir),
            "tool_call_count": self.tool_call_count,
            "production_ready": self.production_ready,
            "metadata": self.metadata,
            "messages": [m.model_dump(mode="json", exclude_none=True) for m in self.messages],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session | None:
        try:
            messages: list[ChatMessage] = []
            for item in raw.get("messages") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    messages.append(ChatMessage.model_validate(item))
                except Exception:  # pydantic ValidationError - skip bad row
                    continue
            return cls(
                session_id=str(raw["session_id"]),
                messages=messages,
                created_at=float(raw.get("created_at", time.time())),
                last_active=float(raw.get("last_active", time.time())),
                working_dir=Path(raw.get("working_dir") or Path.cwd()),
                tool_call_count=int(raw.get("tool_call_count", 0)),
                production_ready=bool(raw.get("production_ready", False)),
                metadata=dict(raw.get("metadata") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("skipping unreadable session record: %s", exc)
            return None


class SessionStore:
    """Thread-safe (well, task-safe) session registry."""

    def __init__(self, settings: Settings, *, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.logger = logger or logging.getLogger("nour_code.sessions")
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        await self.load()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="nour-code-session-sweeper")
        self.logger.info(
            "session store started",
            extra={"file": str(self.settings.sessions_file), "ttl": self.settings.session_ttl_seconds},
        )

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        await self.persist()

    # -- crud ---------------------------------------------------------------
    async def get_or_create(
        self,
        session_id: str | None = None,
        *,
        working_dir: Path | None = None,
        production_ready: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id) if session_id else None
            if session is None:
                from .models import new_id

                session = Session(
                    session_id=session_id or new_id("sess_"),
                    working_dir=working_dir or self.settings.working_dir,
                    production_ready=production_ready,
                    metadata=dict(metadata or {}),
                )
                self._sessions[session.session_id] = session
                self.logger.info("session created", extra={"session_id": session.session_id})
            else:
                session.touch()
                if working_dir is not None:
                    session.working_dir = working_dir
                if production_ready:
                    session.production_ready = True
                if metadata:
                    session.metadata.update(metadata)
            return session

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._locks.pop(session_id, None)
        if session is None:
            return False
        self.logger.info("session deleted", extra={"session_id": session_id})
        return True

    async def list(self) -> SessionListResponse:
        async with self._lock:
            sessions = [s.info() for s in self._sessions.values()]
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return SessionListResponse(
            count=len(sessions), ttl_seconds=self.settings.session_ttl_seconds, sessions=sessions
        )

    def lock(self, session_id: str) -> asyncio.Lock:
        """Per-session lock so concurrent requests cannot interleave history."""
        existing = self._locks.get(session_id)
        if existing is None:
            existing = asyncio.Lock()
            self._locks[session_id] = existing
        return existing

    # -- expiry -------------------------------------------------------------
    async def sweep_expired(self) -> list[str]:
        ttl = self.settings.session_ttl_seconds
        async with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.expired(ttl)]
            for sid in stale:
                del self._sessions[sid]
                self._locks.pop(sid, None)
        if stale:
            self.logger.info("expired %d idle session(s)", len(stale), extra={"sessions": stale})
        return stale

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.session_sweep_interval_seconds)
                await self.sweep_expired()
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise

    # -- persistence --------------------------------------------------------
    def _serialise(self) -> dict[str, Any]:
        return {
            "version": PERSIST_VERSION,
            "saved_at": time.time(),
            "app": "nour-code",
            "sessions": [s.to_dict() for s in self._sessions.values()],
        }

    async def persist(self) -> int:
        """Atomically write all sessions to disk. Returns the number saved."""
        path = self.settings.sessions_file
        async with self._lock:
            payload = json.dumps(self._serialise(), ensure_ascii=False, default=str, indent=2)
            count = len(self._sessions)
        try:
            await asyncio.to_thread(self._write_sync, path, payload)
        except OSError as exc:
            self.logger.error("could not persist sessions to %s: %s", path, exc)
            return 0
        self.logger.info("persisted %d session(s) to %s", count, path)
        return count

    @staticmethod
    def _write_sync(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    async def load(self) -> int:
        """Reload sessions from disk, dropping expired or corrupt records."""
        path = self.settings.sessions_file
        if not path.is_file():
            return 0
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.logger.warning("ignoring unreadable session file %s: %s", path, exc)
            return 0
        if not isinstance(data, dict) or int(data.get("version", 0)) != PERSIST_VERSION:
            self.logger.warning("ignoring session file %s: unsupported format", path)
            return 0

        ttl = self.settings.session_ttl_seconds
        loaded = 0
        async with self._lock:
            for record in data.get("sessions") or []:
                session = Session.from_dict(record) if isinstance(record, dict) else None
                if session is None:
                    continue
                if session.expired(ttl):
                    self.logger.debug("dropping expired session %s on load", session.session_id)
                    continue
                self._sessions[session.session_id] = session
                loaded += 1
        if loaded:
            self.logger.info("restored %d session(s) from %s", loaded, path)
        return loaded
