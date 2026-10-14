"""Session store: lifecycle, TTL expiry, persistence."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from nour_code.models import ChatMessage
from nour_code.sessions import Session, SessionStore


async def test_get_or_create_is_stable(settings) -> None:
    store = SessionStore(settings)
    first = await store.get_or_create("abc")
    second = await store.get_or_create("abc")
    assert first is second
    listed = await store.list()
    assert listed.count == 1


async def test_get_or_create_generates_ids(settings) -> None:
    store = SessionStore(settings)
    first = await store.get_or_create()
    second = await store.get_or_create()
    assert first.session_id != second.session_id


async def test_delete_session(settings) -> None:
    store = SessionStore(settings)
    await store.get_or_create("abc")
    assert await store.delete("abc") is True
    assert await store.delete("abc") is False
    assert (await store.list()).count == 0


async def test_list_sessions_sorted_by_activity(settings) -> None:
    store = SessionStore(settings)
    old = await store.get_or_create("old")
    new = await store.get_or_create("new")
    old.last_active = time.time() - 100
    listed = await store.list()
    assert [s.session_id for s in listed.sessions] == ["new", "old"]


async def test_session_touch_and_idle(settings) -> None:
    store = SessionStore(settings)
    session = await store.get_or_create("s1")
    before = session.last_active
    await asyncio.sleep(0.01)
    session.touch()
    assert session.last_active > before
    assert session.idle_seconds >= 0


async def test_expired_sessions_are_swept(settings) -> None:
    settings.session_ttl_seconds = 1
    store = SessionStore(settings)
    session = await store.get_or_create("s1")
    session.last_active = time.time() - 10
    removed = await store.sweep_expired()
    assert removed == ["s1"]
    assert (await store.list()).count == 0


async def test_persist_and_reload(settings) -> None:
    store = SessionStore(settings)
    session = await store.get_or_create("s1")
    session.add(ChatMessage.user("hello"))
    session.add(ChatMessage.assistant("hi"))
    session.tool_call_count = 3
    assert await store.persist() == 1

    reloaded = SessionStore(settings)
    assert await reloaded.load() == 1
    restored = await reloaded.get("s1")
    assert restored is not None
    assert [m.content for m in restored.messages] == ["hello", "hi"]
    assert restored.tool_call_count == 3


async def test_reload_drops_expired_sessions(settings) -> None:
    store = SessionStore(settings)
    session = await store.get_or_create("s1")
    session.last_active = time.time() - 10_000
    await store.persist()

    settings.session_ttl_seconds = 60
    fresh = SessionStore(settings)
    assert await fresh.load() == 0


async def test_load_ignores_corrupt_file(settings) -> None:
    settings.sessions_file.write_text("{not json")
    store = SessionStore(settings)
    assert await store.load() == 0


async def test_load_ignores_unsupported_version(settings) -> None:
    settings.sessions_file.write_text(json.dumps({"version": 99, "sessions": []}))
    store = SessionStore(settings)
    assert await store.load() == 0


async def test_load_without_file(settings) -> None:
    assert not settings.sessions_file.exists()
    store = SessionStore(settings)
    assert await store.load() == 0


async def test_persist_is_atomic(settings) -> None:
    store = SessionStore(settings)
    await store.get_or_create("s1")
    await store.persist()
    assert settings.sessions_file.is_file()
    assert not settings.sessions_file.with_suffix(".json.tmp").exists()


async def test_start_and_stop_lifecycle(settings) -> None:
    settings.session_sweep_interval_seconds = 1
    store = SessionStore(settings)
    await store.start()
    assert store._sweeper is not None
    await store.stop()
    assert store._sweeper is None


async def test_trim_keeps_recent_messages(settings) -> None:
    session = Session("s1")
    session.add(ChatMessage.system("sys"))
    for index in range(20):
        session.add(ChatMessage.user(f"m{index}"))
    session.trim(5)
    assert len(session.messages) == 6
    assert session.messages[0].role == "system"


async def test_session_serialisation_roundtrip() -> None:
    session = Session("s1", working_dir=Path("/tmp"))
    session.add(ChatMessage.user("hello"))
    session.add(ChatMessage.assistant("hi", tool_calls=None))
    data = session.to_dict()
    restored = Session.from_dict(data)
    assert restored is not None
    assert restored.session_id == "s1"
    assert len(restored.messages) == 2


async def test_session_from_dict_rejects_garbage() -> None:
    assert Session.from_dict({"nonsense": True}) is None
    assert Session.from_dict({"session_id": "s1", "messages": ["not-a-dict"]}) is not None


async def test_session_info_reports_counts(settings) -> None:
    store = SessionStore(settings)
    session = await store.get_or_create("s1")
    session.add(ChatMessage.user("hi"))
    info = session.info()
    assert info.message_count == 1
    assert info.session_id == "s1"
