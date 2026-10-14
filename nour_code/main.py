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
from .config import APP_NAME, Settings, load_settings
from .models import (
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

        agent = Agent(
            settings,
            client=client,
            registry=registry,
            store=store,
            permissions=permissions,
            skills=skills,
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
            return await container.agent.run_sync(
                session,
                payload.message,
                max_iterations=payload.max_iterations,
                production_mode=payload.production_ready,
                audit=container.audit,
            )

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
def _console_html() -> str:
    """Tiny single-page client used to poke at the server from a browser."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>nour-code console</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --bad:#f85149; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; letter-spacing:.5px; }
  .badge { padding:2px 8px; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:12px; }
  .badge.ok { color:var(--ok); border-color:var(--ok); }
  .badge.bad { color:var(--bad); border-color:var(--bad); }
  main { display:grid; grid-template-columns: 1fr 320px; gap:12px; padding:12px 16px; height:calc(100vh - 53px); }
  @media (max-width: 900px) { main { grid-template-columns:1fr; height:auto; } }
  #log { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; overflow:auto; white-space:pre-wrap; }
  #log .role { color:var(--accent); }
  #log .tool { color:#d29922; }
  #log .err { color:var(--bad); }
  #log .meta { color:var(--muted); font-size:12px; }
  aside { display:flex; flex-direction:column; gap:8px; }
  textarea, input, select { width:100%; background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }
  textarea { min-height:120px; resize:vertical; }
  button { background:var(--accent); color:#05121f; border:0; border-radius:6px; padding:8px 12px; font-weight:700; cursor:pointer; }
  button[disabled] { opacity:.5; cursor:not-allowed; }
  .row { display:flex; gap:8px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; }
  .panel h2 { font-size:12px; text-transform:uppercase; color:var(--muted); margin:0 0 6px; letter-spacing:.08em; }
  ul { margin:6px 0 0; padding-left:18px; }
</style>
</head>
<body>
<header>
  <h1>nour-code</h1>
  <span id="health" class="badge">checking…</span>
  <span id="session" class="badge">session: –</span>
  <span id="skills" class="badge">skills: –</span>
  <span style="flex:1"></span>
  <button id="clear" style="background:transparent;color:var(--muted);border:1px solid var(--line)">new session</button>
</header>
<main>
  <section id="log">Send a message to start.\\n</section>
  <aside>
    <div class="panel">
      <h2>message</h2>
      <textarea id="message" placeholder="e.g. review this folder for security issues, then make it production ready"></textarea>
      <div class="row" style="margin-top:8px">
        <button id="send" style="flex:1">stream (SSE)</button>
        <button id="sync" style="flex:1;background:transparent;color:var(--accent);border:1px solid var(--accent)">sync</button>
      </div>
      <label class="meta" style="display:block;margin-top:8px">
        <input type="checkbox" id="prod" /> force production-ready mode
      </label>
    </div>
    <div class="panel">
      <h2>loaded skills</h2>
      <ul id="skillList"><li class="meta">loading…</li></ul>
    </div>
    <div class="panel">
      <h2>tools</h2>
      <ul id="toolList"><li class="meta">–</li></ul>
    </div>
  </aside>
</main>
<script>
const log = document.getElementById('log');
let sessionId = localStorage.getItem('nour-code-session') || '';

function append(node) { log.appendChild(node); log.scrollTop = log.scrollHeight; }
function line(text, cls) {
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  append(div);
  return div;
}

async function refreshStatus() {
  try {
    const [health, skills] = await Promise.all([fetch('/health'), fetch('/skills')]);
    const h = await health.json(), s = await skills.json();
    const badge = document.getElementById('health');
    badge.textContent = `ollama ${h.ollama_reachable ? 'up' : 'down'} · ${h.model}${h.model_loaded ? '' : ' (not pulled)'}`;
    badge.className = 'badge ' + (h.ollama_reachable && h.model_loaded ? 'ok' : 'bad');
    document.getElementById('skills').textContent = `skills: ${s.count}`;
    const list = document.getElementById('skillList');
    list.innerHTML = '';
    (s.skills || []).forEach(sk => {
      const li = document.createElement('li');
      li.innerHTML = `<b>${sk.name}</b><div class="meta">${sk.description}</div><div class="meta">tools: ${(sk.tools||[]).join(', ')}</div>`;
      list.appendChild(li);
    });
    if (!s.count) list.innerHTML = '<li class="meta">none loaded</li>';
  } catch (err) {
    document.getElementById('health').textContent = 'offline';
    document.getElementById('health').className = 'badge bad';
  }
}

async function send(stream) {
  const message = document.getElementById('message').value.trim();
  if (!message) return;
  const button = stream ? document.getElementById('send') : document.getElementById('sync');
  button.disabled = true;
  line('\\n▌ you: ' + message, 'role');
  const target = line('', 'meta');
  try {
    const response = await fetch(stream ? '/chat' : '/chat/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        session_id: sessionId || null,
        production_ready: document.getElementById('prod').checked ? true : null,
      }),
    });
    if (!response.ok) {
      const detail = await response.text();
      line('error ' + response.status + ': ' + detail, 'err');
      return;
    }
    if (!stream) {
      const data = await response.json();
      sessionId = data.session_id;
      localStorage.setItem('nour-code-session', sessionId);
      document.getElementById('session').textContent = 'session: ' + sessionId;
      line(data.content || '(empty response)');
      (data.tool_calls || []).forEach(call => line(`· ${call.tool} → ${call.status} (${call.duration_ms} ms)`, 'tool'));
      (data.warnings || []).forEach(w => line('⚠ ' + w, 'err'));
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let answer = line('');
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\\n\\n');
      buffer = frames.pop() || '';
      for (const frame of frames) {
        if (frame.startsWith(':')) continue;
        const typeMatch = /^event: (.+)$/m.exec(frame);
        const dataMatch = /^data: (.+)$/m.exec(frame);
        if (!typeMatch || !dataMatch) continue;
        const type = typeMatch[1].trim();
        const data = JSON.parse(dataMatch[1]);
        if (type === 'token') answer.textContent += data.text;
        else if (type === 'tool_call') line(`→ ${data.tool}(${JSON.stringify(data.arguments).slice(0, 220)})`, 'tool');
        else if (type === 'tool_result') line(`← ${data.tool} [${data.status}] ${(data.result || data.error || '').slice(0, 400)}`, data.status === 'ok' ? 'meta' : 'err');
        else if (type === 'skill_loaded') line(`✦ skill loaded: ${data.name} (${(data.tools||[]).join(', ')})`, 'meta');
        else if (type === 'error') line('✖ ' + data.message, 'err');
        else if (type === 'done') {
          const r = data.response;
          sessionId = r.session_id;
          localStorage.setItem('nour-code-session', sessionId);
          document.getElementById('session').textContent = 'session: ' + sessionId;
          line('', 'meta').textContent = `— ${r.iterations} iteration(s), ${r.tool_calls.length} tool call(s), ${Math.round(r.duration_ms)} ms`;
          (r.warnings || []).forEach(w => line('⚠ ' + w, 'err'));
          (r.production_checklist || []).forEach(row => line(`${row.symbol} ${row.item} — ${row.detail}`));
        }
      }
    }
  } catch (err) {
    line('request failed: ' + err, 'err');
  } finally {
    button.disabled = false;
    target.remove();
  }
}

document.getElementById('send').onclick = () => send(true);
document.getElementById('sync').onclick = () => send(false);
document.getElementById('clear').onclick = async () => {
  if (sessionId) { await fetch('/sessions/' + sessionId, { method: 'DELETE' }).catch(() => {}); }
  sessionId = '';
  localStorage.removeItem('nour-code-session');
  document.getElementById('session').textContent = 'session: –';
  log.textContent = 'New session.\\n';
};
document.getElementById('session').textContent = sessionId ? 'session: ' + sessionId : 'session: new';
refreshStatus();
</script>
</body>
</html>
"""


#: Default ASGI application, so ``uvicorn nour_code.main:app`` works out of the box.
#: Tests and embeddings should call :func:`create_app` with explicit settings instead.
app = create_app()
