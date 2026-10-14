"""Permission scope: traversal, symlinks, shell allow-lists, timeouts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from nour_code.permissions import (
    PermissionConfig,
    PermissionDeniedError,
    PermissionManager,
)
from tests.conftest import write_permissions


def make_manager(root: Path, *, tmp_path: Path, delete_file: bool = True) -> PermissionManager:
    path = write_permissions(tmp_path, root=root, delete_file=delete_file)
    return PermissionManager.load(path, working_dir=root)


def test_relative_path_resolves_inside_root(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    assert manager.resolve_path("a/b.txt", must_exist=False) == (sandbox / "a" / "b.txt").resolve()


def test_absolute_path_inside_root_allowed(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    target = sandbox / "file.txt"
    assert manager.resolve_path(str(target), must_exist=False) == target.resolve()


def test_traversal_is_rejected(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.resolve_path("../../etc/passwd")
    assert excinfo.value.code == "outside_root"


def test_absolute_escape_is_rejected(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError):
        manager.resolve_path("/etc/passwd")


def test_windows_path_rejected_on_posix(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    if os.name == "nt":  # pragma: no cover - host dependent
        pytest.skip("Windows host: drive letters are valid")
    with pytest.raises(PermissionDeniedError):
        manager.resolve_path("C:/Users/HP/projects/app.js")


def test_control_characters_rejected(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError):
        manager.resolve_path("evil\x00.txt")


def test_symlink_escape_is_rejected(sandbox: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    link = sandbox / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks unavailable")
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.resolve_path("escape/secret.txt")
    assert excinfo.value.code in {"outside_root", "symlink_escape"}


def test_symlink_inside_root_is_allowed(sandbox: Path, tmp_path: Path) -> None:
    inner = sandbox / "inner"
    inner.mkdir()
    (inner / "ok.txt").write_text("fine")
    link = sandbox / "link"
    try:
        link.symlink_to(inner, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("symlinks unavailable")
    manager = make_manager(sandbox, tmp_path=tmp_path)
    assert manager.resolve_path("link/ok.txt", must_exist=True).name == "ok.txt"


def test_denied_glob_blocks_pem(sandbox: Path, tmp_path: Path) -> None:
    (sandbox / "key.pem").write_text("x")
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.resolve_path("key.pem", must_exist=True)
    assert excinfo.value.code == "denied_pattern"


def test_missing_file_with_must_exist(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.resolve_path("nope.txt", must_exist=True)
    assert excinfo.value.code == "not_found"


def test_disabled_tool_is_rejected(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path, delete_file=False)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.check_tool("delete_file")
    assert excinfo.value.code == "tool_disabled"


def test_shell_allow_list(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    assert manager.check_shell("npm run build") == "npm"
    assert manager.check_shell("git status") == "git"
    assert manager.check_shell("python -m pytest") == "python"
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.check_shell("curl http://example.com")
    assert excinfo.value.code == "command_not_allowed"


def test_shell_denied_patterns(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    for command in ("rm -rf /", "git clean -xfd; rm -rf node_modules"):
        with pytest.raises(PermissionDeniedError) as excinfo:
            manager.check_shell(command)
        assert excinfo.value.code == "denied_pattern"


def test_shell_rejects_blank_and_multiline(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    for command in ("", "   ", "npm run build\nrm -rf /"):
        with pytest.raises(PermissionDeniedError):
            manager.check_shell(command)


def test_shell_rejects_unparsable_quotes(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.check_shell('git log --format="%h')
    assert excinfo.value.code in {"invalid_command", "denied_pattern"}


def test_timeout_is_clamped(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    assert manager.clamp_timeout(None) == 20
    assert manager.clamp_timeout(5) == 5
    assert manager.clamp_timeout(10_000) == 60


def test_missing_permissions_file_falls_back(tmp_path: Path, sandbox: Path) -> None:
    manager = PermissionManager.load(tmp_path / "nope.yaml", working_dir=sandbox)
    assert manager.roots == [sandbox.resolve()]
    assert any("not found" in warning for warning in manager.warnings)


def test_invalid_yaml_falls_back(tmp_path: Path, sandbox: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("allowed_roots: [unclosed\n  - : :\n")
    manager = PermissionManager.load(broken, working_dir=sandbox)
    assert manager.roots


def test_network_switch(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    manager.config.network.allow_outbound = False
    with pytest.raises(PermissionDeniedError) as excinfo:
        manager.check_shell("npm install")
    assert excinfo.value.code == "network_denied"


def test_describe_exposes_scope(sandbox: Path, tmp_path: Path) -> None:
    manager = make_manager(sandbox, tmp_path=tmp_path)
    payload = manager.describe()
    assert str(sandbox.resolve()) in payload["allowed_roots"]
    assert "npm" in payload["shell_allowed_commands"]
    assert payload["network_allow_outbound"] is True


def test_default_config_parses() -> None:
    config = PermissionConfig.model_validate({"allowed_roots": ["."], "tools": {"delete_file": False}})
    assert config.tools["delete_file"] is False
    assert config.shell.default_timeout_seconds == 60
