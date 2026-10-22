"""A2A WebSocket connection manager for XiaoYi protocol.

Manages dual WebSocket connections to XiaoYi A2A servers with:
- Automatic reconnection with exponential backoff + jitter (never gives up
  permanently: after ``reconnect_max`` attempts it cools down and resumes)
- Session-to-server routing for responses
- Heartbeat monitoring (application-level, first beat right after connect)
- Support for clearContext and tasks/cancel control messages
- Delayed session cleanup after clearContext
- Per-session serial processing of incoming requests

Logging discipline
------------------
A flapping remote server used to produce one WARNING per failed heartbeat and
per reconnect attempt — thousands of identical lines. Every recurring
condition now goes through :class:`~a2a.log_throttle.ThrottledLogger`:
the first occurrence is logged in full, repeats are suppressed for
``LOG_THROTTLE_COOLDOWN`` seconds, and a single summary line is emitted when
the window elapses.

Failure escalation
------------------
Three consecutive "connected, then closed by the server within
``WS_FAST_CLOSE_WINDOW`` seconds" events are treated as an authentication /
configuration problem (invalid AK/SK, unpublished agent) rather than a
network problem: an actionable error is logged once, the ``on_auth_failure``
callback fires (HA shows a reauth card), and reconnects pause for
``WS_AUTH_BACKOFF`` seconds instead of hammering the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..const import (
    A2A_MSG_TYPE_INIT,
    LOG_THROTTLE_COOLDOWN,
    SESSION_CLEANUP_TIMEOUT,
    SHUTDOWN_TASK_GRACE,
    WS_AUTH_BACKOFF,
    WS_CONNECT_TIMEOUT,
    WS_FAST_CLOSE_THRESHOLD,
    WS_FAST_CLOSE_WINDOW,
    WS_GIVE_UP_COOLDOWN,
    WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_JITTER,
    WS_RECONNECT_MAX_DELAY,
    WS_RESUME_GRACE,
    WS_STABLE_THRESHOLD,
)
from .auth import XiaoYiAuth
from .heartbeat import HeartbeatManager
from .log_throttle import ThrottledLogger
from .message import (
    build_a2a_text_response,
    build_clear_context_response,
    build_status_update,
    build_tasks_cancel_response,
    extract_session_id,
    is_a2a_request_message,
    is_clear_context_message,
    is_tasks_cancel_message,
    parse_a2a_message,
)
from .session import SessionManager

_LOGGER = logging.getLogger(__name__)

__all__ = ["XiaoYiAuthError", "XiaoYiWSConfig", "XiaoYiWebSocketManager"]


def _describe_rejection(response: Any) -> str:
    """Summarise the body of a refused WebSocket upgrade.

    The 小艺 gateway returns JSON such as
    ``{"code":"badRequest","desc":"Resource is not found or param error"}``.
    Returning that text verbatim is the only way a user can tell a rejected
    credential from an unpublished agent, because the status code is the same
    for every rejection.

    Args:
        response: The ``Response`` attached to ``websockets.InvalidStatus``.

    Returns:
        A short ``code: description`` string, or an empty string.
    """
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        return ""

    text = bytes(body).decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:200]
    if not isinstance(parsed, dict):
        return text[:200]

    code = parsed.get("code") or parsed.get("error") or ""
    desc = parsed.get("desc") or parsed.get("message") or ""
    if code and desc:
        return f"{code}: {desc}"
    return str(code or desc)[:200]

# Type aliases
ServerId = str  # "server1" | "server2"
MessageCallback = Callable[[Any], Awaitable[None]]
ClearCallback = Callable[[str], Awaitable[None]]  # session_id
CancelCallback = Callable[[str, str], Awaitable[None]]  # session_id, task_id
StateCallback = Callable[[], None]  # sync listener for state changes


class XiaoYiAuthError(Exception):
    """Raised when the XiaoYi server rejects the handshake.

    The gateway answers **every** rejected upgrade with HTTP 401 and the same
    body (``{"code":"badRequest","desc":"Resource is not found or param
    error"}``), whether the AK/SK pair is wrong, the agent is not published or
    the credential was created for the wrong platform. The status and the raw
    server description are therefore carried along so the logs (and the config
    flow) can show what the server actually said.

    Attributes:
        status: HTTP status code of the refused upgrade, if any.
        detail: Server-provided code/description, if any.
        server_id: "server1" / "server2" that refused the connection.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: str | None = None,
        server_id: str | None = None,
    ) -> None:
        """Initialize the error and keep the server response for diagnostics."""
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.server_id = server_id


@dataclass
class ServerState:
    """Connection state for a single server."""

    connected: bool = False
    ready: bool = False
    last_heartbeat: float = 0.0
    reconnect_attempts: int = 0
    # Monotonic timestamp of the last successful connect (0 = never).
    connected_at: float = 0.0
    # Consecutive "connected then closed almost immediately" events.
    fast_close_count: int = 0


@dataclass
class XiaoYiWSConfig:
    """Resolved WebSocket configuration."""

    ws_url1: str
    ws_url2: str
    ak: str
    sk: str
    agent_id: str
    reconnect_max: int = 50
    heartbeat_interval: float = 20.0
    heartbeat_first_delay: float = 1.0
    single_server: bool = False
    skip_init: bool = False


class XiaoYiWebSocketManager:
    """Manages dual WebSocket connections to XiaoYi A2A servers.

    Follows the same dual-server architecture as the XiaoYi OpenClaw channel
    plugin: both servers are connected simultaneously for high availability.
    Responses are routed back to the server that originated the session's
    messages.

    Args:
        config: WebSocket configuration.
        session_mgr: Session manager for conversation ID mapping.
        on_message: Callback for A2A message/stream requests.
        on_clear: Callback for clearContext requests.
        on_cancel: Callback for tasks/cancel requests.
        on_auth_failure: Callback when credentials look invalid (reauth).
    """

    def __init__(
        self,
        config: XiaoYiWSConfig,
        session_mgr: SessionManager,
        on_message: MessageCallback,
        on_clear: ClearCallback,
        on_cancel: CancelCallback,
        on_auth_failure: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the WebSocket manager."""
        self.config = config
        self.auth = XiaoYiAuth(config.ak, config.sk, config.agent_id)
        self.session_mgr = session_mgr
        self.on_message = on_message
        self.on_clear = on_clear
        self.on_cancel = on_cancel
        self.on_auth_failure = on_auth_failure

        # WebSocket connections
        self._ws1: Any = None
        self._ws2: Any = None

        # Connection states
        self._state1 = ServerState()
        self._state2 = ServerState()

        # Heartbeat managers
        self._heartbeat1: HeartbeatManager | None = None
        self._heartbeat2: HeartbeatManager | None = None

        # Session → server mapping
        self._session_server_map: dict[str, ServerId] = {}

        # Per-session locks: messages of one session are processed in order,
        # different sessions still run concurrently.
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Delayed session cleanup after clearContext: session_id → task
        self._cleanup_tasks: dict[str, asyncio.Task] = {}

        # Background tasks
        self._receive_task1: asyncio.Task | None = None
        self._receive_task2: asyncio.Task | None = None
        self._reconnect_task1: asyncio.Task | None = None
        self._reconnect_task2: asyncio.Task | None = None
        self._stable_timer1: asyncio.Task | None = None
        self._stable_timer2: asyncio.Task | None = None
        self._cooldown_task1: asyncio.Task | None = None
        self._cooldown_task2: asyncio.Task | None = None

        # In-flight message processing tasks (awaited on shutdown)
        self._processing_tasks: set[asyncio.Task] = set()

        # Running flag
        self._running = False

        # Monotonic deadline until which reconnects are paused (auth failure)
        self._auth_pause_until: dict[ServerId, float] = {}

        # Servers currently being torn down deliberately: the receive loop
        # must not schedule a reconnect when it is cancelled for that reason.
        self._no_reconnect: set[ServerId] = set()

        # Servers this manager is allowed to keep connected (see single_server).
        self._active_servers: set[ServerId] = {"server1", "server2"}

        # Exact connect time per server; kept until the socket is torn down so
        # the close log can measure the real lifetime.
        self._connected_at: dict[ServerId, float] = {}
        # When clawd_bot_init went out, to correlate it with an early close.
        self._init_sent_at: dict[ServerId, float] = {}

        # Last close code/reason per server (surfaced in diagnostics).
        self._last_close: dict[ServerId, dict[str, Any]] = {}

        # Servers whose certificate chain could not be verified (IP endpoints).
        # Remembered so a reconnect does not repeat the doomed verified attempt.
        self._tls_degraded: set[ServerId] = set()

        # Rate-limited logger shared by every recurring message
        self._throttle = ThrottledLogger(_LOGGER, LOG_THROTTLE_COOLDOWN)

        # State-change listeners (used by the binary_sensor platform)
        self._listeners: list[StateCallback] = []

    # ─── Public Interface ─────────────────────────────────────────────────

    def add_listener(self, callback: StateCallback) -> Callable[[], None]:
        """Register a callback fired on connection state changes.

        Args:
            callback: Zero-argument callable invoked from the event loop.

        Returns:
            A function that removes the listener again.
        """
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _notify_listeners(self) -> None:
        """Notify all state-change listeners."""
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Error in state change listener")

    async def connect(self) -> None:
        """Connect to both WebSocket servers.

        At least one connection must succeed.

        Raises:
            XiaoYiAuthError: The server rejected the credentials (401/403).
            ConnectionError: Both servers are unreachable.
        """
        _LOGGER.info("Connecting to XiaoYi A2A servers...")
        _LOGGER.info("  Server 1: %s", self.config.ws_url1)
        _LOGGER.info("  Server 2: %s", self.config.ws_url2)

        self._running = True

        if self.config.single_server:
            await self._connect_single_server()
            return

        self._active_servers = {"server1", "server2"}

        results = await asyncio.gather(
            self._connect_server(self.config.ws_url1, "server1"),
            self._connect_server(self.config.ws_url2, "server2"),
            return_exceptions=True,
        )

        server1_ok = not isinstance(results[0], Exception)
        server2_ok = not isinstance(results[1], Exception)

        # An auth rejection is fatal for the whole integration, even if the
        # other server happens to answer.
        for result in results:
            if isinstance(result, XiaoYiAuthError):
                raise result

        if not server1_ok and not server2_ok:
            _LOGGER.error("Failed to connect to both servers")
            raise ConnectionError("Failed to connect to both XiaoYi servers")

        # Retry the servers that failed at startup, otherwise a partial
        # outage would leave the bridge on one leg forever. (Auth failures
        # have already raised above.)
        if not server1_ok:
            self._schedule_reconnect("server1")
        if not server2_ok:
            self._schedule_reconnect("server2")

        _LOGGER.info(
            "Connection results: Server1=%s, Server2=%s",
            "OK" if server1_ok else "FAILED",
            "OK" if server2_ok else "FAILED",
        )
        if not (server1_ok and server2_ok):
            self._throttle.warning(
                "partial_connect",
                "Only one XiaoYi server is reachable; running degraded",
            )
        self._notify_listeners()

    async def _connect_single_server(self) -> None:
        """Connect to the primary endpoint only, falling back to the backup.

        Useful when the platform allows a single session per agent: holding two
        connections then makes each one kick the other. A failed primary is
        still retried periodically (the fallback stays active in the meantime).

        Raises:
            XiaoYiAuthError: The server rejected the credentials (401/403).
            ConnectionError: Neither endpoint is reachable.
        """
        self._active_servers = {"server1"}
        try:
            await self._connect_server(self.config.ws_url1, "server1")
        except XiaoYiAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - fall back to the backup URL
            self._throttle.warning(
                "single_primary_failed",
                "Primary XiaoYi endpoint is unreachable (%s); trying the backup",
                err,
            )
            self._active_servers = {"server2"}
            await self._connect_server(self.config.ws_url2, "server2")

        _LOGGER.info("Connected in single-server mode: %s", self._active_servers)
        self._notify_listeners()

    async def disconnect(self) -> None:
        """Disconnect from all servers and stop all tasks."""
        _LOGGER.info("Disconnecting from all servers...")
        self._running = False

        # Stop heartbeats
        for heartbeat in (self._heartbeat1, self._heartbeat2):
            if heartbeat:
                await heartbeat.stop()
        self._heartbeat1 = None
        self._heartbeat2 = None

        # Cancel receive tasks
        for task in (self._receive_task1, self._receive_task2):
            if task and not task.done():
                task.cancel()
        self._receive_task1 = None
        self._receive_task2 = None

        # Cancel reconnect tasks
        for task in (
            self._reconnect_task1,
            self._reconnect_task2,
            self._cooldown_task1,
            self._cooldown_task2,
        ):
            if task and not task.done():
                task.cancel()
        self._reconnect_task1 = None
        self._reconnect_task2 = None
        self._cooldown_task1 = None
        self._cooldown_task2 = None

        # Cancel stable connection timers
        for task in (self._stable_timer1, self._stable_timer2):
            if task and not task.done():
                task.cancel()
        self._stable_timer1 = None
        self._stable_timer2 = None

        # Cancel delayed session cleanups
        for task in list(self._cleanup_tasks.values()):
            if not task.done():
                task.cancel()
        self._cleanup_tasks.clear()

        # Stop in-flight message processing. Waiting for it unbounded would
        # block config-entry unloading (and therefore options reloads and
        # restarts) for up to CONVERSE_TIMEOUT seconds, so cancel first and
        # give the tasks a short grace period to unwind.
        if self._processing_tasks:
            processing = list(self._processing_tasks)
            for task in processing:
                task.cancel()
            await asyncio.wait(processing, timeout=SHUTDOWN_TASK_GRACE)
        self._processing_tasks.clear()

        # Close WebSocket connections
        for ws in (self._ws1, self._ws2):
            if ws:
                try:
                    await ws.close()
                except Exception as err:
                    _LOGGER.debug("Error closing WebSocket: %s", err)

        self._ws1 = None
        self._ws2 = None
        self._state1 = ServerState()
        self._state2 = ServerState()
        self._session_server_map.clear()
        self._session_locks.clear()
        self._auth_pause_until.clear()
        self._no_reconnect.clear()
        self._tls_degraded.clear()
        self._last_close.clear()
        self._connected_at.clear()
        self._init_sent_at.clear()
        self._active_servers = {"server1", "server2"}
        self._throttle.reset()
        _LOGGER.info("Disconnected from all servers")
        self._notify_listeners()

    async def reconnect_now(self) -> None:
        """Force a fresh connection to both servers (used by the service)."""
        _LOGGER.info("Reconnecting both XiaoYi servers on request")
        await self._disconnect_server("server1")
        await self._disconnect_server("server2")
        await self.connect()

    async def send_response(
        self,
        session_id: str,
        task_id: str,
        message_id: str,
        text: str,
    ) -> None:
        """Send an A2A text response to the appropriate server.

        Routes the response to the server that originated the session's messages.

        Args:
            session_id: A2A session ID.
            task_id: A2A task ID.
            message_id: Original request message ID.
            text: Response text content.
        """
        message = build_a2a_text_response(
            agent_id=self.config.agent_id,
            session_id=session_id,
            task_id=task_id,
            message_id=message_id,
            text=text,
        )
        await self._send_to_session(session_id, message)

    async def send_status_update(
        self,
        session_id: str,
        task_id: str,
        text: str,
        state: str = "working",
        message_id: str | None = None,
    ) -> None:
        """Send an A2A status-update message.

        Args:
            session_id: A2A session ID.
            task_id: A2A task ID.
            text: Status message text.
            state: Status state (e.g., "working", "completed", "failed").
            message_id: JSON-RPC id of the original request, echoed back so
                小艺 can correlate the status frame with its request.
        """
        message = build_status_update(
            agent_id=self.config.agent_id,
            session_id=session_id,
            task_id=task_id,
            text=text,
            state=state,
            message_id=message_id,
        )
        await self._send_to_session(session_id, message)

    async def send_clear_context_response(
        self, session_id: str, request_id: str, success: bool = True
    ) -> None:
        """Send a clearContext response.

        Args:
            session_id: A2A session ID.
            request_id: Original clearContext request ID.
            success: Whether the clear operation succeeded.
        """
        server_id = self._session_server_map.get(session_id)
        message = build_clear_context_response(
            agent_id=self.config.agent_id,
            session_id=session_id,
            request_id=request_id,
            success=success,
        )
        if server_id:
            await self._send_to_server(server_id, message)

    async def send_tasks_cancel_response(
        self, session_id: str, request_id: str, success: bool = True
    ) -> None:
        """Send a tasks/cancel response.

        Args:
            session_id: A2A session ID.
            request_id: Original tasks/cancel request ID.
            success: Whether the cancel operation succeeded.
        """
        server_id = self._session_server_map.get(session_id)
        message = build_tasks_cancel_response(
            agent_id=self.config.agent_id,
            session_id=session_id,
            request_id=request_id,
            success=success,
        )
        if server_id:
            await self._send_to_server(server_id, message)

    @property
    def is_ready(self) -> bool:
        """Check if at least one server is connected and ready."""
        return (
            (self._state1.ready and self._ws1 is not None)
            or (self._state2.ready and self._ws2 is not None)
        )

    def get_debug_state(self) -> dict[str, Any]:
        """Return a snapshot of the connection state for diagnostics."""
        now = time.monotonic()

        def _server(server_id: ServerId, state: ServerState) -> dict[str, Any]:
            return {
                "connected": state.connected,
                "ready": state.ready,
                "reconnect_attempts": state.reconnect_attempts,
                "seconds_since_connect": (
                    round(now - state.connected_at, 1) if state.connected_at else None
                ),
                "last_close": self._last_close.get(server_id),
            }

        return {
            "ready": self.is_ready,
            "server1": _server("server1", self._state1),
            "server2": _server("server2", self._state2),
            "sessions": self.session_mgr.debug_state(),
            "suppressed_log_messages": self._throttle.suppressed_total(),
        }

    # ─── Connection Management ────────────────────────────────────────────

    async def _connect_server(self, url: str, server_id: ServerId) -> None:
        """Connect to a single server.

        Args:
            url: WebSocket server URL.
            server_id: Server identifier ("server1" or "server2").

        Raises:
            XiaoYiAuthError: The server rejected the credentials (401/403).
            ConnectionError: The server is unreachable.
        """
        _LOGGER.info("[%s] Connecting to %s...", server_id, url)

        # Close any stale connection before creating a new one.
        await self._disconnect_server(server_id)

        try:
            # Generate auth headers
            auth_headers = self.auth.generate_auth_headers()

            # Configure the TLS context. It must stay None for ws:// URIs:
            # websockets refuses an ``ssl`` argument for plaintext endpoints
            # ("ssl argument is incompatible with a ws:// URI").
            # NOTE: creating a context is a BLOCKING call (it loads the CA
            # bundle), so it is offloaded with asyncio.to_thread().
            ssl_context: ssl.SSLContext | None = None
            ip_endpoint = False
            if url.startswith("wss"):
                ip_endpoint = self._is_wss_with_ip(url)
                if ip_endpoint and server_id in self._tls_degraded:
                    # Seen before: do not redo the doomed verified handshake.
                    ssl_context = await asyncio.to_thread(
                        self._create_unverified_ssl_context
                    )
                else:
                    context_factory = (
                        self._create_ip_ssl_context
                        if ip_endpoint
                        else ssl.create_default_context
                    )
                    ssl_context = await asyncio.to_thread(context_factory)

            async def _open(ctx: ssl.SSLContext | None) -> Any:
                """Open the WebSocket connection with the given TLS context."""
                return await websockets.connect(
                    url,
                    additional_headers=auth_headers,
                    ssl=ctx,
                    open_timeout=WS_CONNECT_TIMEOUT,
                    ping_interval=None,
                )

            try:
                ws = await _open(ssl_context)
            except ssl.SSLCertVerificationError as err:
                # An IP endpoint can never match the certificate SAN, but the
                # chain itself is normally verifiable. Only when it is NOT
                # (self-signed / wrong CA) do we degrade, loudly and once.
                if not ip_endpoint:
                    raise
                self._throttle.warning(
                    f"{server_id}/tls_unverified",
                    "[%s] The certificate of %s could not be verified (%s); "
                    "continuing WITHOUT TLS verification for this endpoint",
                    server_id,
                    url,
                    err,
                )
                ssl_context = await asyncio.to_thread(
                    self._create_unverified_ssl_context
                )
                ws = await _open(ssl_context)
                self._tls_degraded.add(server_id)
            except InvalidStatus as err:
                status = getattr(err.response, "status_code", None)
                detail = _describe_rejection(getattr(err, "response", None))
                suffix = f": {detail}" if detail else ""
                if status in (401, 403):
                    raise XiaoYiAuthError(
                        f"{server_id} rejected the handshake "
                        f"(HTTP {status}{suffix})",
                        status=status,
                        detail=detail,
                        server_id=server_id,
                    ) from err
                raise ConnectionError(
                    f"{server_id} rejected the connection (HTTP {status}{suffix})"
                ) from err

            # Store connection. The reconnect and fast-close counters are
            # preserved across (re)connects: resetting them here would make
            # the backoff and the auth-failure escalation never trigger.
            self._connected_at[server_id] = time.monotonic()
            if server_id == "server1":
                self._ws1 = ws
                self._state1.connected = True
                self._state1.ready = True
                self._state1.connected_at = self._connected_at[server_id]
            else:
                self._ws2 = ws
                self._state2.connected = True
                self._state2.ready = True
                self._state2.connected_at = self._connected_at[server_id]

            # A fresh connection clears any prior auth pause for this server.
            self._auth_pause_until.pop(server_id, None)
            # …and makes a pending cooldown/resume task obsolete.
            pending_resume = self._cooldown_task(server_id)
            if pending_resume is not None and not pending_resume.done():
                pending_resume.cancel()
                self._set_cooldown_task(server_id, None)

            self._throttle.info(
                f"{server_id}/connected",
                "[%s] Connected successfully",
                server_id,
            )

            # Send init message
            if self.config.skip_init:
                self._throttle.warning(
                    f"{server_id}/init_skipped",
                    "[%s] Diagnostic mode: clawd_bot_init is NOT sent "
                    "(skip_init option is on)",
                    server_id,
                )
            else:
                await ws.send(json.dumps({
                    "msgType": A2A_MSG_TYPE_INIT,
                    "agentId": self.config.agent_id,
                }))
                self._init_sent_at[server_id] = time.monotonic()
                _LOGGER.info("[%s] Sent clawd_bot_init message", server_id)

            # Start heartbeat. The first beat fires after
            # HEARTBEAT_FIRST_DELAY so the server sees application traffic
            # well inside its idle window.
            heartbeat = HeartbeatManager(
                agent_id=self.config.agent_id,
                interval=self.config.heartbeat_interval,
                first_delay=self.config.heartbeat_first_delay,
                on_timeout=lambda err=None, sid=server_id: self._on_heartbeat_timeout(sid, err),
                throttle=self._throttle,
                server_id=server_id,
            )
            await heartbeat.start(ws.send)

            if server_id == "server1":
                self._heartbeat1 = heartbeat
            else:
                self._heartbeat2 = heartbeat

            # Start receive loop
            receive_task = asyncio.create_task(
                self._receive_loop(ws, server_id)
            )
            if server_id == "server1":
                self._receive_task1 = receive_task
            else:
                self._receive_task2 = receive_task

            # Schedule stable connection check
            stable_task = asyncio.create_task(
                self._schedule_stable_connection_check(server_id)
            )
            if server_id == "server1":
                self._stable_timer1 = stable_task
            else:
                self._stable_timer2 = stable_task

            self._notify_listeners()

        except XiaoYiAuthError:
            if server_id == "server1":
                self._state1 = ServerState()
            else:
                self._state2 = ServerState()
            self._notify_listeners()
            raise
        except Exception as err:
            self._throttle.error(
                f"{server_id}/connect_failed",
                "[%s] Connection failed: %s",
                server_id,
                err,
            )
            if server_id == "server1":
                self._state1.connected = False
                self._state1.ready = False
            else:
                self._state2.connected = False
                self._state2.ready = False
            self._notify_listeners()
            raise

    async def _disconnect_server(self, server_id: ServerId) -> None:
        """Disconnect from a specific server.

        Cancels the receive task *before* closing the socket so the receive
        loop's ``finally`` block (which would otherwise schedule a reconnect)
        is suppressed via ``CancelledError``. This prevents the duplicate
        reconnect that caused the reconnect storm.

        Args:
            server_id: Server identifier.
        """
        # The receive loop cancelled below must not schedule a reconnect.
        self._no_reconnect.add(server_id)
        try:
            ws = self._ws1 if server_id == "server1" else self._ws2
            heartbeat = (
                self._heartbeat1 if server_id == "server1" else self._heartbeat2
            )
            receive_task = (
                self._receive_task1 if server_id == "server1" else self._receive_task2
            )
            stable_task = (
                self._stable_timer1 if server_id == "server1" else self._stable_timer2
            )

            # Cancel the receive task first so its finally block does not fire a
            # duplicate _schedule_reconnect. CancelledError is caught in the loop.
            if receive_task and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except asyncio.CancelledError:
                    _LOGGER.debug("[%s] Receive task cancelled", server_id)
                except Exception as err:  # noqa: BLE001 - teardown must not fail
                    _LOGGER.debug("[%s] Receive task ended: %s", server_id, err)

            if stable_task and not stable_task.done():
                stable_task.cancel()

            if heartbeat:
                await heartbeat.stop()

            if ws:
                try:
                    await ws.close()
                except Exception as err:
                    _LOGGER.debug("[%s] Error closing connection: %s", server_id, err)

            if server_id == "server1":
                self._ws1 = None
                # Preserve reconnect_attempts / fast_close_count: they must
                # survive teardown so the backoff grows and repeated fast
                # closes are detected.
                self._state1.connected = False
                self._state1.ready = False
                self._state1.connected_at = 0.0
                self._connected_at.pop("server1", None)
                self._init_sent_at.pop("server1", None)
                self._heartbeat1 = None
                self._receive_task1 = None
                self._stable_timer1 = None
            else:
                self._ws2 = None
                self._state2.connected = False
                self._state2.ready = False
                self._state2.connected_at = 0.0
                self._connected_at.pop("server2", None)
                self._init_sent_at.pop("server2", None)
                self._heartbeat2 = None
                self._receive_task2 = None
                self._stable_timer2 = None
        finally:
            self._no_reconnect.discard(server_id)

    async def _on_heartbeat_timeout(
        self, server_id: ServerId, err: BaseException | None = None
    ) -> None:
        """Handle heartbeat timeout by reconnecting.

        Args:
            server_id: Server that timed out.
            err: Exception raised by the failing heartbeat send, if any. The
                close code it carries is the only clue when the server closes
                the socket before the receive loop notices.
        """
        if isinstance(err, ConnectionClosed):
            # The socket died under us; record why before tearing it down.
            self._log_close(server_id, err)
        self._throttle.warning(
            f"{server_id}/heartbeat_timeout",
            "[%s] Heartbeat timeout, reconnecting...",
            server_id,
        )
        await self._disconnect_server(server_id)
        self._schedule_reconnect(server_id)

    # ─── Message Handling ─────────────────────────────────────────────────

    async def _receive_loop(self, ws: Any, server_id: ServerId) -> None:
        """Main receive loop for a server connection.

        Args:
            ws: WebSocket connection.
            server_id: Server identifier.
        """
        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                    await self._handle_message(message, server_id)
                except json.JSONDecodeError as err:
                    _LOGGER.error("[%s] Failed to parse message: %s", server_id, err)
                except Exception as err:
                    _LOGGER.error("[%s] Error handling message: %s", server_id, err)

        except ConnectionClosed as err:
            self._log_close(server_id, err)
        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Receive loop cancelled", server_id)
        except Exception as err:
            _LOGGER.error("[%s] Receive loop error: %s", server_id, err)
        finally:
            # Mark server as disconnected
            if server_id == "server1":
                self._state1.connected = False
                self._state1.ready = False
            else:
                self._state2.connected = False
                self._state2.ready = False

            # A connection that died almost immediately after init is a
            # strong hint of invalid credentials or an unpublished agent.
            self._register_connection_lifetime(server_id)
            self._notify_listeners()

            # Schedule reconnection if still running and this was not a
            # deliberate teardown (heartbeat timeout / reconnect / shutdown).
            if self._running and server_id not in self._no_reconnect:
                self._schedule_reconnect(server_id)

    def _register_connection_lifetime(self, server_id: ServerId) -> None:
        """Track how long the last connection lived; escalate fast failures.

        Only connections the *server* closed are counted. A deliberate teardown
        (heartbeat timeout, ``reconnect`` service, unload) says nothing about
        the credentials, and counting it would show a "re-authenticate" card to
        users who merely clicked reconnect a few times.

        Args:
            server_id: Server whose connection just ended.
        """
        state = self._state1 if server_id == "server1" else self._state2
        if not state.connected_at:
            return
        if server_id in self._no_reconnect:
            # Deliberate teardown: forget the start time so a later real close
            # is not measured from this connection.
            state.connected_at = 0.0
            return
        recorded = self._last_close.get(server_id, {}).get("lifetime")
        lifetime = recorded if recorded is not None else time.monotonic() - state.connected_at
        state.connected_at = 0.0

        if lifetime < WS_FAST_CLOSE_WINDOW:
            state.fast_close_count += 1
            _LOGGER.debug(
                "[%s] Connection closed after %.1fs (fast close %d/%d)",
                server_id,
                lifetime,
                state.fast_close_count,
                WS_FAST_CLOSE_THRESHOLD,
            )
            if state.fast_close_count >= WS_FAST_CLOSE_THRESHOLD:
                self._handle_fast_close(server_id)
        else:
            state.fast_close_count = 0

    def _log_close(self, server_id: ServerId, err: ConnectionClosed) -> None:
        """Report why a WebSocket ended, including the close code.

        The 小艺 gateway closes sessions gracefully (code 1000) for reasons that
        have nothing to do with the credentials — the agent being unpublished,
        the same agent already connected elsewhere, or rate limiting. The code
        and reason are the only way to tell those apart.

        Args:
            server_id: Server whose socket closed.
            err: The ``ConnectionClosed`` exception from websockets.
        """
        close = getattr(err, "rcvd", None)
        code = getattr(close, "code", None)
        reason = getattr(close, "reason", "") or ""
        now = time.monotonic()
        started = self._connected_at.get(server_id)
        lifetime = now - started if started else 0.0
        init_at = self._init_sent_at.get(server_id)
        init_offset = f"{init_at - started:.3f}s" if (init_at and started) else "—"
        self._last_close[server_id] = {
            "code": code,
            "reason": reason,
            "lifetime": round(lifetime, 3),
            "init_offset": init_offset,
        }
        self._throttle.warning(
            f"{server_id}/closed",
            "[%s] WebSocket closed by the server after %.3fs "
            "(code=%s, reason=%s, clawd_bot_init sent at +%s)",
            server_id,
            lifetime,
            code,
            reason or "-",
            init_offset,
        )

    def _handle_fast_close(self, server_id: ServerId) -> None:
        """Explain a connection that the server closes right after connect.

        This is deliberately **not** treated as an authentication failure: the
        handshake already succeeded, so the AK/SK are valid. Treating it as one
        showed a re-authenticate card to users whose credentials were fine.

        Args:
            server_id: Server that keeps closing the connection.
        """
        self._throttle.warning(
            f"{server_id}/fast_close",
            "[%s] The server closes the connection right after it is "
            "established (close code %s). The credentials are accepted, so "
            "this is about the agent, not the AK/SK. Check: (1) the agent is "
            "published and this account is on its test whitelist, (2) the same "
            "agent is not connected by another client (a second Home "
            "Assistant, the OpenClaw plugin, ...), (3) the platform is not "
            "rate limiting. Set the integration to debug and look for the "
            "close code/reason above.",
            server_id,
            self._last_close.get(server_id, {}).get("code"),
        )

    def _handle_auth_failure(self, server_id: ServerId) -> None:
        """Escalate repeated fast closes to an authentication failure.

        Logs one actionable error, asks HA to show the reauth card, and pauses
        reconnects for ``WS_AUTH_BACKOFF`` seconds so the server is not
        hammered while the user fixes the credentials.
        """
        if self._auth_pause_until.get(server_id):
            return  # already escalating

        self._throttle.error(
            f"{server_id}/auth_failure",
            "[%s] The XiaoYi server keeps closing the connection right after "
            "connect. This usually means the AK/SK credentials are invalid or "
            "the agent is not published yet. Check 小艺开放平台 → 凭证 / 上架, "
            "then re-authenticate this integration.",
            server_id,
        )
        deadline = time.monotonic() + WS_AUTH_BACKOFF
        self._auth_pause_until[server_id] = deadline

        # Arm an automatic resume. Without it the pause silently turns into a
        # permanent stop whenever the "invalid credentials" verdict was a false
        # positive (a network blip that closed the socket three times in a row
        # looks exactly like a rejected AK/SK).
        self._schedule_resume(
            server_id, max(deadline - time.monotonic(), 0.0) + WS_RESUME_GRACE
        )

        if self.on_auth_failure:
            try:
                self.on_auth_failure()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("Error in auth failure callback")

    async def _handle_message(
        self, message: dict[str, Any], source_server: ServerId
    ) -> None:
        """Route an incoming message to the appropriate handler.

        Args:
            message: Parsed JSON message.
            source_server: Server that sent the message.
        """
        _LOGGER.debug(
            "[%s] Received message: method=%s",
            source_server,
            message.get("method", message.get("action", "unknown")),
        )

        # Validate agentId
        if message.get("agentId") and message["agentId"] != self.config.agent_id:
            _LOGGER.warning(
                "[%s] Mismatched agentId: %s (expected: %s), discarding",
                source_server,
                message["agentId"],
                self.config.agent_id,
            )
            return

        # Extract and record session → server mapping
        session_id = extract_session_id(message)
        if session_id:
            self._session_server_map[session_id] = source_server
            _LOGGER.debug("[MAP] Session %s → %s", session_id, source_server)

        # Route to appropriate handler
        if is_a2a_request_message(message):
            parsed = parse_a2a_message(message)
            if parsed:
                await self._dispatch_request(parsed, source_server)
            else:
                _LOGGER.warning("[%s] Failed to parse A2A request", source_server)

        elif is_clear_context_message(message):
            if session_id:
                _LOGGER.info(
                    "[%s] clearContext for session %s", source_server, session_id
                )
                # Send response first
                request_id = message.get("id", "")
                await self.send_clear_context_response(session_id, request_id)
                # Then invoke callback
                try:
                    await self.on_clear(session_id)
                except Exception as err:
                    _LOGGER.error(
                        "[%s] Error in clear callback: %s", source_server, err
                    )
                # Delayed cleanup: an in-flight task may still answer.
                self._mark_session_for_cleanup(session_id)

        elif is_tasks_cancel_message(message):
            if session_id:
                task_id = message.get("taskId") or message.get("id", "")
                _LOGGER.info(
                    "[%s] tasks/cancel for session %s, task %s",
                    source_server,
                    session_id,
                    task_id,
                )
                request_id = message.get("id", "")
                await self.send_tasks_cancel_response(session_id, request_id)
                try:
                    await self.on_cancel(session_id, task_id)
                except Exception as err:
                    _LOGGER.error(
                        "[%s] Error in cancel callback: %s", source_server, err
                    )

        else:
            # 小艺 often sends one last frame explaining why it is about to
            # close; keeping this at debug made that frame invisible.
            self._throttle.warning(
                f"{source_server}/unknown_frame",
                "[%s] Ignoring an unrecognised frame: %s",
                source_server,
                json.dumps(message, ensure_ascii=False)[:300],
            )

    async def _dispatch_request(
        self, parsed: Any, source_server: ServerId
    ) -> None:
        """Process an A2A request in the background, serialized per session.

        The receive loop keeps reading while Assist works on the command;
        messages of the same session are processed in arrival order.

        Args:
            parsed: Parsed A2A request message.
            source_server: Server that delivered the request.
        """
        session_id = parsed.session_id
        # A message for a session pending cleanup revives that session.
        self._cancel_cleanup(session_id)

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async def _run() -> None:
            async with lock:
                try:
                    await self.on_message(parsed)
                except Exception as err:
                    _LOGGER.error(
                        "[%s] Error in message callback: %s", source_server, err
                    )

        task = asyncio.create_task(_run())
        self._processing_tasks.add(task)
        task.add_done_callback(self._processing_tasks.discard)

    # ─── Delayed session cleanup ──────────────────────────────────────────

    def _mark_session_for_cleanup(self, session_id: str) -> None:
        """Schedule delayed cleanup of a session after clearContext.

        Args:
            session_id: Session to clean up after SESSION_CLEANUP_TIMEOUT.
        """
        self._cancel_cleanup(session_id)

        async def _cleanup() -> None:
            await asyncio.sleep(SESSION_CLEANUP_TIMEOUT)
            self._session_server_map.pop(session_id, None)
            self._session_locks.pop(session_id, None)
            self._cleanup_tasks.pop(session_id, None)
            # Drop the conversation mapping too, otherwise the session table
            # (session_id → conversation_id/push_id) grows for the lifetime of
            # the process whenever 小艺 ends a conversation.
            self.session_mgr.remove_session(session_id)
            _LOGGER.debug("Session %s cleaned up", session_id)

        task = asyncio.create_task(_cleanup())
        self._cleanup_tasks[session_id] = task

    def _cancel_cleanup(self, session_id: str) -> None:
        """Cancel a pending session cleanup (the session became active again)."""
        task = self._cleanup_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    # ─── Sending ──────────────────────────────────────────────────────────

    async def _send_to_session(
        self, session_id: str, message: dict[str, Any]
    ) -> None:
        """Send a message to the server that owns a session.

        Args:
            session_id: A2A session ID.
            message: Complete outbound message dict.

        Raises:
            ConnectionError: If no server is available for the session.
        """
        server_id = self._session_server_map.get(session_id)
        if not server_id:
            _LOGGER.error("[ROUTE] Unknown server for session %s", session_id)
            raise ConnectionError(f"Cannot route response: unknown session {session_id}")
        if session_id in self._cleanup_tasks:
            # The user cleared this context; the reference implementation drops
            # replies for such sessions instead of reviving a conversation that
            # was just ended. A new request cancels the cleanup and revives it.
            _LOGGER.debug(
                "[ROUTE] Dropping %s for cleared session %s",
                message.get("msgType", "message"),
                session_id,
            )
            return
        await self._send_to_server(server_id, message)

    async def _send_to_server(
        self, server_id: ServerId, message: dict[str, Any]
    ) -> None:
        """Send a message to a specific server.

        Args:
            server_id: Target server identifier.
            message: Complete outbound message dict.

        Raises:
            ConnectionError: If the target server is not connected.
        """
        ws = self._ws1 if server_id == "server1" else self._ws2
        state = self._state1 if server_id == "server1" else self._state2

        if not ws or not state.connected:
            _LOGGER.error("[ROUTE] %s not connected", server_id)
            raise ConnectionError(f"{server_id} is not available")

        try:
            await ws.send(json.dumps(message))
            _LOGGER.debug(
                "[ROUTE] Message sent to %s (msgType=%s)",
                server_id,
                message.get("msgType", "unknown"),
            )
        except Exception as err:
            _LOGGER.error("[ROUTE] Failed to send to %s: %s", server_id, err)
            raise

    # ─── Reconnection ─────────────────────────────────────────────────────

    def _schedule_reconnect(self, server_id: ServerId) -> None:
        """Schedule a reconnection attempt for a server.

        De-duplicates concurrent callers: if a reconnect task for this server
        is already pending (and the caller is not that task itself), the call
        is a no-op. This prevents the reconnect storm where both the heartbeat
        timeout and the receive-loop ``finally`` block fire for the same
        disconnect event and each spawns its own reconnect chain.

        Reaching ``reconnect_max`` no longer gives up permanently: after
        ``WS_GIVE_UP_COOLDOWN`` seconds the counter resets and reconnects
        resume, so a long outage heals without a Home Assistant restart.

        Args:
            server_id: Server to reconnect.
        """
        if server_id not in self._active_servers:
            # single_server mode: the other leg is deliberately not connected.
            return

        # Paused after an auth failure — the cooldown task resumes us.
        pause_until = self._auth_pause_until.get(server_id)
        if pause_until and time.monotonic() < pause_until:
            _LOGGER.debug(
                "[%s] Reconnect paused after auth failure for %.0fs more",
                server_id,
                pause_until - time.monotonic(),
            )
            return

        # De-dup: skip if a reconnect is already pending for this server,
        # unless WE are that pending task (retrying after a failure).
        current = asyncio.current_task()
        existing = self._reconnect_task1 if server_id == "server1" else self._reconnect_task2
        if existing is not None and not existing.done() and existing is not current:
            _LOGGER.debug(
                "[%s] Reconnect already scheduled, skipping duplicate", server_id
            )
            return

        state = self._state1 if server_id == "server1" else self._state2
        reconnect_max = self.config.reconnect_max

        if state.reconnect_attempts >= reconnect_max:
            # Cool down, then start a fresh cycle instead of giving up.
            self._throttle.warning(
                f"{server_id}/max_reached",
                "[%s] Max reconnection attempts reached (%d); retrying in %.0f min",
                server_id,
                reconnect_max,
                WS_GIVE_UP_COOLDOWN / 60,
            )
            existing_cooldown = self._cooldown_task(server_id)
            if existing_cooldown is not None and not existing_cooldown.done():
                return  # cooldown already running
            state.reconnect_attempts = 0
            self._schedule_resume(server_id, WS_GIVE_UP_COOLDOWN)
            return

        # Exponential backoff with jitter so both servers never retry in
        # lockstep and a server-side hiccup is not amplified.
        delay = min(
            WS_RECONNECT_BASE_DELAY * (2 ** state.reconnect_attempts),
            WS_RECONNECT_MAX_DELAY,
        )
        delay = delay * random.uniform(1 - WS_RECONNECT_JITTER, 1 + WS_RECONNECT_JITTER)
        state.reconnect_attempts += 1

        # Routine retries are debug noise; only surface them once the
        # failure is no longer transient.
        if state.reconnect_attempts > 3:
            self._throttle.warning(
                f"{server_id}/reconnect_scheduled",
                "[%s] Scheduling reconnect attempt %d/%d in %.1fs",
                server_id,
                state.reconnect_attempts,
                reconnect_max,
                delay,
            )
        else:
            _LOGGER.debug(
                "[%s] Scheduling reconnect attempt %d/%d in %.1fs",
                server_id,
                state.reconnect_attempts,
                reconnect_max,
                delay,
            )

        url = self.config.ws_url1 if server_id == "server1" else self.config.ws_url2

        async def _do_reconnect() -> None:
            try:
                await asyncio.sleep(delay)
                if not self._running:
                    return
                if self._auth_pause_until.get(server_id, 0.0) > time.monotonic():
                    # Paused after an auth failure: do not hammer the server
                    # while the user fixes the credentials.
                    return
                await self._connect_server(url, server_id)
                _LOGGER.info("[%s] Reconnected successfully", server_id)
            except asyncio.CancelledError:
                pass
            except XiaoYiAuthError:
                # _connect_server already surfaced the credentials problem.
                self._handle_auth_failure(server_id)
            except Exception as err:
                self._throttle.error(
                    f"{server_id}/reconnect_failed",
                    "[%s] Reconnection failed: %s",
                    server_id,
                    err,
                )
                self._schedule_reconnect(server_id)

        task = asyncio.create_task(_do_reconnect())
        if server_id == "server1":
            self._reconnect_task1 = task
        else:
            self._reconnect_task2 = task

    def _cooldown_task(self, server_id: ServerId) -> asyncio.Task | None:
        """Return the pending cooldown/resume task for a server, if any."""
        return (
            self._cooldown_task1
            if server_id == "server1"
            else self._cooldown_task2
        )

    def _set_cooldown_task(
        self, server_id: ServerId, task: asyncio.Task | None
    ) -> None:
        """Store (or clear) the cooldown/resume task of a server."""
        if server_id == "server1":
            self._cooldown_task1 = task
        else:
            self._cooldown_task2 = task

    def _schedule_resume(self, server_id: ServerId, delay: float) -> None:
        """Try a server again after ``delay`` seconds.

        Used both for the give-up cooldown and for the pause after an auth
        failure. Exactly one resume task is kept per server, and the task
        **clears its own reference when it finishes**: leaving a completed task
        in place made the next give-up cycle return early and stop reconnecting
        for good, so the bridge stayed dead until Home Assistant was restarted.

        Args:
            server_id: Server to resume.
            delay: Seconds to wait before scheduling the next attempt.
        """
        existing = self._cooldown_task(server_id)
        if existing is not None and not existing.done():
            return

        async def _resume() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                pass
            else:
                if self._running:
                    _LOGGER.info(
                        "[%s] Backoff finished, retrying the connection", server_id
                    )
                    self._schedule_reconnect(server_id)
            finally:
                if self._cooldown_task(server_id) is asyncio.current_task():
                    self._set_cooldown_task(server_id, None)

        self._set_cooldown_task(server_id, asyncio.create_task(_resume()))

    async def _schedule_stable_connection_check(self, server_id: ServerId) -> None:
        """Check if a connection has been stable long enough to reset reconnect counter.

        Args:
            server_id: Server to check.
        """
        try:
            await asyncio.sleep(WS_STABLE_THRESHOLD)
            state = self._state1 if server_id == "server1" else self._state2
            if state.connected:
                _LOGGER.info(
                    "[%s] Connection stable for %ds, resetting reconnect counter",
                    server_id,
                    WS_STABLE_THRESHOLD,
                )
                state.reconnect_attempts = 0
                state.fast_close_count = 0
        except asyncio.CancelledError:
            pass

    # ─── Utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _create_ip_ssl_context() -> ssl.SSLContext:
        """Create the TLS context for a wss:// endpoint addressed by IP.

        A certificate never carries an IP SAN for these endpoints, so hostname
        verification cannot succeed and is disabled — but **chain verification
        stays enabled** (``CERT_REQUIRED``): a MITM presenting an untrusted
        certificate is still rejected. If the endpoint cannot produce a
        verifiable chain, :meth:`_connect_server` degrades to
        :meth:`_create_unverified_ssl_context` explicitly and logs one
        throttled warning, instead of silently trusting everything.

        This is a BLOCKING call (loads the CA bundle); call it via
        ``asyncio.to_thread``.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    @staticmethod
    def _create_unverified_ssl_context() -> ssl.SSLContext:
        """Create an SSL context that skips certificate verification.

        This is a BLOCKING call (must be called via asyncio.to_thread)
        because ssl.create_default_context loads certificates from disk.

        Only used as an explicit fallback for IP endpoints whose certificate
        chain cannot be verified — never for the default configuration.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @staticmethod
    def _is_wss_with_ip(url: str) -> bool:
        """Check if URL is wss:// with an IP address (skip certificate verification).

        Args:
            url: WebSocket URL string.

        Returns:
            True if the URL uses wss with an IP address.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme != "wss":
                return False
            hostname = parsed.hostname or ""
            # IPv4 check
            ipv4_pattern = r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
            match = re.match(ipv4_pattern, hostname)
            if match:
                return all(0 <= int(octet) <= 255 for octet in match.groups())
            # IPv6 check
            return ":" in hostname
        except Exception:  # noqa: BLE001 - a malformed URL is not an IP endpoint
            return False
