"""Heartbeat management for A2A WebSocket connections.

Sends periodic application-level heartbeat messages to keep the XiaoYi A2A
WebSocket connection alive and detects connection loss via send failures.

Liveness model
-------------
The XiaoYi A2A server does NOT answer the JSON ``heartbeat`` message with a
WebSocket pong frame (pong only answers a protocol-level WS ping). We therefore
treat a *successful send* as proof the connection is alive:

* ``ws.send()`` succeeds  → connection alive (reset failure counter)
* ``ws.send()`` raises ``ConnectionClosed`` → connection dead → reconnect
* ``ws.send()`` raises a transient error → tolerated up to ``max_failures``

Cadence
-------
The first heartbeat is sent shortly after the connection is established
(``HEARTBEAT_FIRST_DELAY``) instead of after a full interval: the XiaoYi
server closes long-lived connections that see no application-level traffic,
and a 30s first beat arrives too late to keep the socket open. The default
interval of 20s matches the XiaoYi OpenClaw channel reference implementation.

Protocol-level liveness (half-open / NAT-timeout detection) is deliberately
NOT configured: the reference implementation does not ping either, and a
ping/pong mismatch with the server's middleboxes shows up as spurious
disconnects. A failed application heartbeat detects a dead socket just as
well, one interval later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from websockets.exceptions import ConnectionClosed

from ..const import HEARTBEAT_FIRST_DELAY, HEARTBEAT_MAX_FAILURES
from .log_throttle import ThrottledLogger

_LOGGER = logging.getLogger(__name__)

__all__ = ["HeartbeatManager"]


class HeartbeatManager:
    """Manages periodic heartbeat messages for a WebSocket connection."""

    def __init__(
        self,
        agent_id: str,
        interval: float = 20.0,
        on_timeout: Callable[[BaseException | None], Awaitable[None]] | None = None,
        first_delay: float | None = None,
        on_success: Callable[[], Awaitable[None]] | None = None,
        throttle: ThrottledLogger | None = None,
        server_id: str = "server",
    ) -> None:
        """Initialize heartbeat manager.

        Args:
            agent_id: XiaoYi Agent ID for heartbeat messages.
            interval: Heartbeat interval in seconds.
            on_timeout: Async callback when the connection is declared dead;
                it receives the exception that killed the socket (if any).
            first_delay: Seconds to wait before the first heartbeat;
                defaults to ``HEARTBEAT_FIRST_DELAY`` (read at call time).
            on_success: Async callback on successful heartbeat.
            throttle: Rate-limited logger; repeated failures are suppressed.
            server_id: Server identifier used in log messages.
        """
        self.agent_id = agent_id
        self.interval = interval
        self.on_timeout = on_timeout
        # Read the module constant at call time so tests/config can override it.
        self.first_delay = HEARTBEAT_FIRST_DELAY if first_delay is None else first_delay
        self.on_success = on_success
        self._throttle = throttle or ThrottledLogger(_LOGGER)
        self._server_id = server_id
        self._task: asyncio.Task | None = None
        self._running = False
        # Consecutive transient send failures tolerated before declaring the
        # connection dead. A single ConnectionClosed is always fatal.
        self._consecutive_failures = 0
        self._max_failures = HEARTBEAT_MAX_FAILURES
        self.last_success: float = 0.0

    @property
    def is_running(self) -> bool:
        """Check if heartbeat is currently running."""
        return self._running

    def _build_heartbeat_message(self) -> str:
        """Build a heartbeat message."""
        return json.dumps({
            "msgType": "heartbeat",
            "agentId": self.agent_id,
            "timestamp": int(time.time() * 1000),
        })

    async def start(self, ws_send: Callable[[str], Awaitable[None]]) -> None:
        """Start the heartbeat loop.

        Args:
            ws_send: Async callable that sends a string message through the WebSocket.
        """
        if self._running:
            _LOGGER.warning("Heartbeat already running, skipping start")
            return

        self._running = True
        self._consecutive_failures = 0
        self._task = asyncio.create_task(self._heartbeat_loop(ws_send))
        _LOGGER.debug(
            "[%s] Heartbeat started (interval=%.0fs)", self._server_id, self.interval
        )

    async def _heartbeat_loop(self, ws_send: Callable[[str], Awaitable[None]]) -> None:
        """Main heartbeat loop that sends periodic messages."""
        # First beat soon after connect so the server sees traffic early.
        await asyncio.sleep(self.first_delay)

        try:
            while self._running:
                try:
                    message = self._build_heartbeat_message()
                    await ws_send(message)
                    # Successful send = connection alive
                    self._consecutive_failures = 0
                    self.last_success = time.monotonic()
                    _LOGGER.debug("[%s] Heartbeat sent", self._server_id)

                    if self.on_success:
                        await self.on_success()

                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    # ConnectionClosed (and subclasses) is fatal: the socket
                    # is gone. Transient errors are tolerated up to a limit.
                    is_fatal = isinstance(err, ConnectionClosed)
                    self._consecutive_failures += 1
                    if is_fatal or self._consecutive_failures >= self._max_failures:
                        # Rate-limited: a reconnect loop must not turn into
                        # hundreds of identical log entries.
                        self._throttle.warning(
                            f"{self._server_id}/heartbeat_send",
                            "[%s] Heartbeat send failed (%s): connection lost",
                            self._server_id,
                            err,
                        )
                        if self.on_timeout:
                            await self.on_timeout(err)
                        return  # Stop heartbeat; reconnect starts a new one
                    _LOGGER.debug(
                        "[%s] Heartbeat send failed (transient %d/%d): %s",
                        self._server_id,
                        self._consecutive_failures,
                        self._max_failures,
                        err,
                    )

                await asyncio.sleep(self.interval)

        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Heartbeat loop cancelled", self._server_id)
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
