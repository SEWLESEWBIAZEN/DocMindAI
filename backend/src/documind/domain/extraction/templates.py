"""Extraction templates: what to pull out of documents (ADR 0034)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

FieldType = Literal["text", "date", "money", "number"]
Mode = Literal["fields", "items"]

MAX_CUSTOM_FIELDS = 12
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class TemplateError(ValueError):
    """An invalid template or custom field list."""


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    label: str
    description: str
    type: FieldType = "text"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    name: str
    description: str
    mode: Mode
    fields: tuple[Field, ...]
    #: What one item is, for items mode ("obligation").
    item_noun: str = "item"


TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        Template(
            "obligations",
            "Obligations and deadlines",
            "Every obligation with who must do it and when: payments, notices, reports, renewals.",
            "items",
            (
                Field("obligation", "Obligation", "What must be done, in a short sentence."),
                Field("party", "Responsible party", "Who must do it."),
                Field(
                    "deadline",
                    "Deadline",
                    "When it is due, as the document says it (for example 'within 30 days of "
                    "the invoice date').",
                ),
                Field(
                    "due_date",
                    "Due date",
                    "The calendar date it is due, only if the document states a specific date.",
                    "date",
                ),
                Field("recurrence", "Recurrence", "How often it repeats, if it does."),
            ),
            item_noun="obligation",
        ),
        Template(
            "contract_terms",
            "Contract key terms",
            "Parties, dates, term and renewal, notice periods, payment terms, liability and "
            "governing law. Good for comparing contracts side by side.",
            "fields",
            (
                Field("parties", "Parties", "The parties to the agreement."),
                Field("effective_date", "Effective date", "When the agreement starts.", "date"),
                Field("term", "Term", "How long the agreement runs."),
                Field("renewal", "Renewal", "Whether and how it renews."),
                Field(
                    "termination_notice",
                    "Notice to end or not renew",
                    "Notice needed to terminate or stop renewal.",
                ),
                Field("payment_terms", "Payment terms", "Fees, invoicing and when payment is due."),
                Field("liability_cap", "Liability cap", "Limits on liability.", "money"),
                Field("governing_law", "Governing law", "Which law governs the agreement."),
            ),
        ),
        Template(
            "invoice",
            "Invoice details",
            "Vendor, invoice number, dates and amounts.",
            "fields",
            (
                Field("vendor", "Vendor", "Who issued the invoice."),
                Field("invoice_number", "Invoice number", "The invoice's number or reference."),
                Field("invoice_date", "Invoice date", "When the invoice was issued.", "date"),
                Field("due_date", "Due date", "When payment is due.", "date"),
                Field("total", "Total", "The total amount due, with currency.", "money"),
            ),
        ),
    )
}

CUSTOM = "custom"


def custom_fields(raw: list[dict[str, Any]]) -> tuple[Field, ...]:
    """Validate user-defined fields: 1 to 12, unique keys derived from labels."""
    if not raw:
        raise TemplateError("Add at least one field.")
    if len(raw) > MAX_CUSTOM_FIELDS:
        raise TemplateError(f"Use at most {MAX_CUSTOM_FIELDS} fields.")
    fields: list[Field] = []
    seen: set[str] = set()
    for item in raw:
        label = " ".join(str(item.get("label", "")).split())[:80]
        if not label:
            raise TemplateError("Every field needs a name.")
        key = str(item.get("key") or "").strip() or _slug(label)
        if not _KEY.match(key):
            key = _slug(label)
        base, n = key, 2
        while key in seen:
            key = f"{base[:36]}_{n}"
            n += 1
        seen.add(key)
        kind = item.get("type", "text")
        if kind not in ("text", "date", "money", "number"):
            raise TemplateError(f"Unknown field type '{kind}'.")
        description = " ".join(str(item.get("description") or "").split())[:300] or label
        fields.append(Field(key, label, description, kind))
    return tuple(fields)


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "field"
    return slug if slug[0].isalpha() else f"f_{slug}"[:40]
