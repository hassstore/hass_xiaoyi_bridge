"""Tests for the heartbeat manager (async)."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from custom_components.hass_xiaoyi_bridge.a2a.heartbeat import HeartbeatManager
from custom_components.hass_xiaoyi_bridge.a2a.log_throttle import ThrottledLogger


def make_closed_error() -> ConnectionClosed:
    """Build the ConnectionClosed error a dead socket raises on send."""
    return ConnectionClosed(Close(1000, "OK"), Close(1000, "OK"), True)


class FakeSocket:
    """WebSocket stub that records sends and can be told to fail."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.sent: list[str] = []
        self.failures = list(failures or [])

    async def send(self, message: str) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)


@pytest.fixture
def no_first_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the first heartbeat immediately instead of after 1s."""
    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.a2a.heartbeat.HEARTBEAT_FIRST_DELAY",
        0,
    )


async def test_heartbeat_sends_periodically(no_first_delay: None) -> None:
    """Heartbeats are sent as JSON frames with the agent ID."""
    socket = FakeSocket()
    manager = HeartbeatManager(agent_id="agent-1", interval=0.02)

    await manager.start(socket.send)
    try:
        for _ in range(10):
            await asyncio.sleep(0.02)
            if len(socket.sent) >= 3:
                break
    finally:
        await manager.stop()

    assert len(socket.sent) >= 3
    payload = json.loads(socket.sent[0])
    assert payload["msgType"] == "heartbeat"
    assert payload["agentId"] == "agent-1"
    assert isinstance(payload["timestamp"], int)


async def test_connection_closed_triggers_timeout(no_first_delay: None) -> None:
    """A closed socket fires the timeout callback exactly once and stops."""
    socket = FakeSocket(failures=[make_closed_error()])
    timeouts = []

    async def on_timeout() -> None:
        timeouts.append(1)

    manager = HeartbeatManager(
        agent_id="agent-1", interval=0.02, on_timeout=on_timeout
    )
    await manager.start(socket.send)
    await asyncio.sleep(0.1)
    await manager.stop()

    assert timeouts == [1]
    assert manager.is_running is False


async def test_transient_failures_are_tolerated(no_first_delay: None) -> None:
    """A couple of transient send errors do not kill the heartbeat."""
    socket = FakeSocket(failures=[OSError("boom"), OSError("boom")])
    timeouts = []

    async def on_timeout() -> None:
        timeouts.append(1)

    manager = HeartbeatManager(
        agent_id="agent-1", interval=0.02, on_timeout=on_timeout
    )
    await manager.start(socket.send)
    await asyncio.sleep(0.2)
    await manager.stop()

    assert timeouts == []
    assert len(socket.sent) >= 1


async def test_repeated_fatal_failures_log_once(
    no_first_delay: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The throttled logger keeps a reconnect loop from flooding the log."""
    logger = logging.getLogger("custom_components.hass_xiaoyi_bridge.a2a.heartbeat")
    throttle = ThrottledLogger(logger, cooldown=300)

    for _ in range(3):
        socket = FakeSocket(failures=[make_closed_error()])
        manager = HeartbeatManager(
            agent_id="agent-1",
            interval=0.02,
            throttle=throttle,
            server_id="server1",
        )
        await manager.start(socket.send)
        await asyncio.sleep(0.05)
        await manager.stop()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Heartbeat send failed" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "connection lost" in warnings[0].getMessage()
    assert throttle.suppressed("server1/heartbeat_send") == 2


async def test_heartbeat_refuses_double_start(caplog) -> None:
    """Starting an already running heartbeat is a no-op."""
    socket = FakeSocket()
    manager = HeartbeatManager(agent_id="agent-1", interval=0.02)
    await manager.start(socket.send)
    try:
        with caplog.at_level(logging.WARNING):
            await manager.start(socket.send)
        assert "already running" in caplog.text
    finally:
        await manager.stop()


async def test_heartbeat_stop_is_idempotent(no_first_delay: None) -> None:
    """Stopping a stopped heartbeat does nothing harmful."""
    manager = HeartbeatManager(agent_id="agent-1", interval=0.02)
    await manager.start(FakeSocket().send)
    await manager.stop()
    await manager.stop()
    assert manager.is_running is False


async def test_first_heartbeat_does_not_wait_for_the_interval() -> None:
    """The first beat goes out ~1s after connect, not one interval later.

    This is the fix for the log flood: the XiaoYi server drops sockets that see
    no application traffic for ~20s, so waiting a full interval (the old 30s)
    guaranteed a close/reconnect loop. The fixture-free timing here is the
    regression guard — patching HEARTBEAT_FIRST_DELAY would hide the bug again.
    """
    socket = FakeSocket()
    manager = HeartbeatManager(agent_id="agent-1", interval=30)

    await manager.start(socket.send)
    try:
        for _ in range(40):  # 40 * 0.05s = 2s budget
            if socket.sent:
                break
            await asyncio.sleep(0.05)
        assert socket.sent, "first heartbeat waited for the full interval"
    finally:
        await manager.stop()


def test_default_interval_matches_the_server_idle_window() -> None:
    """The default cadence is part of the fix: 20s, not the old 30s.

    The XiaoYi server closes sockets that stay silent for ~20s, so a 30s
    interval recreates the connect/close/reconnect loop that flooded the log.
    """
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import XiaoYiWSConfig
    from custom_components.hass_xiaoyi_bridge.const import DEFAULT_HEARTBEAT_INTERVAL

    assert DEFAULT_HEARTBEAT_INTERVAL == 20
    config = XiaoYiWSConfig(
        ws_url1="wss://a", ws_url2="wss://b", ak="ak", sk="sk", agent_id="agent"
    )
    assert config.heartbeat_interval == 20.0
