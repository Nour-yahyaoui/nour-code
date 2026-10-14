"""Production skill: logging, error handling, validation, Dockerfile, CI."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml
from nour_code.tools import ToolContext


def context(container) -> ToolContext:
    ctx = ToolContext(
        session_id="test",
        working_dir=container.settings.working_dir,
        settings=container.settings,
        permissions=container.permissions,
        audit=container.audit,
        logger=container.logger,
    )
    ctx.extra["registry"] = container.registry
    return ctx


async def invoke(container, tool: str, **kwargs):
    return await container.registry.call(tool, kwargs, context(container))


PYTHON_SERVICE = '''import os


def load(path):
    with open(path) as handle:
        return handle.read()


async def fetch(url, timeout):
    return await client.get(url, timeout=timeout)
'''

PY_TYPED = '''def greet(name: str, count: int, extra):
    return name * count
'''

JS_SERVICE = '''import { fetchUser } from "./db";

export async function handler(req) {
  const user = await fetchUser(req.id);
  return { user };
}

export function sync(name) {
  return name.trim();
}
'''


# ---------------------------------------------------------------------------
# add_logging
# ---------------------------------------------------------------------------
async def test_add_logging_python(container, sandbox: Path) -> None:
    target = sandbox / "service.py"
    target.write_text(PYTHON_SERVICE)
    result = await invoke(container, "add_logging", file_path=str(target))
    assert result.ok, result.error
    assert result.output["write"]["written"] is True

    content = target.read_text()
    assert "import logging" in content
    assert "logger = logging.getLogger(__name__)" in content
    assert 'logger.info("load() called")' in content
    # the import must come before the logger is created, PEP8 spacing preserved
    assert content.index("import logging") < content.index("logger = logging.getLogger")
    assert "logger = logging.getLogger(__name__)\n\n\n" in content
    assert 'logger.info("fetch() called")' in content
    ast.parse(content)  # must remain valid Python
    assert set(result.output["changes"]["instrumented"]) == {"load", "fetch"}


async def test_add_logging_python_is_idempotent(container, sandbox: Path) -> None:
    target = sandbox / "service.py"
    target.write_text(PYTHON_SERVICE)
    await invoke(container, "add_logging", file_path=str(target))
    first = target.read_text()
    second_result = await invoke(container, "add_logging", file_path=str(target))
    assert second_result.output.get("already_compliant") is True
    assert target.read_text() == first


async def test_add_logging_dry_run_writes_nothing(container, sandbox: Path) -> None:
    target = sandbox / "service.py"
    original = PYTHON_SERVICE
    target.write_text(original)
    result = await invoke(container, "add_logging", file_path=str(target), dry_run=True)
    assert result.ok
    assert target.read_text() == original


async def test_add_logging_javascript(container, sandbox: Path) -> None:
    target = sandbox / "handler.js"
    target.write_text(JS_SERVICE)
    result = await invoke(container, "add_logging", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "logger" in content
    assert 'logger.info("handler() called")' in content
    assert 'logger.info("sync() called")' in content
    assert content.count("{") == content.count("}")
    # the log line must land *inside* the function body, not above it
    lines = content.splitlines()
    header = next(i for i, line in enumerate(lines) if "async function handler" in line)
    body = next(i for i, line in enumerate(lines) if "await fetchUser" in line)
    logged = next(i for i, line in enumerate(lines) if 'logger.info("handler() called")' in line)
    assert header < logged < body


async def test_add_logging_rejects_non_source(container, sandbox: Path) -> None:
    target = sandbox / "notes.txt"
    target.write_text("hello")
    result = await invoke(container, "add_logging", file_path=str(target))
    assert not result.ok
    assert "unsupported file type" in (result.error or "")


async def test_add_logging_rejects_syntax_errors(container, sandbox: Path) -> None:
    target = sandbox / "broken.py"
    target.write_text("def oops(:\n    pass\n")
    result = await invoke(container, "add_logging", file_path=str(target))
    assert not result.ok
    assert "cannot parse" in (result.error or "")


# ---------------------------------------------------------------------------
# add_error_handling
# ---------------------------------------------------------------------------
async def test_add_error_handling_python(container, sandbox: Path) -> None:
    target = sandbox / "service.py"
    target.write_text(PYTHON_SERVICE)
    result = await invoke(container, "add_error_handling", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "try:" in content
    assert "except Exception as exc:" in content
    assert "logger.error(" in content
    assert content.count("try:") >= 2
    ast.parse(content)
    assert set(result.output["changes"]["wrapped"]) == {"load", "fetch"}


async def test_add_error_handling_is_idempotent(container, sandbox: Path) -> None:
    target = sandbox / "service.py"
    target.write_text(PYTHON_SERVICE)
    await invoke(container, "add_error_handling", file_path=str(target))
    after_first = target.read_text()
    result = await invoke(container, "add_error_handling", file_path=str(target))
    assert result.output.get("already_compliant") is True
    assert target.read_text() == after_first


async def test_add_error_handling_javascript(container, sandbox: Path) -> None:
    target = sandbox / "handler.js"
    target.write_text(JS_SERVICE)
    result = await invoke(container, "add_error_handling", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "try {" in content
    assert "} catch (error) {" in content
    assert "throw error;" in content
    assert "logger.error(" in content
    assert content.count("{") == content.count("}")
    assert set(result.output["changes"]["wrapped"]) == {"handler"}


async def test_add_error_handling_skips_safe_functions(container, sandbox: Path) -> None:
    target = sandbox / "math_utils.py"
    target.write_text("def add(a, b):\n    return a + b\n")
    result = await invoke(container, "add_error_handling", file_path=str(target))
    assert result.output.get("already_compliant") is True
    assert "try:" not in target.read_text()


# ---------------------------------------------------------------------------
# add_input_validation
# ---------------------------------------------------------------------------
async def test_add_input_validation_python(container, sandbox: Path) -> None:
    target = sandbox / "typed.py"
    target.write_text(PY_TYPED)
    result = await invoke(container, "add_input_validation", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "if not isinstance(name, str):" in content
    assert "if not isinstance(count, int):" in content
    assert "raise TypeError" in content
    assert "raise ValueError" in content  # unannotated `extra`
    ast.parse(content)


async def test_add_input_validation_is_idempotent(container, sandbox: Path) -> None:
    target = sandbox / "typed.py"
    target.write_text(PY_TYPED)
    await invoke(container, "add_input_validation", file_path=str(target))
    after_first = target.read_text()
    result = await invoke(container, "add_input_validation", file_path=str(target))
    assert result.output.get("already_compliant") is True
    assert target.read_text() == after_first


async def test_add_input_validation_skips_self(container, sandbox: Path) -> None:
    target = sandbox / "repo.py"
    target.write_text(
        "class Repo:\n"
        "    def find(self, user_id: int):\n"
        "        return user_id\n"
    )
    await invoke(container, "add_input_validation", file_path=str(target))
    content = target.read_text()
    assert "if not isinstance(user_id, int):" in content
    assert "isinstance(self" not in content
    ast.parse(content)


async def test_add_input_validation_javascript(container, sandbox: Path) -> None:
    target = sandbox / "handler.js"
    target.write_text(JS_SERVICE)
    result = await invoke(container, "add_input_validation", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "throw new TypeError(\"req is required\")" in content
    assert content.count("{") == content.count("}")
    lines = content.splitlines()
    header = next(i for i, line in enumerate(lines) if "async function handler" in line)
    guard = next(i for i, line in enumerate(lines) if 'throw new TypeError("req is required")' in line)
    body = next(i for i, line in enumerate(lines) if "await fetchUser" in line)
    assert header < guard < body


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------
async def test_generate_dockerfile_python(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (sandbox / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = await invoke(container, "generate_dockerfile", project_root=str(sandbox))
    assert result.ok, result.error
    dockerfile = sandbox / "Dockerfile"
    assert dockerfile.is_file()
    content = dockerfile.read_text()
    assert "python:3.11-slim" in content
    assert "HEALTHCHECK" in content
    assert "uvicorn" in content and "main:app" in content
    assert "USER appuser" in content
    assert (sandbox / ".dockerignore").is_file()


async def test_generate_dockerfile_nextjs(container, sandbox: Path) -> None:
    (sandbox / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "14.2.0", "react": "18.0.0"},
                    "scripts": {"build": "next build", "start": "next start"}})
    )
    (sandbox / "package-lock.json").write_text("{}")
    result = await invoke(container, "generate_dockerfile", project_root=str(sandbox))
    assert result.ok, result.error
    content = (sandbox / "Dockerfile").read_text()
    assert "node:20-alpine" in content
    assert "standalone" in content
    assert "npm ci" in content
    assert "USER nextjs" in content
    assert "HEALTHCHECK" in content


async def test_generate_dockerfile_detects_package_manager(container, sandbox: Path) -> None:
    (sandbox / "package.json").write_text(json.dumps({"name": "api"}))
    (sandbox / "pnpm-lock.yaml").write_text("")
    await invoke(container, "generate_dockerfile", project_root=str(sandbox))
    content = (sandbox / "Dockerfile").read_text()
    assert "pnpm install --frozen-lockfile" in content


async def test_generate_dockerfile_respects_existing_file(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\n")
    (sandbox / "Dockerfile").write_text("FROM scratch\n")
    first = await invoke(container, "generate_dockerfile", project_root=str(sandbox))
    assert first.ok, first.error
    assert first.output["written"] is False
    assert (sandbox / "Dockerfile").read_text() == "FROM scratch\n"

    forced = await invoke(container, "generate_dockerfile", project_root=str(sandbox), overwrite=True)
    assert forced.output["written"] is True
    assert "python:" in (sandbox / "Dockerfile").read_text()


async def test_generate_dockerfile_requires_runtime(container, sandbox: Path) -> None:
    result = await invoke(container, "generate_dockerfile", project_root=str(sandbox))
    assert not result.ok
    assert "could not detect the runtime" in (result.error or "")


# ---------------------------------------------------------------------------
# CI pipeline
# ---------------------------------------------------------------------------
async def test_generate_ci_pipeline_github_node(container, sandbox: Path) -> None:
    (sandbox / "package.json").write_text(json.dumps({"name": "api", "scripts": {"test": "jest"}}))
    (sandbox / "package-lock.json").write_text("{}")
    result = await invoke(container, "generate_ci_pipeline", project_root=str(sandbox), target="github")
    assert result.ok, result.error
    workflow = sandbox / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file()
    parsed = yaml.safe_load(workflow.read_text())
    assert "postgres:16" in json.dumps(parsed["jobs"]["test"]["services"])
    assert "actions/checkout@v4" in json.dumps(parsed)
    assert "jobs" in parsed and "test" in parsed["jobs"]


async def test_generate_ci_pipeline_github_python(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\n")
    result = await invoke(container, "generate_ci_pipeline", project_root=str(sandbox))
    assert result.ok, result.error
    content = (sandbox / ".github" / "workflows" / "ci.yml").read_text()
    assert "actions/setup-python@v5" in content
    assert "pytest -q" in content
    yaml.safe_load(content)  # valid YAML


async def test_generate_ci_pipeline_gitlab(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\n")
    result = await invoke(container, "generate_ci_pipeline", project_root=str(sandbox), target="gitlab")
    assert result.ok, result.error
    pipeline = sandbox / ".gitlab-ci.yml"
    assert pipeline.is_file()
    parsed = yaml.safe_load(pipeline.read_text())
    assert set(parsed["stages"]) == {"lint", "test", "build"}


async def test_generate_ci_pipeline_rejects_unknown_target(container, sandbox: Path) -> None:
    result = await invoke(container, "generate_ci_pipeline", project_root=str(sandbox), target="jenkins")
    assert not result.ok
    assert "unsupported CI target" in (result.error or "")


async def test_generate_ci_pipeline_respects_existing_file(container, sandbox: Path) -> None:
    (sandbox / "requirements.txt").write_text("fastapi\n")
    workflow_dir = sandbox / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("name: hand written\n")
    result = await invoke(container, "generate_ci_pipeline", project_root=str(sandbox))
    assert result.output["written"] is False
    assert (workflow_dir / "ci.yml").read_text() == "name: hand written\n"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
async def test_production_tools_reject_paths_outside_roots(container, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    result = await invoke(container, "add_logging", file_path=str(outside))
    assert result.status == "denied"


async def test_missing_argument_is_reported(container, sandbox: Path) -> None:
    result = await invoke(container, "add_logging")
    assert not result.ok
    assert "missing required argument" in (result.error or "")


async def test_error_handling_then_validation_compose(container, sandbox: Path) -> None:
    """add_error_handling adds a bare `raise`; validation must still be applied."""
    target = sandbox / "compose.py"
    target.write_text("def load(path: str):\n    return open(path).read()\n")
    await invoke(container, "add_logging", file_path=str(target))
    await invoke(container, "add_error_handling", file_path=str(target))
    result = await invoke(container, "add_input_validation", file_path=str(target))
    assert result.ok, result.error
    content = target.read_text()
    assert "if not isinstance(path, str):" in content
    assert "except Exception as exc:" in content
    ast.parse(content)


async def test_validation_refuses_to_duplicate(container, sandbox: Path) -> None:
    target = sandbox / "validated.py"
    target.write_text(
        "def load(path):\n"
        "    if not isinstance(path, str):\n"
        "        raise TypeError('bad')\n"
        "    return open(path).read()\n"
    )
    result = await invoke(container, "add_input_validation", file_path=str(target))
    assert result.output.get("already_compliant") is True
    assert target.read_text().count("isinstance(path, str)") == 1
