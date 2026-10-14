"""Configuration for **nour-code**.

Every knob is driven by an environment variable prefixed with ``NOUR_CODE_``
(e.g. ``NOUR_CODE_MODEL``, ``NOUR_CODE_PORT``).  A ``.env`` file in the current
working directory is read automatically if present.

Nothing here is mutable at runtime: :class:`Settings` is built once during
application start-up and injected into every component (no global state).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "nour-code"
ENV_PREFIX = "NOUR_CODE_"
VERSION = "1.2.0"

_PACKAGE_DIR = Path(__file__).resolve().parent
#: Repository root (the directory that contains ``nour_code/``).
PROJECT_ROOT = _PACKAGE_DIR.parent


def _split_paths(raw: str | None) -> list[Path]:
    """Split an ``os.pathsep`` separated env value into a list of paths."""
    if not raw:
        return []
    out: list[Path] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            out.append(Path(chunk).expanduser())
    return out


def _split_str(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


class Settings(BaseSettings):
    """Strongly typed, validated process configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- server ---------------------------------------------------------
    app_name: str = APP_NAME
    version: str = VERSION
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    environment: str = "development"
    root_path: str = ""
    cors_origins: str = "*"  # comma separated; "*" allows everything

    # ---- Ollama / model -------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str = "ollama"  # Ollama ignores it, the SDK requires it
    model: str = "qwen3:8b"
    request_timeout_seconds: float = Field(default=600.0, gt=0)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    health_timeout_seconds: float = Field(default=5.0, gt=0)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    num_ctx: int = Field(default=16384, ge=512)
    num_predict: int | None = Field(default=None, ge=1)
    seed: int | None = None
    # --- 1.2.0 sampling controls -----------------------------------------
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    repeat_penalty: float | None = Field(default=None, ge=0.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    stop: str | None = Field(default=None, description="Comma separated stop sequences.")
    enable_thinking: bool | None = Field(
        default=None, description="Qwen3 thinking mode (sent as options.enable_thinking)."
    )
    max_model_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=1.0, ge=0.0)

    # ---- agent loop -----------------------------------------------------
    max_iterations: int = Field(default=15, ge=1, le=100)
    # --- 1.2.0 loop guards -------------------------------------------------
    max_turn_seconds: float = Field(default=900.0, gt=0, description="Wall-clock budget for one turn.")
    max_turn_tokens: int = Field(
        default=0, ge=0, description="Token budget for one turn (0 = unlimited)."
    )
    tool_repair_attempts: int = Field(
        default=2, ge=0, le=5, description="How often a malformed tool call is fed back for repair."
    )
    loop_detection_threshold: int = Field(
        default=3, ge=2, le=10, description="Identical tool calls in a row that count as a loop."
    )
    verify_default: bool = Field(default=False, description="Run the project checks after every turn.")
    verify_timeout_seconds: int = Field(default=180, ge=1, description="Timeout for the verification command.")
    plan_mode: str = Field(default="auto", description="off | auto | on — ask for a plan before acting.")
    max_concurrent_turns: int = Field(default=4, ge=1, description="Parallel agent turns allowed (0 = unlimited).")
    max_tool_output_chars: int = Field(default=100_000, ge=1_000)
    max_tool_history_chars: int = Field(default=20_000, ge=1_000)
    max_history_messages: int = Field(default=200, ge=4)
    parallel_tool_calls: bool = False  # qwen3:8b is happier with one at a time

    # ---- production-ready mode -----------------------------------------
    production_max_remediation_rounds: int = Field(default=2, ge=0, le=5)
    production_auto_harden: bool = True
    production_max_files: int = Field(default=25, ge=1)

    # ---- sessions -------------------------------------------------------
    session_ttl_seconds: int = Field(default=7200, ge=60)  # 2 hours
    session_sweep_interval_seconds: int = Field(default=60, ge=5)
    sessions_file: Path = PROJECT_ROOT / "nour-code-sessions.json"

    # ---- paths ----------------------------------------------------------
    working_dir: Path = Field(default_factory=Path.cwd)
    permissions_file: Path = PROJECT_ROOT / "permissions.yaml"
    log_dir: Path = PROJECT_ROOT / "logs"
    skills_dirs: str | None = None  # os.pathsep separated list
    allowed_roots: str | None = None  # os.pathsep separated override

    # ---- observability --------------------------------------------------
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=64 * 1024)
    log_backup_count: int = Field(default=5, ge=0)
    metrics_enabled: bool = True
    audit_enabled: bool = True

    # ---- misc -----------------------------------------------------------
    shell_default_timeout_seconds: int = Field(default=60, ge=1)
    skill_tool_timeout_seconds: float = Field(default=180.0, gt=0)

    # -- validators -------------------------------------------------------
    @field_validator("plan_mode")
    @classmethod
    def _lower_plan_mode(cls, value: str) -> str:
        allowed = {"off", "auto", "on"}
        lowered = (value or "auto").strip().lower()
        if lowered not in allowed:
            raise ValueError(f"plan_mode must be one of {sorted(allowed)}")
        return lowered

    @field_validator("stop")
    @classmethod
    def _clean_stop(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator(
        "working_dir",
        "permissions_file",
        "log_dir",
        "sessions_file",
        mode="before",
    )
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @model_validator(mode="after")
    def _finalise(self) -> Settings:
        self.working_dir = self.working_dir.expanduser()
        if not self.working_dir.is_absolute():
            self.working_dir = (PROJECT_ROOT / self.working_dir).resolve()
        self.working_dir = self.working_dir.resolve()
        self.log_dir = self.log_dir.expanduser()
        if not self.log_dir.is_absolute():
            self.log_dir = PROJECT_ROOT / self.log_dir
        self.sessions_file = self.sessions_file.expanduser()
        if not self.sessions_file.is_absolute():
            self.sessions_file = PROJECT_ROOT / self.sessions_file
        self.permissions_file = self.permissions_file.expanduser()
        if not self.permissions_file.is_absolute():
            self.permissions_file = PROJECT_ROOT / self.permissions_file
        return self

    # -- derived ----------------------------------------------------------
    @property
    def ollama_openai_base_url(self) -> str:
        """Base URL of Ollama's OpenAI-compatible API (``/v1``)."""
        return self.ollama_base_url.rstrip("/") + "/v1"

    @property
    def chat_completions_url(self) -> str:
        return f"{self.ollama_openai_base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.ollama_openai_base_url}/models"

    @property
    def native_tags_url(self) -> str:
        return f"{self.ollama_base_url.rstrip('/')}/api/tags"

    @property
    def ollama_version_url(self) -> str:
        return f"{self.ollama_base_url.rstrip('/')}/api/version"

    @property
    def audit_file(self) -> Path:
        return self.log_dir / "nour-code-audit.jsonl"

    @property
    def app_log_file(self) -> Path:
        return self.log_dir / "nour-code.log"

    @property
    def skill_directories(self) -> list[Path]:
        """Directories scanned for ``*.py`` skills, in priority order."""
        if self.skills_dirs:
            return _split_paths(self.skills_dirs)
        return [
            _PACKAGE_DIR / "skills",  # bundled example skills (loader lives here too)
            PROJECT_ROOT / "skills",  # user drop-in directory
        ]

    @property
    def allowed_root_overrides(self) -> list[Path]:
        return _split_paths(self.allowed_roots)

    @property
    def cors_origin_list(self) -> list[str]:
        origins = _split_str(self.cors_origins)
        return origins or ["*"]

    @property
    def stop_sequences(self) -> list[str]:
        return _split_str(self.stop)

    @property
    def sandbox_dir(self) -> Path:
        return PROJECT_ROOT / "sandbox"

    def describe(self) -> dict[str, Any]:
        """Non-secret snapshot used by ``/health`` and start-up logging."""
        return {
            "app": self.app_name,
            "version": self.version,
            "environment": self.environment,
            "model": self.model,
            "ollama_base_url": self.ollama_base_url,
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "max_turn_seconds": self.max_turn_seconds,
            "plan_mode": self.plan_mode,
            "verify_default": self.verify_default,
            "max_concurrent_turns": self.max_concurrent_turns,
            "max_tool_output_chars": self.max_tool_output_chars,
            "working_dir": str(self.working_dir),
            "permissions_file": str(self.permissions_file),
            "skills_dirs": [str(p) for p in self.skill_directories],
            "sessions_file": str(self.sessions_file),
            "session_ttl_seconds": self.session_ttl_seconds,
            "log_dir": str(self.log_dir),
            "metrics_enabled": self.metrics_enabled,
        }


def load_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings`, applying explicit ``overrides`` last."""
    return Settings(**overrides)
