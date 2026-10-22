"""Tests for the integration setup, message bridging, services and diagnostics."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)

from custom_components.hass_xiaoyi_bridge.a2a.log_throttle import ThrottledLogger
from custom_components.hass_xiaoyi_bridge.a2a.message import A2ARequestMessage
from custom_components.hass_xiaoyi_bridge.a2a.push import XiaoYiPushService
from custom_components.hass_xiaoyi_bridge.a2a.websocket import (
    XiaoYiAuthError,
)
from custom_components.hass_xiaoyi_bridge.const import (
    ATTR_API_ID,
    ATTR_ENTRY_ID,
    ATTR_MESSAGE,
    DOMAIN,
    SERVICE_RECONNECT,
    SERVICE_SEND_PUSH,
)
from tests.fixtures import AGENT_ID, make_config_entry

_LOGGER = logging.getLogger("test.xiaoyi_bridge")


class SimpleResponseType:
    """Enum-like response type."""

    def __init__(self, value: str) -> None:
        self.value = value


class SimpleResponse:
    """Response object exposing speech and response_type."""

    def __init__(self, speech: str) -> None:
        self.speech = {"plain": {"speech": speech, "extra_data": None}}
        self.response_type = SimpleResponseType("action_done")


class FakeConversationResult:
    """Minimal stand-in for conversation.ConversationResult."""

    def __init__(self, speech: str, conversation_id: str = "conv-1") -> None:
        self.conversation_id = conversation_id
        self.response = SimpleResponse(speech)


def make_request(
    text: str = "打开客厅灯", session_id: str = "session-1"
) -> A2ARequestMessage:
    """Build a parsed A2A request for the bridging tests."""
    return A2ARequestMessage(
        agent_id=AGENT_ID,
        message_id="msg-1",
        method="message/stream",
        session_id=session_id,
        task_id="task-1",
        parts=[{"kind": "text", "text": text}],
    )


class FakeManager:
    """Records the calls the integration makes on the WebSocket manager."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connected = False
        self.disconnected = False
        self.on_message = kwargs.get("on_message")
        self.on_clear = kwargs.get("on_clear")
        self.on_cancel = kwargs.get("on_cancel")
        self.on_auth_failure = kwargs.get("on_auth_failure")
        self.responses: list[tuple[str, str]] = []
        self.status_updates: list[tuple[str, str]] = []
        self.status_message_ids: list[str | None] = []
        # Chronological order of everything sent to 小艺.
        self.events: list[tuple[str, str]] = []
        self.reconnected = False
        self.is_ready = True
        self._listeners: list[Any] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_response(self, session_id, task_id, message_id, text) -> None:
        self.responses.append((session_id, text))
        self.events.append(("response", text))

    async def send_status_update(
        self, session_id, task_id, text, state="working", message_id=None
    ) -> None:
        self.status_updates.append((state, text))
        self.status_message_ids.append(message_id)
        self.events.append(("status", state))

    async def reconnect_now(self) -> None:
        self.reconnected = True

    def add_listener(self, callback) -> Any:
        self._listeners.append(callback)

        def _remove() -> None:
            self._listeners.remove(callback)

        return _remove

    def get_debug_state(self) -> dict[str, Any]:
        return {"ready": self.is_ready}


@pytest.fixture
def fake_manager() -> FakeManager:
    """A fresh fake manager instance."""
    return FakeManager()


@pytest.fixture
def patch_manager(
    monkeypatch: pytest.MonkeyPatch, fake_manager: FakeManager
) -> FakeManager:
    """Replace the WebSocket manager class with the fake."""

    def factory(*args: Any, **kwargs: Any) -> FakeManager:
        for key in ("on_message", "on_clear", "on_cancel", "on_auth_failure"):
            setattr(fake_manager, key, kwargs.get(key))
        return fake_manager

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.XiaoYiWebSocketManager", factory
    )
    return fake_manager


async def bridge_one_message(
    hass: HomeAssistant,
    entry: Any,
    ws_mgr: Any,
    parsed: A2ARequestMessage,
    *,
    language: str = "auto",
    status_updates: bool = True,
    throttle: ThrottledLogger | None = None,
) -> None:
    """Drive the bridging core the way the manager would."""
    from custom_components.hass_xiaoyi_bridge import _handle_a2a_message

    await _handle_a2a_message(
        hass=hass,
        ws_mgr=ws_mgr,
        session_mgr=entry.runtime_data.session_manager,
        parsed=parsed,
        language=language,
        ha_agent_id=None,
        status_updates=status_updates,
        throttle=throttle or ThrottledLogger(_LOGGER),
    )


# ─── Setup / unload ──────────────────────────────────────────────────────────


async def test_setup_entry_connects_and_creates_entities(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A successful connect creates the connection sensor."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert patch_manager.connected is True

    entity_ids = hass.states.async_entity_ids("binary_sensor")
    assert len(entity_ids) == 1
    assert hass.states.get(entity_ids[0]).state == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert patch_manager.disconnected is True
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_retries_when_servers_unreachable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable server becomes ConfigEntryNotReady (HA retries)."""

    class UnreachableManager(FakeManager):
        async def connect(self) -> None:
            raise ConnectionError("refused")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.XiaoYiWebSocketManager",
        UnreachableManager,
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    from custom_components.hass_xiaoyi_bridge import async_setup_entry

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_setup_entry_triggers_reauth_on_auth_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected credentials become ConfigEntryAuthFailed (reauth card)."""

    class RejectedManager(FakeManager):
        async def connect(self) -> None:
            raise XiaoYiAuthError("HTTP 401")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.XiaoYiWebSocketManager",
        RejectedManager,
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    from custom_components.hass_xiaoyi_bridge import async_setup_entry

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


# ─── Message bridging ────────────────────────────────────────────────────────


async def test_a2a_message_is_bridged_to_assist(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A 小艺 utterance is answered with HA's speech."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("好的，已经打开客厅灯")),
    ):
        await bridge_one_message(
            hass, entry, patch_manager, make_request()
        )

    assert patch_manager.responses == [("session-1", "好的，已经打开客厅灯")]
    # The session remembers the conversation for multi-turn context.
    assert entry.runtime_data.session_manager.get_conversation_id("session-1") == (
        "conv-1"
    )
    assert ("working", "正在处理您的请求...") in patch_manager.status_updates
    assert ("completed", "处理完成") in patch_manager.status_updates


async def test_a2a_message_handles_missing_speech(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """An Assist result without speech still gets a friendly answer."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = FakeConversationResult("")
    result.response.speech = {}

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=result),
    ):
        await bridge_one_message(
            hass, entry, patch_manager, make_request(), status_updates=False
        )

    assert patch_manager.responses == [("session-1", "已处理您的请求")]
    assert patch_manager.status_updates == []


async def test_a2a_message_error_response(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A failing Assist call is reported back as an apology."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(side_effect=HomeAssistantError("intent failed")),
    ):
        await bridge_one_message(
            hass, entry, patch_manager, make_request(), status_updates=False
        )

    assert patch_manager.responses == [("session-1", "抱歉，无法处理您的请求")]


async def test_empty_text_is_ignored(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A message without text is dropped before Assist is called."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    converse = AsyncMock()
    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse", converse
    ):
        await bridge_one_message(
            hass, entry, patch_manager, make_request(text="   "), status_updates=False
        )

    assert converse.called is False
    assert patch_manager.responses == []


# ─── Services ────────────────────────────────────────────────────────────────


async def test_send_push_requires_configuration(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """Without api_id/push_id the service reports what is missing."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人"},
            blocking=True,
        )
    assert err.value.translation_key == "push_not_configured"
    assert "api_id" in err.value.translation_placeholders["missing"]


async def test_send_push_success(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A configured push goes through the push service."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"api_id": "api-1"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entry.runtime_data.session_manager.set_push_id("session-1", "push-1")
    sent: list[tuple[str, str]] = []

    async def fake_send(self, text, push_text, post, timeout=10.0) -> bool:
        sent.append((text, push_text))
        return True

    with patch.object(XiaoYiPushService, "send", fake_send):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人"},
            blocking=True,
        )

    assert sent == [("门口有人", "门口有人")]


async def test_send_push_failure_raises(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A rejected push surfaces as a HomeAssistantError."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"api_id": "api-1"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entry.runtime_data.session_manager.set_push_id("session-1", "push-1")

    async def fake_send(self, text, push_text, post, timeout=10.0) -> bool:
        return False

    with (
        patch.object(XiaoYiPushService, "send", fake_send),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人"},
            blocking=True,
        )
    assert err.value.translation_key == "push_failed"


async def test_send_push_api_id_override(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The service field overrides the configured API ID."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entry.runtime_data.session_manager.set_push_id("session-1", "push-1")
    seen: list[str] = []

    async def fake_send(self, text, push_text, post, timeout=10.0) -> bool:
        seen.append(self.api_id)
        return True

    with patch.object(XiaoYiPushService, "send", fake_send):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "hi", ATTR_API_ID: "api-override"},
            blocking=True,
        )

    assert seen == ["api-override"]


async def test_reconnect_service(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The reconnect service forces a fresh connection."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, SERVICE_RECONNECT, {}, blocking=True)
    assert patch_manager.reconnected is True


async def test_services_require_a_loaded_entry(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """Service calls without a loaded entry are rejected.

    The services are registered in ``async_setup``, so the integration must
    have been initialised once (here via a config entry that is unloaded
    again before the call).
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, SERVICE_RECONNECT, {}, blocking=True)
    assert err.value.translation_key == "no_loaded_entry"


# ─── Diagnostics ─────────────────────────────────────────────────────────────


async def test_diagnostics_redacts_credentials(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """Diagnostics never contain AK/SK."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.hass_xiaoyi_bridge.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["entry_data"]["ak"] == "**REDACTED**"
    assert data["entry_data"]["sk"] == "**REDACTED**"
    assert data["entry_data"]["agent_id"] == AGENT_ID
    assert data["connection"]["ready"] is True


# ─── Service entry resolution ────────────────────────────────────────────────


async def test_service_with_unknown_entry_id(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """An entry ID that does not exist is reported."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONNECT,
            {ATTR_ENTRY_ID: "does-not-exist"},
            blocking=True,
        )
    assert err.value.translation_key == "unknown_entry"


async def test_service_with_unloaded_entry(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """An entry that exists but is not loaded is reported."""
    loaded = make_config_entry()
    loaded.add_to_hass(hass)
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    other = MockConfigEntry(
        domain=DOMAIN,
        title="小艺桥接 (agent-456)",
        data={**loaded.data, "agent_id": "agent-456"},
        unique_id="agent-456",
    )
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECONNECT,
            {ATTR_ENTRY_ID: other.entry_id},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_loaded"


# ─── Push HTTP plumbing ──────────────────────────────────────────────────────


async def test_send_push_posts_through_ha_session(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The push goes through Home Assistant's shared HTTP session."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"api_id": "api-1"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.session_manager.set_push_id("session-1", "push-1")

    posted = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            posted.update(
                {"url": url, "payload": json, "headers": headers, "timeout": timeout}
            )
            return FakeResponse()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_get_clientsession",
        return_value=FakeSession(),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人"},
            blocking=True,
        )

    assert posted["url"].endswith("/agent-webhook")
    assert posted["payload"]["result"]["apiId"] == "api-1"
    assert posted["payload"]["result"]["pushId"] == "push-1"


# ─── Reauth wiring and callbacks ─────────────────────────────────────────────


async def test_auth_failure_callback_starts_reauth(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A credentials problem reported by the manager starts reauth."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entry.runtime_data.ws_manager.on_auth_failure()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_cancel_callback_is_invoked(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """tasks/cancel is acknowledged and logged."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await entry.runtime_data.ws_manager.on_cancel("session-1", "task-1")
    assert entry.runtime_data.session_manager.active_session_count == 0


# ─── Conversation timeout ────────────────────────────────────────────────────


async def test_conversation_timeout_is_reported(
    hass: HomeAssistant, patch_manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung Assist call gets a timeout answer instead of hanging forever."""
    import asyncio as aio

    import custom_components.hass_xiaoyi_bridge as integration

    monkeypatch.setattr(integration, "CONVERSE_TIMEOUT", 0.01)

    async def slow_converse(**kwargs):
        await aio.sleep(5)

    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        side_effect=slow_converse,
    ):
        await bridge_one_message(
            hass, entry, patch_manager, make_request(), status_updates=False
        )

    assert patch_manager.responses == [("session-1", "处理超时，请稍后重试")]


# ─── Push ID propagation ─────────────────────────────────────────────────────


async def test_push_id_from_message_is_remembered(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The push_id carried in a 小艺 message is stored for later pushes."""
    from custom_components.hass_xiaoyi_bridge.a2a.message import A2ARequestMessage

    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    request = A2ARequestMessage(
        agent_id=AGENT_ID,
        message_id="msg-1",
        method="message/stream",
        session_id="session-1",
        task_id="task-1",
        parts=[
            {"kind": "text", "text": "hi"},
            {
                "kind": "data",
                "data": {"variables": {"systemVariables": {"push_id": "push-xyz"}}},
            },
        ],
    )

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("你好")),
    ):
        await bridge_one_message(
            hass, entry, patch_manager, request, status_updates=False
        )

    assert entry.runtime_data.session_manager.get_push_id("session-1") == "push-xyz"


# ─── Send failures are rate-limited, not fatal ───────────────────────────────


async def test_response_send_failure_is_logged_once(
    hass: HomeAssistant, patch_manager: FakeManager, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing response send is reported without breaking the bridge."""
    async def failing_send_response(**kwargs):
        raise ConnectionError("socket gone")

    patch_manager.send_response = failing_send_response

    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # One shared throttle, like the integration creates at setup time.
    throttle = ThrottledLogger(_LOGGER)
    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("好的")),
    ):
        for _ in range(3):
            await bridge_one_message(
                hass,
                entry,
                patch_manager,
                make_request(),
                status_updates=False,
                throttle=throttle,
            )

    failures = [
        r for r in caplog.records if "Failed to send A2A response" in r.getMessage()
    ]
    assert len(failures) == 1


async def test_status_update_failure_does_not_stop_processing(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A failing status update is tolerated; the answer still goes out."""
    async def failing_status(**kwargs):
        raise ConnectionError("socket gone")

    patch_manager.send_status_update = failing_status

    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("已经关灯")),
    ):
        await bridge_one_message(hass, entry, patch_manager, make_request())

    assert patch_manager.responses == [("session-1", "已经关灯")]


async def test_push_http_error_is_reported(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """A network error while pushing becomes a HomeAssistantError."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"api_id": "api-1"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.session_manager.set_push_id("session-1", "push-1")

    async def exploding_send(self, text, push_text, post, timeout=10.0) -> bool:
        raise OSError("network down")

    with (
        patch.object(XiaoYiPushService, "send", exploding_send),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人"},
            blocking=True,
        )
    assert err.value.translation_key == "push_failed"


# ─── Regression tests for the conversation reply path ────────────────────────


async def test_completion_status_precedes_the_final_response(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The stream is closed by the final artifact, so status goes first.

    The reference implementation sends the ``completed`` status update and then
    the ``final: true`` artifact; a status frame after the final chunk sits
    outside the stream and may be dropped by 小艺.
    """
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("好的，已经打开客厅灯")),
    ):
        await bridge_one_message(hass, entry, patch_manager, make_request())

    assert patch_manager.events == [
        ("status", "working"),
        ("status", "completed"),
        ("response", "好的，已经打开客厅灯"),
    ]


async def test_status_updates_echo_the_request_message_id(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """Both status frames carry the JSON-RPC id of the incoming request."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("好的")),
    ):
        await bridge_one_message(hass, entry, patch_manager, make_request())

    assert patch_manager.status_message_ids == ["msg-1", "msg-1"]


async def test_voice_text_is_not_logged_at_info(
    hass: HomeAssistant,
    patch_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spoken text is personal data and must not reach the INFO log."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch(
            "custom_components.hass_xiaoyi_bridge.async_converse",
            AsyncMock(return_value=FakeConversationResult("好的，已经打开客厅灯")),
        ),
        caplog.at_level(logging.INFO),
    ):
        await bridge_one_message(hass, entry, patch_manager, make_request())

    assert "打开客厅灯" not in caplog.text
    assert "已经打开客厅灯" not in caplog.text


async def test_diagnostics_redact_options_too(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The push API ID lives in options and must be redacted as well."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.hass_xiaoyi_bridge.const import CONF_API_ID
    from tests.fixtures import make_entry_data

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"小艺桥接 ({AGENT_ID})",
        data=make_entry_data(),
        options={CONF_API_ID: "api-secret-123"},
        unique_id=AGENT_ID,
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.hass_xiaoyi_bridge.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["options"][CONF_API_ID] == "**REDACTED**"
    assert "api-secret-123" not in str(data)


async def test_language_option_is_mapped_for_assist(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The option key is translated to a BCP-47 tag; ``auto`` means default."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.hass_xiaoyi_bridge.async_converse",
        AsyncMock(return_value=FakeConversationResult("好的")),
    ) as converse:
        await bridge_one_message(
            hass, entry, patch_manager, make_request(), language="zh_hans"
        )
        assert converse.call_args.kwargs["language"] == "zh-Hans"

        await bridge_one_message(
            hass, entry, patch_manager, make_request(), language="auto"
        )
        assert converse.call_args.kwargs["language"] is None


async def test_send_push_falls_back_to_the_static_push_id(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """Without a learned push_id the configured option is used."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={"api_id": "api-1", "push_id": "static-push"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    seen: list[str | None] = []
    original_init = XiaoYiPushService.__init__

    def capture(self, *args, **kwargs) -> None:
        seen.append(kwargs.get("push_id"))
        original_init(self, *args, **kwargs)

    async def fake_send(self, text, push_text, post, timeout=10.0) -> bool:
        return True

    with (
        patch.object(XiaoYiPushService, "__init__", capture),
        patch.object(XiaoYiPushService, "send", fake_send),
    ):
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_PUSH, {ATTR_MESSAGE: "门口有人"}, blocking=True
        )

    assert seen == ["static-push"]


async def test_send_push_prefers_the_explicit_push_id(
    hass: HomeAssistant, patch_manager: FakeManager
) -> None:
    """The service field overrides both the session and the option."""
    entry = make_config_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={"api_id": "api-1", "push_id": "static-push"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.session_manager.set_push_id("session-1", "learned-push")

    seen: list[str | None] = []
    original_init = XiaoYiPushService.__init__

    def capture(self, *args, **kwargs) -> None:
        seen.append(kwargs.get("push_id"))
        original_init(self, *args, **kwargs)

    async def fake_send(self, text, push_text, post, timeout=10.0) -> bool:
        return True

    with (
        patch.object(XiaoYiPushService, "__init__", capture),
        patch.object(XiaoYiPushService, "send", fake_send),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PUSH,
            {ATTR_MESSAGE: "门口有人", "push_id": "explicit-push"},
            blocking=True,
        )

    assert seen == ["explicit-push"]
