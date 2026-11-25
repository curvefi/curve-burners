"""Logging that stays small: a size-capped file and no repeated lines.

A keeper runs for days. Two things keep its log readable and bounded:

* ``RepeatFilter`` lets a line through once and then holds back the same
  line (same logger, level and text) for ``window`` seconds; the next time
  it does get through it says how many were held back. A loop that goes
  wrong therefore writes one line per window, not one per iteration.
* with a log file set, the file is rotated at ``max_mb`` megabytes and
  ``backups`` old files are kept, so the log has a hard size limit.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Distinct lines remembered by the filter (the oldest are forgotten first).
MAX_REMEMBERED = 4096


@dataclass(frozen=True)
class LogSettings:
    # Path of the log file (unset: standard error, whose size is the caller's business).
    file: str | None = None
    # Megabytes a log file may reach before it is rotated, and old files kept.
    max_mb: int = 20
    backups: int = 3
    # Seconds the same line is held back after it was written (0: never).
    repeat: int = 300


class RepeatFilter(logging.Filter):
    def __init__(self, window: float, *, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.window = window
        self.clock = clock
        # line -> [when it was last written, how many were held back since]
        self._seen: dict[tuple[str, int, str], list[float]] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.window or getattr(record, "repeat_checked", False):
            return True
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is the handler's to report
            return True
        key = (record.name, record.levelno, text)
        now = self.clock()
        with self._lock:
            seen = self._seen.pop(key, None)
            if seen is not None and now - seen[0] < self.window:
                seen[1] += 1
                self._seen[key] = seen
                return False
            self._seen[key] = [now, 0]
            while len(self._seen) > MAX_REMEMBERED:
                del self._seen[next(iter(self._seen))]
        if seen is not None and seen[1]:
            record.msg = (
                f"{text} (and {int(seen[1])} more like it in the last {now - seen[0]:.0f}s)"
            )
            record.args = None
        # One record passes several handlers: decide once.
        record.repeat_checked = True
        return True


def setup(settings: LogSettings, *, verbose: bool = False) -> logging.Handler:
    """Send the root logger to the settings' file (rotated) or to standard
    error, through a ``RepeatFilter``. Returns the handler."""
    handler: logging.Handler
    if settings.file:
        handler = RotatingFileHandler(
            settings.file,
            maxBytes=settings.max_mb * 1024 * 1024,
            backupCount=settings.backups,
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(RepeatFilter(settings.repeat))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # The libraries' own chatter (a line per HTTP request in debug) is not the keeper's log.
    for noisy in ("urllib3", "web3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler


__all__ = ["FORMAT", "LogSettings", "RepeatFilter", "setup"]
