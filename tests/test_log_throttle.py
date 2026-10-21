"""Tests for the rate-limited logger (pure logic)."""

from __future__ import annotations

import logging

from custom_components.hass_xiaoyi_bridge.a2a.log_throttle import ThrottledLogger


def test_first_message_is_logged_immediately(caplog) -> None:
    """The first occurrence of a message is logged in full."""
    throttle = ThrottledLogger(logging.getLogger("test.throttle"), cooldown=300)
    with caplog.at_level(logging.WARNING, logger="test.throttle"):
        throttle.warning("key", "something broke: %s", "detail")
    assert "something broke: detail" in caplog.text


def test_repeats_are_suppressed_then_summarised(caplog) -> None:
    """Repeats inside the cooldown are counted, then summarised once."""
    logger = logging.getLogger("test.throttle2")
    throttle = ThrottledLogger(logger, cooldown=0.2)

    with caplog.at_level(logging.WARNING, logger="test.throttle2"):
        throttle.warning("key", "still broken")
        throttle.warning("key", "still broken")
        throttle.warning("key", "still broken")
        assert len(caplog.records) == 1
        assert throttle.suppressed("key") == 2

        # After the cooldown the next call emits the summary line.
        throttle._state["key"] = (0.0, 2)  # simulate an expired window
        throttle.warning("key", "still broken")

    assert len(caplog.records) == 2
    assert "suppressed 2 similar messages" in caplog.text
    assert throttle.suppressed("key") == 0


def test_independent_keys(caplog) -> None:
    """Different keys are throttled independently."""
    logger = logging.getLogger("test.throttle3")
    throttle = ThrottledLogger(logger, cooldown=300)
    with caplog.at_level(logging.WARNING, logger="test.throttle3"):
        throttle.warning("a", "message a")
        throttle.warning("b", "message b")
    assert len(caplog.records) == 2
    assert throttle.suppressed_total() == 0


def test_reset_forgets_state(caplog) -> None:
    """reset() lets a suppressed message through immediately."""
    logger = logging.getLogger("test.throttle4")
    throttle = ThrottledLogger(logger, cooldown=300)
    with caplog.at_level(logging.WARNING, logger="test.throttle4"):
        throttle.warning("key", "again")
        throttle.warning("key", "again")
        assert len(caplog.records) == 1
        throttle.reset("key")
        throttle.warning("key", "again")
    assert len(caplog.records) == 2


def test_cooldown_and_totals(caplog) -> None:
    """The cooldown and the suppressed total are exposed for diagnostics."""
    logger = logging.getLogger("test.throttle5")
    throttle = ThrottledLogger(logger, cooldown=123)
    assert throttle.cooldown == 123

    with caplog.at_level(logging.WARNING, logger="test.throttle5"):
        throttle.warning("a", "msg a")
        throttle.warning("a", "msg a")
        throttle.warning("b", "msg b")
    assert throttle.suppressed("a") == 1
    assert throttle.suppressed("b") == 0
    assert throttle.suppressed_total() == 1

    throttle.reset()
    assert throttle.suppressed_total() == 0
