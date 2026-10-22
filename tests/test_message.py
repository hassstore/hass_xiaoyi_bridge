"""Tests for the A2A message parsing and building helpers (pure logic)."""

from __future__ import annotations

import json
import logging

from custom_components.hass_xiaoyi_bridge.a2a.message import (
    build_a2a_text_response,
    build_clear_context_response,
    build_status_update,
    build_tasks_cancel_response,
    extract_push_id,
    extract_session_id,
    is_a2a_request_message,
    is_clear_context_message,
    is_tasks_cancel_message,
    parse_a2a_message,
)
from tests.fixtures import (
    A2A_REQUEST,
    A2A_REQUEST_WITH_PUSH_ID,
    CLEAR_CONTEXT_REQUEST,
    TASKS_CANCEL_REQUEST,
)


def test_parse_a2a_message() -> None:
    """A message/stream request is parsed into its fields."""
    parsed = parse_a2a_message(A2A_REQUEST)
    assert parsed is not None
    assert parsed.agent_id == "agent-123"
    assert parsed.message_id == "msg-1"
    assert parsed.method == "message/stream"
    assert parsed.session_id == "session-1"
    assert parsed.task_id == "task-1"
    assert parsed.text == "打开客厅灯"
    assert parsed.push_id is None


def test_parse_a2a_message_falls_back_to_top_level_session_id() -> None:
    """sessionId may live at the top level instead of inside params."""
    raw = {
        "agentId": "agent-123",
        "jsonrpc": "2.0",
        "id": "msg-3",
        "method": "message/stream",
        "sessionId": "session-top",
        "params": {
            "id": "task-3",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        },
    }
    parsed = parse_a2a_message(raw)
    assert parsed is not None
    assert parsed.session_id == "session-top"


def test_parse_a2a_message_rejects_garbage() -> None:
    """A message without params parses into an empty context, not a crash."""
    parsed = parse_a2a_message({"nonsense": True})
    assert parsed is not None
    assert parsed.session_id == ""


def test_extract_push_id() -> None:
    """push_id is read from data.variables.systemVariables."""
    parsed = parse_a2a_message(A2A_REQUEST_WITH_PUSH_ID)
    assert parsed is not None
    assert parsed.push_id == "push-abc"
    assert parsed.text == "今天天气怎么样"


def test_extract_push_id_missing() -> None:
    """No data part means no push ID."""
    assert extract_push_id([{"kind": "text", "text": "x"}]) is None


def test_is_a2a_request_message() -> None:
    """Valid requests are recognised; missing sessionId is rejected."""
    assert is_a2a_request_message(A2A_REQUEST) is True
    assert is_a2a_request_message(A2A_REQUEST_WITH_PUSH_ID) is True

    no_session = json.loads(json.dumps(A2A_REQUEST))
    del no_session["params"]["sessionId"]
    del no_session["sessionId"]
    assert is_a2a_request_message(no_session) is False

    wrong_method = json.loads(json.dumps(A2A_REQUEST))
    wrong_method["method"] = "tasks/cancel"
    assert is_a2a_request_message(wrong_method) is False


def test_control_message_detection() -> None:
    """clearContext and tasks/cancel are detected, including legacy actions."""
    assert is_clear_context_message(CLEAR_CONTEXT_REQUEST) is True
    assert is_clear_context_message({"action": "clear"}) is True
    assert is_clear_context_message(A2A_REQUEST) is False

    assert is_tasks_cancel_message(TASKS_CANCEL_REQUEST) is True
    assert is_tasks_cancel_message({"action": "tasks/cancel"}) is True
    assert is_tasks_cancel_message(A2A_REQUEST) is False


def test_extract_session_id_per_method() -> None:
    """sessionId is taken from the right place for each method."""
    assert extract_session_id(A2A_REQUEST) == "session-1"
    assert extract_session_id(CLEAR_CONTEXT_REQUEST) == "session-1"
    assert extract_session_id(TASKS_CANCEL_REQUEST) == "session-1"
    assert extract_session_id({"sessionId": "s"}) == "s"


def test_build_a2a_text_response() -> None:
    """The final response frame carries the full text in one artifact."""
    message = build_a2a_text_response(
        agent_id="agent-123",
        session_id="session-1",
        task_id="task-1",
        message_id="msg-1",
        text="已打开客厅灯",
    )
    assert message["msgType"] == "agent_response"
    assert message["agentId"] == "agent-123"
    assert message["sessionId"] == "session-1"
    assert message["taskId"] == "task-1"

    detail = json.loads(message["msgDetail"])
    assert detail["jsonrpc"] == "2.0"
    assert detail["id"] == "msg-1"
    result = detail["result"]
    assert result["taskId"] == "task-1"
    assert result["kind"] == "artifact-update"
    assert result["append"] is False
    assert result["lastChunk"] is True
    assert result["final"] is True
    assert result["artifact"]["parts"] == [{"kind": "text", "text": "已打开客厅灯"}]


def test_build_status_update() -> None:
    """Status updates are non-final status-update events."""
    message = build_status_update(
        agent_id="agent-123",
        session_id="session-1",
        task_id="task-1",
        text="正在处理",
        state="working",
    )
    detail = json.loads(message["msgDetail"])
    assert detail["result"]["kind"] == "status-update"
    assert detail["result"]["final"] is False
    assert detail["result"]["status"]["state"] == "working"
    assert detail["result"]["status"]["message"]["parts"][0]["text"] == "正在处理"


def test_build_clear_context_response() -> None:
    """clearContext responses report the cleared state."""
    message = build_clear_context_response(
        agent_id="agent-123", session_id="s", request_id="clear-1"
    )
    detail = json.loads(message["msgDetail"])
    assert detail["id"] == "clear-1"
    assert detail["result"]["status"]["state"] == "cleared"


def test_build_tasks_cancel_response() -> None:
    """tasks/cancel responses report the canceled state."""
    message = build_tasks_cancel_response(
        agent_id="agent-123", session_id="s", request_id="cancel-1"
    )
    detail = json.loads(message["msgDetail"])
    assert detail["id"] == "cancel-1"
    assert detail["result"]["status"]["state"] == "canceled"


def test_parse_a2a_message_handles_unexpected_input(caplog) -> None:
    """A message with the wrong shape is reported and yields None."""
    with caplog.at_level(logging.ERROR):
        assert parse_a2a_message({"params": "not-a-dict"}) is None
    assert "Failed to parse A2A message" in caplog.text


def test_extract_push_id_ignores_malformed_data() -> None:
    """Malformed data parts do not break push ID extraction."""
    assert extract_push_id([{"kind": "data", "data": {}}]) is None
    assert extract_push_id([{"kind": "data"}]) is None
    assert extract_push_id([{"kind": "data", "data": {"variables": {}}}]) is None


def test_status_update_reuses_the_request_message_id() -> None:
    """Status frames echo the request id, like the reference implementation."""
    message = build_status_update(
        agent_id="agent-123",
        session_id="session-1",
        task_id="task-1",
        text="处理完成",
        state="completed",
        message_id="msg-1",
    )
    detail = json.loads(message["msgDetail"])
    assert detail["id"] == "msg-1"
    assert detail["result"]["status"]["state"] == "completed"


def test_extract_push_id_accepts_camel_case_and_other_nesting() -> None:
    """The platform renamed the field once; the fallback keeps push working."""
    camel = [{"kind": "data", "data": {"variables": {"systemVariables": {"pushId": "p-1"}}}}]
    nested = [{"kind": "data", "data": {"meta": {"context": {"push_id": "p-2"}}}}]
    assert extract_push_id(camel) == "p-1"
    assert extract_push_id(nested) == "p-2"


def test_parse_a2a_message_scans_the_whole_frame_for_push_id() -> None:
    """A push_id outside params.message.parts is still picked up."""
    payload = json.loads(json.dumps(A2A_REQUEST))
    payload["params"]["metadata"] = {"clientInfo": {"push_id": "frame-level"}}
    parsed = parse_a2a_message(payload)
    assert parsed is not None
    assert parsed.push_id == "frame-level"
