"""Tests for the XiaoYi push service (pure logic)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from custom_components.hass_xiaoyi_bridge.a2a.push import XiaoYiPushService
from custom_components.hass_xiaoyi_bridge.const import DEFAULT_PUSH_URL
from tests.fixtures import AK, SK


def make_service(**kwargs) -> XiaoYiPushService:
    """Build a configured push service."""
    params = {"ak": AK, "sk": SK, "api_id": "api-1", "push_id": "push-1"}
    params.update(kwargs)
    return XiaoYiPushService(**params)


def test_is_configured_and_missing_fields() -> None:
    """Configuration completeness is reported field by field."""
    assert make_service().is_configured() is True
    assert make_service().missing_fields() == []

    partial = make_service(api_id="", push_id=None)
    assert partial.is_configured() is False
    assert partial.missing_fields() == ["api_id", "push_id"]


def test_build_headers_signature() -> None:
    """Headers carry the AK/SK signature and a trace ID."""
    service = make_service()
    headers = service.build_headers()

    assert headers["X-Access-Key"] == AK
    assert headers["Content-Type"] == "application/json"
    assert headers["x-hag-trace-id"]
    expected = base64.b64encode(
        hmac.new(SK.encode(), headers["X-Ts"].encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["X-Sign"] == expected


def test_build_payload_structure() -> None:
    """The payload is the documented agent-webhook task result."""
    service = make_service()
    payload = service.build_payload("summary text", "Doorbell rang")

    assert payload["jsonrpc"] == "2.0"
    result = payload["result"]
    assert result["apiId"] == "api-1"
    assert result["pushId"] == "push-1"
    assert result["pushText"] == "Doorbell rang"
    assert result["kind"] == "task"
    assert result["status"] == {"state": "completed"}
    assert result["artifacts"][0]["parts"] == [
        {"kind": "text", "text": "summary text"}
    ]


async def test_send_success() -> None:
    """A 200 response marks the push as sent."""
    service = make_service()
    calls = []

    async def post(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return 200

    assert await service.send("text", "push text", post) is True
    assert calls[0][0] == DEFAULT_PUSH_URL


async def test_send_failure_status() -> None:
    """A non-200 response is reported as a failure."""
    service = make_service()

    async def post(url, payload, headers, timeout):
        return 500

    assert await service.send("text", "push text", post) is False


async def test_send_without_configuration() -> None:
    """Nothing is sent when the push configuration is incomplete."""
    service = make_service(api_id="")

    async def post(url, payload, headers, timeout):
        raise AssertionError("post must not be called")

    assert await service.send("text", "push text", post) is False


async def test_send_logs_when_unconfigured(caplog) -> None:
    """An incomplete configuration is reported, not attempted."""
    service = make_service(api_id="", push_id=None)

    async def post(url, payload, headers, timeout):
        raise AssertionError("post must not be called")

    with caplog.at_level(logging.WARNING):
        assert await service.send("text", "push", post) is False
    assert "Push not sent" in caplog.text
    assert "api_id, push_id" in caplog.text


def test_payload_json_helper() -> None:
    """The JSON helper serialises without escaping Chinese characters."""
    service = make_service()
    payload = service.build_payload("你好", "通知")
    assert '"你好"' in service.payload_json(payload)
