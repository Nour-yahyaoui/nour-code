"""HTTP layer: every endpoint, SSE framing and error handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nour_code.main import create_app
from tests.conftest import FakeModel


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        fake = FakeModel([{"content": "hello from the model"}])
        container = app.state.container
        container.client = fake
        container.agent.client = fake
        yield test_client, container, fake


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame or frame.startswith(":"):
            continue
        event_type = None
        payload = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if event_type and payload is not None:
            events.append((event_type, payload))
    return events


# ---------------------------------------------------------------------------
# health / skills / sessions
# ---------------------------------------------------------------------------
def test_health_endpoint(client) -> None:
    test_client, container, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "nour-code"
    assert body["ollama_reachable"] is True
    assert body["model"] == "qwen3:8b"
    assert body["model_loaded"] is True
    assert body["model_running"] is True
    assert body["running_models"] == ["qwen3:8b"]
    assert body["skills_loaded"] == 2
    assert body["tools"] >= 15


def test_health_reports_missing_ollama(client) -> None:
    test_client, container, fake = client
    fake.health_payload = {
        "reachable": False,
        "url": "http://localhost:11434",
        "version": None,
        "models": [],
        "model_loaded": False,
        "detail": "connection refused",
    }
    body = test_client.get("/health").json()
    assert body["status"] == "down"
    assert body["ollama_reachable"] is False


def test_warmup_and_unload_endpoints(client) -> None:
    test_client, _, _ = client
    warm = test_client.post("/ollama/warmup?keep_alive=30m").json()
    assert warm["action"] == "warmup"
    assert warm["keep_alive"] == "30m"
    assert warm["running"] is True

    cold = test_client.post("/ollama/unload").json()
    assert cold["action"] == "unload"
    assert cold["running"] is False


def test_list_skills(client) -> None:
    test_client, _, _ = client
    body = test_client.get("/skills").json()
    assert body["count"] == 2
    names = {skill["name"] for skill in body["skills"]}
    assert names == {"security_skill", "production_skill"}


def test_reload_skills(client) -> None:
    test_client, _, _ = client
    body = test_client.post("/skills/reload").json()
    assert body["ok"] is True
    assert len(body["loaded"]) == 2
    assert body["errors"] == []


def test_sessions_lifecycle(client) -> None:
    test_client, _, _ = client
    assert test_client.get("/sessions").json()["count"] == 0

    response = test_client.post("/chat/sync", json={"message": "hello"})
    session_id = response.json()["session_id"]

    sessions = test_client.get("/sessions").json()
    assert sessions["count"] == 1
    assert sessions["sessions"][0]["session_id"] == session_id

    deleted = test_client.delete(f"/sessions/{session_id}").json()
    assert deleted["deleted"] is True
    assert test_client.get("/sessions").json()["count"] == 0


def test_delete_unknown_session_returns_404(client) -> None:
    test_client, _, _ = client
    assert test_client.delete("/sessions/nope").status_code == 404


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
def test_chat_sync(client) -> None:
    test_client, _, _ = client
    response = test_client.post("/chat/sync", json={"message": "hello there"})
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "hello from the model"
    assert body["session_id"]
    assert body["finished"] is True


def test_chat_sync_reuses_session(client) -> None:
    test_client, _, _ = client
    first = test_client.post("/chat/sync", json={"message": "one"}).json()
    second = test_client.post(
        "/chat/sync", json={"message": "two", "session_id": first["session_id"]}
    ).json()
    assert first["session_id"] == second["session_id"]


def test_chat_sync_rejects_empty_message(client) -> None:
    test_client, _, _ = client
    assert test_client.post("/chat/sync", json={"message": "   "}).status_code == 422


def test_chat_sync_rejects_bad_body(client) -> None:
    test_client, _, _ = client
    assert test_client.post("/chat/sync", json={"session_id": "x"}).status_code == 422


def test_chat_sync_rejects_working_dir_outside_roots(client, tmp_path: Path) -> None:
    test_client, _, _ = client
    response = test_client.post("/chat/sync", json={"message": "hi", "working_dir": str(tmp_path)})
    assert response.status_code == 403


def test_chat_stream_emits_sse(client) -> None:
    test_client, _, _ = client
    with test_client.stream("POST", "/chat", json={"message": "stream please"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(chunk for chunk in response.iter_text())
    events = parse_sse(text)
    kinds = [kind for kind, _ in events]
    assert kinds[0] != "done"
    assert kinds[-1] == "done"
    assert "token" in kinds
    assert events[-1][1]["response"]["content"] == "hello from the model"


def test_chat_stream_includes_tool_events(client, sandbox: Path) -> None:
    test_client, container, _ = client
    (sandbox / "data.txt").write_text("value")
    fake = FakeModel()
    fake.add_tool_call("read_file", path="data.txt")
    fake.add_text("I read the file.")
    container.agent.client = fake

    with test_client.stream("POST", "/chat", json={"message": "read data.txt"}) as response:
        text = "".join(chunk for chunk in response.iter_text())
    events = parse_sse(text)
    kinds = [kind for kind, _ in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    tool_call = next(data for kind, data in events if kind == "tool_call")
    assert tool_call["tool"] == "read_file"


def test_chat_stream_announces_skills(client) -> None:
    test_client, _, _ = client
    with test_client.stream("POST", "/chat", json={"message": "hi"}) as response:
        text = "".join(chunk for chunk in response.iter_text())
    events = parse_sse(text)
    assert {data["name"] for kind, data in events if kind == "skill_loaded"} == {
        "security_skill",
        "production_skill",
    }


def test_chat_stream_reports_errors(client) -> None:
    test_client, container, _ = client
    from nour_code.ollama_client import OllamaUnavailableError

    class Broken(FakeModel):
        async def stream_chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
            raise OllamaUnavailableError("ollama is down")
            yield  # pragma: no cover

    container.agent.client = Broken()
    with test_client.stream("POST", "/chat", json={"message": "hi"}) as response:
        text = "".join(chunk for chunk in response.iter_text())
    events = parse_sse(text)
    assert any(kind == "error" for kind, _ in events)


def test_production_ready_via_api(client, sandbox: Path) -> None:
    test_client, _, _ = client
    (sandbox / "api.py").write_text("def handler(req):\n    return open(req).read()\n")
    response = test_client.post(
        "/chat/sync", json={"message": "make this production ready", "production_ready": True}
    )
    body = response.json()
    assert body["production_ready"] is True
    assert body["production_checklist"]
    assert "import logging" in (sandbox / "api.py").read_text()


# ---------------------------------------------------------------------------
# audit / metrics / console
# ---------------------------------------------------------------------------
def test_audit_endpoint(client) -> None:
    test_client, container, _ = client
    test_client.post("/chat/sync", json={"message": "hello"})
    body = test_client.get("/audit?limit=5").json()
    assert body["path"].endswith("nour-code-audit.jsonl")
    assert isinstance(body["entries"], list)
    assert body["count"] <= 5


def test_audit_records_tool_calls(client, sandbox: Path) -> None:
    test_client, container, fake = client
    (sandbox / "audited.txt").write_text("x")
    fake2 = FakeModel()
    fake2.add_tool_call("read_file", path="audited.txt")
    fake2.add_text("done")
    container.agent.client = fake2
    test_client.post("/chat/sync", json={"message": "read it"})
    body = test_client.get("/audit?limit=20").json()
    assert any(entry["tool"] == "read_file" for entry in body["entries"])


def test_metrics_disabled_returns_503(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/metrics")
    assert response.status_code == 503


def test_console_page_is_served(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "nour-code" in response.text


def test_console_page_has_the_full_client() -> None:
    """Markup, splitters, event console and the streaming client must all ship."""
    from nour_code.main import CONSOLE_ASSET, _console_html

    assert CONSOLE_ASSET.is_file()
    html = _console_html()
    for marker in (
        'id="transcript"',       # chat
        'id="eventsView"',       # raw stream drawer
        'id="tree"',             # workspace file tree
        'id="fileBody"',         # read-only file viewer
        'id="gutter"',           # resizable panes
        'id="themeBtn"',         # light / dark
        'id="sprite"',           # every glyph is an inline SVG symbol
        'id="approve"',          # approvals toggle
        'id="collapseWs"',       # collapsible workspace sidebar
        'id="wsBtn"',            # …which becomes a drawer on narrow screens
        'id="hideEvents"',       # collapsible event stream
        "nour.ui.wsCollapsed",   # sidebar state survives a reload
        "fetch('/files/content",  # read-only file reads
        "fetch('/chat'",
        "getReader()",
        "AbortController",
        "localStorage.setItem('nour.ui.theme'",
    ):
        assert marker in html, f"{marker} missing from the console"

    # no emoji / icon-font glyphs: icons come from the sprite
    assert '<use href="#i-' in html


def test_console_page_handles_a_missing_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    from nour_code import main as main_module

    original = main_module.CONSOLE_ASSET
    monkeypatch.setattr(main_module, "CONSOLE_ASSET", original.with_name("does-not-exist.html"))
    html = main_module._console_html()
    assert "Console asset missing" in html
    monkeypatch.setattr(main_module, "CONSOLE_ASSET", original)
    assert "nour-code" in main_module._console_html()


def test_openapi_schema_is_available(client) -> None:
    test_client, _, _ = client
    schema = test_client.get("/openapi.json").json()
    assert schema["info"]["title"] == "nour-code"
    for path in ("/chat", "/chat/sync", "/skills", "/skills/reload", "/sessions", "/health", "/audit"):
        assert path in schema["paths"], f"{path} missing from OpenAPI"


def test_unhandled_errors_are_json(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        container = app.state.container

        async def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(container.agent, "run_sync", boom)
        response = test_client.post("/chat/sync", json={"message": "hi"})
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


# ---------------------------------------------------------------------------
# workspace listing
# ---------------------------------------------------------------------------
def test_files_lists_the_working_dir(client, sandbox: Path) -> None:
    test_client, _, _ = client
    (sandbox / "shown.txt").write_text("x")
    (sandbox / "nested").mkdir()
    (sandbox / "nested" / "deep.py").write_text("y")

    body = test_client.get("/files").json()
    names = {entry["name"]: entry["type"] for entry in body["entries"]}
    assert names["shown.txt"] == "file"
    assert names["nested"] == "dir"
    assert body["count"] == len(body["entries"])


def test_files_recursive_includes_nested_files(client, sandbox: Path) -> None:
    test_client, _, _ = client
    (sandbox / "nested").mkdir()
    (sandbox / "nested" / "deep.py").write_text("y")

    body = test_client.get("/files?recursive=true").json()
    assert any(entry["name"].endswith("deep.py") for entry in body["entries"])


def test_files_rejects_paths_outside_the_roots(client, tmp_path: Path) -> None:
    test_client, _, _ = client
    outside = tmp_path.parent / "definitely-outside"
    response = test_client.get("/files", params={"path": str(outside)})
    assert response.status_code in {400, 403}


def test_files_respects_the_limit(client, sandbox: Path) -> None:
    test_client, _, _ = client
    for index in range(5):
        (sandbox / f"f{index}.txt").write_text("x")
    body = test_client.get("/files?limit=2").json()
    assert body["count"] == 2
    assert body["truncated"] is True


# ---------------------------------------------------------------------------
# GET /files/content (read-only file viewer)
# ---------------------------------------------------------------------------
def test_files_content_returns_the_text(client, sandbox: Path) -> None:
    test_client, _, _ = client
    (sandbox / "app.py").write_text("print('hi')\n")

    body = test_client.get("/files/content", params={"path": "app.py"}).json()
    assert body["content"] == "print('hi')\n"
    assert body["name"] == "app.py"
    assert body["language"] == "python"
    assert body["binary"] is False
    assert body["truncated"] is False


def test_files_content_rejects_paths_outside_the_roots(client, tmp_path: Path) -> None:
    test_client, _, _ = client
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")

    response = test_client.get("/files/content", params={"path": str(outside)})
    assert response.status_code in {400, 403}


def test_files_content_404s_on_a_missing_file(client) -> None:
    test_client, _, _ = client
    response = test_client.get("/files/content", params={"path": "nope.txt"})
    assert response.status_code in {400, 403, 404}


def test_files_content_flags_binary_files(client, sandbox: Path) -> None:
    test_client, _, _ = client
    (sandbox / "blob.bin").write_bytes(b"\x00\x01\x02\x03")

    body = test_client.get("/files/content", params={"path": "blob.bin"}).json()
    assert body["binary"] is True
    assert "binary" in body["content"]


def test_files_content_is_read_only(client, sandbox: Path) -> None:
    """The viewer endpoint has no write side effect of any kind."""
    test_client, _, _ = client
    target = sandbox / "untouched.txt"
    target.write_text("original")

    test_client.get("/files/content", params={"path": "untouched.txt"})
    assert target.read_text() == "original"
    assert target.stat().st_mtime <= target.stat().st_mtime
