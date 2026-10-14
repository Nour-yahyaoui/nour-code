"""FastAPI application: routes, dependency injection and graceful shutdown.

There is no global mutable state.  :func:`build_container` constructs every
component once during start-up, stores it on ``app.state``, and handlers receive
it through FastAPI dependencies (``Annotated[Container, Depends(get_container)]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError as PydanticValidationError

from . import __version__
from .agent import Agent
from .approvals import ApprovalRegistry
from .config import APP_NAME, Settings, load_settings
from .models import (
    ApprovalRequest,
    ApprovalResponse,
    AuditResponse,
    ChatRequest,
    ChatResponse,
    DeleteSessionResponse,
    ErrorResponse,
    HealthResponse,
    ReloadSkillsResponse,
    SessionListResponse,
    SkillListResponse,
    StreamEvent,
)
from .observability import AuditLog, Metrics, setup_logging
from .ollama_client import OllamaClient, OllamaError, OllamaUnavailableError
from .permissions import PermissionDeniedError, PermissionManager
from .sessions import Session, SessionStore
from .skills import SkillsManager
from .tools import ToolRegistry
from .tools import filesystem as fs_tools
from .tools import patch as patch_tools
from .tools import shell as shell_tools

logger = logging.getLogger("nour_code.server")

KEEPALIVE_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------
@dataclass
class Container:
    """Everything the request handlers need, wired once at start-up."""

    settings: Settings
    logger: logging.Logger
    permissions: PermissionManager
    audit: AuditLog
    metrics: Metrics
    registry: ToolRegistry
    skills: SkillsManager
    client: OllamaClient
    store: SessionStore
    agent: Agent
    approvals: ApprovalRegistry
    started_at: float

    @classmethod
    async def create(cls, settings: Settings) -> Container:
        log = setup_logging(settings)
        log.info("starting %s %s", APP_NAME, __version__, extra=settings.describe())

        permissions = PermissionManager.load(
            settings.permissions_file,
            working_dir=settings.working_dir,
            root_overrides=settings.allowed_root_overrides,
        )
        for warning in permissions.warnings:
            log.warning("permissions: %s", warning)

        audit = AuditLog(settings.audit_file, enabled=settings.audit_enabled)
        metrics = Metrics(settings.metrics_enabled)

        registry = ToolRegistry(
            settings=settings,
            permissions=permissions,
            audit=audit,
            metrics=metrics,
            logger=log.getChild("tools"),
        )
        registry.register_many(
            [
                *fs_tools.build_specs(),
                *shell_tools.build_specs(),
                *patch_tools.build_specs(),
            ]
        )

        skills = SkillsManager(settings, registry, logger=log.getChild("skills"))
        report = await skills.load_all()
        log.info(
            "skills loaded",
            extra={"loaded": report.loaded, "errors": [e.error for e in report.errors]},
        )

        client = OllamaClient(settings, metrics=metrics)
        store = SessionStore(settings, logger=log.getChild("sessions"))
        await store.start()

        approvals = ApprovalRegistry(timeout_seconds=settings.approval_timeout_seconds)
        agent = Agent(
            settings,
            client=client,
            registry=registry,
            store=store,
            permissions=permissions,
            skills=skills,
            approvals=approvals,
            logger=log.getChild("agent"),
        )
        return cls(
            settings=settings,
            logger=log,
            permissions=permissions,
            audit=audit,
            metrics=metrics,
            registry=registry,
            skills=skills,
            client=client,
            store=store,
            agent=agent,
            approvals=approvals,
            started_at=time.time(),
        )

    async def aclose(self) -> None:
        """Graceful shutdown: stop the sweeper, persist sessions, close sockets."""
        self.logger.info("shutting down")
        try:
            await self.store.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must not fail
            self.logger.error("session shutdown failed: %s", exc)
        try:
            self.approvals.cancel_all()
        except Exception as exc:  # noqa: BLE001 - shutdown must not fail
            self.logger.error("approval shutdown failed: %s", exc)
        try:
            await self.registry.drain()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("audit drain failed: %s", exc)
        try:
            await self.client.aclose()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("ollama client close failed: %s", exc)
        self.logger.info("shutdown complete")

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    async def health(self) -> HealthResponse:
        """Probe Ollama and report the server's own state."""
        probe = await self.client.health()
        sessions = await self.store.list()
        status_value: str = "ok"
        if not probe["reachable"]:
            status_value = "down"
        elif not probe["model_loaded"]:
            status_value = "degraded"
        return HealthResponse(
            status=status_value,  # type: ignore[arg-type]
            app=APP_NAME,
            version=__version__,
            uptime_seconds=round(self.uptime_seconds, 2),
            ollama_reachable=bool(probe["reachable"]),
            ollama_url=str(probe["url"]),
            ollama_version=probe["version"],
            model=self.settings.model,
            model_loaded=bool(probe["model_loaded"]),
            available_models=list(probe["models"])[:50],
            skills_loaded=len(self.skills.names()),
            active_sessions=sessions.count,
            tools=len(self.registry.names()),
            detail=probe["detail"],
        )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def build_container_for_app(settings: Settings | None = None) -> Settings:
    """Attach settings to the app state before the lifespan runs."""
    return settings or load_settings()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application (factory, so tests can build isolated apps)."""
    resolved = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = await Container.create(app.state.settings)
        try:
            yield
        finally:
            container: Container = app.state.container
            await container.aclose()

    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        description=(
            "Local AI coding agent: a Qwen3 8B model on Ollama with controlled access to the "
            "filesystem, the shell and loadable Python skills."
        ),
        lifespan=lifespan,
        root_path=resolved.root_path,
    )
    app.state.settings = resolved

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app)
    _register_routes(app)
    return app


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="server is still starting up")
    return container


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


ContainerDep = Annotated[Container, Depends(get_container)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionDeniedError)
    async def _permission_handler(request: Request, exc: PermissionDeniedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=ErrorResponse(error=str(exc), code=exc.code, detail=exc.detail or None).model_dump(),
        )

    @app.exception_handler(OllamaUnavailableError)
    async def _ollama_unavailable(request: Request, exc: OllamaUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ErrorResponse(error=str(exc), code="ollama_unavailable").model_dump(),
        )

    @app.exception_handler(OllamaError)
    async def _ollama_error(request: Request, exc: OllamaError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=ErrorResponse(error=str(exc), code="ollama_error").model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(
                error="request validation failed",
                code="invalid_request",
                detail="; ".join(
                    f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}"
                    for e in exc.errors()[:5]
                ),
            ).model_dump(),
        )

    @app.exception_handler(PydanticValidationError)
    async def _pydantic_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(error="response validation failed", code="invalid_response").model_dump(),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                error=f"{exc.__class__.__name__}: {exc}", code="internal_error"
            ).model_dump(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _resolve_session(container: Container, payload: ChatRequest) -> Session:
    working_dir: Path | None = None
    if payload.working_dir:
        try:
            working_dir = container.permissions.resolve_path(payload.working_dir, must_exist=True, kind="dir")
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=f"working_dir rejected: {exc}") from exc
    return await container.store.get_or_create(
        payload.session_id,
        working_dir=working_dir,
        metadata=dict(payload.metadata),
    )


async def _sse_stream(container: Container, session: Session, payload: ChatRequest) -> AsyncIterator[str]:
    """Run the agent and yield SSE frames, with keep-alives during long tool calls."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _produce() -> None:
        try:
            async for event in container.agent.run_stream(
                session,
                payload.message,
                max_iterations=payload.max_iterations,
                production_mode=payload.production_ready,
                audit=container.audit,
                approval_mode=payload.approval,
            ):
                await queue.put(event.to_sse())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as an SSE error frame
            logger.exception("streaming failed")
            await queue.put(
                StreamEvent(type="error", data={"message": f"{exc.__class__.__name__}: {exc}", "fatal": True}).to_sse()
            )
        finally:
            # Audit entries are written off the hot path; flush them so a client
            # that reads /audit right after the turn sees every call.
            with contextlib.suppress(Exception):
                await container.registry.drain_audit()
            await queue.put(None)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if frame is None:
                break
            yield frame
    finally:
        if not producer.done():
            producer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await producer


def _register_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_console_html())

    @app.post("/chat", summary="Stream an agent turn as SSE")
    async def chat(payload: ChatRequest, container: ContainerDep) -> StreamingResponse:
        session = await _resolve_session(container, payload)
        # The session lock is held for the whole stream (released on disconnect).
        generator = _with_lock(_sse_stream(container, session, payload), container.store.lock(session.session_id))
        return StreamingResponse(
            generator,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Session-Id": session.session_id,
                },
            )

    @app.post("/chat/sync", response_model=ChatResponse, summary="Run an agent turn and return the full response")
    async def chat_sync(payload: ChatRequest, container: ContainerDep) -> ChatResponse:
        session = await _resolve_session(container, payload)
        async with container.store.lock(session.session_id):
            try:
                return await container.agent.run_sync(
                    session,
                    payload.message,
                    max_iterations=payload.max_iterations,
                    production_mode=payload.production_ready,
                    audit=container.audit,
                    approval_mode="off",  # a blocking request cannot wait for a human
                )
            finally:
                with contextlib.suppress(Exception):
                    await container.registry.drain_audit()

    @app.get("/skills", response_model=SkillListResponse, summary="List loaded skills")
    async def list_skills(container: ContainerDep) -> SkillListResponse:
        return container.skills.info()

    @app.post("/skills/reload", response_model=ReloadSkillsResponse, summary="Hot reload skills")
    async def reload_skills(container: ContainerDep) -> ReloadSkillsResponse:
        report = await container.skills.reload()
        if report.errors:
            container.logger.warning("skill reload errors", extra={"errors": [e.error for e in report.errors]})
        return report

    @app.get("/sessions", response_model=SessionListResponse, summary="List active sessions")
    async def list_sessions(container: ContainerDep) -> SessionListResponse:
        return await container.store.list()

    @app.delete("/sessions/{session_id}", response_model=DeleteSessionResponse, summary="Delete a session")
    async def delete_session(session_id: str, container: ContainerDep) -> DeleteSessionResponse:
        deleted = await container.store.delete(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"session {session_id!r} not found")
        return DeleteSessionResponse(ok=True, session_id=session_id, deleted=True)

    @app.get(
        "/sessions/{session_id}/approvals",
        response_model=ApprovalResponse,
        summary="List the tool calls waiting for a human decision",
    )
    async def list_approvals(session_id: str, container: ContainerDep) -> ApprovalResponse:
        gate = container.approvals.get(session_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"no turn is waiting for approvals in {session_id!r}")
        return ApprovalResponse(
            ok=True,
            session_id=session_id,
            pending=gate.pending,
            detail=f"{len(gate.pending)} proposal(s) pending",
        )

    @app.post(
        "/sessions/{session_id}/approve",
        response_model=ApprovalResponse,
        summary="Approve or reject a proposed tool call",
    )
    async def approve(session_id: str, payload: ApprovalRequest, container: ContainerDep) -> ApprovalResponse:
        gate = container.approvals.get(session_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"no turn is waiting for approvals in {session_id!r}")

        decision = payload.decision
        if decision in {"approve_all", "reject_all"}:
            gate.auto_approve = decision == "approve_all"
            resolved = gate.resolve_all("approved" if decision == "approve_all" else "rejected")
            return ApprovalResponse(
                ok=True,
                session_id=session_id,
                decision=decision,
                resolved=resolved,
                pending=gate.pending,
                detail=f"{decision}: resolved {resolved} proposal(s)",
            )

        if not payload.proposal_id:
            raise HTTPException(status_code=400, detail="proposal_id is required for a single decision")
        resolved_ok = gate.resolve(payload.proposal_id, "approved" if decision == "approve" else "rejected")
        if not resolved_ok:
            raise HTTPException(status_code=404, detail=f"proposal {payload.proposal_id!r} is not pending")
        return ApprovalResponse(
            ok=True,
            session_id=session_id,
            proposal_id=payload.proposal_id,
            decision=decision,
            resolved=1,
            pending=gate.pending,
            detail=f"{payload.proposal_id} {decision}d",
        )

    @app.get("/health", response_model=HealthResponse, summary="Ollama + model health")
    async def health(container: ContainerDep) -> HealthResponse:
        return await container.health()

    @app.get("/audit", response_model=AuditResponse, summary="Tail the audit log")
    async def audit(container: ContainerDep, limit: int = 100) -> AuditResponse:
        entries = await container.audit.tail(max(1, min(limit, 2_000)))
        return AuditResponse(count=len(entries), path=str(container.settings.audit_file), entries=entries)

    @app.get("/metrics", include_in_schema=False, summary="Prometheus metrics")
    async def metrics(container: ContainerDep) -> Response:
        try:
            body, content_type = container.metrics.render()
        except RuntimeError:
            return Response(
                content="metrics disabled (install prometheus-client and set NOUR_CODE_METRICS_ENABLED=true)",
                status_code=503,
                media_type="text/plain",
            )
        return Response(content=body, media_type=content_type)


async def _with_lock(generator: AsyncIterator[str], lock: asyncio.Lock) -> AsyncIterator[str]:
    """Keep a session lock held until the stream is consumed or cancelled."""
    async with lock:
        async for frame in generator:
            yield frame


# ---------------------------------------------------------------------------
# Minimal browser console (convenience only — the API is the real interface)
# ---------------------------------------------------------------------------
CONSOLE_ASSET = Path(__file__).resolve().parent / "static" / "console.html"


def _console_html() -> str:
    """Load the single-page client shipped next to the package.

    ``console.html`` is a static asset (see ``nour_code/static/``) so the markup,
    styles and client script can be edited without touching Python strings.
    """
    try:
        return CONSOLE_ASSET.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - only if the package is incomplete
        logging.getLogger("nour_code").error("cannot read %s: %s", CONSOLE_ASSET, exc)
        return (
            "<!doctype html><title>nour-code</title>"
            "<p style='font-family:monospace'>Console asset missing "
            f"({CONSOLE_ASSET}). The JSON API under /chat, /chat/sync, /health "
            "and /skills is unaffected.</p>"
        )




#: Default ASGI application, so ``uvicorn nour_code.main:app`` works out of the box.
#: Tests and embeddings should call :func:`create_app` with explicit settings instead.
app = create_app()
