"""``python -m nour_code --doctor``: every check is pure, so everything is faked."""

from __future__ import annotations

from typing import Any

from nour_code import doctor
from nour_code.config import Settings


def fake_get(routes: dict[str, Any], *, errors: dict[str, Exception] | None = None):
    calls: list[str] = []

    def _get(url: str, timeout: float = 2.0) -> tuple[int, Any]:
        calls.append(url)
        for suffix, exc in (errors or {}).items():
            if url.endswith(suffix):
                raise exc
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return 200, payload
        raise AssertionError(f"unexpected URL {url}")

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


def ok_routes(model: str = "qwen3:8b", context: int | None = None, expires: str = "2026-10-20T18:00:00Z") -> dict[str, Any]:
    running: dict[str, Any] = {"name": model, "size": 5_900_000_000, "size_vram": 0}
    if context is not None:
        running["context_length"] = context
    if expires:
        running["expires_at"] = expires
    return {
        "/api/version": {"version": "0.12.0"},
        "/api/tags": {"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:1b"}]},
        "/api/ps": {"models": [running] if model else []},
    }


def test_python_and_dependencies_pass() -> None:
    assert doctor.python_check().status == doctor.OK
    assert doctor.dependency_check().status == doctor.OK


def test_python_check_flags_an_old_interpreter() -> None:
    check = doctor.python_check(minimum=(3, 99))
    assert check.status == doctor.FAIL
    assert "3.99" in check.detail


def test_ollama_reachable_and_model_present() -> None:
    settings = Settings()
    checks = doctor.ollama_checks(settings, get=fake_get(ok_routes(context=4096)))
    names = [check.name for check in checks]
    assert names == ["ollama", "model pulled", "model resident", "model context"]
    assert all(check.status == doctor.OK for check in checks[:3])
    assert "4096" in checks[3].detail and "16384" in checks[3].detail
    assert checks[3].status == doctor.WARN


def test_model_resident_explains_why_ps_is_empty() -> None:
    settings = Settings()
    routes = ok_routes()
    routes["/api/ps"] = {"models": []}
    checks = doctor.ollama_checks(settings, get=fake_get(routes))
    resident = checks[2]
    assert resident.status == doctor.OK
    assert "idle" in resident.detail and "/ollama/warmup" in resident.detail


def test_missing_model_is_a_failure() -> None:
    settings = Settings()
    routes = ok_routes()
    routes["/api/tags"] = {"models": [{"name": "llama3.2:1b"}]}
    checks = doctor.ollama_checks(settings, get=fake_get(routes))
    assert checks[1].status == doctor.FAIL
    assert "ollama pull qwen3:8b" in checks[1].detail


def test_unreachable_ollama_stops_after_one_line() -> None:
    settings = Settings()
    get = fake_get({}, errors={"/api/version": ConnectionRefusedError("refused")})
    checks = doctor.ollama_checks(settings, get=get)
    assert len(checks) == 1
    assert checks[0].status == doctor.FAIL
    assert "ollama serve" in checks[0].detail


def test_residency_probe_error_is_only_a_warning() -> None:
    settings = Settings()
    get = fake_get(ok_routes(), errors={"/api/ps": ConnectionResetError("boom")})
    checks = doctor.ollama_checks(settings, get=get)
    assert checks[0].status == doctor.OK
    assert checks[2].name == "model resident" and checks[2].status == doctor.WARN


def test_port_reports_free_when_nothing_listens(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_port_open", lambda host, port, timeout=0.3: False)
    check = doctor.port_check("127.0.0.1", 8000)
    assert check.status == doctor.OK
    assert "free" in check.detail


def test_port_detects_our_own_server(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_port_open", lambda host, port, timeout=0.3: True)

    def get(url: str, timeout: float = 2.0) -> tuple[int, Any]:
        assert url.endswith("/health")
        return 200, {"app": "nour-code", "status": "ok"}

    check = doctor.port_check("127.0.0.1", 8000, get=get)
    assert check.status == doctor.OK
    assert "already running" in check.detail


def test_port_detects_a_foreign_process(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_port_open", lambda host, port, timeout=0.3: True)

    def get(url: str, timeout: float = 2.0) -> tuple[int, Any]:
        raise ValueError("not json")

    check = doctor.port_check("127.0.0.1", 8000, get=get)
    assert check.status == doctor.FAIL
    assert "another program" in check.detail


def test_run_doctor_returns_non_zero_on_failure(capsys) -> None:
    settings = Settings(model="qwen3:8b")
    get = fake_get({}, errors={"/api/version": ConnectionRefusedError("refused")})
    import pytest

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "_port_open", lambda host, port, timeout=0.3: False)
        code = doctor.run_doctor(settings, get=get)
    output = capsys.readouterr().out
    assert code == 1
    assert "[FAIL]" in output
    assert "python -m nour_code" in output


def test_run_doctor_returns_zero_when_ready(capsys) -> None:
    settings = Settings(model="qwen3:8b")
    import pytest

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "_port_open", lambda host, port, timeout=0.3: False)
        code = doctor.run_doctor(settings, get=fake_get(ok_routes(context=4096)))
    output = capsys.readouterr().out
    assert code == 0
    assert "all checks passed" not in output  # context warning keeps it honest
    assert "1 warning" in output
