"""Structured JSON logging plus secret redaction.

STDOUT IS THE MCP JSON-RPC CHANNEL under stdio transport. A single stray print()
corrupts the protocol stream, so every log record goes to stderr. This module is
the only place a handler is installed; nothing else in the package writes to a
stream directly.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("cwops_correlation_id", default="-")

# Keys whose values are redacted wholesale, and value shapes that look like
# credentials regardless of key name (Anthropic keys, Google access/refresh tokens).
_SECRET_KEY_RE = re.compile(
    r"(token|secret|api[_-]?key|password|credential|authorization|client[_-]?id)", re.I
)
_SECRET_VALUE_RE = re.compile(r"^(sk-ant-|ya29\.|1//|AIza)[A-Za-z0-9_\-./+]+")
REDACTED = "[REDACTED]"

_MAX_DEPTH = 6


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact anything that looks like a secret.

    Applied to every log record and every audit-log detail payload, so a secret
    cannot reach durable storage even if a caller passes one in by mistake.
    """
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = scrub(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.match(value):
        return REDACTED
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None) or get_correlation_id(),
        }
        extra = getattr(record, "cwops", None)
        if isinstance(extra, dict):
            payload.update(scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install a single stderr JSON handler. Idempotent."""
    root = logging.getLogger("cwops")
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"cwops.{name}")


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Emit a structured event; ``fields`` are scrubbed before serialization."""
    logger.log(level, msg, extra={"cwops": fields, "correlation_id": get_correlation_id()})
