"""Tests for the A2A WebSocket manager (async, with fake sockets)."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close

from custom_components.hass_xiaoyi_bridge.a2a import websocket as ws_module
from custom_components.hass_xiaoyi_bridge.a2a.session import SessionManager
from custom_components.hass_xiaoyi_bridge.a2a.websocket import (
    XiaoYiAuthError,
    XiaoYiWebSocketManager,
    XiaoYiWSConfig,
)
from tests.fixtures import (
    A2A_REQUEST,
    AGENT_ID,
    CLEAR_CONTEXT_REQUEST,
    TASKS_CANCEL_REQUEST,
)


def make_closed_error() -> ConnectionClosed:
    """Build the error a closed socket raises."""
    return ConnectionClosed(Close(1000, "OK"), Close(1000, "OK"), True)


class FakeConnection:
    """Fake websockets client connection.

    ``behaviour`` decides what the receive loop sees:
    ``"open"``     → stays open until closed (blocks on an event)
    ``"close"``    → closes immediately after connecting
    ``"messages"`` → yields the queued messages, then closes
    """

    def __init__(self, behaviour: str = "open", messages: list[str] | None = None):
        self.behaviour = behaviour
        self.messages = list(messages or [])
        self.sent: list[str] = []
        self.closed = False
        self._close_event = asyncio.Event()

    async def send(self, message: str) -> None:
        if self.closed:
            raise make_closed_error()
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self._close_event.set()

    def __aiter__(self) -> FakeConnection:
        return self

    async def __anext__(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.behaviour == "close":
            raise make_closed_error()
        # "open": block until the connection is closed.
        await self._close_event.wait()
        raise make_closed_error()


def patch_connect(
    monkeypatch: pytest.MonkeyPatch, connections: list[FakeConnection | Exception]
) -> None:
    """Replace websockets.connect with a fake returning queued results."""
    queue = list(connections)

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        result = queue.pop(0) if queue else FakeConnection("close")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    # Keep the tests fast: no real backoff delays.
    monkeypatch.setattr(ws_module, "WS_RECONNECT_BASE_DELAY", 0.01)
    monkeypatch.setattr(ws_module, "WS_RECONNECT_MAX_DELAY", 0.02)


def make_manager(
    on_message: Any = None,
    on_clear: Any = None,
    on_cancel: Any = None,
    on_auth_failure: Any = None,
    session_mgr: SessionManager | None = None,
    **config_kwargs: Any,
) -> XiaoYiWebSocketManager:
    """Build a manager with recording callbacks."""
    defaults: dict[str, Any] = {
        "ws_url1": "ws://server1.test",
        "ws_url2": "ws://server2.test",
        "ak": "ak",
        "sk": "sk",
        "agent_id": AGENT_ID,
    }
    defaults.update(config_kwargs)
    calls: dict[str, list] = {
        "message": [],
        "clear": [],
        "cancel": [],
        "auth": [],
    }

    async def record_message(parsed: Any) -> None:
        calls["message"].append(parsed)

    async def record_clear(session_id: str) -> None:
        calls["clear"].append(session_id)

    async def record_cancel(session_id: str, task_id: str) -> None:
        calls["cancel"].append((session_id, task_id))

    def record_auth_failure() -> None:
        calls["auth"].append(1)
        if on_auth_failure:
            on_auth_failure()

    manager = XiaoYiWebSocketManager(
        config=XiaoYiWSConfig(**defaults),
        session_mgr=session_mgr or SessionManager(),
        on_message=on_message or record_message,
        on_clear=on_clear or record_clear,
        on_cancel=on_cancel or record_cancel,
        on_auth_failure=record_auth_failure,
    )
    manager.test_calls = calls  # type: ignore[attr-defined]
    return manager


async def drain(manager: XiaoYiWebSocketManager) -> None:
    """Wait for all in-flight message processing tasks."""
    if manager._processing_tasks:
        await asyncio.gather(*list(manager._processing_tasks))


# ─── Connection ──────────────────────────────────────────────────────────────


async def test_connect_fails_when_both_servers_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both servers failing raises ConnectionError."""
    patch_connect(
        monkeypatch, [ConnectionError("refused"), ConnectionError("refused")]
    )
    manager = make_manager()
    with pytest.raises(ConnectionError):
        await manager.connect()
    assert manager.is_ready is False


async def test_connect_raises_auth_error_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 handshake is surfaced as an authentication error."""
    rejected = InvalidStatus(SimpleNamespace(status_code=401))
    patch_connect(monkeypatch, [rejected, rejected])
    manager = make_manager()
    with pytest.raises(XiaoYiAuthError):
        await manager.connect()


async def test_connect_succeeds_with_one_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One healthy server is enough to run (degraded)."""
    patch_connect(
        monkeypatch, [FakeConnection("open"), ConnectionError("refused")]
    )
    manager = make_manager()
    await manager.connect()
    assert manager.is_ready is True
    await manager.disconnect()


async def test_connect_sends_init_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clawd_bot_init handshake is sent right after connecting."""
    connection = FakeConnection("open")
    patch_connect(monkeypatch, [connection, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    init = json.loads(connection.sent[0])
    assert init == {"msgType": "clawd_bot_init", "agentId": AGENT_ID}
    await manager.disconnect()


# ─── Routing ─────────────────────────────────────────────────────────────────


async def test_response_is_routed_to_the_owning_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responses go back to the server that delivered the session."""
    conn1 = FakeConnection("open")
    conn2 = FakeConnection("open")
    patch_connect(monkeypatch, [conn1, conn2])
    manager = make_manager()
    await manager.connect()

    manager._session_server_map["session-1"] = "server2"
    await manager.send_response("session-1", "task-1", "msg-1", "已打开")

    assert conn2.sent, "server2 must have received the response"
    assert not any(
        json.loads(m).get("sessionId") == "session-1" for m in conn1.sent
    )
    await manager.disconnect()


async def test_response_for_unknown_session_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session without a known server cannot be answered."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    with pytest.raises(ConnectionError):
        await manager.send_response("ghost", "task-1", "msg-1", "hi")
    await manager.disconnect()


# ─── Control messages ────────────────────────────────────────────────────────


async def test_clear_context_answers_and_defers_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """clearContext is answered, then the session is cleaned up later."""
    conn1 = FakeConnection("open")
    patch_connect(monkeypatch, [conn1, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    manager._session_server_map["session-1"] = "server1"

    await manager._handle_message(CLEAR_CONTEXT_REQUEST, "server1")
    await drain(manager)

    assert manager.test_calls["clear"] == ["session-1"]
    assert "session-1" in manager._cleanup_tasks
    responses = [
        json.loads(json.loads(m)["msgDetail"])
        for m in conn1.sent
        if json.loads(m).get("msgType") == "agent_response"
    ]
    assert responses[-1]["result"]["status"]["state"] == "cleared"

    # A new message for the same session revives it.
    await manager._handle_message(A2A_REQUEST, "server1")
    await drain(manager)
    assert "session-1" not in manager._cleanup_tasks
    assert len(manager.test_calls["message"]) == 1

    await manager.disconnect()


async def test_incoming_request_is_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message/stream request reaches the message callback with its text."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    await manager._handle_message(A2A_REQUEST, "server1")
    await drain(manager)

    assert len(manager.test_calls["message"]) == 1
    parsed = manager.test_calls["message"][0]
    assert parsed.text == "打开客厅灯"
    assert parsed.session_id == "session-1"
    await manager.disconnect()


async def test_mismatched_agent_id_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Messages for a different agent are ignored."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    foreign = json.loads(json.dumps(A2A_REQUEST))
    foreign["agentId"] = "someone-else"
    await manager._handle_message(foreign, "server1")
    await drain(manager)

    assert manager.test_calls["message"] == []
    await manager.disconnect()


# ─── Failure escalation ──────────────────────────────────────────────────────


async def test_repeated_fast_closes_warn_but_do_not_force_reauth(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A post-handshake close is not an authentication failure.

    The handshake already succeeded, so showing a re-authenticate card (and
    pausing reconnects for 10 minutes) was a false alarm: the gateway closes
    with code 1000 for unpublished agents, duplicate connections and rate
    limiting alike.
    """
    patch_connect(monkeypatch, [FakeConnection("close") for _ in range(12)])
    manager = make_manager()

    with caplog.at_level(logging.WARNING):
        await manager.connect()
        for _ in range(30):
            await asyncio.sleep(0.02)
            if (
                manager._state1.fast_close_count >= 3
                and manager._state2.fast_close_count >= 3
            ):
                break

    assert manager.test_calls["auth"] == []
    assert manager._auth_pause_until == {}
    assert manager._last_close["server1"]["code"] == 1000
    assert "WebSocket closed by the server" in caplog.text
    assert "code=1000" in caplog.text
    assert "closes the connection right after" in caplog.text
    await manager.disconnect()


async def test_stable_connection_resets_reconnect_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that survives the stability window resets the counter."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    monkeypatch.setattr(ws_module, "WS_STABLE_THRESHOLD", 0.01)
    manager = make_manager()
    await manager.connect()
    manager._state1.reconnect_attempts = 7

    await asyncio.sleep(0.1)
    assert manager._state1.reconnect_attempts == 0
    await manager.disconnect()


# ─── Lifecycle ───────────────────────────────────────────────────────────────


async def test_disconnect_cleans_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disconnecting closes sockets and cancels all background tasks."""
    conn1 = FakeConnection("open")
    patch_connect(monkeypatch, [conn1, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    manager._session_server_map["session-1"] = "server1"

    await manager.disconnect()

    assert conn1.closed is True
    assert manager.is_ready is False
    assert manager._session_server_map == {}
    for task in (
        manager._receive_task1,
        manager._receive_task2,
        manager._reconnect_task1,
        manager._reconnect_task2,
        manager._heartbeat1,
        manager._heartbeat2,
    ):
        assert task is None


async def test_debug_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diagnostics expose per-server state and session counts."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    state = manager.get_debug_state()
    assert state["ready"] is True
    assert state["server1"]["connected"] is True
    assert state["server2"]["connected"] is True
    assert state["sessions"]["active_sessions"] == 0
    await manager.disconnect()


async def test_state_listeners_are_notified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registered listeners fire on connect and disconnect."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    events = []
    remove = manager.add_listener(lambda: events.append(1))

    await manager.connect()
    await manager.disconnect()

    assert len(events) >= 2
    remove()
    await manager.connect()
    count = len(events)
    await manager.disconnect()
    assert len(events) == count


# ─── More manager paths ──────────────────────────────────────────────────────


async def test_heartbeat_timeout_schedules_reconnect(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A heartbeat timeout tears the socket down and reconnects."""
    conn1 = FakeConnection("open")
    patch_connect(monkeypatch, [conn1, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    with caplog.at_level(logging.WARNING):
        await manager._on_heartbeat_timeout("server1")

    assert conn1.closed is True
    assert manager._state1.ready is False
    assert manager._state2.ready is True
    assert any(
        "Heartbeat timeout, reconnecting" in r.getMessage() for r in caplog.records
    )
    # A reconnect was scheduled for the dead server.
    assert manager._reconnect_task1 is not None
    await manager.disconnect()


async def test_reconnect_now_replaces_both_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconnect_now closes and re-opens both connections."""
    first = FakeConnection("open")
    second = FakeConnection("open")
    patch_connect(
        monkeypatch, [first, first, second, FakeConnection("open")]
    )
    manager = make_manager()
    await manager.connect()

    await manager.reconnect_now()

    assert first.closed is True
    assert manager.is_ready is True
    await manager.disconnect()
    assert second.closed is True


async def test_give_up_cooldown_resumes_reconnects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """After the attempt limit the bridge cools down instead of dying."""
    monkeypatch.setattr(ws_module, "WS_GIVE_UP_COOLDOWN", 0.05)
    patch_connect(
        monkeypatch,
        [
            FakeConnection("open"),
            FakeConnection("open"),
            ConnectionError("refused"),
            FakeConnection("open"),
        ],
    )
    manager = make_manager(reconnect_max=1)
    await manager.connect()

    # Kill server1 (heartbeat timeout) → attempt 1/1 fails → cooldown.
    await manager._on_heartbeat_timeout("server1")
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            await asyncio.sleep(0.02)
            if manager._state1.connected:
                break

    assert any(
        "Max reconnection attempts reached" in r.getMessage() for r in caplog.records
    )
    # The cooldown elapsed and the bridge came back on its own.
    assert manager._state1.connected is True
    assert manager.is_ready is True
    await manager.disconnect()


async def test_tasks_cancel_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    """tasks/cancel is acknowledged and forwarded to the callback."""
    conn1 = FakeConnection("open")
    patch_connect(monkeypatch, [conn1, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    manager._session_server_map["session-1"] = "server1"

    await manager._handle_message(TASKS_CANCEL_REQUEST, "server1")
    await drain(manager)

    assert manager.test_calls["cancel"] == [("session-1", "task-1")]
    responses = [
        json.loads(json.loads(m)["msgDetail"])
        for m in conn1.sent
        if json.loads(m).get("msgType") == "agent_response"
    ]
    assert responses[-1]["result"]["status"]["state"] == "canceled"
    await manager.disconnect()


async def test_malformed_json_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON frame is reported without killing the receive loop."""
    conn = FakeConnection("open", messages=["not-json{"])
    patch_connect(monkeypatch, [conn, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    await asyncio.sleep(0.05)

    assert manager.test_calls["message"] == []
    await manager.disconnect()


async def test_message_callback_error_is_contained(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception in the message callback does not kill the manager."""
    async def exploding(parsed: Any) -> None:
        raise RuntimeError("boom")

    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager(on_message=exploding)
    await manager.connect()

    with caplog.at_level(logging.ERROR):
        await manager._handle_message(A2A_REQUEST, "server1")
        await drain(manager)

    assert any("Error in message callback" in r.getMessage() for r in caplog.records)
    assert manager.is_ready is True
    await manager.disconnect()


async def test_status_update_routes_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status updates are sent to the server that owns the session."""
    conn2 = FakeConnection("open")
    patch_connect(monkeypatch, [FakeConnection("open"), conn2])
    manager = make_manager()
    await manager.connect()
    manager._session_server_map["session-9"] = "server2"

    await manager.send_status_update("session-9", "task-9", "thinking")

    sent = [
        json.loads(json.loads(m)["msgDetail"])
        for m in conn2.sent
        if json.loads(m).get("msgType") == "agent_response"
    ]
    assert sent[-1]["result"]["kind"] == "status-update"
    assert sent[-1]["result"]["status"]["state"] == "working"
    await manager.disconnect()


async def test_clear_context_without_session_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clearContext without sessionId is dropped."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    await manager._handle_message(
        {"method": "clearContext", "id": "x"}, "server1"
    )
    assert manager.test_calls["clear"] == []
    await manager.disconnect()


async def test_debug_state_when_never_connected() -> None:
    """Diagnostics work before any connection attempt."""
    manager = make_manager()
    state = manager.get_debug_state()
    assert state["ready"] is False
    assert state["server1"]["connected"] is False
    assert state["server1"]["seconds_since_connect"] is None


async def test_send_to_unavailable_server_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending to a disconnected server raises ConnectionError."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    await manager._disconnect_server("server1")

    with pytest.raises(ConnectionError):
        await manager._send_to_server("server1", {"msgType": "x"})
    await manager.disconnect()


def test_wss_with_ip_detection() -> None:
    """Certificate verification is skipped only for wss + IP endpoints."""
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import (
        XiaoYiWebSocketManager as Manager,
    )

    assert Manager._is_wss_with_ip("wss://116.63.174.231/openclaw/v1/ws/link")
    assert Manager._is_wss_with_ip("wss://[2001:db8::1]/link")
    assert not Manager._is_wss_with_ip("wss://hag.cloud.huawei.com/link")
    assert not Manager._is_wss_with_ip("ws://116.63.174.231/link")
    assert not Manager._is_wss_with_ip("not a url")


def test_unverified_ssl_context() -> None:
    """The unverified context disables hostname and chain checks."""
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import (
        XiaoYiWebSocketManager as Manager,
    )

    context = Manager._create_unverified_ssl_context()
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


async def test_wss_ip_connection_skips_certificate_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wss+IP endpoint connects with an unverified SSL context."""
    seen = {}

    async def fake_connect(url, **kwargs):
        seen.update(kwargs)
        return FakeConnection("open")

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager(
        ws_url1="wss://116.63.174.231/openclaw/v1/ws/link",
        ws_url2="wss://116.63.174.231/openclaw/v1/ws/link",
    )
    await manager.connect()

    assert isinstance(seen["ssl"], ssl.SSLContext)
    assert seen["ssl"].check_hostname is False
    assert seen["ping_interval"] is None
    await manager.disconnect()


# ─── Regression tests for the reconnect state machine ────────────────────────


async def test_cooldown_resumes_reconnects_more_than_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every give-up cycle recovers, not just the first one.

    A completed cooldown task used to be left in place, so the *second* time
    the bridge hit ``reconnect_max`` the cooldown branch returned early and no
    further reconnect was ever scheduled — the bridge stayed dead until Home
    Assistant was restarted.
    """
    monkeypatch.setattr(ws_module, "WS_GIVE_UP_COOLDOWN", 0.05)
    patch_connect(monkeypatch, [FakeConnection("open") for _ in range(12)])
    manager = make_manager(reconnect_max=1)
    await manager.connect()

    for cycle in range(3):
        await manager._on_heartbeat_timeout("server1")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if manager._state1.connected:
                break
        assert manager._state1.connected is True, f"cycle {cycle + 1} did not recover"

    await manager.disconnect()


async def test_auth_pause_resumes_after_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause after an auth failure ends by itself.

    Nothing used to re-arm the reconnect loop once ``WS_AUTH_BACKOFF`` elapsed,
    so a false positive escalated into a permanent stop.
    """
    monkeypatch.setattr(ws_module, "WS_AUTH_BACKOFF", 0.05)
    monkeypatch.setattr(ws_module, "WS_RESUME_GRACE", 0.05)
    patch_connect(monkeypatch, [FakeConnection("open") for _ in range(4)])
    manager = make_manager()
    await manager.connect()

    await manager._disconnect_server("server1")
    manager._handle_auth_failure("server1")
    assert manager._state1.connected is False

    for _ in range(100):
        await asyncio.sleep(0.02)
        if manager._state1.connected:
            break

    assert manager._state1.connected is True
    await manager.disconnect()


async def test_deliberate_disconnect_is_not_a_fast_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnecting on purpose must not look like a rejected credential.

    Heartbeat timeouts and the ``reconnect`` service tear the socket down on
    purpose; counting those as "server closed right after connect" showed a
    re-authenticate card to users who just clicked reconnect three times.
    """
    patch_connect(monkeypatch, [FakeConnection("open") for _ in range(8)])
    manager = make_manager()
    await manager.connect()

    for _ in range(3):
        await manager._disconnect_server("server1")
        await manager._connect_server("ws://server1.test", "server1")

    assert manager._state1.fast_close_count == 0
    assert manager.test_calls["auth"] == []
    await manager.disconnect()


# ─── Regression tests for TLS / URI handling ─────────────────────────────────


async def test_ws_scheme_does_not_pass_an_ssl_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ws:// endpoints connect without ``ssl`` (websockets rejects it)."""
    seen: list[dict[str, Any]] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        seen.append(kwargs)
        return FakeConnection("open")

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager(ws_url1="ws://s1.test", ws_url2="ws://s2.test")
    await manager.connect()

    assert len(seen) == 2
    assert all(kwargs["ssl"] is None for kwargs in seen)
    await manager.disconnect()


def test_ip_ssl_context_keeps_certificate_verification() -> None:
    """An IP endpoint skips hostname matching but still verifies the chain."""
    context = ws_module.XiaoYiWebSocketManager._create_ip_ssl_context()
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED


async def test_ip_endpoint_falls_back_only_when_the_chain_is_unverifiable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Verification is dropped explicitly (and once) instead of silently."""
    contexts: list[Any] = []
    failed_once: set[str] = set()

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        context = kwargs["ssl"]
        contexts.append(context)
        # The first (verified) attempt of every endpoint fails the chain check;
        # the degraded retry succeeds.
        if (
            context.verify_mode == ssl.CERT_REQUIRED
            and url not in failed_once
        ):
            failed_once.add(url)
            raise ssl.SSLCertVerificationError("unable to get local issuer cert")
        return FakeConnection("open")

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager(
        ws_url1="wss://116.63.174.231/openclaw/v1/ws/link",
        ws_url2="wss://116.63.174.231/openclaw/v1/ws/link",
    )
    with caplog.at_level(logging.WARNING):
        await manager.connect()

    # Chain verification is the default; only the explicit retry drops it.
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[-1].verify_mode == ssl.CERT_NONE
    assert "WITHOUT TLS verification" in caplog.text
    assert manager.is_ready is True
    await manager.disconnect()


# ─── Regression tests for session handling ───────────────────────────────────


async def test_messages_of_one_session_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests on the same session never run concurrently."""
    second = json.loads(json.dumps(A2A_REQUEST))
    second["id"] = "msg-2"
    second["params"]["id"] = "task-2"
    second["params"]["message"]["parts"] = [{"kind": "text", "text": "关灯"}]

    running = 0
    max_running = 0
    order: list[str] = []

    async def on_message(parsed: Any) -> None:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        order.append(parsed.text)
        await asyncio.sleep(0.02)
        running -= 1

    patch_connect(
        monkeypatch,
        [
            FakeConnection(
                "messages",
                messages=[json.dumps(A2A_REQUEST), json.dumps(second)],
            ),
            FakeConnection("open"),
        ],
    )
    manager = make_manager(on_message=on_message)
    await manager.connect()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(order) >= 2:
            break
    await drain(manager)

    assert max_running == 1
    assert order == ["打开客厅灯", "关灯"]
    await manager.disconnect()


async def test_cleared_session_drops_late_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply for a cleared context is not pushed to the user again."""
    connection = FakeConnection("open")
    patch_connect(monkeypatch, [connection, FakeConnection("open")])
    manager = make_manager()
    await manager.connect()
    manager._session_server_map["session-1"] = "server1"
    manager._mark_session_for_cleanup("session-1")
    sent_before = len(connection.sent)

    await manager.send_response("session-1", "task-1", "msg-1", "迟到的回答")

    assert len(connection.sent) == sent_before
    await manager.disconnect()


async def test_delayed_cleanup_forgets_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session table is pruned, not only the routing map."""
    monkeypatch.setattr(ws_module, "SESSION_CLEANUP_TIMEOUT", 0.2)
    session_mgr = SessionManager()
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager(session_mgr=session_mgr)
    await manager.connect()
    manager._session_server_map["session-1"] = "server1"
    session_mgr.set_conversation_id("session-1", "conversation-1")

    manager._mark_session_for_cleanup("session-1")
    # Cleanup is deferred: an in-flight task may still answer this session.
    await asyncio.sleep(0.05)
    assert session_mgr.has_session("session-1") is True
    assert "session-1" in manager._session_server_map

    await asyncio.sleep(0.3)

    assert session_mgr.has_session("session-1") is False
    assert "session-1" not in manager._session_server_map
    await manager.disconnect()


async def test_deliberate_disconnect_does_not_schedule_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tearing a connection down on purpose must not start a reconnect chain."""
    patch_connect(monkeypatch, [FakeConnection("open"), FakeConnection("open")])
    manager = make_manager()
    await manager.connect()

    await manager._disconnect_server("server1")
    await asyncio.sleep(0.05)

    assert manager._reconnect_task1 is None
    await manager.disconnect()


# ─── Regression tests for the handshake-rejection diagnostics ───────────────


def test_describe_rejection_extracts_code_and_description() -> None:
    """The gateway's JSON body is surfaced verbatim (only clue for a 401)."""

    class FakeResponse:
        body = bytearray(
            b'{"code":"badRequest","desc":"Resource is not found or param error"}'
        )

    assert ws_module._describe_rejection(FakeResponse()) == (
        "badRequest: Resource is not found or param error"
    )


def test_describe_rejection_handles_non_json_and_missing_bodies() -> None:
    """Non-JSON bodies are truncated, absent bodies yield an empty string."""

    class PlainText:
        body = b"<html><body>403 Forbidden</body></html>"

    class NoBody:
        body = None

    assert ws_module._describe_rejection(PlainText()) == "<html><body>403 Forbidden</body></html>"
    assert ws_module._describe_rejection(NoBody()) == ""


def test_auth_error_carries_status_and_server_detail() -> None:
    """The exception keeps what the server said so callers can log it."""
    err = ws_module.XiaoYiAuthError(
        "server1 rejected", status=401, detail="badRequest: x", server_id="server1"
    )
    assert err.status == 401
    assert err.detail == "badRequest: x"
    assert err.server_id == "server1"


async def test_connect_surfaces_the_gateway_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused upgrade reports the HTTP status *and* the server's message."""
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    body = b'{"code":"badRequest","desc":"Resource is not found or param error"}'

    async def fake_connect(url: str, **kwargs: Any) -> Any:
        raise InvalidStatus(Response(401, "Unauthorized", Headers(), body))

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager()

    with pytest.raises(XiaoYiAuthError) as err:
        await manager.connect()

    assert err.value.status == 401
    assert "badRequest" in (err.value.detail or "")
    assert "Resource is not found" in str(err.value)
    await manager.disconnect()


# ─── Regression tests for the single-server mode and close diagnostics ──────


async def test_single_server_connects_only_the_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """single_server mode opens one leg and never both."""
    seen: list[str] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        seen.append(url)
        return FakeConnection("open")

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager(single_server=True)
    await manager.connect()

    assert seen == ["ws://server1.test"]
    assert manager._active_servers == {"server1"}
    assert manager._state2.connected is False
    await manager.disconnect()


async def test_single_server_falls_back_to_the_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead primary falls back to the backup endpoint once."""
    seen: list[str] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        seen.append(url)
        if "server1" in url:
            raise ConnectionError("refused")
        return FakeConnection("open")

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    manager = make_manager(single_server=True)
    await manager.connect()

    assert seen == ["ws://server1.test", "ws://server2.test"]
    assert manager._active_servers == {"server2"}
    assert manager._state2.connected is True
    await manager.disconnect()


async def test_heartbeat_timeout_records_the_close_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close seen by the heartbeat (not the receive loop) is still recorded."""
    manager = make_manager()
    manager._state1.connected_at = __import__("time").monotonic()

    await manager._on_heartbeat_timeout("server1", make_closed_error())

    assert manager._last_close["server1"]["code"] == 1000
    await manager.disconnect()
