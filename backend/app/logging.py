"""Structured JSON logging (PLAN §12.3). One JSON object per line on stderr.

Never pass media or landmark arrays to a logger.
"""

import json
import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_context: ContextVar[dict[str, str] | None] = ContextVar("log_context", default=None)

_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(_context.get() or {})
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


@contextmanager
def bind_context(**fields: str) -> Generator[None]:
    """Add `session_id`, `job_id`, `stage`, ... to every log line inside the block."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def configure_logging(level: str) -> None:
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        raise ValueError(f"invalid LOG_LEVEL: {level!r}")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(numeric)
