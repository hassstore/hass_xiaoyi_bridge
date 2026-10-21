"""Diagnostics for XiaoYi Bridge.

Downloads a redacted snapshot of the config entry and the live connection
state from the device page (Settings → Devices & Services → ⋯ →
Download diagnostics). Credentials are never included.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import XiaoYiBridgeConfigEntry
from .const import CONF_AK, CONF_API_ID, CONF_SK

TO_REDACT = {CONF_AK, CONF_SK, CONF_API_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: XiaoYiBridgeConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for the config entry.

    Args:
        hass: Home Assistant instance.
        entry: The config entry to describe.

    Returns:
        A JSON-serializable diagnostics payload.
    """
    runtime = entry.runtime_data

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "connection": (
            runtime.ws_manager.get_debug_state()
            if runtime is not None
            else {"ready": False, "reason": "entry not loaded"}
        ),
    }
