"""Human-in-the-loop approvals: proposals, decisions and the HTTP endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from nour_code.approvals import ApprovalGate, ApprovalRegistry, is_mutating, risk_of
from nour_code.models import StreamEvent


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", ["write_file", "apply_patch", "delete_file", "run_shell"])
def test_builtin_mutating_tools(tool: str) -> None:
    assert is_mutating(tool) is True


@pytest.mark.parametrize("tool", ["read_file", "list_directory", "search_files"])
def test_builtin_read_only_tools(tool: str) -> None:
    assert is_mutating(tool) is False


def test_skill_tools_default_to_mutating() -> None:
    assert is_mutating("add_logging", skill="production_skill") is True
    assert is_mutating("security_scan", skill="security_skill") is False
    assert is_mutating("check_secrets", skill="security_skill") is False


def test_risk_labels() -> None:
    assert risk_of("run_shell") == "high"
    assert risk_of("delete_file") == "medium"
    assert risk_of("write_file") == "low"


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
async def test_gate_parks_until_approved() -> None:
    emitted: list[StreamEvent] = []

    async def emit(event: StreamEvent) -> None:
        emitted.append(event)

    gate = ApprovalGate(session_id="sess_1", timeout_seconds=5, emit=emit)
    task = asyncio.create_task(gate.request(tool="write_file", arguments={"path": "a.py"}, iteration=1))

    await asyncio.sleep(0)  # let the proposal be emitted
    assert len(emitted) == 1
    assert emitted[0].type == "tool_proposal"
    assert gate.pending and gate.pending[0]["tool"] == "write_file"
    assert not task.done()

    proposal_id = gate.pending[0]["id"]
    assert gate.resolve(proposal_id, "approved") is True
    assert await task == "approved"
    assert gate.pending == []
    assert gate.history[0]["decision"] == "approved"


async def test_gate_reports_rejection() -> None:
    gate = ApprovalGate(session_id="sess_2", timeout_seconds=5)
    task = asyncio.create_task(gate.request(tool="run_shell", arguments={"command": "rm -rf /"}))
    await asyncio.sleep(0)
    gate.resolve_all("rejected")
    assert await task == "rejected"


async def test_gate_times_out() -> None:
    gate = ApprovalGate(session_id="sess_3", timeout_seconds=0.05)
    decision = await gate.request(tool="delete_file", arguments={"path": "a.py"})
    assert decision == "timeout"


async def test_gate_auto_approve_skips_proposals() -> None:
    emitted: list[StreamEvent] = []

    async def emit(event: StreamEvent) -> None:
        emitted.append(event)

    gate = ApprovalGate(session_id="sess_4", timeout_seconds=5, emit=emit)
    gate.auto_approve = True
    assert await gate.request(tool="write_file", arguments={"path": "a.py"}) == "approved"
    assert emitted == []


async def test_resolving_an_unknown_proposal_is_a_noop() -> None:
    gate = ApprovalGate(session_id="sess_5", timeout_seconds=5)
    assert gate.resolve("nope", "approved") is False


async def test_cancel_releases_every_waiter() -> None:
    gate = ApprovalGate(session_id="sess_6", timeout_seconds=5)
    tasks = [asyncio.create_task(gate.request(tool="write_file", arguments={})) for _ in range(3)]
    await asyncio.sleep(0)
    assert gate.cancel() == 3
    assert await asyncio.gather(*tasks) == ["cancelled"] * 3


def test_registry_lifecycle() -> None:
    registry = ApprovalRegistry()
    gate = registry.create("sess_7")
    assert registry.active == ["sess_7"]
    assert registry.get("sess_7") is gate
    registry.discard("sess_7")
    assert registry.get("sess_7") is None
    assert registry.active == []


# ---------------------------------------------------------------------------
# through the agent + API
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def streaming_client(settings):
    """Async HTTP client sharing one event loop with the app (approvals need that)."""
    from httpx import ASGITransport, AsyncClient
    from nour_code.main import create_app
    from tests.conftest import FakeModel

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        fake = FakeModel()  # each test scripts its own responses
        app.state.container.client = fake
        app.state.container.agent.client = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, app.state.container, fake


async def _settle(container, decision: str, *, attempts: int = 300) -> int:
    """Resolve every pending proposal as soon as one appears."""
    for _ in range(attempts):
        await asyncio.sleep(0.01)
        for session_id in container.approvals.active:
            gate = container.approvals.get(session_id)
            if gate and gate.pending:
                gate.resolve_all(decision)
                return len(gate.history)
    return 0


async def test_agent_waits_for_approval_before_writing(streaming_client, sandbox: Path) -> None:
    client, container, fake = streaming_client
    fake.add_tool_call("write_file", path="needs_ok.txt", content="hello")
    fake.add_text("written")

    task = asyncio.create_task(_settle(container, "approved"))
    response = await client.post("/chat", json={"message": "write a file", "approval": "mutating"})
    await task

    assert response.status_code == 200
    assert (sandbox / "needs_ok.txt").read_text() == "hello"


async def test_rejected_proposal_never_writes(streaming_client, sandbox: Path) -> None:
    client, container, fake = streaming_client
    fake.add_tool_call("write_file", path="denied.txt", content="nope")
    fake.add_text("ok")

    task = asyncio.create_task(_settle(container, "rejected"))
    response = await client.post("/chat", json={"message": "write a file", "approval": "mutating"})
    await task

    assert response.status_code == 200
    assert not (sandbox / "denied.txt").exists()
    assert "rejected" in response.text


async def test_proposals_are_announced_over_sse(streaming_client) -> None:
    client, container, fake = streaming_client
    fake.add_tool_call("delete_file", path="gone.txt")
    fake.add_text("done")

    task = asyncio.create_task(_settle(container, "rejected"))
    response = await client.post("/chat", json={"message": "delete it", "approval": "all"})
    await task

    kinds = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: tool_proposal" in kinds


async def test_read_only_tools_need_no_approval(streaming_client, sandbox: Path) -> None:
    client, container, fake = streaming_client
    (sandbox / "readable.txt").write_text("data")
    fake.add_tool_call("read_file", path="readable.txt")
    fake.add_text("read")

    response = await client.post("/chat", json={"message": "read it", "approval": "mutating"})
    assert response.status_code == 200
    assert "tool_proposal" not in response.text


def test_approve_endpoint_404_without_a_running_turn(client) -> None:
    test_client, _, _ = client
    response = test_client.post("/sessions/sess_nope/approve", json={"decision": "approve_all"})
    assert response.status_code == 404


async def test_approvals_endpoint_lists_pending(client) -> None:
    test_client, container, _ = client
    gate = container.approvals.create("sess_pending")
    task = asyncio.create_task(gate.request(tool="write_file", arguments={"path": "x.py"}, iteration=1))
    await asyncio.sleep(0)  # let the proposal park itself
    try:
        response = test_client.get("/sessions/sess_pending/approvals")
        assert response.status_code == 200
        body = response.json()
        assert body["pending"][0]["tool"] == "write_file"
        assert body["pending"][0]["risk"] == "low"

        approved = test_client.post(
            "/sessions/sess_pending/approve",
            json={"decision": "approve", "proposal_id": body["pending"][0]["id"]},
        )
        assert approved.json()["resolved"] == 1
    finally:
        task.cancel()
        container.approvals.discard("sess_pending")


async def test_approve_all_sets_the_auto_flag(client) -> None:
    test_client, container, _ = client
    gate = container.approvals.create("sess_all")
    task = asyncio.create_task(gate.request(tool="run_shell", arguments={"command": "pytest"}))
    await asyncio.sleep(0)
    try:
        response = test_client.post("/sessions/sess_all/approve", json={"decision": "approve_all"})
        assert response.status_code == 200
        assert gate.auto_approve is True
    finally:
        task.cancel()
        container.approvals.discard("sess_all")
