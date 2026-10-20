"""``python -m nour_code --doctor``: a one-shot self-diagnosis.

Answers the three questions behind most *"the API does not respond"* reports:

* is the FastAPI server listening (and on which port)?
* is Ollama reachable from this shell?
* is the configured model pulled, and is it resident (with which context)?

The module uses the standard library only for its probes, so it keeps working
when the optional dependencies are missing and the server cannot start.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

OK = "ok"
WARN = "warn"
FAIL = "fail"

_CORE_PACKAGES = ("fastapi", "uvicorn", "httpx", "pydantic", "yaml")


@dataclass(slots=True)
class Check:
    """One line of the doctor report."""

    name: str
    status: str
    detail: str

    def render(self) -> str:
        return f"  [{self.status.upper():<4}] {self.name:<15} {self.detail}"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
MINIMUM_PYTHON = (3, 11)


def python_check(minimum: tuple[int, int] = MINIMUM_PYTHON) -> Check:
    """The version guard, kept configurable so it can be tested."""
    version = platform.python_version()
    if sys.version_info[:2] >= minimum:
        return Check("python", OK, version)
    required = ".".join(str(part) for part in minimum)
    return Check("python", FAIL, f"{version} (needs {required}+)")


def dependency_check() -> Check:
    missing: list[str] = []
    versions: list[str] = []
    for name in _CORE_PACKAGES:
        try:
            module = __import__(name)
        except Exception:
            missing.append(name)
            continue
        versions.append(f"{name} {getattr(module, '__version__', '?')}")
    if missing:
        return Check("dependencies", FAIL, f"missing: {', '.join(missing)}; run: pip install -r requirements.txt")
    return Check("dependencies", OK, ", ".join(versions))


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib so the probe survives a broken install)
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: float = 2.0) -> tuple[int, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local daemon only
        body = response.read().decode("utf-8", errors="replace")
    return int(response.status), json.loads(body) if body else {}


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host


def port_check(
    host: str,
    port: int,
    *,
    get: Callable[[str, float], tuple[int, Any]] = _get_json,
) -> Check:
    """Is the API port free, and if not, is the process on it nour-code?"""
    probe = _probe_host(host)
    if _port_open(probe, port):
        try:
            _, payload = get(f"http://{probe}:{port}/health", 1.5)
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("app") == "nour-code":
            return Check("api port", OK, f"nour-code is already running on http://{probe}:{port}")
        return Check("api port", FAIL, f"{host}:{port} is in use by another program; set NOUR_CODE_PORT or stop it")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((probe, port))
        except OSError as exc:
            return Check("api port", FAIL, f"cannot bind {host}:{port} ({exc}); set NOUR_CODE_PORT")
    return Check("api port", OK, f"{host}:{port} is free (the server is not running yet)")


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
def _model_present(names: list[str], model: str) -> bool:
    """Tolerant "is this family pulled" check (``qwen3`` matches ``qwen3:8b``)."""
    if model in names:
        return True
    family = model.split(":", 1)[0]
    return any(name == family or name.split(":", 1)[0] == family for name in names)


def _entry_name(entry: dict[str, Any]) -> str:
    return str(entry.get("name") or entry.get("model") or "")


def ollama_checks(
    settings: Settings,
    *,
    get: Callable[[str, float], tuple[int, Any]] = _get_json,
    timeout: float = 2.0,
) -> list[Check]:
    """Reachability, pulled-model and residency checks, in that order."""
    base = settings.ollama_base_url.rstrip("/")
    try:
        _, payload = get(f"{base}/api/version", timeout)
    except Exception as exc:
        return [
            Check(
                "ollama",
                FAIL,
                f"cannot reach {base} ({exc.__class__.__name__}); is `ollama serve` running?",
            )
        ]

    version = payload.get("version", "?") if isinstance(payload, dict) else "?"
    checks = [Check("ollama", OK, f"reachable at {base} (v{version})")]

    names: list[str] = []
    try:
        _, tags = get(f"{base}/api/tags", timeout)
        for item in (tags or {}).get("models", []) if isinstance(tags, dict) else []:
            if isinstance(item, dict) and _entry_name(item):
                names.append(_entry_name(item))
    except Exception as exc:
        checks.append(Check("model pulled", WARN, f"cannot list models: {exc.__class__.__name__}"))
    else:
        if _model_present(names, settings.model):
            checks.append(Check("model pulled", OK, settings.model))
        else:
            checks.append(
                Check("model pulled", FAIL, f"{settings.model} is missing; run: ollama pull {settings.model}")
            )

    try:
        _, ps = get(f"{base}/api/ps", timeout)
    except Exception as exc:
        checks.append(Check("model resident", WARN, f"cannot read /api/ps: {exc.__class__.__name__}"))
        return checks

    entries = [
        entry for entry in (ps or {}).get("models", []) if isinstance(entry, dict) and _entry_name(entry)
    ] if isinstance(ps, dict) else []
    match = next((entry for entry in entries if _model_present([_entry_name(entry)], settings.model)), None)
    if match is None:
        checks.append(
            Check(
                "model resident",
                OK,
                f"{settings.model} is idle (loads on the first turn; POST /ollama/warmup to preload)",
            )
        )
        return checks

    context = match.get("context_length")
    detail = f"{settings.model} is in memory"
    if match.get("expires_at"):
        detail += f" until {match['expires_at']}"
    checks.append(Check("model resident", OK, detail))
    if isinstance(context, int) and context < settings.num_ctx:
        checks.append(
            Check(
                "model context",
                WARN,
                f"allocated {context} tokens, configured num_ctx={settings.num_ctx}; Ollama's "
                "/v1 endpoint ignores num_ctx - set OLLAMA_CONTEXT_LENGTH or use the native API",
            )
        )
    return checks


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def run_doctor(
    settings: Settings,
    *,
    host: str | None = None,
    port: int | None = None,
    out: Callable[[str], None] = print,
    get: Callable[[str, float], tuple[int, Any]] = _get_json,
) -> int:
    """Print the report; return 0 when every check passes, 1 otherwise."""
    bind_host = host or settings.host
    bind_port = port or settings.port
    checks = [
        python_check(),
        dependency_check(),
        port_check(bind_host, bind_port, get=get),
    ]
    checks.extend(ollama_checks(settings, get=get))

    out("nour-code doctor")
    for check in checks:
        out(check.render())

    failures = [check for check in checks if check.status == FAIL]
    warnings = [check for check in checks if check.status == WARN]
    out("")
    if failures:
        out(f"{len(failures)} check(s) failed - fix the [FAIL] lines above.")
        out(f"then start the API with: python -m nour_code --host {bind_host} --port {bind_port}")
        return 1
    out(f"ready, with {len(warnings)} warning(s)." if warnings else "all checks passed.")
    out(f"start the API with: python -m nour_code --host {bind_host} --port {bind_port}")
    return 0
