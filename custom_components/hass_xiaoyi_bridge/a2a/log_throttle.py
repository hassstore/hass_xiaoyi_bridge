"""Rate-limited logging helpers.

A long-running bridge produces the *same* warning over and over while a
remote server is unreachable (heartbeat failures, reconnect attempts...).
Home Assistant's log viewer counts every occurrence, so an unthrottled
reconnect loop buries everything else in the log — the exact failure mode
reported as "1730 occurrences of Heartbeat send failed".

``ThrottledLogger`` keeps the first occurrence of a message (full detail,
correct severity) and then suppresses identical messages for a cooldown
window, emitting a single periodic summary with the suppressed count::

    Heartbeat send failed (received 1000 (OK); then sent 1000 (OK)): connection lost
    Heartbeat send failed ...: connection lost (suppressed 41 similar messages in the last 300s)

Keys are arbitrary strings; the caller picks a stable key per message
family (e.g. ``"server1/heartbeat_send"``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

__all__ = ["ThrottledLogger"]


class ThrottledLogger:
    """Log a message immediately, then at most once per cooldown window."""

    def __init__(self, logger: logging.Logger, cooldown: float = 300.0) -> None:
        """Initialize the throttled logger.

        Args:
            logger: Standard logger used for the actual output.
            cooldown: Seconds to suppress identical messages after the
                first occurrence.
        """
        self._logger = logger
        self._cooldown = cooldown
        # key → (monotonic time of last emitted message, suppressed count)
        self._state: dict[str, tuple[float, int]] = {}

    @property
    def cooldown(self) -> float:
        """Return the suppression window in seconds."""
        return self._cooldown

    def suppressed(self, key: str) -> int:
        """Return how many messages are currently suppressed for a key."""
        entry = self._state.get(key)
        return entry[1] if entry else 0

    def suppressed_total(self) -> int:
        """Return the total number of suppressed messages across all keys."""
        return sum(count for _, count in self._state.values())

    def reset(self, key: str | None = None) -> None:
        """Forget the throttle state for one key, or for all keys."""
        if key is None:
            self._state.clear()
        else:
            self._state.pop(key, None)

    def debug(self, key: str, msg: str, *args: Any) -> None:
        """Log a debug message through the throttle."""
        self._emit(logging.DEBUG, key, msg, args)

    def info(self, key: str, msg: str, *args: Any) -> None:
        """Log an info message through the throttle."""
        self._emit(logging.INFO, key, msg, args)

    def warning(self, key: str, msg: str, *args: Any) -> None:
        """Log a warning message through the throttle."""
        self._emit(logging.WARNING, key, msg, args)

    def error(self, key: str, msg: str, *args: Any) -> None:
        """Log an error message through the throttle."""
        self._emit(logging.ERROR, key, msg, args)

    def _emit(self, level: int, key: str, msg: str, args: tuple[Any, ...]) -> None:
        """Emit a log record, honouring the per-key cooldown."""
        now = time.monotonic()
        last, suppressed = self._state.get(key, (0.0, 0))

        if last and now - last < self._cooldown:
            # Suppressed: only count it. Nothing is written to the log.
            self._state[key] = (last, suppressed + 1)
            return

        if suppressed:
            # Cooldown elapsed: emit the summary of what was suppressed.
            summary = f"{msg} (suppressed {suppressed} similar messages in the last {self._cooldown:.0f}s)"
            self._logger.log(level, summary, *args)
        else:
            self._logger.log(level, msg, *args)

        self._state[key] = (now, 0)
