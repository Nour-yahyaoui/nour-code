"""Audit trail, JSON logging and Prometheus metrics."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from nour_code.config import Settings
from nour_code.observability import AuditLog, JsonFormatter, Metrics, hash_args, setup_logging


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------
async def test_audit_tool_call_writes_expected_keys(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "logs" / "audit.jsonl")
    await audit.tool_call(
        session_id="sess_1",
        tool="read_file",
        args={"path": "app.py"},
        status="ok",
        duration_ms=12.3456,
    )
    lines = [line for line in (tmp_path / "logs" / "audit.jsonl").read_text().splitlines() if line]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == {"ts", "ts_epoch", "session_id", "tool", "args_hash", "result_status", "duration_ms"}
    assert entry["session_id"] == "sess_1"
    assert entry["tool"] == "read_file"
    assert entry["result_status"] == "ok"
    assert entry["duration_ms"] == 12.35
    assert entry["ts"].endswith("Z")
    assert len(entry["args_hash"]) == 16


async def test_audit_records_errors_and_extra(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    await audit.tool_call(
        session_id="sess_2",
        tool="run_shell",
        args={"command": "rm -rf /"},
        status="denied",
        duration_ms=0.5,
        error="command denied by policy",
        extra={"rule": "denied_patterns"},
    )
    entry = (await audit.tail())[0]
    assert entry["result_status"] == "denied"
    assert entry["error"] == "command denied by policy"
    assert entry["rule"] == "denied_patterns"


async def test_audit_disabled_writes_nothing(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", enabled=False)
    await audit.tool_call(session_id="s", tool="read_file", args={}, status="ok", duration_ms=1)
    assert not (tmp_path / "audit.jsonl").exists()


async def test_audit_tail_limits_and_survives_garbage(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for index in range(5):
        await audit.tool_call(session_id=f"s{index}", tool="read_file", args={}, status="ok", duration_ms=1)
    path.write_text(path.read_text() + "{not json}\n")

    # the corrupt line is the newest one, so it shows up in every tail
    entries = await audit.tail(limit=3)
    assert len(entries) == 3
    assert [e["session_id"] for e in entries[:2]] == ["s3", "s4"]
    assert entries[-1]["tool"] == "<unparsable>"
    assert entries[-1]["raw"] == "{not json}"
    # the four older entries are still readable
    assert len(await audit.tail(limit=6)) == 6


async def test_audit_tail_missing_file(tmp_path: Path) -> None:
    assert await AuditLog(tmp_path / "nope.jsonl").tail() == []


async def test_audit_appends_across_instances(tmp_path: Path) -> None:
    for index in range(3):
        await AuditLog(tmp_path / "audit.jsonl").tool_call(
            session_id=f"s{index}", tool="read_file", args={}, status="ok", duration_ms=1
        )
    assert len(await AuditLog(tmp_path / "audit.jsonl").tail()) == 3


# ---------------------------------------------------------------------------
# args hashing
# ---------------------------------------------------------------------------
def test_hash_args_is_stable_and_order_independent() -> None:
    assert hash_args({"a": 1, "b": 2}) == hash_args({"b": 2, "a": 1})
    assert hash_args({"a": 1}) != hash_args({"a": 2})


def test_hash_args_never_leaks_values() -> None:
    digest = hash_args({"token": "super-secret"})
    assert "super-secret" not in digest
    assert len(digest) == 16


def test_hash_args_handles_unserialisable_values() -> None:
    assert hash_args({"path": Path("/tmp/x")}) == hash_args({"path": "/tmp/x"})


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def test_json_formatter_emits_structured_records() -> None:
    record = logging.LogRecord("nour_code", logging.INFO, "x.py", 1, "hello %s", ("world",), None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "nour_code"
    assert payload["message"] == "hello world"
    assert "ts" in payload


def test_json_formatter_includes_exception_text() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("nour_code", logging.ERROR, "x.py", 1, "failed", (), None)
        record.exc_info = logging.sys.exc_info() if hasattr(logging, "sys") else None
        payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "failed"


def test_json_formatter_survives_odd_payloads() -> None:
    record = logging.LogRecord("nour_code", logging.WARNING, "x.py", 1, "oops", (), None)
    record.payload = {"object": object()}
    assert "oops" in JsonFormatter().format(record)


def test_setup_logging_creates_files(tmp_path: Path) -> None:
    settings = Settings(log_dir=tmp_path / "logs", log_level="DEBUG", metrics_enabled=False)
    logger = setup_logging(settings)
    assert logger.level == logging.DEBUG
    assert (tmp_path / "logs" / "nour-code.log").parent.is_dir()
