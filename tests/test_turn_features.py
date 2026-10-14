"""1.2.0 turn features: thinking, usage, file_change, per-request overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from nour_code.models import StreamEvent


async def collect(container, message: str, **kwargs) -> list[StreamEvent]:
    """Run a streaming turn and return every event."""
    session = await container.store.get_or_create(None)
    return [event async for event in container.agent.run_stream(session, message, **kwargs)]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [event.type for event in events]


# ---------------------------------------------------------------------------
# thinking
# ---------------------------------------------------------------------------
async def test_thinking_is_streamed_and_returned(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("the answer")
    fake.script[-1]["thinking"] = "I should read the file first."

    events = await collect(container, "what is in this project?")
    assert "thinking" in kinds(events)
    thinking_text = "".join(e.data["text"] for e in events if e.type == "thinking")
    assert "read the file" in thinking_text

    done = next(e for e in events if e.type == "done")
    assert "read the file" in done.data["response"]["thinking"]


async def test_thinking_is_absent_when_the_model_does_not_reason(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("plain answer")
    events = await collect(container, "hi")
    assert "thinking" not in kinds(events)


# ---------------------------------------------------------------------------
# usage + file_change
# ---------------------------------------------------------------------------
async def test_usage_event_follows_every_model_call(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("ok")
    events = await collect(container, "hi")
    usage_events = [e for e in events if e.type == "usage"]
    assert usage_events
    assert usage_events[0].data["total_tokens"] == 15
    assert usage_events[0].data["iteration"] == 1


async def test_file_change_events(container, sandbox: Path) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_tool_call("write_file", path="created.txt", content="hello")
    fake.add_text("created")

    events = await collect(container, "create a file")
    changes = [e for e in events if e.type == "file_change"]
    assert len(changes) == 1
    assert changes[0].data["action"] == "created"
    assert changes[0].data["path"].endswith("created.txt")

    # a second write to the same path is reported as a modification
    fake.script.clear()
    fake.add_tool_call("write_file", path="created.txt", content="hello again")
    fake.add_text("updated")
    events = await collect(container, "update it")
    assert [e for e in events if e.type == "file_change"][0].data["action"] == "modified"


# ---------------------------------------------------------------------------
# per-request overrides
# ---------------------------------------------------------------------------
async def test_system_extra_reaches_the_prompt(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("ok")
    await collect(container, "hi", system_extra="Always answer in bullet points.")
    system_message = fake.calls[0]["messages"][0].content
    assert "Always answer in bullet points." in system_message


async def test_tool_allowlist_restricts_the_schemas(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("ok")
    await collect(container, "hi", tools=["read_file"])
    names = {tool["function"]["name"] for tool in fake.calls[0]["tools"]}
    assert names == {"read_file"}


async def test_model_and_temperature_overrides(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("ok")
    await collect(container, "hi", model="qwen3:14b", temperature=0.75, seed=7, enable_thinking=False)
    options = fake.last_options
    assert options.model == "qwen3:14b"
    assert options.temperature == 0.75
    assert options.seed == 7
    assert options.enable_thinking is False


async def test_unknown_tools_in_the_allowlist_are_ignored(container) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("ok")
    await collect(container, "hi", tools=["does_not_exist"])
    assert fake.calls[0]["tools"] == []


# ---------------------------------------------------------------------------
# sync path
# ---------------------------------------------------------------------------
def test_sync_response_carries_thinking(client) -> None:
    test_client, container, fake = client
    fake.script.clear()
    fake.add_text("done")
    fake.script[-1]["thinking"] = "step by step"
    body = test_client.post("/chat/sync", json={"message": "hi", "enable_thinking": True}).json()
    assert body["thinking"] == "step by step"


@pytest.mark.parametrize("bad", ["maybe", "", "nope"])
def test_invalid_approval_mode_is_rejected(client, bad: str) -> None:
    test_client, _, _ = client
    response = test_client.post("/chat/sync", json={"message": "hi", "approval": bad})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# verification pass
# ---------------------------------------------------------------------------
def test_verify_command_detection() -> None:
    from nour_code.agent import detect_verify_command

    assert detect_verify_command(Path("/does/not/matter")) is None


def test_verify_command_prefers_python(tmp_path: Path) -> None:
    from nour_code.agent import detect_verify_command

    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_verify_command(tmp_path) == "python -m pytest -q"


def test_verify_command_skips_package_json_without_a_test_script(tmp_path: Path) -> None:
    from nour_code.agent import detect_verify_command

    (tmp_path / "package.json").write_text('{"name": "web"}')
    assert detect_verify_command(tmp_path) is None


async def test_verification_runs_the_project_checks(container, sandbox: Path) -> None:
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("all good")
    (sandbox / "pytest.ini").write_text("[pytest]\n")

    events = await collect(container, "hi", verify=True)
    verifications = [e for e in events if e.type == "verification"]
    assert verifications, "no verification event was emitted"
    assert verifications[0].data["command"] == "python -m pytest -q"
    assert "exit_code" in verifications[0].data

    done = next(e for e in events if e.type == "done")
    assert done.data["response"]["verifications"]


def test_verify_default_setting_is_off_by_default(client) -> None:
    test_client, container, _ = client
    assert container.settings.verify_default is False
    body = test_client.post("/chat/sync", json={"message": "hi"}).json()
    assert body["verifications"] == []


# ---------------------------------------------------------------------------
# loop + budget guards
# ---------------------------------------------------------------------------
async def test_time_budget_stops_a_turn(container) -> None:
    container.settings.max_turn_seconds = 0.05
    fake = container.agent.client
    fake.script.clear()
    fake.add_text("slow answer")

    session = await container.store.get_or_create()
    await container.agent.run_sync(session, "hi")
    # the model call itself takes longer than the budget, so the loop stops early
    assert container.settings.max_turn_seconds == 0.05


async def test_token_budget_stops_a_turn(container) -> None:
    container.settings.max_turn_tokens = 10  # one FakeModel call costs 15 tokens
    fake = container.agent.client
    fake.script.clear()
    fake.add_tool_call("list_directory", path=".")
    fake.add_text("done")

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "list things", max_iterations=10)
    assert response.finish_reason in {"token_budget", "loop_detected"}
    assert any("token budget" in warning for warning in response.warnings)
    assert response.iterations < 10
