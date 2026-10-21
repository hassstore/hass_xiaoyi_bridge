"""Binary sensor platform for XiaoYi Bridge.

Exposes the bridge's connection state so users can see at a glance whether
小艺 can currently reach Home Assistant, without digging through logs.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaoYiBridgeConfigEntry
from .const import DOMAIN

# 只读平台：状态由 WebSocket 管理器推送，不需要并发请求配额。
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaoYiBridgeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the connection binary sensor."""
    async_add_entities([XiaoYiBridgeConnectionSensor(entry)])


class XiaoYiBridgeConnectionSensor(BinarySensorEntity):
    """Whether at least one XiaoYi A2A server is connected."""

    _attr_has_entity_name = True
    # 状态完全由 WebSocket 管理器推送，不需要轮询。
    _attr_should_poll = False
    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: XiaoYiBridgeConfigEntry) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or "XiaoYi Bridge",
            manufacturer="Huawei",
            model="XiaoYi A2A Bridge",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/hassstore/hass_xiaoyi_bridge",
        )

    @property
    def is_on(self) -> bool:
        """Return True when at least one A2A server is connected and ready."""
        runtime = self._entry.runtime_data
        return bool(runtime and runtime.ws_manager.is_ready)

    async def async_added_to_hass(self) -> None:
        """Subscribe to connection state changes."""
        await super().async_added_to_hass()
        runtime = self._entry.runtime_data
        if runtime is not None:
            self.async_on_remove(runtime.ws_manager.add_listener(self._on_state_change))

    def _on_state_change(self) -> None:
        """Push a state update when the bridge connects or disconnects."""
        self.async_write_ha_state()
