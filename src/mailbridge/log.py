from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Sequence
from typing import Final

REDACTED: Final = "***"

_LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

_MIN_REDACTABLE_LENGTH: Final = 6


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str], fmt: str, datefmt: str) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        # Longest first, so an overlapping secret cannot leave a fragment behind.
        self._secrets: Sequence[str] = sorted(
            {s for s in secrets if len(s) >= _MIN_REDACTABLE_LENGTH},
            key=len,
            reverse=True,
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, REDACTED)
        return rendered


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Install a single stderr handler. Safe to call again to reconfigure."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(secrets, _LOG_FORMAT, _DATE_FORMAT))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
