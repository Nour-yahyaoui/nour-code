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
    assert "nour-code" in response.text


def test_openapi_schema_is_available(client) -> None:
    test_client, _, _ = client
    schema = test_client.get("/openapi.json").json()
    assert schema["info"]["title"] == "nour-code"
    for path in ("/chat", "/chat/sync", "/skills", "/skills/reload", "/sessions", "/health", "/audit"):
        assert path in schema["paths"], f"{path} missing from OpenAPI"


def test_unhandled_errors_are_json(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, container, _ = client

    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(container.agent, "run_sync", boom)
    response = test_client.post("/chat/sync", json={"message": "hi"})
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
