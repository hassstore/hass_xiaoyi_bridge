"""Tests for the AK/SK authentication helper (pure logic)."""

from __future__ import annotations

import base64
import hashlib
import hmac

from custom_components.hass_xiaoyi_bridge.a2a.auth import XiaoYiAuth
from tests.fixtures import AGENT_ID, AK, SK


def test_signature_is_base64_hmac_sha256_of_timestamp() -> None:
    """The signature is Base64(HMAC-SHA256(sk, timestamp))."""
    auth = XiaoYiAuth(AK, SK, AGENT_ID)
    expected = base64.b64encode(
        hmac.new(SK.encode(), b"1514764800000", hashlib.sha256).digest()
    ).decode()
    assert auth.generate_signature("1514764800000") == expected


def test_generate_auth_headers() -> None:
    """All four handshake headers are present and consistent."""
    auth = XiaoYiAuth(AK, SK, AGENT_ID)
    headers = auth.generate_auth_headers()

    assert headers["x-access-key"] == AK
    assert headers["x-agent-id"] == AGENT_ID
    assert headers["x-sign"] == auth.generate_signature(headers["x-ts"])
    assert headers["x-ts"].isdigit()


def test_verify_signature() -> None:
    """A matching signature verifies, a wrong one does not."""
    auth = XiaoYiAuth(AK, SK, AGENT_ID)
    signature = auth.generate_signature("1700000000000")
    assert auth.verify_signature("1700000000000", signature) is True
    assert auth.verify_signature("1700000000000", "not-the-signature") is False


def test_timestamp_header_is_milliseconds() -> None:
    """The signed timestamp is a 13-digit millisecond value.

    A seconds value is also all digits, so asserting ``isdigit()`` alone let a
    seconds-based (always rejected by the server) regression pass the suite.
    """
    import time

    auth = XiaoYiAuth(AK, SK, AGENT_ID)
    headers = auth.generate_auth_headers()
    timestamp = int(headers["x-ts"])

    assert len(headers["x-ts"]) == 13
    assert abs(timestamp / 1000 - time.time()) < 5
    assert headers["x-sign"] == auth.generate_signature(headers["x-ts"])
