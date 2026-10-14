"""Tests for the built-in filesystem, shell and patch tools."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from nour_code.tools import ToolContext, ToolResult, truncate_output
from tests.conftest import FakeModel


async def call(container, name: str, **kwargs):
    ctx = ToolContext(
        session_id="test",
        working_dir=container.settings.working_dir,
        settings=container.settings,
        permissions=container.permissions,
        audit=container.audit,
        logger=container.logger,
    )
    ctx.extra["registry"] = container.registry
    return await container.registry.call(name, kwargs, ctx)


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------
async def test_write_then_read_file(container, sandbox: Path) -> None:
    result = await call(container, "write_file", path="src/app.py", content="print('hi')\n")
    assert result.ok, result.error
    assert (sandbox / "src" / "app.py").read_text() == "print('hi')\n"

    read = await call(container, "read_file", path="src/app.py")
    assert read.ok
    assert read.output == "print('hi')\n"


async def test_write_file_creates_parent_directories(container, sandbox: Path) -> None:
    result = await call(container, "write_file", path="a/b/c/deep.txt", content="x")
    assert result.ok
    assert (sandbox / "a" / "b" / "c" / "deep.txt").is_file()


async def test_write_file_without_parents_fails(container) -> None:
    result = await call(container, "write_file", path="missing-dir/file.txt", content="x", create_parents=False)
    assert not result.ok
    assert "parent directory" in (result.error or "")


async def test_append_mode(container, sandbox: Path) -> None:
    await call(container, "write_file", path="log.txt", content="one\n")
    await call(container, "write_file", path="log.txt", content="two\n", append=True)
    assert (sandbox / "log.txt").read_text() == "one\ntwo\n"


async def test_read_missing_file(container) -> None:
    result = await call(container, "read_file", path="ghost.txt")
    assert not result.ok
    assert result.status == "denied"


async def test_read_outside_root_is_denied(container) -> None:
    result = await call(container, "read_file", path="/etc/passwd")
    assert result.status == "denied"
    assert "outside the allowed roots" in (result.error or "")


async def test_list_directory(container, sandbox: Path) -> None:
    (sandbox / "a.py").write_text("x")
    (sandbox / "pkg").mkdir()
    (sandbox / "pkg" / "b.py").write_text("y")
    result = await call(container, "list_directory", path=".")
    assert result.ok
    names = {entry["name"] for entry in result.output["entries"]}
    assert {"a.py", "pkg"} <= names

    recursive = await call(container, "list_directory", path=".", recursive=True)
    pkg = next(e for e in recursive.output["entries"] if e["name"] == "pkg")
    assert [child["name"] for child in pkg["children"]] == ["b.py"]


async def test_list_directory_ignores_noisy_dirs(container, sandbox: Path) -> None:
    (sandbox / "node_modules").mkdir()
    (sandbox / "node_modules" / "x.js").write_text("x")
    (sandbox / "keep.py").write_text("x")
    result = await call(container, "list_directory", path=".")
    names = [entry["name"] for entry in result.output["entries"]]
    assert "node_modules" not in names
    assert "keep.py" in names


async def test_search_files_by_glob(container, sandbox: Path) -> None:
    (sandbox / "src").mkdir()
    (sandbox / "src" / "one.py").write_text("value = 1\n")
    (sandbox / "src" / "two.ts").write_text("const x = 2;\n")
    result = await call(container, "search_files", root=".", pattern="*.py")
    assert result.ok
    assert any(match["path"].endswith("one.py") for match in result.output["matches"])
    assert not any(match["path"].endswith("two.ts") for match in result.output["matches"])


async def test_search_files_by_content(container, sandbox: Path) -> None:
    (sandbox / "a.py").write_text("PASSWORD = 'hunter2'\n")
    (sandbox / "b.py").write_text("value = 3\n")
    result = await call(container, "search_files", root=".", pattern="*.py", content_regex=r"PASSWORD")
    assert result.output["count"] == 1
    assert result.output["matches"][0]["path"].endswith("a.py")


async def test_search_files_invalid_regex(container) -> None:
    result = await call(container, "search_files", root=".", pattern="*.py", content_regex="([")
    assert not result.ok
    assert "invalid arguments" in (result.error or "")


async def test_delete_file(container, sandbox: Path) -> None:
    (sandbox / "doomed.txt").write_text("bye")
    result = await call(container, "delete_file", path="doomed.txt")
    assert result.ok
    assert not (sandbox / "doomed.txt").exists()


async def test_delete_directory_requires_recursive(container, sandbox: Path) -> None:
    (sandbox / "dir").mkdir()
    (sandbox / "dir" / "f.txt").write_text("x")
    denied = await call(container, "delete_file", path="dir")
    assert not denied.ok
    assert "recursive" in (denied.error or "")

    ok = await call(container, "delete_file", path="dir", recursive=True)
    assert ok.ok
    assert not (sandbox / "dir").exists()


async def test_delete_refuses_allowed_root(container, sandbox: Path) -> None:
    result = await call(container, "delete_file", path=str(sandbox), recursive=True)
    assert result.status == "denied"
    assert "allowed root" in (result.error or "")


async def test_read_binary_file_is_refused(container, sandbox: Path) -> None:
    (sandbox / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    result = await call(container, "read_file", path="blob.bin")
    assert not result.ok
    assert "binary" in (result.error or "")


async def test_output_is_capped(container, sandbox: Path) -> None:
    (sandbox / "big.txt").write_text("x" * 5_000)
    result = await call(container, "read_file", path="big.txt", max_chars=100)
    assert result.ok
    assert len(result.output) <= 100 + 80
    assert result.output.endswith("...") or "truncated" in result.output


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------
async def test_run_shell_captures_stdout(container, sandbox: Path) -> None:
    result = await call(container, "run_shell", command="python -c \"print('hello')\"", cwd=str(sandbox))
    assert result.ok, result.error
    assert result.output["stdout"].strip() == "hello"
    assert result.output["exit_code"] == 0


async def test_run_shell_reports_nonzero_exit(container, sandbox: Path) -> None:
    result = await call(container, "run_shell", command="python -c \"import sys; sys.exit(3)\"", cwd=str(sandbox))
    assert not result.ok
    assert result.output["exit_code"] == 3


async def test_run_shell_denies_unlisted_command(container, sandbox: Path) -> None:
    result = await call(container, "run_shell", command="curl https://example.com", cwd=str(sandbox))
    assert result.status == "denied"


async def test_run_shell_denies_dangerous_pattern(container, sandbox: Path) -> None:
    result = await call(container, "run_shell", command="rm -rf .", cwd=str(sandbox))
    assert result.status == "denied"
    assert "denied pattern" in (result.error or "")


async def test_run_shell_denies_cwd_outside_root(container, tmp_path: Path, sandbox: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    result = await call(container, "run_shell", command="git status", cwd=str(outside))
    assert result.status == "denied"


async def test_run_shell_timeout_kills_process(container, sandbox: Path) -> None:
    result = await call(
        container,
        "run_shell",
        command="python -c \"import time; time.sleep(30)\"",
        cwd=str(sandbox),
        timeout_seconds=2,
    )
    assert not result.ok
    assert result.output["timed_out"] is True


async def test_run_shell_disabled_by_permissions(container, sandbox: Path) -> None:
    container.permissions.config.tools["run_shell"] = False
    result = await call(container, "run_shell", command="git status", cwd=str(sandbox))
    assert result.status == "denied"
    assert "disabled" in (result.error or "")
    container.permissions.config.tools["run_shell"] = True


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------
UNIFIED_DIFF = """--- a/greet.py
+++ b/greet.py
@@ -1,3 +1,4 @@
 def greet(name):
-    return "hi " + name
+    greeting = "hello " + name
+    return greeting
 
"""


async def test_apply_patch_modifies_file(container, sandbox: Path) -> None:
    (sandbox / "greet.py").write_text('def greet(name):\n    return "hi " + name\n\n')
    result = await call(container, "apply_patch", path=str(sandbox / "greet.py"), diff=UNIFIED_DIFF)
    assert result.ok, result.error
    assert 'greeting = "hello " + name' in (sandbox / "greet.py").read_text()


async def test_apply_patch_dry_run_changes_nothing(container, sandbox: Path) -> None:
    original = 'def greet(name):\n    return "hi " + name\n\n'
    (sandbox / "greet.py").write_text(original)
    result = await call(
        container, "apply_patch", path=str(sandbox / "greet.py"), diff=UNIFIED_DIFF, dry_run=True
    )
    assert result.ok, result.error
    assert (sandbox / "greet.py").read_text() == original


async def test_apply_patch_creates_new_file(container, sandbox: Path) -> None:
    diff = """--- /dev/null
+++ b/brand_new.py
@@ -0,0 +1,2 @@
+def main():
+    return 1
"""
    result = await call(container, "apply_patch", path=str(sandbox), diff=diff)
    assert result.ok, result.error
    assert (sandbox / "brand_new.py").read_text().startswith("def main():")


async def test_apply_patch_rejects_garbage(container, sandbox: Path) -> None:
    result = await call(container, "apply_patch", path=str(sandbox), diff="not a diff at all")
    assert not result.ok
    assert "unified diff" in (result.error or "")


async def test_apply_patch_rejects_non_matching_hunks(container, sandbox: Path) -> None:
    (sandbox / "greet.py").write_text("def other():\n    return 1\n")
    result = await call(container, "apply_patch", path=str(sandbox / "greet.py"), diff=UNIFIED_DIFF)
    assert not result.ok


async def test_apply_patch_backup(container, sandbox: Path) -> None:
    (sandbox / "greet.py").write_text('def greet(name):\n    return "hi " + name\n\n')
    result = await call(
        container, "apply_patch", path=str(sandbox / "greet.py"), diff=UNIFIED_DIFF, create_backup=True
    )
    assert result.ok
    backups = result.output.get("backups") or []
    assert backups and Path(backups[0]).is_file()


async def test_pure_python_engine_falls_back(container, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the built-in engine by making both binaries look unusable."""
    import nour_code.tools.patch as patch_module

    monkeypatch.setattr(patch_module.shutil, "which", lambda _name: None)
    (sandbox / "greet.py").write_text('def greet(name):\n    return "hi " + name\n\n')
    result = await call(container, "apply_patch", path=str(sandbox / "greet.py"), diff=UNIFIED_DIFF)
    assert result.ok, result.error
    assert result.output["strategy"] == "python-diff-engine"


async def test_patch_rejects_paths_outside_root(container, tmp_path: Path, sandbox: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    diff = """--- a/outside.py
+++ b/outside.py
@@ -1,1 +1,2 @@
 x = 1
+x = 2
"""
    result = await call(container, "apply_patch", path=str(outside), diff=diff)
    assert result.status == "denied"


# ---------------------------------------------------------------------------
# registry behaviour
# ---------------------------------------------------------------------------
async def test_unknown_tool_returns_error(container) -> None:
    result = await call(container, "no_such_tool")
    assert not result.ok
    assert "unknown tool" in (result.error or "")


async def test_invalid_arguments_are_reported(container) -> None:
    result = await call(container, "read_file", path=123)
    assert not result.ok
    assert "invalid arguments" in (result.error or "")


async def test_audit_log_is_written(container) -> None:
    await call(container, "write_file", path="audited.txt", content="data")
    await container.registry.drain()
    entries = await container.audit.tail(10)
    assert any(entry["tool"] == "write_file" for entry in entries)
    entry = next(e for e in entries if e["tool"] == "write_file")
    assert {"ts", "session_id", "tool", "args_hash", "result_status", "duration_ms"} <= set(entry)


def test_truncate_output_keeps_head_and_tail() -> None:
    text = "a" * 1_000
    truncated, changed = truncate_output(text, 100)
    assert changed is True
    assert len(truncated) <= 100
    assert truncated.startswith("a")
    assert "truncated" in truncated


def test_truncate_output_noop_when_small() -> None:
    text = "short"
    assert truncate_output(text, 100) == (text, False)


async def test_tool_result_render_marks_denials() -> None:
    result = ToolResult.denied("nope")
    assert result.render(100).startswith("DENIED:")
    failure = ToolResult.failure("boom")
    assert failure.render(100).startswith("ERROR:")


async def test_fake_model_records_calls(fake_model: FakeModel) -> None:
    fake_model.add_text("hello")
    result = await fake_model.chat([])
    assert result.content == "hello"
    assert fake_model.calls
