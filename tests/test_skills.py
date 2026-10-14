"""Skill loader (discovery, hot reload, error isolation) + security skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nour_code.skills import SkillsManager
from nour_code.tools import ToolContext

from tests.conftest import FakeModel


def context(container, *, session_id: str = "test") -> ToolContext:
    ctx = ToolContext(
        session_id=session_id,
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


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------
async def test_bundled_skills_load(container) -> None:
    names = container.skills.names()
    assert {"security_skill", "production_skill"} <= set(names)


async def test_skill_tools_are_registered(container) -> None:
    tool_names = container.registry.names()
    for expected in (
        "read_file",
        "write_file",
        "list_directory",
        "search_files",
        "delete_file",
        "run_shell",
        "apply_patch",
        "security_scan",
        "check_secrets",
        "check_dependency_cves",
        "add_logging",
        "add_error_handling",
        "add_input_validation",
        "generate_dockerfile",
        "generate_ci_pipeline",
    ):
        assert expected in tool_names, f"{expected} missing"


async def test_skill_schemas_are_openai_shaped(container) -> None:
    for schema in container.registry.schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert isinstance(function["description"], str)
        assert function["parameters"]["type"] == "object"


async def test_skill_info_lists_triggers(container) -> None:
    info = container.skills.info()
    security = next(s for s in info.skills if s.name == "security_skill")
    assert security.triggers
    assert "security_scan" in security.tools


async def test_hot_reload_picks_up_new_skill(container, tmp_path: Path) -> None:
    directory = tmp_path / "skills"
    directory.mkdir()
    (directory / "greet_skill.py").write_text(
        '''SKILL_NAME = "greet_skill"
SKILL_DESCRIPTION = "says hello"
SKILL_TRIGGERS = ["when the user says hello"]
SKILL_TOOLS = [{
    "type": "function",
    "function": {
        "name": "greet",
        "description": "greet someone",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
}]

async def handle(tool_name, args, ctx):
    return {"ok": True, "message": f"hello {args.get('name', 'world')}"}
'''
    )
    container.settings.skills_dirs = str(directory)
    report = await container.skills.reload()
    assert not report.errors
    assert any(name.startswith("greet_skill") for name in report.loaded)
    assert "greet" in container.registry.names()

    result = await invoke(container, "greet", name="nour")
    assert result.ok
    assert result.output["message"] == "hello nour"


async def test_broken_skill_is_skipped(container, tmp_path: Path) -> None:
    directory = tmp_path / "broken-skills"
    directory.mkdir()
    (directory / "raises_on_import.py").write_text("raise RuntimeError('boom')\n")
    (directory / "no_name.py").write_text("SKILL_DESCRIPTION = 'missing name'\n")
    (directory / "sync_handle.py").write_text(
        'SKILL_NAME = "sync_handle"\nSKILL_DESCRIPTION = "bad"\n'
        "def handle(tool_name, args, ctx):\n    return {}\n"
    )
    container.settings.skills_dirs = str(directory)
    report = await container.skills.reload()
    assert len(report.errors) == 3
    assert container.skills.names() == []
    assert report.ok is False


async def test_duplicate_tool_names_are_rejected(container, tmp_path: Path) -> None:
    directory = tmp_path / "dup-skills"
    directory.mkdir()
    schema = '''SKILL_NAME = "__NAME__"
SKILL_DESCRIPTION = "dup"
SKILL_TOOLS = [{
    "type": "function",
    "function": {
        "name": "shared_tool",
        "description": "x",
        "parameters": {"type": "object", "properties": {}},
    },
}]

async def handle(tool_name, args, ctx):
    return {"ok": True}
'''
    (directory / "a_skill.py").write_text(schema.replace("__NAME__", "a_skill"))
    (directory / "b_skill.py").write_text(schema.replace("__NAME__", "b_skill"))
    container.settings.skills_dirs = str(directory)
    report = await container.skills.reload()
    assert report.errors
    assert "already provided" in " ".join(error.error for error in report.errors)


async def test_skill_exception_is_contained(container, tmp_path: Path) -> None:
    directory = tmp_path / "exploding"
    directory.mkdir()
    (directory / "explodes.py").write_text(
        '''SKILL_NAME = "explodes"
SKILL_DESCRIPTION = "always fails"
SKILL_TOOLS = [{"type": "function", "function": {"name": "explode",
    "description": "boom", "parameters": {"type": "object", "properties": {}}}}]

async def handle(tool_name, args, ctx):
    raise ValueError("kaboom")
'''
    )
    container.settings.skills_dirs = str(directory)
    await container.skills.reload()
    result = await invoke(container, "explode")
    assert not result.ok
    assert "kaboom" in (result.error or "")


async def test_reload_version_bumps(container) -> None:
    before = container.skills.version
    await container.skills.reload()
    assert container.skills.version == before + 1


# ---------------------------------------------------------------------------
# security skill
# ---------------------------------------------------------------------------
async def test_security_scan_flags_dangerous_patterns(container, sandbox: Path) -> None:
    (sandbox / "app.py").write_text(
        "import subprocess\n"
        "API_KEY = 'abcdef123456'\n"
        "def run(cmd):\n"
        "    subprocess.run(cmd, shell=True)\n"
        "    conn = httpx.get(url, verify=False)\n"
    )
    result = await invoke(container, "security_scan", path=str(sandbox))
    assert result.ok, result.error
    rules = {finding["rule"] for finding in result.output["findings"]}
    assert "shell-injection" in rules
    assert "disabled-tls-verification" in rules
    assert result.output["summary"]["total"] >= 2


async def test_security_scan_respects_severity_threshold(container, sandbox: Path) -> None:
    (sandbox / "a.py").write_text("url = 'http://example.com'\n")
    low = await invoke(container, "security_scan", path=str(sandbox), severity_threshold="low")
    critical = await invoke(container, "security_scan", path=str(sandbox), severity_threshold="critical")
    assert low.output["summary"]["total"] >= 1
    assert critical.output["summary"]["total"] == 0


async def test_check_secrets_finds_aws_key_and_redacts(container, sandbox: Path) -> None:
    (sandbox / "config.py").write_text(
        'AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"\n'
        'password = "sup3rs3cretvalue"\n'
    )
    result = await invoke(container, "check_secrets", path=str(sandbox))
    assert result.ok
    rules = {finding["rule"] for finding in result.output["findings"]}
    assert "aws-access-key-id" in rules
    for finding in result.output["findings"]:
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(finding)
        assert "*" in finding["evidence"]


async def test_check_secrets_clean_project(container, sandbox: Path) -> None:
    (sandbox / "clean.py").write_text("value = 1\n")
    result = await invoke(container, "check_secrets", path=str(sandbox))
    assert result.ok
    assert result.output["summary"]["total"] == 0


async def test_check_dependency_cves_without_npm(container, sandbox: Path) -> None:
    (sandbox / "package.json").write_text(json.dumps({"name": "demo", "dependencies": {"next": "14.0.0"}}))
    result = await invoke(container, "check_dependency_cves", package_json_path=str(sandbox / "package.json"))
    assert result.ok, result.error
    assert result.output["status"] in {"ok", "npm_unavailable", "unparsable_output", "no_package_json"}
    assert result.output["declared_dependencies"] == 1


async def test_check_dependency_cves_parses_audit_output(container, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate `npm audit --json` by intercepting run_shell."""
    (sandbox / "package.json").write_text(json.dumps({"name": "demo", "dependencies": {"left-pad": "1.0.0"}}))
    audit_payload = {
        "vulnerabilities": {
            "left-pad": {
                "name": "left-pad",
                "severity": "high",
                "range": "<=1.3.0",
                "isDirect": True,
                "fixAvailable": True,
                "via": [{"title": "Prototype pollution", "url": "https://example.com/advisory"}],
            }
        }
    }
    real_call = container.registry.call

    async def fake_call(name, arguments, ctx, **kwargs):
        if name == "run_shell" and "npm audit" in str(arguments.get("command", "")):
            from nour_code.tools import ToolResult

            return ToolResult(
                ok=False,
                status="error",
                output={"stdout": json.dumps(audit_payload), "stderr": "", "exit_code": 1},
                error="command exited with 1",
                metadata={"stdout": json.dumps(audit_payload), "exit_code": 1},
            )
        return await real_call(name, arguments, ctx, **kwargs)

    monkeypatch.setattr(container.registry, "call", fake_call)
    result = await invoke(container, "check_dependency_cves", package_json_path=str(sandbox))
    assert result.ok
    assert result.output["status"] == "ok"
    assert result.output["total_vulnerabilities"] == 1
    assert result.output["severity_counts"]["high"] == 1
    assert "left-pad" in result.output["fixable_packages"]


async def test_security_tools_reject_paths_outside_roots(container, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    result = await invoke(container, "security_scan", path=str(outside))
    assert result.status == "denied"


async def test_skill_handles_unknown_tool(container) -> None:
    result = await container.skills.dispatch("not_a_skill_tool", {}, context(container))
    assert result["ok"] is False
