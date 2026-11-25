"""Turn a model's JSON reply into cited values, and merge replies (ADR 0034).

Models are asked for JSON only, but replies are parsed defensively: code fences
and surrounding prose are tolerated, and anything without a valid source number
is dropped. A value is never kept without the source it came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from documind.domain.extraction.templates import Field

MAX_VALUE_CHARS = 1000
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class UnreadableReplyError(ValueError):
    """The model's reply wasn't the JSON we asked for."""


@dataclass(frozen=True, slots=True)
class Cited:
    value: str
    source: int
    date: date | None = None


Row = dict[str, Cited]


def _json(text: str) -> dict[str, Any]:
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise UnreadableReplyError("no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise UnreadableReplyError(str(exc)) from exc
    if not isinstance(data, dict):
        raise UnreadableReplyError("not an object")
    return data


def _cited(raw: Any, field: Field, sources: int) -> Cited | None:
    if raw is None:
        return None
    if isinstance(raw, str | int | float):
        return None  # no source given: not kept
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    source = raw.get("source")
    if isinstance(source, str) and source.strip().isdigit():
        source = int(source.strip())
    if isinstance(source, list) and source and isinstance(source[0], int):
        source = source[0]
    if not isinstance(source, int) or isinstance(source, bool) or not 1 <= source <= sources:
        return None
    if value is None or isinstance(value, dict | list):
        return None
    text = " ".join(str(value).split())[:MAX_VALUE_CHARS]
    if not text or text.lower() in ("null", "none", "n/a", "not stated", "unknown"):
        return None
    parsed: date | None = None
    if field.type == "date":
        raw_date = raw.get("date")
        if isinstance(raw_date, str):
            try:
                parsed = date.fromisoformat(raw_date.strip()[:10])
            except ValueError:
                parsed = None
    return Cited(text, source, parsed)


def parse_fields(text: str, fields: tuple[Field, ...], sources: int) -> Row:
    data = _json(text)
    values = data.get("values", data)
    if not isinstance(values, dict):
        raise UnreadableReplyError("values is not an object")
    row: Row = {}
    for field in fields:
        cited = _cited(values.get(field.key), field, sources)
        if cited is not None:
            row[field.key] = cited
    return row


def parse_items(text: str, fields: tuple[Field, ...], sources: int) -> list[Row]:
    data = _json(text)
    items = data.get("items")
    if items is None:
        raise UnreadableReplyError("no items")
    if not isinstance(items, list):
        raise UnreadableReplyError("items is not a list")
    rows: list[Row] = []
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        row: Row = {}
        for field in fields:
            cited = _cited(item.get(field.key), field, sources)
            if cited is not None:
                row[field.key] = cited
        # An item needs its main field (the first one) with a source.
        if fields and fields[0].key in row:
            rows.append(row)
    return rows


def merge_fields(rows: list[Row], fields: tuple[Field, ...]) -> Row:
    """First value found in reading order wins, per field."""
    merged: Row = {}
    for row in rows:
        for field in fields:
            if field.key not in merged and field.key in row:
                merged[field.key] = row[field.key]
    return merged


def dedupe_items(rows: list[Row], fields: tuple[Field, ...]) -> list[Row]:
    """Windows can overlap: drop items whose main value (and date) repeat."""
    seen: set[tuple[str, str]] = set()
    out: list[Row] = []
    main = fields[0].key
    dated = next((f.key for f in fields if f.type == "date"), None)
    for row in rows:
        key = (
            re.sub(r"\W+", " ", row[main].value.lower()).strip(),
            str(row[dated].date) if dated and dated in row else "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
