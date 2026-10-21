"""Shared fixtures and sample A2A payloads for the XiaoYi Bridge tests."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hass_xiaoyi_bridge.const import (
    CONF_AGENT_ID,
    CONF_AK,
    CONF_SK,
    CONF_WS_URL1,
    CONF_WS_URL2,
    DOMAIN,
)

WS_URL_1 = "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"
WS_URL_2 = "wss://116.63.174.231/openclaw/v1/ws/link"
AK = "test-access-key"
SK = "test-secret-key"
AGENT_ID = "agent-123"


def make_entry_data() -> dict[str, Any]:
    """Return the ConfigEntry.data of a configured bridge."""
    return {
        CONF_WS_URL1: WS_URL_1,
        CONF_WS_URL2: WS_URL_2,
        CONF_AK: AK,
        CONF_SK: SK,
        CONF_AGENT_ID: AGENT_ID,
    }


def make_config_entry() -> MockConfigEntry:
    """Return a MockConfigEntry for the XiaoYi Bridge domain."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"小艺桥接 ({AGENT_ID})",
        data=make_entry_data(),
        options={},
        unique_id=AGENT_ID,
        version=1,
    )


# A complete message/stream request as 小艺 sends it.
A2A_REQUEST: dict[str, Any] = {
    "agentId": AGENT_ID,
    "jsonrpc": "2.0",
    "id": "msg-1",
    "method": "message/stream",
    "sessionId": "session-1",
    "params": {
        "id": "task-1",
        "sessionId": "session-1",
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "打开客厅灯"}],
        },
    },
}

# The same request with a push_id inside a data part.
A2A_REQUEST_WITH_PUSH_ID: dict[str, Any] = {
    "agentId": AGENT_ID,
    "jsonrpc": "2.0",
    "id": "msg-2",
    "method": "message/stream",
    "params": {
        "id": "task-2",
        "sessionId": "session-2",
        "message": {
            "role": "user",
            "parts": [
                {"kind": "text", "text": "今天天气怎么样"},
                {
                    "kind": "data",
                    "data": {
                        "variables": {
                            "systemVariables": {"push_id": "push-abc"}
                        }
                    },
                },
            ],
        },
    },
}

CLEAR_CONTEXT_REQUEST: dict[str, Any] = {
    "agentId": AGENT_ID,
    "jsonrpc": "2.0",
    "id": "clear-1",
    "method": "clearContext",
    "sessionId": "session-1",
}

TASKS_CANCEL_REQUEST: dict[str, Any] = {
    "agentId": AGENT_ID,
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "tasks/cancel",
    "sessionId": "session-1",
    "taskId": "task-1",
}
