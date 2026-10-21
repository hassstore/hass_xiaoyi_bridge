"""Proactive push to XiaoYi users via the agent-webhook API.

The A2A WebSocket only carries request/response traffic. To *push* a message
to a 小艺 user out of band (doorbell rang, laundry finished, ...), 小艺开放
平台 exposes an HTTP webhook::

    POST https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook

Authentication reuses the AK/SK pair (``Base64(HMAC-SHA256(sk, ts))``), and
the payload references the ``apiId`` configured for the agent plus the
``pushId`` that 小艺 delivers inside incoming messages
(``data.variables.systemVariables.push_id`` — see :mod:`a2a.message`).

This module is deliberately free of Home Assistant imports: the HTTP call is
injected as a ``post`` callable so the payload/signature logic can be tested
without a running Home Assistant (HA 规范 10.4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..const import DEFAULT_PUSH_URL

_LOGGER = logging.getLogger(__name__)

__all__ = ["XiaoYiPushService"]

# A ``post`` callable receives (url, payload, headers, timeout) and returns
# the HTTP status code.
PostCallable = Callable[[str, dict[str, Any], dict[str, str], float], Awaitable[int]]


def _uuid() -> str:
    """Return a random UUID4 string."""
    return str(uuid.uuid4())


class XiaoYiPushService:
    """Send proactive push notifications to XiaoYi users."""

    def __init__(
        self,
        ak: str,
        sk: str,
        api_id: str,
        push_id: str | None,
        url: str = DEFAULT_PUSH_URL,
    ) -> None:
        """Initialize the push service.

        Args:
            ak: Access Key from the 小艺开放平台.
            sk: Secret Key from the 小艺开放平台.
            api_id: Agent API ID configured on the platform.
            push_id: Push ID observed in incoming messages.
            url: Webhook endpoint (overridable for testing).
        """
        self.ak = ak
        self.sk = sk
        self.api_id = api_id
        self.push_id = push_id
        self.url = url

    def is_configured(self) -> bool:
        """Return True when everything the webhook needs is present."""
        return bool(
            self.ak and self.sk and self.api_id and self.push_id
        )

    def missing_fields(self) -> list[str]:
        """Return the names of the missing configuration fields."""
        missing = []
        if not self.ak:
            missing.append("ak")
        if not self.sk:
            missing.append("sk")
        if not self.api_id:
            missing.append("api_id")
        if not self.push_id:
            missing.append("push_id")
        return missing

    def generate_signature(self, timestamp: str) -> str:
        """Generate the HMAC-SHA256 signature for the webhook headers.

        Args:
            timestamp: Millisecond timestamp as string.

        Returns:
            Base64-encoded signature string.
        """
        digest = hmac.new(
            self.sk.encode("utf-8"),
            timestamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def build_headers(self) -> dict[str, str]:
        """Build the HTTP headers for the webhook request."""
        timestamp = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-hag-trace-id": _uuid(),
            "X-Access-Key": self.ak,
            "X-Sign": self.generate_signature(timestamp),
            "X-Ts": timestamp,
        }

    def build_payload(self, text: str, push_text: str) -> dict[str, Any]:
        """Build the JSON-RPC push payload.

        Args:
            text: Summary text carried in the artifact.
            push_text: Notification text shown to the user.

        Returns:
            Complete webhook payload dict.
        """
        return {
            "jsonrpc": "2.0",
            "id": _uuid(),
            "result": {
                "id": _uuid(),
                "apiId": self.api_id,
                "pushId": self.push_id,
                "pushText": push_text,
                "kind": "task",
                "artifacts": [
                    {
                        "artifactId": _uuid(),
                        "parts": [{"kind": "text", "text": text}],
                    }
                ],
                "status": {"state": "completed"},
            },
        }

    async def send(
        self,
        text: str,
        push_text: str,
        post: PostCallable,
        timeout: float = 10.0,
    ) -> bool:
        """Send the push notification.

        Args:
            text: Summary text carried in the artifact.
            push_text: Notification text shown to the user.
            post: HTTP callable returning the response status code.
            timeout: Request timeout in seconds.

        Returns:
            True when the webhook accepted the message (HTTP 200).
        """
        if not self.is_configured():
            _LOGGER.warning(
                "Push not sent, missing configuration: %s",
                ", ".join(self.missing_fields()),
            )
            return False

        payload = self.build_payload(text, push_text)
        headers = self.build_headers()
        _LOGGER.debug(
            "Sending push notification (%d chars) to %s", len(push_text), self.url
        )
        status = await post(self.url, payload, headers, timeout)
        if status == 200:
            _LOGGER.info("Push notification sent successfully")
            return True
        _LOGGER.error("Push notification failed: HTTP %s", status)
        return False

    @staticmethod
    def payload_json(payload: dict[str, Any]) -> str:
        """Serialize a payload (helper for logging/tests)."""
        return json.dumps(payload, ensure_ascii=False)
