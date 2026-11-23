"""Structured logging built on the standard library.

* ``json`` format emits one JSON object per line (production / log shipping).
* ``console`` format emits human-readable lines (local development).

Every record carries the current request ID (if any) via a context variable so
log lines from one request can be correlated across layers.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord; anything else was passed via `extra=`.
_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "request_id", "taskName", "color_message"}
)


def get_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(request_id: str | None) -> object:
    """Bind a request ID to the current context. Returns a token for :func:`reset_request_id`."""
    return _request_id.set(request_id)


def reset_request_id(token: object) -> None:
    _request_id.reset(token)  # type: ignore[arg-type]


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _RESERVED_ATTRS and not k.startswith("_")
    }


class RequestContextFilter(logging.Filter):
    """Attach the bound request ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s %(name)s%(rid)s %(message)s%(extras)s")

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None)
        record.rid = f" [{request_id}]" if request_id else ""
        extras = _extra_fields(record)
        extras.pop("rid", None)
        extras.pop("extras", None)
        record.extras = (" " + " ".join(f"{k}={v!r}" for k, v in extras.items())) if extras else ""
        return super().format(record)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure root logging. Safe to call more than once."""
    formatter = "json" if fmt == "json" else "console"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_context": {"()": RequestContextFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "console": {"()": ConsoleFormatter},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": formatter,
                    "filters": ["request_context"],
                }
            },
            "root": {"level": level, "handlers": ["stdout"]},
            "loggers": {
                # Route uvicorn through our handler; access logs are replaced by
                # the request logging middleware, which includes request IDs.
                "uvicorn": {"level": level, "handlers": [], "propagate": True},
                "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
                "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING"},
                "botocore": {"level": "WARNING"},
                "boto3": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
            },
        }
    )
