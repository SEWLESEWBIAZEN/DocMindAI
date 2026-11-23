import json
import logging

import pytest

from documind.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    RequestContextFilter,
    bind_request_id,
    reset_request_id,
)


def _record(
    msg: str = "hello %s", args: tuple[object, ...] = ("world",), **extra: object
) -> logging.LogRecord:
    record = logging.LogRecord("documind.test", logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    RequestContextFilter().filter(record)
    return record


def test_json_formatter_basic_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "documind.test"
    assert payload["timestamp"].endswith("+00:00")
    assert "request_id" not in payload


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = bind_request_id("req-123")
    try:
        payload = json.loads(JsonFormatter().format(_record(component="redis", latency_ms=1.5)))
    finally:
        reset_request_id(token)
    assert payload["request_id"] == "req-123"
    assert payload["component"] == "redis"
    assert payload["latency_ms"] == 1.5


def test_json_formatter_serialises_exceptions_on_one_line() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    line = JsonFormatter().format(record)
    assert "\n" not in line
    assert "ValueError: boom" in json.loads(line)["exception"]


def test_console_formatter_is_readable() -> None:
    token = bind_request_id("abc")
    try:
        line = ConsoleFormatter().format(_record(component="db"))
    finally:
        reset_request_id(token)
    assert "INFO" in line
    assert "[abc]" in line
    assert "hello world" in line
    assert "component='db'" in line


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_configure_logging_is_idempotent(fmt: str) -> None:
    from documind.core.logging import configure_logging

    configure_logging("DEBUG", fmt)
    configure_logging("WARNING", fmt)
    assert logging.getLogger().level == logging.WARNING
    assert len(logging.getLogger().handlers) == 1
