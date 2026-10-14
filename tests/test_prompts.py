"""System prompt assembly: stack hints, production detection, checklist."""

from __future__ import annotations

from pathlib import Path

import pytest
from nour_code.prompts import (
    CHECKLIST_ITEMS,
    PLACEHOLDER_REGEX,
    PRODUCTION_INSTRUCTION,
    STACK_HINTS,
    build_production_checklist,
    build_system_prompt,
    detect_production_mode,
    detect_stack,
    render_checklist_markdown,
    render_file_listing,
    skill_trigger_hint,
)


# ---------------------------------------------------------------------------
# stack detection
# ---------------------------------------------------------------------------
def test_detect_stack_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "demo", "dependencies": {"express": "^4.0.0"}}\n')
    detection = detect_stack(tmp_path)
    assert "node" in detection.stacks
    assert detection.evidence["node"] == ["package.json present"]


def test_detect_stack_next_app_router(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "web", "dependencies": {"next": "14.2.0"}}\n')
    (tmp_path / "next.config.mjs").write_text("export default {};\n")
    detection = detect_stack(tmp_path, ["package.json", "next.config.mjs", "app"])
    assert "nextjs" in detection.stacks
    assert "node" in detection.stacks
    assert len(detection.evidence["nextjs"]) == 2


def test_detect_stack_actix_and_sqlx(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nactix-web = "4"\nsqlx = "0.7"\n')
    detection = detect_stack(tmp_path)
    assert "actix" in detection.stacks
    assert "postgresql" in detection.stacks
    assert "sqlx" in " ".join(detection.evidence["postgresql"])


def test_detect_stack_postgres_migrations(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    detection = detect_stack(tmp_path)
    assert detection.stacks == ["postgresql"]


def test_detect_stack_postgres_from_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: postgres:16\n"
    )
    assert "postgresql" in detect_stack(tmp_path).stacks


def test_detect_stack_ignores_compose_without_postgres(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
    assert detect_stack(tmp_path).stacks == []


def test_detect_stack_no_markers(tmp_path: Path) -> None:
    assert detect_stack(tmp_path).stacks == []


def test_detect_stack_as_dict(tmp_path: Path) -> None:
    (tmp_path / "prisma" / "schema.prisma").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "prisma" / "schema.prisma").write_text("datasource db { provider = \"postgresql\" }\n")
    payload = detect_stack(tmp_path).as_dict()
    assert payload["stacks"] == ["postgresql"]
    assert payload["evidence"]["postgresql"] == ["prisma schema"]


def test_stack_hints_cover_all_stacks() -> None:
    assert set(STACK_HINTS) == {"nextjs", "node", "actix", "postgresql"}
    assert '"use client"' in STACK_HINTS["nextjs"]
    assert "sqlx" in STACK_HINTS["actix"]
    assert "migrations" in STACK_HINTS["postgresql"].lower()
    assert "event loop" in STACK_HINTS["node"].lower()


# ---------------------------------------------------------------------------
# production mode detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "message",
    [
        "make it production ready",
        "Prepare this for production please",
        "harden this service",
        "make the API production-grade",
        "is this deploy ready?",
        "get it ready for prod",
    ],
)
def test_production_phrases_detected(message: str) -> None:
    assert detect_production_mode(message) is True


@pytest.mark.parametrize(
    "message",
    ["what does this function do?", "add a health endpoint", "rename the variable", "", "production?"],
)
def test_plain_messages_not_production(message: str) -> None:
    assert detect_production_mode(message) is False


def test_production_detector_ignores_case_and_spacing() -> None:
    assert detect_production_mode("  PRODUCTION   READY  ") is True


def test_production_instruction_mentions_placeholders() -> None:
    assert "PRODUCTION-READY MODE" in PRODUCTION_INSTRUCTION
    assert "TODO" in PRODUCTION_INSTRUCTION
    assert "/health" in PRODUCTION_INSTRUCTION


def test_placeholder_regex() -> None:
    assert PLACEHOLDER_REGEX.search("# TODO: implement") is not None
    assert PLACEHOLDER_REGEX.search("raise NotImplementedError") is not None
    assert PLACEHOLDER_REGEX.search("return compute_total(items)") is None


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------
def test_prompt_lists_cwd_files_and_skills(sandbox: Path) -> None:
    (sandbox / "package.json").write_text('{"name": "web", "dependencies": {"next": "14.2.0"}}\n')
    (sandbox / "demo.py").write_text("print('x')\n")
    (sandbox / "nested").mkdir()
    (sandbox / "nested" / "deep.py").write_text("x = 1\n")

    files = [
        {"name": "demo.py", "type": "file", "size": 12},
        {"name": "nested", "type": "dir"},
        {"name": "package.json", "type": "file", "size": 48},
    ]
    skills = [
        {
            "name": "production_skill",
            "description": "Hardens code for production.",
            "triggers": ["when the user says production ready"],
            "tools": ["add_logging", "generate_dockerfile"],
        }
    ]
    prompt = build_system_prompt(cwd=str(sandbox), files=files, skills=skills, production_mode=True)

    assert f"Working directory: `{sandbox}`" in prompt
    assert "- demo.py  12B" in prompt
    assert "- nested/" in prompt
    assert "Stack hints (detected: nextjs, node)" in prompt
    assert '"use client"' in prompt
    assert "**production_skill**" in prompt
    assert "triggers: when the user says production ready" in prompt
    assert "PRODUCTION-READY MODE" in prompt
    # nested files are not part of a shallow listing
    assert "deep.py" not in prompt


def test_prompt_states_permission_scope(sandbox: Path) -> None:
    permissions = {
        "allowed_roots": [str(sandbox)],
        "shell_allowed_commands": ["git", "npm"],
        "shell_denied_patterns": ["rm -rf"],
        "network_allow_outbound": True,
        "tools": {"delete_file": False, "run_shell": True},
        "warnings": ["roots narrowed to existing directories"],
    }
    prompt = build_system_prompt(cwd=str(sandbox), permissions=permissions)
    assert f"Allowed roots: `{sandbox}`" in prompt
    assert "Shell allow-list: `git`, `npm`" in prompt
    assert "Shell deny-list: `rm -rf`" in prompt
    assert "Outbound network: allowed" in prompt
    assert "Disabled tools: `delete_file`" in prompt
    assert "Scope note: roots narrowed to existing directories" in prompt


def test_prompt_lists_tools_with_skill_tag(sandbox: Path) -> None:
    tools = [
        {"name": "read_file", "description": "Read a file from the workspace.\nSecond line."},
        {"name": "security_scan", "description": "Scan for dangerous patterns", "skill": "security_skill"},
    ]
    prompt = build_system_prompt(cwd=str(sandbox), tools=tools)
    assert "- `read_file` — Read a file from the workspace." in prompt
    assert "- `security_scan` [skill: security_skill] — Scan for dangerous patterns" in prompt


def test_prompt_says_when_nothing_is_loaded(sandbox: Path) -> None:
    prompt = build_system_prompt(cwd=str(sandbox))
    assert "(empty directory)" in prompt
    assert "(no skills loaded)" in prompt
    assert "(scope unavailable)" in prompt
    assert "No framework markers detected" in prompt


def test_prompt_appends_extra_context(sandbox: Path) -> None:
    prompt = build_system_prompt(cwd=str(sandbox), extra_context="User is on Windows.", production_mode=False)
    assert "## Additional context for this turn" in prompt
    assert "User is on Windows." in prompt
    assert "PRODUCTION-READY MODE" not in prompt


def test_prompt_is_deterministic(sandbox: Path) -> None:
    kwargs = {"cwd": str(sandbox), "files": [{"name": "a.py", "type": "file", "size": 1}]}
    assert build_system_prompt(**kwargs) == build_system_prompt(**kwargs)


# ---------------------------------------------------------------------------
# file listing helper
# ---------------------------------------------------------------------------
def test_render_file_listing_truncates() -> None:
    entries = [{"name": f"f{i}.txt", "type": "file", "size": i} for i in range(5)]
    text = render_file_listing(entries, limit=2)
    assert "- f0.txt  0B" in text
    assert "... and 3 more" in text


def test_render_file_listing_empty() -> None:
    assert render_file_listing([]) == "(empty directory)"


# ---------------------------------------------------------------------------
# checklist
# ---------------------------------------------------------------------------
def test_checklist_reports_every_item() -> None:
    rows = build_production_checklist({"logging": (True, "logger added")})
    assert [row["item"] for row in rows] == [key for key, _ in CHECKLIST_ITEMS]
    logging_row = next(row for row in rows if row["item"] == "logging")
    assert logging_row["status"] == "ok" and logging_row["symbol"] == "[x]"
    tests_row = next(row for row in rows if row["item"] == "tests")
    assert tests_row["status"] == "missing" and tests_row["detail"] == "not verified"


def test_checklist_markdown_counts_open_items() -> None:
    rows = build_production_checklist({"logging": (True, "logger added")})
    text = render_checklist_markdown(rows)
    assert "### Production-readiness checklist" in text
    assert "- [x] Structured logging present" in text
    assert "**8 item(s) still open.**" in text


def test_checklist_markdown_all_clear() -> None:
    status = {key: (True, "verified") for key, _ in CHECKLIST_ITEMS}
    text = render_checklist_markdown(build_production_checklist(status))
    assert "**All checks passed.**" in text
    assert "[ ]" not in text


# ---------------------------------------------------------------------------
# skill trigger hint
# ---------------------------------------------------------------------------
def test_skill_trigger_hint_lists_triggers() -> None:
    hint = skill_trigger_hint(
        [{"name": "security_skill", "triggers": ["security review"], "tools": ["security_scan"]}]
    )
    assert hint.startswith("## Skill triggers")
    assert "- use `security_skill` when: security review" in hint


def test_skill_trigger_hint_skills_without_triggers() -> None:
    assert skill_trigger_hint([{"name": "quiet_skill", "triggers": []}]) == ""
