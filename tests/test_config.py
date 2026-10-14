"""Settings: env-prefix parsing, path resolution and derived URLs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from nour_code.config import (
    ENV_PREFIX,
    PROJECT_ROOT,
    VERSION,
    Settings,
    _split_paths,
    _split_str,
    load_settings,
)


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------
def test_defaults_match_the_spec() -> None:
    settings = Settings()
    assert settings.model == "qwen3:8b"
    assert settings.num_ctx == 16384
    assert settings.temperature == 0.1
    assert settings.max_iterations == 15
    assert settings.session_ttl_seconds == 7200  # two hours
    assert settings.max_tool_output_chars == 100_000
    assert settings.host == "0.0.0.0" and settings.port == 8000


def test_derived_urls() -> None:
    settings = Settings(ollama_base_url="http://localhost:11434/")
    assert settings.ollama_openai_base_url == "http://localhost:11434/v1"
    assert settings.chat_completions_url == "http://localhost:11434/v1/chat/completions"
    assert settings.models_url == "http://localhost:11434/v1/models"
    assert settings.native_tags_url == "http://localhost:11434/api/tags"
    assert settings.ollama_version_url == "http://localhost:11434/api/version"


def test_derived_paths() -> None:
    settings = Settings(log_dir=PROJECT_ROOT / "logs")
    assert settings.app_log_file == PROJECT_ROOT / "logs" / "nour-code.log"
    assert settings.audit_file == PROJECT_ROOT / "logs" / "nour-code-audit.jsonl"


def test_skill_directories_default_to_bundled_and_dropin() -> None:
    dirs = Settings().skill_directories
    assert PROJECT_ROOT / "nour_code" / "skills" in dirs
    assert PROJECT_ROOT / "skills" in dirs


def test_skill_directories_override(tmp_path: Path) -> None:
    settings = Settings(skills_dirs=os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert settings.skill_directories == [tmp_path / "a", tmp_path / "b"]


def test_allowed_root_overrides_empty_by_default() -> None:
    assert Settings().allowed_root_overrides == []
    assert Settings(allowed_roots=str(PROJECT_ROOT)).allowed_root_overrides == [PROJECT_ROOT]


# ---------------------------------------------------------------------------
# environment variables
# ---------------------------------------------------------------------------
def test_settings_read_prefixed_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}MODEL", "qwen3:14b")
    monkeypatch.setenv(f"{ENV_PREFIX}MAX_ITERATIONS", "8")
    monkeypatch.setenv(f"{ENV_PREFIX}LOG_LEVEL", "debug")
    monkeypatch.setenv(f"{ENV_PREFIX}WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("NOUR_CODE_UNRELATED_VARIABLE", "ignored")
    settings = Settings()
    assert settings.model == "qwen3:14b"
    assert settings.max_iterations == 8
    assert settings.log_level == "DEBUG"
    assert settings.working_dir == tmp_path.resolve()


def test_cors_origins_splitting() -> None:
    assert Settings(cors_origins="https://a.dev, https://b.dev").cors_origin_list == [
        "https://a.dev",
        "https://b.dev",
    ]
    assert Settings(cors_origins="").cors_origin_list == ["*"]


def test_load_settings_applies_overrides() -> None:
    settings = load_settings(model="llama3.2:1b", temperature=0.9)
    assert settings.model == "llama3.2:1b"
    assert settings.temperature == 0.9


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------
def test_relative_paths_resolve_against_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        working_dir="sandbox",
        log_dir="logs",
        sessions_file="sessions.json",
        permissions_file="permissions.yaml",
    )
    assert settings.working_dir == (PROJECT_ROOT / "sandbox").resolve()
    assert settings.log_dir == PROJECT_ROOT / "logs"
    assert settings.sessions_file == PROJECT_ROOT / "sessions.json"
    assert settings.permissions_file == PROJECT_ROOT / "permissions.yaml"


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    settings = Settings(working_dir=tmp_path, log_dir=tmp_path / "l")
    assert settings.working_dir == tmp_path.resolve()
    assert settings.log_dir == tmp_path / "l"


def test_tilde_is_expanded() -> None:
    settings = Settings(log_dir="~/nour-logs")
    assert settings.log_dir == Path.home() / "nour-logs"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field, value",
    [
        ("log_level", "VERBOSE"),
        ("temperature", 5.0),
        ("num_ctx", 8),
        ("max_iterations", 0),
        ("port", 0),
        ("max_tool_output_chars", 10),
        ("session_ttl_seconds", 5),
        ("request_timeout_seconds", 0),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_describe_hides_nothing_sensitive_but_is_stable() -> None:
    payload = Settings(metrics_enabled=False).describe()
    assert payload["version"] == VERSION
    assert payload["app"] == "nour-code"
    assert payload["metrics_enabled"] is False
    assert "ollama_api_key" not in payload


def test_unknown_env_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}NOT_A_FIELD", "x")
    assert Settings().model == "qwen3:8b"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_split_paths_skips_empty_chunks() -> None:
    raw = os.pathsep.join(["/tmp/a", "", "  ", "/tmp/b"])
    assert _split_paths(raw) == [Path("/tmp/a"), Path("/tmp/b")]
    assert _split_paths(None) == []


def test_split_str_trims() -> None:
    assert _split_str(" a , b ,, ") == ["a", "b"]
    assert _split_str(None) == []
