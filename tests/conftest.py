"""Shared pytest fixtures: an isolated sandbox, settings and container.

The Ollama model is replaced by :class:`FakeModel`, which replays a scripted list
of responses so the agent loop can be exercised without a GPU.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from nour_code.agent import Agent
from nour_code.config import PROJECT_ROOT, Settings
from nour_code.main import Container
from nour_code.models import ChatMessage, ToolCall
from nour_code.ollama_client import ChatResult, StreamChunk, Usage
from nour_code.sessions import SessionStore
from nour_code.skills import SkillsManager
from nour_code.tools import ToolRegistry

PERMISSIONS_YAML = """\
version: 1
allowed_roots:
  - {root}
filesystem:
  max_read_bytes: 500000
  max_write_bytes: 500000
  denied_globs:
    - "**/*.pem"
  ignore_dirs: [.git, node_modules, __pycache__, .venv, dist, build, target, .next]
shell:
  allowed_commands: [npm, npx, node, cargo, git, python, pip, docker]
  denied_patterns: ["rm -rf", "format", "del /f /s /q C:"]
  default_timeout_seconds: 20
  max_timeout_seconds: 60
network:
  allow_outbound: true
tools:
  delete_file: {delete_file}
  run_shell: true
  read_file: true
  write_file: true
  list_directory: true
  search_files: true
  apply_patch: true
"""


def write_permissions(directory: Path, *, root: Path, delete_file: bool = True) -> Path:
    path = directory / "permissions.yaml"
    path.write_text(PERMISSIONS_YAML.format(root=str(root), delete_file=str(delete_file).lower()))
    return path


class FakeModel:
    """Scripted stand-in for :class:`~nour_code.ollama_client.OllamaClient`.

    Each script entry is either::

        {"content": "text"}                       # final / intermediate answer
        {"tool_calls": [{"name": ..., "arguments": {...}}]}
    """

    def __init__(self, script: Sequence[dict[str, Any]] | None = None) -> None:
        self.script: list[dict[str, Any]] = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self.streamed: bool = False
        self.health_payload: dict[str, Any] = {
            "reachable": True,
            "url": "http://localhost:11434",
            "version": "0.0.0-test",
            "models": ["qwen3:8b"],
            "model_loaded": True,
            "detail": None,
        }

    # -- script control ----------------------------------------------------
    def add_text(self, content: str) -> FakeModel:
        self.script.append({"content": content})
        return self

    def add_tool_call(self, name: str, **arguments: Any) -> FakeModel:
        self.script.append({"tool_calls": [{"name": name, "arguments": arguments}]})
        return self

    def _next(self) -> dict[str, Any]:
        if not self.script:
            return {"content": "done"}
        if len(self.script) == 1:
            return self.script[0]  # repeat the last step if the loop asks again
        return self.script.pop(0)

    @staticmethod
    def _to_calls(entry: dict[str, Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, item in enumerate(entry.get("tool_calls") or []):
            calls.append(
                ToolCall.model_validate(
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "index": index,
                        "function": {
                            "name": item["name"],
                            "arguments": json.dumps(item.get("arguments") or {}),
                        },
                    }
                )
            )
        return calls

    # -- OllamaClient protocol ---------------------------------------------
    async def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        self.calls.append({"messages": list(messages), "tools": list(tools or [])})
        entry = self._next()
        return ChatResult(
            content=entry.get("content", ""),
            tool_calls=self._to_calls(entry),
            finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model="qwen3:8b",
        )

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        num_ctx: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.streamed = True
        self.calls.append({"messages": list(messages), "tools": list(tools or [])})
        entry = self._next()
        for text in _chunk_text(str(entry.get("content", ""))):
            yield StreamChunk(kind="token", text=text)
        calls = self._to_calls(entry)
        if calls:
            yield StreamChunk(kind="tool_calls", tool_calls=calls, finish_reason="tool_calls")
        yield StreamChunk(
            kind="usage",
            finish_reason="tool_calls" if calls else "stop",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def health(self) -> dict[str, Any]:
        return dict(self.health_payload)

    async def aclose(self) -> None:
        return None


def _chunk_text(text: str, size: int = 12):
    for start in range(0, len(text), size):
        yield text[start : start + size]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """An empty directory that acts as the only allowed root."""
    root = tmp_path / "sandbox"
    root.mkdir()
    return root


@pytest.fixture
def settings(sandbox: Path, tmp_path: Path) -> Settings:
    permissions_file = write_permissions(tmp_path, root=sandbox, delete_file=True)
    return Settings(
        model="qwen3:8b",
        working_dir=sandbox,
        permissions_file=permissions_file,
        log_dir=tmp_path / "logs",
        sessions_file=tmp_path / "nour-code-sessions.json",
        skills_dirs=str(PROJECT_ROOT / "nour_code" / "skills"),
        allowed_roots=str(sandbox),
        max_iterations=6,
        session_ttl_seconds=3600,
        metrics_enabled=False,
        audit_enabled=True,
        production_max_remediation_rounds=1,
    )


@pytest_asyncio.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    """A fully wired container with the Ollama client swapped for FakeModel."""
    instance = await Container.create(settings)
    fake = FakeModel()
    instance.client = fake
    instance.agent.client = fake
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest_asyncio.fixture
async def registry_container(settings: Settings) -> AsyncIterator[tuple[ToolRegistry, SkillsManager, SessionStore, Agent]]:
    """Lighter fixture: registry + skills + a fake-model agent, no HTTP layer."""
    instance = await Container.create(settings)
    fake = FakeModel()
    instance.agent.client = fake
    try:
        yield instance.registry, instance.skills, instance.store, instance.agent
    finally:
        await instance.aclose()


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()
