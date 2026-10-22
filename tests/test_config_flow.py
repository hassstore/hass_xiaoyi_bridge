"""Tests for the config flow, reauth, reconfigure and options flow."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hass_xiaoyi_bridge.const import (
    CONF_AGENT_ID,
    CONF_AK,
    CONF_API_ID,
    CONF_HA_AGENT_ID,
    CONF_HEARTBEAT_INTERVAL,
    CONF_LANGUAGE,
    CONF_RECONNECT_MAX,
    CONF_SK,
    CONF_STATUS_UPDATES,
    CONF_WS_URL1,
    CONF_WS_URL2,
    DOMAIN,
)
from tests.fixtures import AGENT_ID, make_config_entry

NEW_AK = "rotated-access-key"
NEW_SK = "rotated-secret-key"


@pytest.fixture
def bypass_test_connection(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stub out the live connection test, recording its arguments."""
    calls: list[dict] = []

    async def fake_test_connection(ws_url1, ws_url2, ak, sk, agent_id):
        calls.append(
            {
                "ws_url1": ws_url1,
                "ws_url2": ws_url2,
                "ak": ak,
                "sk": sk,
                "agent_id": agent_id,
            }
        )

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        fake_test_connection,
    )
    return calls


async def test_user_flow_creates_entry(hass, bypass_test_connection) -> None:
    """A complete form creates the entry with trimmed values."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "wss://hag.cloud.huawei.com/openclaw/v1/ws/link",
            CONF_WS_URL2: "  ",
            CONF_AK: "  ak  ",
            CONF_SK: "sk",
            CONF_AGENT_ID: " agent-1 ",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AK] == "ak"
    assert result["data"][CONF_AGENT_ID] == "agent-1"
    # An empty backup URL falls back to the default endpoint.
    assert result["data"][CONF_WS_URL2].startswith("wss://")
    assert len(bypass_test_connection) == 1


async def test_user_flow_rejects_duplicate_agent(
    hass, bypass_test_connection
) -> None:
    """The same agent ID cannot be configured twice."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "ws://a",
            CONF_AK: "ak",
            CONF_SK: "sk",
            CONF_AGENT_ID: AGENT_ID,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The duplicate is detected without contacting the server.
    assert bypass_test_connection == []


async def test_user_flow_missing_required_fields(
    hass, bypass_test_connection
) -> None:
    """AK, SK and agent ID are all required."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={CONF_WS_URL1: "ws://a", CONF_AK: "", CONF_SK: "sk", CONF_AGENT_ID: ""},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "missing_required"


async def test_user_flow_invalid_url(hass, bypass_test_connection) -> None:
    """A non-WebSocket URL is rejected on its own field."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "https://not-a-websocket",
            CONF_AK: "ak",
            CONF_SK: "sk",
            CONF_AGENT_ID: "agent-1",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_WS_URL1] == "invalid_url"


async def test_user_flow_auth_error(hass, monkeypatch) -> None:
    """A rejected credential surfaces as invalid_auth."""
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import XiaoYiAuthError

    async def failing_test_connection(*args, **kwargs):
        raise XiaoYiAuthError("HTTP 401")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        failing_test_connection,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "ws://a",
            CONF_AK: "ak",
            CONF_SK: "sk",
            CONF_AGENT_ID: "agent-1",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_user_flow_cannot_connect(hass, monkeypatch) -> None:
    """An unreachable server surfaces as cannot_connect."""

    async def failing_test_connection(*args, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        failing_test_connection,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "ws://a",
            CONF_AK: "ak",
            CONF_SK: "sk",
            CONF_AGENT_ID: "agent-1",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_reauth_updates_credentials(hass, bypass_test_connection) -> None:
    """Re-authentication stores the rotated AK/SK and reloads."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_AK: NEW_AK, CONF_SK: NEW_SK},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    await hass.async_block_till_done()
    assert entry.data[CONF_AK] == NEW_AK
    assert entry.data[CONF_SK] == NEW_SK
    # The agent ID never changes during reauth.
    assert entry.data[CONF_AGENT_ID] == AGENT_ID
    assert bypass_test_connection[0]["ak"] == NEW_AK


async def test_reauth_rejects_empty_credentials(
    hass, bypass_test_connection
) -> None:
    """Empty credentials keep the reauth form open."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AK: "", CONF_SK: ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "missing_required"


async def test_reconfigure_updates_endpoints(hass, bypass_test_connection) -> None:
    """Reconfigure changes the server endpoints and reloads."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_WS_URL1: "wss://new-primary.test/link", CONF_WS_URL2: ""},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    await hass.async_block_till_done()
    assert entry.data[CONF_WS_URL1] == "wss://new-primary.test/link"
    assert entry.data[CONF_WS_URL2].startswith("wss://")


async def test_options_flow_saves_preferences(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Options are stored and the entry is reloaded."""
    # The test environment has no conversation.openai_conversation entity.
    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow.async_get_agent",
        lambda hass, agent_id: object(),
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LANGUAGE: "zh_hans",
            CONF_HA_AGENT_ID: "conversation.openai_conversation",
            CONF_API_ID: "api-9",
            CONF_STATUS_UPDATES: False,
            CONF_HEARTBEAT_INTERVAL: 30,
            CONF_RECONNECT_MAX: 10,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_LANGUAGE] == "zh_hans"
    assert entry.options[CONF_HA_AGENT_ID] == "conversation.openai_conversation"
    assert entry.options[CONF_API_ID] == "api-9"
    assert entry.options[CONF_STATUS_UPDATES] is False
    assert entry.options[CONF_HEARTBEAT_INTERVAL] == 30
    assert entry.options[CONF_RECONNECT_MAX] == 10


async def test_options_flow_rejects_unknown_agent(hass, monkeypatch) -> None:
    """A conversation agent ID that does not exist is rejected."""
    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow.async_get_agent",
        lambda hass, agent_id: None,
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LANGUAGE: "auto",
            CONF_HA_AGENT_ID: "conversation.does_not_exist",
            CONF_API_ID: "",
            CONF_STATUS_UPDATES: True,
            CONF_HEARTBEAT_INTERVAL: 20,
            CONF_RECONNECT_MAX: 50,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_HA_AGENT_ID] == "unknown_agent"


# ─── Connection test helper ──────────────────────────────────────────────────


async def test_test_connection_uses_the_manager(hass, monkeypatch) -> None:
    """The helper builds a manager, connects and disconnects again."""
    from custom_components.hass_xiaoyi_bridge import config_flow as flow_module

    events = []

    class RecordingManager:
        def __init__(self, **kwargs) -> None:
            events.append(("init", kwargs["config"].agent_id))

        async def connect(self) -> None:
            events.append(("connect", None))

        async def disconnect(self) -> None:
            events.append(("disconnect", None))

    monkeypatch.setattr(flow_module, "XiaoYiWebSocketManager", RecordingManager)

    await flow_module._async_test_connection("ws://a", "ws://b", "ak", "sk", "agent-1")
    assert events == [("init", "agent-1"), ("connect", None), ("disconnect", None)]


async def test_user_flow_rejects_invalid_backup_url(
    hass, bypass_test_connection
) -> None:
    """The backup URL is validated too."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_WS_URL1: "ws://a",
            CONF_WS_URL2: "http://not-a-ws",
            CONF_AK: "ak",
            CONF_SK: "sk",
            CONF_AGENT_ID: "agent-1",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_WS_URL2] == "invalid_url"


async def test_reauth_reports_connection_errors(hass, monkeypatch) -> None:
    """Reauth distinguishes rejected credentials from an unreachable server."""
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import XiaoYiAuthError

    entry = make_config_entry()
    entry.add_to_hass(hass)

    async def failing(*args, **kwargs):
        raise XiaoYiAuthError("HTTP 403")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        failing,
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AK: "ak", CONF_SK: "sk"}
    )
    assert result["errors"]["base"] == "invalid_auth"

    async def unreachable(*args, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        unreachable,
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AK: "ak", CONF_SK: "sk"}
    )
    assert result["errors"]["base"] == "cannot_connect"


async def test_reconfigure_reports_errors(hass, monkeypatch) -> None:
    """Reconfigure validates URLs and reports connection problems."""
    from custom_components.hass_xiaoyi_bridge.a2a.websocket import XiaoYiAuthError

    entry = make_config_entry()
    entry.add_to_hass(hass)

    async def ok(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection", ok
    )

    # Invalid URL keeps the form open with a field error.
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_WS_URL1: "ftp://nope", CONF_WS_URL2: ""},
    )
    assert result["errors"][CONF_WS_URL1] == "invalid_url"

    # The backup URL is validated too.
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_WS_URL1: "ws://ok", CONF_WS_URL2: "nope"},
    )
    assert result["errors"][CONF_WS_URL2] == "invalid_url"

    # An auth error is reported on the base key.
    async def rejected(*args, **kwargs):
        raise XiaoYiAuthError("HTTP 401")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        rejected,
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_WS_URL1: "ws://b", CONF_WS_URL2: ""}
    )
    assert result["errors"]["base"] == "invalid_auth"

    # An unreachable server is reported as cannot_connect.
    async def unreachable(*args, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow._async_test_connection",
        unreachable,
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_WS_URL1: "ws://b", CONF_WS_URL2: ""}
    )
    assert result["errors"]["base"] == "cannot_connect"


async def test_options_agent_check_tolerates_failures(hass, monkeypatch) -> None:
    """A broken agent lookup does not block the options form."""
    def exploding_get_agent(hass, agent_id):
        raise KeyError("conversation not set up")

    monkeypatch.setattr(
        "custom_components.hass_xiaoyi_bridge.config_flow.async_get_agent",
        exploding_get_agent,
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LANGUAGE: "auto",
            CONF_HA_AGENT_ID: "conversation.whatever",
            CONF_API_ID: "",
            CONF_STATUS_UPDATES: True,
            CONF_HEARTBEAT_INTERVAL: 20,
            CONF_RECONNECT_MAX: 50,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HA_AGENT_ID] == "conversation.whatever"


# ─── 对话代理下拉选择 ───────────────────────────────────────────────────────


async def test_options_flow_lists_conversation_agents(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent field is a dropdown filled from the loaded agents."""
    from types import SimpleNamespace

    from custom_components.hass_xiaoyi_bridge import config_flow

    class FakeManager:
        def async_get_agent_info(self):
            return [
                SimpleNamespace(id="conversation.openai", name="OpenAI"),
                SimpleNamespace(id="conversation.google", name="Google"),
            ]

    monkeypatch.setattr(
        config_flow, "get_agent_manager", lambda hass: FakeManager()
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # The default option plus every loaded agent must be offered.
    options = config_flow._agent_select_options(hass)
    values = [option["value"] for option in options]
    assert values[0] == ""
    assert "conversation.openai" in values
    assert "conversation.google" in values
    labels = {option["value"]: option["label"] for option in options}
    assert "OpenAI" in labels["conversation.openai"]


async def test_options_schema_keeps_an_unloaded_current_agent(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A currently configured agent stays selectable even if not loaded."""
    from types import SimpleNamespace

    from homeassistant.helpers.selector import SelectSelector

    from custom_components.hass_xiaoyi_bridge import config_flow

    class FakeManager:
        def async_get_agent_info(self):
            return [SimpleNamespace(id="conversation.openai", name="OpenAI")]

    monkeypatch.setattr(config_flow, "get_agent_manager", lambda hass: FakeManager())
    schema = config_flow._options_schema(hass, "conversation.gone")

    marker, selector = next(
        (key, value)
        for key, value in schema.schema.items()
        if getattr(key, "schema", key) == "ha_agent_id"
    )
    assert isinstance(selector, SelectSelector)
    values = [option["value"] for option in selector.config["options"]]
    # The missing current value is injected so the form never drops it.
    assert values == ["", "conversation.openai", "conversation.gone"]
