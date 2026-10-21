"""Tests for the session manager (pure logic)."""

from __future__ import annotations

from custom_components.hass_xiaoyi_bridge.a2a.session import SessionManager


def test_conversation_id_round_trip() -> None:
    """Conversation IDs are stored and retrieved per session."""
    mgr = SessionManager()
    assert mgr.get_conversation_id("s1") is None

    mgr.set_conversation_id("s1", "conv-1")
    assert mgr.get_conversation_id("s1") == "conv-1"

    mgr.set_conversation_id("s1", "conv-2")
    assert mgr.get_conversation_id("s1") == "conv-2"
    assert mgr.active_session_count == 1


def test_update_session_creates_and_updates() -> None:
    """update_session fills in task/message/push IDs."""
    mgr = SessionManager()
    mgr.update_session("s1", task_id="t1", message_id="m1", push_id="p1")

    ctx = mgr.get_session("s1")
    assert ctx is not None
    assert ctx.task_id == "t1"
    assert ctx.message_id == "m1"
    assert ctx.push_id == "p1"

    mgr.update_session("s1", task_id="t2")
    assert mgr.get_session("s1").task_id == "t2"
    assert mgr.get_session("s1").message_id == "m1"


def test_push_id_falls_back_to_global() -> None:
    """A push ID is remembered per session and as the global fallback."""
    mgr = SessionManager()
    assert mgr.get_push_id("s1") is None

    mgr.set_push_id("s1", "push-1")
    assert mgr.get_push_id("s1") == "push-1"
    # Another session inherits the last seen push ID.
    assert mgr.get_push_id("s2") == "push-1"
    assert mgr.get_push_id() == "push-1"


def test_set_push_id_rejects_empty() -> None:
    """An empty push ID is refused instead of poisoning the fallback."""
    mgr = SessionManager()
    mgr.set_push_id("s1", "push-1")
    mgr.set_push_id("s1", "")
    assert mgr.get_push_id("s1") == "push-1"


def test_remove_and_clear() -> None:
    """Sessions can be removed individually or all at once."""
    mgr = SessionManager()
    mgr.set_conversation_id("s1", "conv-1")
    mgr.set_conversation_id("s2", "conv-2")

    mgr.remove_session("s1")
    assert mgr.has_session("s1") is False
    assert mgr.has_session("s2") is True

    mgr.clear_all()
    assert mgr.active_session_count == 0
    assert mgr.get_push_id() is None


def test_debug_state() -> None:
    """Diagnostics expose session counts without leaking the push ID."""
    mgr = SessionManager()
    mgr.set_conversation_id("s1", "conv-1")
    mgr.set_push_id("s1", "push-1")

    state = mgr.debug_state()
    assert state == {"active_sessions": 1, "global_push_id": True}


def test_update_session_on_existing_session() -> None:
    """Metadata updates keep previously stored fields."""
    mgr = SessionManager()
    mgr.update_session("s1", task_id="t1", message_id="m1")
    mgr.update_session("s1", task_id="t2")

    ctx = mgr.get_session("s1")
    assert ctx is not None
    assert ctx.task_id == "t2"
    assert ctx.message_id == "m1"


def test_get_session_missing() -> None:
    """An unknown session has no context."""
    mgr = SessionManager()
    assert mgr.get_session("nope") is None
    assert mgr.has_session("nope") is False
