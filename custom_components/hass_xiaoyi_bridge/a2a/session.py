"""Session management for mapping XiaoYi sessions to HA conversations.

Maps XiaoYi A2A sessionId to HomeAssistant conversation_id so that
multi-turn conversations maintain context across both systems.

Also tracks the ``push_id`` that 小艺 carries in the data part of incoming
messages (``variables.systemVariables.push_id``); it is required by the
proactive push API (``agent-webhook``) and falls back to the most recently
seen value when a session is unknown, mirroring the reference plugin's
``ConfigManager``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

__all__ = ["SessionContext", "SessionManager"]


@dataclass
class SessionContext:
    """Context for an active A2A session."""

    session_id: str
    conversation_id: str | None = None
    task_id: str | None = None
    message_id: str | None = None
    push_id: str | None = None
    created_at: float = 0.0
    last_active_at: float = 0.0


class SessionManager:
    """Manages mappings between XiaoYi sessionId and HA conversation_id.

    Each XiaoYi session maps to a HA conversation to maintain context
    across multi-turn interactions.
    """

    def __init__(self) -> None:
        """Initialize session manager."""
        self._sessions: dict[str, SessionContext] = {}
        self._global_push_id: str | None = None

    # ─── Conversation mapping ─────────────────────────────────────────────

    def get_conversation_id(self, session_id: str) -> str | None:
        """Get the HA conversation_id for a XiaoYi session.

        Args:
            session_id: XiaoYi A2A session ID.

        Returns:
            HA conversation_id if found, None otherwise.
        """
        ctx = self._sessions.get(session_id)
        return ctx.conversation_id if ctx else None

    def set_conversation_id(self, session_id: str, conversation_id: str) -> None:
        """Set or update the HA conversation_id for a session.

        Args:
            session_id: XiaoYi A2A session ID.
            conversation_id: HA conversation_id returned from async_converse.
        """
        ctx = self._sessions.get(session_id)
        if ctx:
            ctx.conversation_id = conversation_id
            ctx.last_active_at = time.monotonic()
        else:
            self._sessions[session_id] = SessionContext(
                session_id=session_id,
                conversation_id=conversation_id,
                created_at=time.monotonic(),
                last_active_at=time.monotonic(),
            )
        _LOGGER.debug(
            "Session %s mapped to conversation %s", session_id, conversation_id
        )

    def update_session(
        self,
        session_id: str,
        task_id: str | None = None,
        message_id: str | None = None,
        push_id: str | None = None,
    ) -> None:
        """Update session metadata.

        Args:
            session_id: XiaoYi A2A session ID.
            task_id: Current task ID.
            message_id: Current message ID.
            push_id: Push notification ID.
        """
        ctx = self._sessions.get(session_id)
        if ctx:
            if task_id is not None:
                ctx.task_id = task_id
            if message_id is not None:
                ctx.message_id = message_id
            if push_id is not None:
                ctx.push_id = push_id
            ctx.last_active_at = time.monotonic()
        else:
            self._sessions[session_id] = SessionContext(
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
                push_id=push_id,
                created_at=time.monotonic(),
                last_active_at=time.monotonic(),
            )

    # ─── Push ID handling ─────────────────────────────────────────────────

    def set_push_id(self, session_id: str, push_id: str) -> None:
        """Remember the push_id for a session (and as the global fallback).

        Args:
            session_id: XiaoYi A2A session ID.
            push_id: Push ID extracted from the incoming message.
        """
        if not push_id:
            _LOGGER.warning(
                "Refusing to store an empty push_id for session %s", session_id
            )
            return
        self.update_session(session_id, push_id=push_id)
        self._global_push_id = push_id

    def get_push_id(self, session_id: str | None = None) -> str | None:
        """Return the push_id for a session, falling back to the last seen one.

        Args:
            session_id: XiaoYi A2A session ID, or None for the global value.

        Returns:
            Push ID if known, None otherwise.
        """
        if session_id:
            ctx = self._sessions.get(session_id)
            if ctx and ctx.push_id:
                return ctx.push_id
        return self._global_push_id

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def remove_session(self, session_id: str) -> None:
        """Remove a session mapping (e.g., on clearContext).

        Args:
            session_id: XiaoYi A2A session ID.
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            _LOGGER.debug("Session %s removed", session_id)

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists.

        Args:
            session_id: XiaoYi A2A session ID.

        Returns:
            True if the session exists.
        """
        return session_id in self._sessions

    def get_session(self, session_id: str) -> SessionContext | None:
        """Get the full session context.

        Args:
            session_id: XiaoYi A2A session ID.

        Returns:
            SessionContext if found, None otherwise.
        """
        return self._sessions.get(session_id)

    def clear_all(self) -> None:
        """Clear all session mappings and cached push IDs."""
        self._sessions.clear()
        self._global_push_id = None
        _LOGGER.debug("All sessions cleared")

    @property
    def active_session_count(self) -> int:
        """Return the number of active sessions."""
        return len(self._sessions)

    def debug_state(self) -> dict[str, Any]:
        """Return a snapshot of the session table for diagnostics."""
        return {
            "active_sessions": len(self._sessions),
            "global_push_id": bool(self._global_push_id),
        }
