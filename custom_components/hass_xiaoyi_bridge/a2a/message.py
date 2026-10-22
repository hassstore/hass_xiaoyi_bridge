"""A2A message types and parsing/building utilities.

Handles the XiaoYi A2A (Agent-to-Agent) JSON-RPC 2.0 protocol message format.

Incoming message format (XiaoYi → plugin):
    {
      "agentId": "your-agent-id",
      "jsonrpc": "2.0",
      "id": "msg-123",
      "method": "message/stream",
      "sessionId": "session-789",
      "params": {
        "id": "task-456",
        "sessionId": "session-789",
        "message": {
          "role": "user",
          "parts": [{"kind": "text", "text": "打开客厅灯"}]
        }
      }
    }

Outgoing response format (plugin → XiaoYi):
    {
      "msgType": "agent_response",
      "agentId": "your-agent-id",
      "sessionId": "session-789",
      "taskId": "task-456",
      "msgDetail": "<JSON-RPC response as string>"
    }

Reference: 小艺开放平台「发起会话 / message/stream」文档
(https://developer.huawei.com/consumer/cn/doc/service/message-stream-0000002505761434)
and the XiaoYi OpenClaw channel plugin (@ynhcj/xiaoyi), which this module
mirrors message-for-message.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "A2ARequestMessage",
    "build_a2a_text_response",
    "build_clear_context_response",
    "build_status_update",
    "build_tasks_cancel_response",
    "extract_push_id",
    "extract_session_id",
    "extract_text_from_parts",
    "is_a2a_request_message",
    "is_clear_context_message",
    "is_tasks_cancel_message",
    "parse_a2a_message",
]


# ─── Incoming Message Types ──────────────────────────────────────────────────


@dataclass
class A2ARequestMessage:
    """Parsed A2A request message."""

    agent_id: str
    message_id: str  # JSON-RPC id
    method: str
    session_id: str
    task_id: str  # params.id
    parts: list[dict[str, Any]] = field(default_factory=list)
    # push_id found anywhere else in the raw frame (fallback).
    push_id_hint: str | None = None

    @property
    def text(self) -> str:
        """Extract text content from message parts."""
        return extract_text_from_parts(self.parts)

    @property
    def file_parts(self) -> list[dict[str, Any]]:
        """Extract file parts from message parts."""
        return [p for p in self.parts if p.get("kind") == "file"]

    @property
    def push_id(self) -> str | None:
        """Extract the push_id carried in data parts, if any.

        小艺在 data part 的 variables.systemVariables.push_id 里带上推送 ID，
        主动推送（agent-webhook）时需要它。
        """
        return extract_push_id(self.parts) or self.push_id_hint


# ─── Parsing ─────────────────────────────────────────────────────────────────


def parse_a2a_message(raw: dict[str, Any]) -> A2ARequestMessage | None:
    """Parse a raw A2A WebSocket message into an A2ARequestMessage.

    Args:
        raw: Raw parsed JSON message from WebSocket.

    Returns:
        Parsed A2ARequestMessage, or None if not a valid A2A request.
    """
    try:
        # Extract sessionId from params or top-level
        params = raw.get("params", {})
        session_id = params.get("sessionId") or raw.get("sessionId", "")
        parts = params.get("message", {}).get("parts", [])

        return A2ARequestMessage(
            agent_id=raw.get("agentId", ""),
            message_id=raw.get("id", ""),
            method=raw.get("method", ""),
            session_id=session_id,
            task_id=params.get("id", ""),
            parts=parts,
            push_id_hint=find_push_id(raw),
        )
    except Exception as err:
        _LOGGER.error("Failed to parse A2A message: %s", err)
        return None


def extract_text_from_parts(parts: list[dict[str, Any]]) -> str:
    """Extract text from A2A message parts.

    Args:
        parts: List of A2A message parts.

    Returns:
        Concatenated text from all text parts.
    """
    texts = []
    for part in parts:
        if part.get("kind") == "text" and part.get("text"):
            texts.append(part["text"])
    return "".join(texts)


# 平台下发的字段名大小写不统一（push_id / pushId），位置也可能变化，
# 因此除了文档路径外，再做一次有界的递归查找作为兜底。
PUSH_ID_KEYS = ("push_id", "pushId")


def find_push_id(payload: Any, depth: int = 0) -> str | None:
    """Find the first push_id-like value in a nested payload.

    Args:
        payload: Any JSON-decoded structure.
        depth: Current recursion depth (bounded to avoid pathological frames).

    Returns:
        The push id string, or None.
    """
    if depth > 6:
        return None
    if isinstance(payload, dict):
        for key in PUSH_ID_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = find_push_id(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_push_id(item, depth + 1)
            if found:
                return found
    return None


def extract_push_id(parts: list[dict[str, Any]]) -> str | None:
    """Extract push_id from A2A data parts.

    The documented location is
    ``data.variables.systemVariables.push_id``; the recursive fallback also
    accepts ``pushId`` and other nesting so a platform-side rename does not
    silently disable proactive push.

    Args:
        parts: List of A2A message parts.

    Returns:
        Push ID string if found, None otherwise.
    """
    for part in parts:
        if part.get("kind") != "data":
            continue
        data = part.get("data", {})
        push_id = (
            data.get("variables", {})
            .get("systemVariables", {})
            .get("push_id")
        )
        if push_id:
            return push_id
    return find_push_id(parts)


# ─── Helper: UUID Generation ─────────────────────────────────────────────────


def _generate_uuid() -> str:
    """Generate a random UUID v4."""
    return str(uuid.uuid4())


# ─── Outgoing Message Builders ───────────────────────────────────────────────


def build_a2a_text_response(
    agent_id: str,
    session_id: str,
    task_id: str,
    message_id: str,
    text: str,
) -> dict[str, Any]:
    """Build a complete A2A text response (non-streaming, final).

    Sends the full text in a single final artifact-update message
    (``append=false``, ``lastChunk=true``, ``final=true``). This is the same
    final frame the XiaoYi OpenClaw channel plugin emits after streaming, so
    one-shot replies from HA's Assist pipeline are protocol-correct.

    Args:
        agent_id: XiaoYi Agent ID.
        session_id: A2A session ID.
        task_id: A2A task ID.
        message_id: Original request message ID.
        text: Complete response text.

    Returns:
        Complete outbound WebSocket message dict.
    """
    artifact_id = f"artifact_{int(time.time())}_{_generate_uuid()[:8]}"

    json_rpc_response = {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "taskId": task_id,
            "kind": "artifact-update",
            "append": False,
            "lastChunk": True,
            "final": True,
            "artifact": {
                "artifactId": artifact_id,
                "parts": [
                    {
                        "kind": "text",
                        "text": text,
                    }
                ],
            },
        },
    }

    return {
        "msgType": "agent_response",
        "agentId": agent_id,
        "sessionId": session_id,
        "taskId": task_id,
        "msgDetail": json.dumps(json_rpc_response),
    }


def build_status_update(
    agent_id: str,
    session_id: str,
    task_id: str,
    text: str,
    state: str = "working",
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build an A2A status-update message.

    Used to indicate intermediate processing status while the agent is working.

    Args:
        agent_id: XiaoYi Agent ID.
        session_id: A2A session ID.
        task_id: A2A task ID.
        text: Status message text (e.g., "正在处理您的请求...").
        state: Status state - "working", "completed", "failed", etc.
        message_id: JSON-RPC id of the original request. The reference
            implementation echoes it on every status frame; 小艺 correlates
            responses by id, so a self-generated id may be ignored.

    Returns:
        Complete outbound WebSocket message dict.
    """
    json_rpc_id = message_id or f"status_{int(time.time())}_{_generate_uuid()[:8]}"

    json_rpc_response = {
        "jsonrpc": "2.0",
        "id": json_rpc_id,
        "result": {
            "taskId": task_id,
            "kind": "status-update",
            "final": False,
            "status": {
                "message": {
                    "role": "agent",
                    "parts": [
                        {
                            "kind": "text",
                            "text": text,
                        }
                    ],
                },
                "state": state,
            },
        },
    }

    return {
        "msgType": "agent_response",
        "agentId": agent_id,
        "sessionId": session_id,
        "taskId": task_id,
        "msgDetail": json.dumps(json_rpc_response),
    }


def build_clear_context_response(
    agent_id: str,
    session_id: str,
    request_id: str,
    success: bool = True,
) -> dict[str, Any]:
    """Build an A2A clearContext response.

    Args:
        agent_id: XiaoYi Agent ID.
        session_id: A2A session ID.
        request_id: Original clearContext request ID.
        success: Whether the clear operation succeeded.

    Returns:
        Complete outbound WebSocket message dict.
    """
    json_rpc_response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "status": {
                "state": "cleared" if success else "failed",
            }
        },
    }

    return {
        "msgType": "agent_response",
        "agentId": agent_id,
        "sessionId": session_id,
        "taskId": request_id,
        "msgDetail": json.dumps(json_rpc_response),
    }


def build_tasks_cancel_response(
    agent_id: str,
    session_id: str,
    request_id: str,
    success: bool = True,
) -> dict[str, Any]:
    """Build an A2A tasks/cancel response.

    Args:
        agent_id: XiaoYi Agent ID.
        session_id: A2A session ID.
        request_id: Original tasks/cancel request ID.
        success: Whether the cancel operation succeeded.

    Returns:
        Complete outbound WebSocket message dict.
    """
    json_rpc_response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "id": request_id,
            "status": {
                "state": "canceled" if success else "failed",
            }
        },
    }

    return {
        "msgType": "agent_response",
        "agentId": agent_id,
        "sessionId": session_id,
        "taskId": request_id,
        "msgDetail": json.dumps(json_rpc_response),
    }


# ─── Message Type Detection ──────────────────────────────────────────────────


def is_a2a_request_message(raw: dict[str, Any]) -> bool:
    """Check if a raw message is a valid A2A request message.

    A sessionId (in params or at the top level) is required: responses are
    routed back to the server that delivered the session.

    Args:
        raw: Raw parsed JSON message from WebSocket.

    Returns:
        True if this is a valid A2A message/stream request.
    """
    params = raw.get("params", {})
    return (
        isinstance(raw.get("agentId"), str)
        and raw.get("jsonrpc") == "2.0"
        and isinstance(raw.get("id"), str)
        and raw.get("method") == "message/stream"
        and isinstance(params.get("id"), str)
        and params.get("message") is not None
        and (
            isinstance(params.get("sessionId"), str)
            or isinstance(raw.get("sessionId"), str)
        )
    )


def is_clear_context_message(raw: dict[str, Any]) -> bool:
    """Check if a raw message is a clearContext request."""
    return raw.get("method") == "clearContext" or raw.get("action") == "clear"


def is_tasks_cancel_message(raw: dict[str, Any]) -> bool:
    """Check if a raw message is a tasks/cancel request."""
    return (
        raw.get("method") == "tasks/cancel"
        or raw.get("action") == "tasks/cancel"
    )


def extract_session_id(raw: dict[str, Any]) -> str | None:
    """Extract sessionId from a message based on its method type.

    Different methods have sessionId in different locations:
    - message/stream: params.sessionId, fallback to top-level sessionId
    - tasks/cancel: top-level sessionId
    - clearContext: top-level sessionId

    Args:
        raw: Raw parsed JSON message.

    Returns:
        Session ID string if found, None otherwise.
    """
    if raw.get("method") == "message/stream":
        return raw.get("params", {}).get("sessionId") or raw.get("sessionId")
    if is_clear_context_message(raw) or is_tasks_cancel_message(raw):
        return raw.get("params", {}).get("sessionId") or raw.get("sessionId")
    return raw.get("sessionId")
