"""Agent loop: tool calls, streaming events, limits and production-ready mode."""

from __future__ import annotations

from pathlib import Path

from nour_code.models import ChatResponse, StreamEvent
from nour_code.sessions import Session
from tests.conftest import FakeModel


async def run_stream(container, session: Session, message: str, **kwargs) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    async for event in container.agent.run_stream(session, message, **kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------
async def test_plain_answer_without_tools(container) -> None:
    session = await container.store.get_or_create()
    container.agent.client = FakeModel([{"content": "Hello from qwen."}])
    response = await container.agent.run_sync(session, "hi")
    assert isinstance(response, ChatResponse)
    assert response.content == "Hello from qwen."
    assert response.iterations == 1
    assert response.tool_calls == []
    assert response.finished is True


async def test_tool_call_then_final_answer(container, sandbox: Path) -> None:
    (sandbox / "notes.txt").write_text("secret value\n")
    model = FakeModel()
    model.add_tool_call("read_file", path="notes.txt")
    model.add_text("The file contains 'secret value'.")
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "read notes.txt")
    assert response.content.startswith("The file contains")
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool == "read_file"
    assert response.tool_calls[0].status == "ok"
    assert response.iterations == 2
    roles = [message.role for message in session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_denied_tool_reports_reason_to_model(container) -> None:
    model = FakeModel()
    model.add_tool_call("read_file", path="/etc/passwd")
    model.add_text("I cannot read that file.")
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "read /etc/passwd")
    assert response.tool_calls[0].status == "denied"
    tool_message = next(message for message in session.messages if message.role == "tool")
    assert "DENIED" in (tool_message.content or "")


async def test_invalid_tool_arguments_are_reported(container) -> None:
    model = FakeModel()
    model.add_tool_call("read_file", path=12345)
    model.add_text("Bad arguments, retrying.")
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "read the file")
    assert response.tool_calls[0].status == "error"
    assert "invalid arguments" in (response.tool_calls[0].error or "")


async def test_unknown_tool_does_not_crash(container) -> None:
    model = FakeModel()
    model.add_tool_call("teleport", destination="mars")
    model.add_text("That tool does not exist.")
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "teleport")
    assert response.tool_calls[0].status == "error"
    assert "unknown tool" in (response.tool_calls[0].error or "")
    assert response.content.startswith("That tool")


async def test_max_iterations_produces_warning(container) -> None:
    """Distinct calls each iteration: the loop is stopped by the iteration cap."""
    model = FakeModel()
    for index in range(4):
        model.add_tool_call("list_directory", path=f"dir{index}")
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "keep listing", max_iterations=3)
    assert response.finished is False
    assert response.finish_reason == "max_iterations"
    assert any("3 iterations" in warning for warning in response.warnings)


async def test_stuck_loop_is_detected_earlier_than_the_cap(container) -> None:
    """The same call repeated: the guard stops the turn with a specific reason."""
    model = FakeModel([{"tool_calls": [{"name": "list_directory", "arguments": {"path": "."}}]}])
    container.agent.client = model

    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "loop forever", max_iterations=10)
    assert response.finished is False
    assert response.finish_reason == "loop_detected"
    assert any("in a row" in warning for warning in response.warnings)
    assert response.iterations < 10


async def test_malformed_tool_arguments_json(container) -> None:
    from nour_code.models import ToolCall

    call = ToolCall.model_validate(
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}
    )
    parsed, error = container.agent._parse_arguments(call.function.arguments)
    assert parsed is None and error is not None


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------
async def test_stream_emits_token_and_done(container) -> None:
    container.agent.client = FakeModel([{"content": "streamed answer"}])
    session = await container.store.get_or_create()
    events = await run_stream(container, session, "hello")
    kinds = [event.type for event in events]
    assert "token" in kinds
    assert kinds[-1] == "done"
    assert "".join(e.data["text"] for e in events if e.type == "token") == "streamed answer"


async def test_stream_emits_tool_events(container, sandbox: Path) -> None:
    (sandbox / "a.txt").write_text("data")
    model = FakeModel()
    model.add_tool_call("read_file", path="a.txt")
    model.add_text("done reading")
    container.agent.client = model

    session = await container.store.get_or_create()
    events = await run_stream(container, session, "read a.txt")
    kinds = [event.type for event in events]
    assert kinds.index("tool_call") < kinds.index("tool_result")
    tool_call = next(e for e in events if e.type == "tool_call")
    assert tool_call.data["tool"] == "read_file"
    tool_result = next(e for e in events if e.type == "tool_result")
    assert "data" in tool_result.data["result"]


async def test_stream_first_turn_announces_skills(container) -> None:
    container.agent.client = FakeModel([{"content": "ok"}])
    session = await container.store.get_or_create()
    events = await run_stream(container, session, "hi")
    skill_events = [e for e in events if e.type == "skill_loaded"]
    assert {e.data["name"] for e in skill_events} == {"security_skill", "production_skill"}

    # second turn in the same session: no repeat
    events2 = await run_stream(container, session, "and again")
    assert [e for e in events2 if e.type == "skill_loaded"] == []


async def test_stream_reports_ollama_failure(container) -> None:
    from nour_code.ollama_client import OllamaUnavailableError

    class Broken(FakeModel):
        async def stream_chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
            raise OllamaUnavailableError("ollama is down")
            yield  # pragma: no cover

        async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
            raise OllamaUnavailableError("ollama is down")

    container.agent.client = Broken()
    session = await container.store.get_or_create()
    events = await run_stream(container, session, "hi")
    error_events = [e for e in events if e.type == "error"]
    assert error_events and "ollama is down" in error_events[0].data["message"]
    assert events[-1].type == "done"


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------
async def test_system_prompt_contains_context(container, sandbox: Path) -> None:
    (sandbox / "app.py").write_text("print('x')\n")
    container.agent.client = FakeModel([{"content": "ok"}])
    session = await container.store.get_or_create()
    await container.agent.run_sync(session, "hello")

    system = container.agent._system_message(session, production_mode=False)
    assert str(sandbox) in system.content
    assert "app.py" in system.content
    assert "security_skill" in system.content
    assert "read_file" in system.content
    assert "Allowed roots" in system.content


async def test_system_prompt_includes_production_instructions(container) -> None:
    session = await container.store.get_or_create()
    system = container.agent._system_message(session, production_mode=True)
    assert "PRODUCTION-READY MODE" in system.content
    assert "TODO" in system.content


# ---------------------------------------------------------------------------
# production-ready mode
# ---------------------------------------------------------------------------
async def test_production_mode_is_detected() -> None:
    from nour_code.prompts import detect_production_mode

    for message in (
        "make it production ready",
        "Please prepare this for production",
        "harden this service",
        "we need it production-grade",
    ):
        assert detect_production_mode(message) is True
    assert detect_production_mode("what does this function do?") is False


async def test_production_mode_hardens_files_and_adds_checklist(container, sandbox: Path) -> None:
    (sandbox / "service.py").write_text("def load(path):\n    return open(path).read()\n")
    container.agent.client = FakeModel([{"content": "Hardened the service."}])
    session = await container.store.get_or_create()

    response = await container.agent.run_sync(session, "make this production ready")
    assert response.production_ready is True
    assert response.production_checklist
    items = {row["item"] for row in response.production_checklist}
    assert {"logging", "error_handling", "input_validation", "no_placeholders"} <= items

    content = (sandbox / "service.py").read_text()
    assert "import logging" in content
    assert "try:" in content

    hardening = [call for call in response.tool_calls if call.tool == "add_logging"]
    assert hardening, "production mode should invoke the production skill automatically"
    assert "Production-readiness checklist" in response.content


async def test_production_mode_can_be_forced_off(container, sandbox: Path) -> None:
    (sandbox / "service.py").write_text("def load(path):\n    return open(path).read()\n")
    container.agent.client = FakeModel([{"content": "just answering"}])
    session = await container.store.get_or_create()
    response = await container.agent.run_sync(
        session, "make it production ready", production_mode=False
    )
    assert response.production_ready is False
    assert response.production_checklist == []
    assert "import logging" not in (sandbox / "service.py").read_text()


async def test_production_mode_flags_placeholders(container, sandbox: Path) -> None:
    (sandbox / "service.py").write_text("def todo_impl():\n    # TODO: implement\n    pass\n")
    container.agent.client = FakeModel([{"content": "Here is the code."}])
    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "prepare for production", max_iterations=4)
    assert response.production_ready is True
    no_placeholders = next(row for row in response.production_checklist if row["item"] == "no_placeholders")
    assert no_placeholders["status"] == "missing"
    assert any("placeholder" in warning for warning in response.warnings)


async def test_production_mode_generates_dockerfile_when_asked(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\n")
    (sandbox / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    container.agent.client = FakeModel([{"content": "Productionised."}])
    session = await container.store.get_or_create()
    await container.agent.run_sync(session, "make it production ready and add a docker image")
    assert (sandbox / "Dockerfile").is_file()


# ---------------------------------------------------------------------------
# history + usage
# ---------------------------------------------------------------------------
async def test_history_is_replayed_to_the_model(container) -> None:
    model = FakeModel([{"content": "first"}, {"content": "second"}])
    container.agent.client = model
    session = await container.store.get_or_create()
    await container.agent.run_sync(session, "one")
    await container.agent.run_sync(session, "two")
    assert len(model.calls) == 2
    second_call_messages = model.calls[1]["messages"]
    assert any(message.role == "user" and message.content == "one" for message in second_call_messages)


async def test_usage_is_accumulated(container) -> None:
    container.agent.client = FakeModel([{"content": "hi"}])
    session = await container.store.get_or_create()
    response = await container.agent.run_sync(session, "hello")
    assert response.usage.calls == 1
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5


async def test_history_trimming(container) -> None:
    container.settings.max_history_messages = 6
    container.agent.client = FakeModel([{"content": "ok"}])
    session = await container.store.get_or_create()
    for index in range(6):
        await container.agent.run_sync(session, f"message {index}")
    assert len(session.messages) <= 8


async def test_sessions_are_isolated(container) -> None:
    container.agent.client = FakeModel([{"content": "ok"}])
    first = await container.store.get_or_create()
    second = await container.store.get_or_create()
    await container.agent.run_sync(first, "hello")
    assert len(first.messages) == 2
    assert second.messages == []
