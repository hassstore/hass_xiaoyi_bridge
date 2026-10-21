"""Shared test fixtures for the XiaoYi Bridge integration."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from homeassistant.setup import async_setup_component

pytest_plugins = "pytest_homeassistant_custom_component"

# Imported at module load time: pytest-homeassistant-custom-component installs
# its own ``custom_components`` package into sys.modules once its fixtures run,
# which would shadow this repository's namespace package.
from custom_components.hass_xiaoyi_bridge.a2a.websocket import (  # noqa: E402
    XiaoYiWebSocketManager,
)


class OfflineManager(XiaoYiWebSocketManager):
    """Manager that never touches the network."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


@pytest.fixture(autouse=True)
async def setup_homeassistant_component(hass) -> None:
    """Set up the ``homeassistant`` component.

    The integration depends on ``conversation``, whose default agent reads
    exposed entities from the ``homeassistant`` component. A real Home
    Assistant always has it loaded; the test harness does not.
    """
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture(autouse=True)
def patch_ws_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never open real sockets when an entry is set up during a test.

    Tests that exercise the connection itself patch the manager or the
    ``websockets.connect`` call themselves.
    """
    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.XiaoYiWebSocketManager", OfflineManager
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load custom_components/ from the repository root in every test."""
    yield
