"""AK/SK authentication for XiaoYi A2A protocol.

Generates HMAC-SHA256 signatures for WebSocket connection authentication.
Reference: https://developer.huawei.com/consumer/cn/doc/service/pushmessage-0000002505761436

Signature format: Base64(HMAC-SHA256(secretKey, timestamp))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

_LOGGER = logging.getLogger(__name__)


class XiaoYiAuth:
    """XiaoYi AK/SK authentication handler."""

    def __init__(self, ak: str, sk: str, agent_id: str) -> None:
        """Initialize authentication.

        Args:
            ak: Access Key from Huawei developer console.
            sk: Secret Key from Huawei developer console.
            agent_id: XiaoYi Agent ID.
        """
        self.ak = ak
        self.sk = sk
        self.agent_id = agent_id

    def generate_signature(self, timestamp: str) -> str:
        """Generate HMAC-SHA256 signature.

        Format: Base64(HMAC-SHA256(secretKey, timestamp))

        Args:
            timestamp: Millisecond timestamp as string.

        Returns:
            Base64-encoded signature string.
        """
        h = hmac.new(
            self.sk.encode("utf-8"),
            timestamp.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(h.digest()).decode("utf-8")

    def generate_auth_headers(self) -> dict[str, str]:
        """Generate authentication headers for WebSocket connection.

        Returns:
            Dictionary of HTTP headers required for A2A WebSocket authentication.
        """
        timestamp = str(int(time.time() * 1000))
        signature = self.generate_signature(timestamp)
        return {
            "x-access-key": self.ak,
            "x-sign": signature,
            "x-ts": timestamp,
            "x-agent-id": self.agent_id,
        }

    def verify_signature(self, timestamp: str, signature: str) -> bool:
        """Verify a signature against the stored credentials.

        Args:
            timestamp: The timestamp used to generate the signature.
            signature: The signature to verify.

        Returns:
            True if the signature is valid.
        """
        expected = self.generate_signature(timestamp)
        return hmac.compare_digest(expected, signature)
