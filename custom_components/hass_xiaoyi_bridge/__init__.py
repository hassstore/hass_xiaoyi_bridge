"""XiaoYi Bridge integration.

Bridges XiaoYi (小艺) A2A voice commands to Home Assistant's Assist pipeline
for smart home device control.

Flow:
   小艺用户 → A2A WebSocket → [This Component] → HA async_converse() → HA设备控制
                                                               ↓
   小艺用户 ← A2A WebSocket ← [This Component] ← HA response speech ←

The A2A protocol (dual WebSocket servers, ``clawd_bot_init`` handshake,
application heartbeats, JSON-RPC ``message/stream`` requests wrapped in
``agent_response`` frames) follows the 小艺开放平台 OpenClaw channel
documentation and reference plugin; see ``a2a/`` for the protocol layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientTimeout
from homeassistant.components.conversation import async_converse
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .a2a.log_throttle import ThrottledLogger
from .a2a.message import A2ARequestMessage
from .a2a.push import XiaoYiPushService
from .a2a.session import SessionManager
from .a2a.websocket import XiaoYiAuthError, XiaoYiWebSocketManager, XiaoYiWSConfig
from .const import (
    ATTR_API_ID,
    ATTR_ENTRY_ID,
    ATTR_MESSAGE,
    ATTR_PUSH_TEXT,
    ATTR_SESSION_ID,
    AUTH_FAILURE_HINT,
    CONF_AGENT_ID,
    CONF_AK,
    CONF_API_ID,
    CONF_HA_AGENT_ID,
    CONF_HEARTBEAT_FIRST_DELAY,
    CONF_HEARTBEAT_INTERVAL,
    CONF_LANGUAGE,
    CONF_RECONNECT_MAX,
    CONF_SINGLE_SERVER,
    CONF_SK,
    CONF_SKIP_INIT,
    CONF_STATUS_UPDATES,
    CONF_WS_URL1,
    CONF_WS_URL2,
    CONVERSE_TIMEOUT,
    DEFAULT_API_ID,
    DEFAULT_HA_AGENT_ID,
    DEFAULT_HEARTBEAT_FIRST_DELAY,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LANGUAGE,
    DEFAULT_RECONNECT_MAX,
    DEFAULT_SINGLE_SERVER,
    DEFAULT_SKIP_INIT,
    DEFAULT_STATUS_UPDATES,
    DEFAULT_WS_URL_1,
    DEFAULT_WS_URL_2,
    DOMAIN,
    LANGUAGE_MAP,
    LOG_THROTTLE_COOLDOWN,
    PUSH_TIMEOUT,
    SERVICE_RECONNECT,
    SERVICE_SEND_PUSH,
)

_LOGGER = logging.getLogger(__name__)

# The integration only supports config entries (no YAML configuration).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


class BridgeRuntimeData:
    """Runtime data stored on the config entry."""

    def __init__(
        self,
        ws_manager: XiaoYiWebSocketManager,
        session_manager: SessionManager,
        language: str,
        ha_agent_id: str | None,
        status_updates: bool,
    ) -> None:
        """Initialize runtime data."""
        self.ws_manager = ws_manager
        self.session_manager = session_manager
        self.language = language
        self.ha_agent_id = ha_agent_id
        self.status_updates = status_updates


type XiaoYiBridgeConfigEntry = ConfigEntry[BridgeRuntimeData]


# ─── Setup ───────────────────────────────────────────────────────────────────


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration and register its services.

    Services are registered here (not in ``async_setup_entry``) so automations
    keep validating even while no config entry is loaded (IQS action-setup).
    """

    async def async_handle_send_push(call: ServiceCall) -> None:
        """Push a notification to XiaoYi users out of band."""
        entry = _async_resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
        runtime = entry.runtime_data

        api_id = call.data.get(ATTR_API_ID) or entry.options.get(
            CONF_API_ID, DEFAULT_API_ID
        )
        session_id = call.data.get(ATTR_SESSION_ID)
        push_id = runtime.session_manager.get_push_id(session_id)

        service = XiaoYiPushService(
            ak=entry.data[CONF_AK],
            sk=entry.data[CONF_SK],
            api_id=api_id,
            push_id=push_id,
        )
        if not service.is_configured():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="push_not_configured",
                translation_placeholders={
                    "missing": ", ".join(service.missing_fields())
                },
            )

        message = call.data[ATTR_MESSAGE]
        push_text = call.data.get(ATTR_PUSH_TEXT) or message[:50]

        async def _post(
            url: str,
            payload: dict[str, Any],
            headers: dict[str, str],
            timeout: float,
        ) -> int:
            """POST the push payload through HA's shared HTTP session."""
            session = async_get_clientsession(hass)
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                return response.status

        try:
            sent = await service.send(
                message, push_text, post=_post, timeout=PUSH_TIMEOUT
            )
        except Exception as err:
            _LOGGER.error("Push request to XiaoYi failed: %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="push_failed"
            ) from err

        if not sent:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="push_failed"
            )

    async def async_handle_reconnect(call: ServiceCall) -> None:
        """Force a fresh connection to both XiaoYi servers."""
        entry = _async_resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
        await entry.runtime_data.ws_manager.reconnect_now()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_PUSH,
        async_handle_send_push,
        schema=vol.Schema(
            {
                vol.Required(ATTR_MESSAGE): str,
                vol.Optional(ATTR_PUSH_TEXT): str,
                vol.Optional(ATTR_SESSION_ID): str,
                vol.Optional(ATTR_API_ID): str,
                vol.Optional(ATTR_ENTRY_ID): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECONNECT,
        async_handle_reconnect,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): str}),
    )
    return True


def _async_resolve_entry(
    hass: HomeAssistant, entry_id: str | None
) -> XiaoYiBridgeConfigEntry:
    """Return the config entry a service call targets.

    Args:
        hass: Home Assistant instance.
        entry_id: Entry ID from the service call, or None for the first entry.

    Returns:
        The loaded config entry.

    Raises:
        ServiceValidationError: No matching loaded entry exists.
    """
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_entry",
                translation_placeholders={"entry_id": entry_id},
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": entry_id},
            )
        return entry

    for candidate in hass.config_entries.async_entries(DOMAIN):
        if candidate.state is ConfigEntryState.LOADED:
            return candidate

    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="no_loaded_entry"
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaoYiBridgeConfigEntry
) -> bool:
    """Set up XiaoYi Bridge from a config entry.

    Creates the A2A WebSocket connection and starts listening for
    XiaoYi voice commands.

    Raises:
        ConfigEntryAuthFailed: The server rejected the AK/SK credentials.
        ConfigEntryNotReady: The servers are unreachable right now.
    """
    _LOGGER.info("Setting up XiaoYi Bridge")

    # Read configuration. Preferences live in options, but fall back to the
    # data dict so entries created before the options split keep working.
    data = entry.data
    options = entry.options
    language = options.get(CONF_LANGUAGE, data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE))
    ha_agent_id = options.get(
        CONF_HA_AGENT_ID, data.get(CONF_HA_AGENT_ID, DEFAULT_HA_AGENT_ID)
    )
    status_updates = options.get(
        CONF_STATUS_UPDATES,
        data.get(CONF_STATUS_UPDATES, DEFAULT_STATUS_UPDATES),
    )
    reconnect_max = options.get(
        CONF_RECONNECT_MAX, data.get(CONF_RECONNECT_MAX, DEFAULT_RECONNECT_MAX)
    )
    heartbeat_interval = options.get(
        CONF_HEARTBEAT_INTERVAL,
        data.get(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL),
    )

    heartbeat_first_delay = options.get(
        CONF_HEARTBEAT_FIRST_DELAY,
        data.get(CONF_HEARTBEAT_FIRST_DELAY, DEFAULT_HEARTBEAT_FIRST_DELAY),
    )
    single_server = options.get(
        CONF_SINGLE_SERVER,
        data.get(CONF_SINGLE_SERVER, DEFAULT_SINGLE_SERVER),
    )
    skip_init = options.get(
        CONF_SKIP_INIT, data.get(CONF_SKIP_INIT, DEFAULT_SKIP_INIT)
    )

    session_mgr = SessionManager()
    throttle = ThrottledLogger(_LOGGER, LOG_THROTTLE_COOLDOWN)

    # Define A2A message callbacks
    async def on_a2a_message(parsed: A2ARequestMessage) -> None:
        """Handle an incoming A2A message by routing to HA Assist pipeline."""
        await _handle_a2a_message(
            hass=hass,
            ws_mgr=ws_mgr,
            session_mgr=session_mgr,
            parsed=parsed,
            language=language,
            ha_agent_id=ha_agent_id,
            status_updates=status_updates,
            throttle=throttle,
        )

    async def on_clear_context(session_id: str) -> None:
        """Handle clearContext by removing session mapping."""
        _LOGGER.info("Clearing context for session %s", session_id)
        session_mgr.remove_session(session_id)

    async def on_tasks_cancel(session_id: str, task_id: str) -> None:
        """Handle tasks/cancel."""
        _LOGGER.info(
            "Cancel requested for session %s, task %s", session_id, task_id
        )
        # HA does not support cancelling an in-progress conversation; the
        # Assist result will still be delivered when it finishes.

    def on_auth_failure() -> None:
        """Escalate a credential problem to a reauth card."""
        _LOGGER.warning(
            "XiaoYi credentials look invalid; starting reauthentication flow"
        )
        entry.async_start_reauth(hass)

    # Create WebSocket configuration
    ws_config = XiaoYiWSConfig(
        ws_url1=data.get(CONF_WS_URL1, DEFAULT_WS_URL_1),
        ws_url2=data.get(CONF_WS_URL2, DEFAULT_WS_URL_2),
        ak=data[CONF_AK],
        sk=data[CONF_SK],
        agent_id=data[CONF_AGENT_ID],
        reconnect_max=reconnect_max,
        heartbeat_interval=heartbeat_interval,
        heartbeat_first_delay=heartbeat_first_delay,
        single_server=single_server,
        skip_init=skip_init,
    )

    # Create WebSocket manager
    ws_mgr = XiaoYiWebSocketManager(
        config=ws_config,
        session_mgr=session_mgr,
        on_message=on_a2a_message,
        on_clear=on_clear_context,
        on_cancel=on_tasks_cancel,
        on_auth_failure=on_auth_failure,
    )

    runtime = BridgeRuntimeData(
        ws_manager=ws_mgr,
        session_manager=session_mgr,
        language=language,
        ha_agent_id=ha_agent_id,
        status_updates=status_updates,
    )
    entry.runtime_data = runtime

    try:
        await ws_mgr.connect()
    except XiaoYiAuthError as err:
        await ws_mgr.disconnect()
        entry.runtime_data = None
        raise ConfigEntryAuthFailed(f"{err} — {AUTH_FAILURE_HINT}") from err
    except Exception as err:
        await ws_mgr.disconnect()
        entry.runtime_data = None
        raise ConfigEntryNotReady(
            f"Cannot reach the XiaoYi servers: {err}"
        ) from err

    _LOGGER.info("XiaoYi A2A bridge started successfully")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: XiaoYiBridgeConfigEntry
) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading XiaoYi Bridge")
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime: BridgeRuntimeData | None = entry.runtime_data
    if runtime is not None:
        await runtime.ws_manager.disconnect()
        runtime.session_manager.clear_all()
    entry.runtime_data = None
    return True


# ─── Message Processing Core ──────────────────────────────────────────────────


async def _handle_a2a_message(
    hass: HomeAssistant,
    ws_mgr: XiaoYiWebSocketManager,
    session_mgr: SessionManager,
    parsed: A2ARequestMessage,
    language: str,
    ha_agent_id: str | None,
    status_updates: bool,
    throttle: ThrottledLogger,
) -> None:
    """Process an A2A message by calling HA's Assist pipeline.

    This is the core bridging logic:
    1. Record session metadata (task/message IDs and the push_id)
    2. Send "processing" status update
    3. Call HA async_converse() to process the command
    4. Extract speech response from HA result
    5. Send response back via A2A WebSocket

    Args:
        hass: HomeAssistant instance.
        ws_mgr: WebSocket manager for sending responses.
        session_mgr: Session manager for conversation_id mapping.
        parsed: Parsed A2A request message.
        language: Language for HA conversation processing
            (``LANGUAGE_AUTO`` follows Home Assistant's language).
        ha_agent_id: Optional HA conversation agent ID.
        status_updates: Whether to send intermediate status updates.
        throttle: Rate-limited logger for recurring send failures.
    """
    session_id = parsed.session_id
    task_id = parsed.task_id
    message_id = parsed.message_id

    # 0. Remember the push_id 小艺 carries for proactive pushes.
    push_id = parsed.push_id
    if push_id:
        session_mgr.set_push_id(session_id, push_id)
    session_mgr.update_session(
        session_id, task_id=task_id, message_id=message_id
    )

    # 1. Extract user text
    user_text = parsed.text
    if not user_text or not user_text.strip():
        _LOGGER.warning("Empty text in A2A message, ignoring")
        return

    # Voice content is personal data: keep it out of the INFO log that users
    # paste into issues.
    _LOGGER.debug(
        "Processing A2A message: session=%s, text=%s",
        session_id,
        user_text[:100],
    )

    # 2. Send "processing" status update
    if status_updates:
        try:
            await ws_mgr.send_status_update(
                session_id=session_id,
                task_id=task_id,
                text="正在处理您的请求...",
                state="working",
                message_id=message_id,
            )
        except Exception as err:
            throttle.warning(
                f"status_update/{session_id}",
                "Failed to send status update: %s",
                err,
            )

    # 3. Get/create HA conversation_id for session context
    conversation_id = session_mgr.get_conversation_id(session_id)

    # 4. Call HA Assist pipeline
    response_text = ""
    try:
        async with asyncio.timeout(CONVERSE_TIMEOUT):
            result = await async_converse(
                hass=hass,
                text=user_text,
                conversation_id=conversation_id,
                context=Context(),
                # "auto" (and a legacy empty value) = let HA use
                # hass.config.language.
                language=LANGUAGE_MAP.get(language),
                agent_id=ha_agent_id or None,
            )

        # 5. Save conversation_id for multi-turn context
        if result.conversation_id:
            session_mgr.set_conversation_id(session_id, result.conversation_id)

        # 6. Extract speech response text
        #    IntentResponse.speech is dict[str, dict]: {"plain": {"speech": "...", "extra_data": None}}
        speech_dict = result.response.speech if result.response else {}
        plain_speech = (
            speech_dict.get("plain", {}) if isinstance(speech_dict, dict) else {}
        )
        response_text = (
            plain_speech.get("speech", "") if isinstance(plain_speech, dict) else ""
        )

        if not response_text:
            # Check for error response
            response_type = result.response.response_type if result.response else None
            if response_type and getattr(response_type, "value", None) == "error":
                response_text = "抱歉，无法处理您的请求"
            else:
                response_text = "已处理您的请求"

        _LOGGER.debug(
            "HA Assist response: session=%s, text=%s",
            session_id,
            response_text[:100],
        )

    except TimeoutError:
        _LOGGER.warning(
            "HA conversation timed out after %ss for session %s",
            CONVERSE_TIMEOUT,
            session_id,
        )
        response_text = "处理超时，请稍后重试"

    except HomeAssistantError as err:
        _LOGGER.warning("HA conversation error for session %s: %s", session_id, err)
        response_text = "抱歉，无法处理您的请求"

    except Exception as err:
        _LOGGER.error(
            "Unexpected error processing conversation for session %s: %s",
            session_id,
            err,
        )
        response_text = "处理出错，请稍后重试"

    # 7. Send the completion status first, then the final artifact. The
    #    reference implementation does it in this order: a status frame after
    #    a final:true artifact is outside the stream and may be dropped.
    if status_updates:
        try:
            await ws_mgr.send_status_update(
                session_id=session_id,
                task_id=task_id,
                text="处理完成",
                state="completed",
                message_id=message_id,
            )
        except Exception as err:
            throttle.warning(
                f"completion_status/{session_id}",
                "Failed to send completion status: %s",
                err,
            )

    # 8. Send the final A2A response back to XiaoYi
    try:
        await ws_mgr.send_response(
            session_id=session_id,
            task_id=task_id,
            message_id=message_id,
            text=response_text,
        )
    except Exception as err:
        throttle.error(
            f"send_response/{session_id}",
            "Failed to send A2A response: %s",
            err,
        )
