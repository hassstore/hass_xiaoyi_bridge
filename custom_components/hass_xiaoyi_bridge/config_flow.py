"""Config flow for the XiaoYi Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.conversation import async_get_agent, get_agent_manager
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .a2a.session import SessionManager
from .a2a.websocket import XiaoYiAuthError, XiaoYiWebSocketManager, XiaoYiWSConfig
from .const import (
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
    LANGUAGE_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# Fields that stay in ConfigEntry.data: they decide whether the bridge works.
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WS_URL1, default=DEFAULT_WS_URL_1): str,
        vol.Optional(CONF_WS_URL2, default=DEFAULT_WS_URL_2): str,
        vol.Required(CONF_AK): str,
        vol.Required(CONF_SK): str,
        vol.Required(CONF_AGENT_ID): str,
    }
)

def _agent_select_options(hass: HomeAssistant) -> list[SelectOptionDict]:
    """List the conversation agents that can be picked in the options form.

    Args:
        hass: Home Assistant instance.

    Returns:
        Selector options: the empty value (default agent) followed by every
        conversation agent that is currently loaded.
    """
    options = [
        SelectOptionDict(value="", label="默认对话代理（Home Assistant）"),
    ]
    try:
        agents = get_agent_manager(hass).async_get_agent_info()
    except Exception:  # noqa: BLE001 - conversation may not be set up yet
        _LOGGER.debug("Cannot list conversation agents", exc_info=True)
        return options

    for agent in agents:
        options.append(
            SelectOptionDict(value=agent.id, label=f"{agent.name}  ({agent.id})")
        )
    return options


def _options_schema(hass: HomeAssistant, current_agent: str) -> vol.Schema:
    """Build the options schema with the live list of conversation agents.

    The agent list is read on every render, so an agent added or removed after
    the integration was configured shows up without a restart.

    Args:
        hass: Home Assistant instance.
        current_agent: Currently configured agent id (kept selectable even if
            it is not loaded right now, so the form never silently drops it).

    Returns:
        The options schema.
    """
    agent_options = _agent_select_options(hass)
    if current_agent and all(option["value"] != current_agent for option in agent_options):
        agent_options.append(
            SelectOptionDict(
                value=current_agent, label=f"{current_agent}（当前值）"
            )
        )

    return vol.Schema(
        {
            vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): SelectSelector(
                SelectSelectorConfig(
                    options=LANGUAGE_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="language",
                )
            ),
            vol.Optional(
                CONF_HA_AGENT_ID, default=DEFAULT_HA_AGENT_ID
            ): SelectSelector(
                SelectSelectorConfig(
                    options=agent_options,
                    mode=SelectSelectorMode.DROPDOWN,
                    # 允许手填：某些代理没有 config entry，或列表尚未加载。
                    custom_value=True,
                )
            ),
            vol.Optional(CONF_API_ID, default=DEFAULT_API_ID): str,
            vol.Optional(CONF_STATUS_UPDATES, default=DEFAULT_STATUS_UPDATES): bool,
            vol.Optional(
                CONF_HEARTBEAT_INTERVAL, default=DEFAULT_HEARTBEAT_INTERVAL
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10, max=120, step=1, unit_of_measurement=UnitOfTime.SECONDS
                )
            ),
            vol.Optional(
                CONF_RECONNECT_MAX, default=DEFAULT_RECONNECT_MAX
            ): NumberSelector(NumberSelectorConfig(min=1, max=100, step=1)),
            vol.Optional(
                CONF_HEARTBEAT_FIRST_DELAY, default=DEFAULT_HEARTBEAT_FIRST_DELAY
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=60, step=1, unit_of_measurement=UnitOfTime.SECONDS
                )
            ),
            vol.Optional(CONF_SINGLE_SERVER, default=DEFAULT_SINGLE_SERVER): bool,
            vol.Optional(CONF_SKIP_INIT, default=DEFAULT_SKIP_INIT): bool,
        }
    )


def _is_valid_ws_url(url: str) -> bool:
    """Check that a WebSocket URL is well formed enough to try."""
    return url.startswith(("ws://", "wss://"))


def _clean(value: Any) -> str:
    """Return a trimmed string (form values may arrive as non-strings)."""
    return value.strip() if isinstance(value, str) else ""


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    """No-op A2A callback used by the connection test."""


async def _async_test_connection(
    ws_url1: str,
    ws_url2: str,
    ak: str,
    sk: str,
    agent_id: str,
) -> None:
    """Try to establish the A2A connection once.

    Args:
        ws_url1: Primary WebSocket server URL.
        ws_url2: Secondary WebSocket server URL.
        ak: Access Key.
        sk: Secret Key.
        agent_id: XiaoYi Agent ID.

    Raises:
        XiaoYiAuthError: The server rejected the credentials.
        ConnectionError: No server could be reached.
    """
    manager = XiaoYiWebSocketManager(
        config=XiaoYiWSConfig(
            ws_url1=ws_url1,
            ws_url2=ws_url2,
            ak=ak,
            sk=sk,
            agent_id=agent_id,
        ),
        session_mgr=SessionManager(),
        on_message=_async_noop,
        on_clear=_async_noop,
        on_cancel=_async_noop,
    )
    try:
        await manager.connect()
    finally:
        await manager.disconnect()


class XiaoYiBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for XiaoYi Bridge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        Shows a form to configure the XiaoYi A2A connection parameters and
        verifies them against the XiaoYi servers before the entry is created.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            ak = _clean(user_input.get(CONF_AK))
            sk = _clean(user_input.get(CONF_SK))
            agent_id = _clean(user_input.get(CONF_AGENT_ID))
            ws_url1 = _clean(user_input.get(CONF_WS_URL1)) or DEFAULT_WS_URL_1
            ws_url2 = _clean(user_input.get(CONF_WS_URL2)) or DEFAULT_WS_URL_2

            if not ak or not sk or not agent_id:
                errors["base"] = "missing_required"
            elif not _is_valid_ws_url(ws_url1):
                errors[CONF_WS_URL1] = "invalid_url"
            elif ws_url2 and not _is_valid_ws_url(ws_url2):
                errors[CONF_WS_URL2] = "invalid_url"
            else:
                # One agent = one bridge; the agent ID is the identity.
                # Check for duplicates before contacting the server.
                await self.async_set_unique_id(agent_id)
                self._abort_if_unique_id_configured()

                try:
                    await _async_test_connection(ws_url1, ws_url2, ak, sk, agent_id)
                except XiaoYiAuthError as err:
                    _LOGGER.warning("XiaoYi refused the handshake: %s", err)
                    _LOGGER.warning("%s", AUTH_FAILURE_HINT)
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Connection test failed")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"小艺桥接 ({agent_id})",
                        data={
                            CONF_WS_URL1: ws_url1,
                            CONF_WS_URL2: ws_url2,
                            CONF_AK: ak,
                            CONF_SK: sk,
                            CONF_AGENT_ID: agent_id,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA,
                {
                    CONF_WS_URL1: DEFAULT_WS_URL_1,
                    CONF_WS_URL2: DEFAULT_WS_URL_2,
                },
            ),
            errors=errors,
            description_placeholders={
                "docs_url": "https://developer.huawei.com/consumer/cn/doc/doccenter-celia/openclaw-0000002518410344",
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and store rotated AK/SK credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            ak = _clean(user_input.get(CONF_AK))
            sk = _clean(user_input.get(CONF_SK))

            if not ak or not sk:
                errors["base"] = "missing_required"
            else:
                try:
                    await _async_test_connection(
                        entry.data.get(CONF_WS_URL1, DEFAULT_WS_URL_1),
                        entry.data.get(CONF_WS_URL2, DEFAULT_WS_URL_2),
                        ak,
                        sk,
                        entry.data[CONF_AGENT_ID],
                    )
                except XiaoYiAuthError as err:
                    _LOGGER.warning("XiaoYi refused the handshake: %s", err)
                    _LOGGER.warning("%s", AUTH_FAILURE_HINT)
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Reauth connection test failed")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_AK: ak, CONF_SK: sk}
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AK, default=entry.data.get(CONF_AK, "")
                    ): str,
                    vol.Required(CONF_SK): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "agent_id": entry.data.get(CONF_AGENT_ID, ""),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server endpoints the bridge connects to."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            ws_url1 = _clean(user_input.get(CONF_WS_URL1)) or DEFAULT_WS_URL_1
            ws_url2 = _clean(user_input.get(CONF_WS_URL2)) or DEFAULT_WS_URL_2

            if not _is_valid_ws_url(ws_url1):
                errors[CONF_WS_URL1] = "invalid_url"
            elif ws_url2 and not _is_valid_ws_url(ws_url2):
                errors[CONF_WS_URL2] = "invalid_url"
            else:
                try:
                    await _async_test_connection(
                        ws_url1,
                        ws_url2,
                        entry.data[CONF_AK],
                        entry.data[CONF_SK],
                        entry.data[CONF_AGENT_ID],
                    )
                except XiaoYiAuthError as err:
                    _LOGGER.warning("XiaoYi refused the handshake: %s", err)
                    _LOGGER.warning("%s", AUTH_FAILURE_HINT)
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Reconfigure connection test failed")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_WS_URL1: ws_url1,
                            CONF_WS_URL2: ws_url2,
                        },
                    )

        current1 = entry.data.get(CONF_WS_URL1, DEFAULT_WS_URL_1)
        current2 = entry.data.get(CONF_WS_URL2, DEFAULT_WS_URL_2)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_WS_URL1, default=current1): str,
                    vol.Optional(CONF_WS_URL2, default=current2): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "agent_id": entry.data.get(CONF_AGENT_ID, ""),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return XiaoYiBridgeOptionsFlow()


class XiaoYiBridgeOptionsFlow(OptionsFlowWithReload):
    """Handle options flow for XiaoYi Bridge.

    Saving reloads the entry, so a new heartbeat interval or language takes
    effect immediately (the bridge reconnects once).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_HA_AGENT_ID] = _clean(user_input.get(CONF_HA_AGENT_ID))
            user_input[CONF_API_ID] = _clean(user_input.get(CONF_API_ID))

            if user_input[CONF_HA_AGENT_ID] and not self._async_agent_exists(
                user_input[CONF_HA_AGENT_ID]
            ):
                errors[CONF_HA_AGENT_ID] = "unknown_agent"
            else:
                return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = _options_schema(
            self.hass, options.get(CONF_HA_AGENT_ID, DEFAULT_HA_AGENT_ID)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_LANGUAGE: options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    CONF_HA_AGENT_ID: options.get(
                        CONF_HA_AGENT_ID, DEFAULT_HA_AGENT_ID
                    ),
                    CONF_API_ID: options.get(CONF_API_ID, DEFAULT_API_ID),
                    CONF_STATUS_UPDATES: options.get(
                        CONF_STATUS_UPDATES, DEFAULT_STATUS_UPDATES
                    ),
                    CONF_HEARTBEAT_INTERVAL: options.get(
                        CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL
                    ),
                    CONF_RECONNECT_MAX: options.get(
                        CONF_RECONNECT_MAX, DEFAULT_RECONNECT_MAX
                    ),
                    CONF_HEARTBEAT_FIRST_DELAY: options.get(
                        CONF_HEARTBEAT_FIRST_DELAY, DEFAULT_HEARTBEAT_FIRST_DELAY
                    ),
                    CONF_SINGLE_SERVER: options.get(
                        CONF_SINGLE_SERVER, DEFAULT_SINGLE_SERVER
                    ),
                    CONF_SKIP_INIT: options.get(
                        CONF_SKIP_INIT, DEFAULT_SKIP_INIT
                    ),
                },
            ),
            errors=errors,
        )

    @callback
    def _async_agent_exists(self, agent_id: str) -> bool:
        """Check that the HA conversation agent ID exists.

        Returns True when the check cannot run (conversation component not
        loaded yet) so the user is never blocked by a missing dependency.
        """
        try:
            return async_get_agent(self.hass, agent_id) is not None
        except Exception:  # noqa: BLE001 - validation is best-effort
            _LOGGER.debug("Could not validate agent %s", agent_id)
            return True
