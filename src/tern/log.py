"""JSON logging for tern processes (CLAUDE.md: stdlib logging, JSON formatter).

At INFO and above, callers log `mid`s and counts only: no subjects, addresses or bodies.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import IO

_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, plus any ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def setup(stream: IO[str], level: int = logging.INFO) -> None:
    """Route the root logger to ``stream`` as JSON lines (replaces existing handlers)."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
