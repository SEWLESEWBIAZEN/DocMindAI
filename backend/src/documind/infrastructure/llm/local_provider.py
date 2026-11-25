"""Deterministic, offline "LLM" for development and tests. NOT for production.

It answers extractively: it reads the ``<source>`` blocks from the prompt,
quotes the sentences that best match the question with their ``[n]``
citation, and otherwise replies with the insufficient-context sentinel. It
cannot paraphrase or reason; configuration validation rejects it in
production.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from documind.application.ports import (
    ChatMessage,
    LLMDelta,
    LLMResult,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMUsage,
)
from documind.domain.processing.chunking import split_sentences
from documind.domain.processing.tokens import estimate_tokens
from documind.domain.rag.prompts import INSUFFICIENT_CONTEXT_SENTINEL

_SOURCE = re.compile(r'<source id="(\d+)"[^>]*>(.*?)</source>', re.DOTALL)
_QUESTION = re.compile(r"<question>(.*?)</question>", re.DOTALL)
_EXTRACTION = re.compile(r"<extraction>(.*?)</extraction>", re.DOTALL)
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_DATE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\s+(\d{{4}})\b", re.IGNORECASE)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DUTY = re.compile(
    r"\b(must|will|shall|is due|are due|are payable|is payable|need to|needs to|has to)\b",
    re.IGNORECASE,
)
_WHEN = re.compile(
    rf"\b(?:within|at least|no later than|by|before|after|every|each)\b[^.;]*?"
    rf"(?:\d+\s+(?:business\s+)?(?:days?|hours?|weeks?|months?|years?)|{_MONTHS}|quarter|year|month)"
    rf"[^.;,]*",
    re.IGNORECASE,
)
_RECUR = re.compile(
    r"\b(every\s+\d*\s*\w+|each\s+\w+(?:\s+\w+)?|annually|monthly|quarterly)\b", re.I
)
_SUBJECT = re.compile(
    r"^(?:\d+(?:\.\d+)*\s+)?((?:[A-Z][\w&'-]*\s?){1,3}?)\s+(?:must|will|shall|may|need)"
)
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "why",
        "when",
        "where",
        "does",
        "did",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "there",
        "their",
        "them",
        "they",
        "have",
        "has",
        "had",
        "our",
        "your",
        "you",
        "can",
        "could",
        "should",
        "would",
        "about",
        "into",
        "over",
        "under",
        "than",
        "then",
        "also",
        "any",
        "all",
        "not",
        "but",
        "its",
        "per",
        "via",
    ]
)


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


class LocalExtractiveLLM:
    model = "local-extractive-v1"

    def _answer(self, messages: list[ChatMessage]) -> str:
        prompt = "\n".join(m.content for m in messages if m.role == "user")
        spec = _EXTRACTION.search(prompt)
        if spec:
            return _extract(json.loads(spec.group(1)), _SOURCE.findall(prompt))
        question_match = _QUESTION.search(prompt)
        raw_question = question_match.group(1) if question_match else prompt
        if "summar" in raw_question.lower():
            # Summaries: the first sentence of the first few sources, cited.
            lead = []
            for source_id, body in _SOURCE.findall(prompt)[:4]:
                sentences = split_sentences(" ".join(body.split()))
                if sentences:
                    lead.append(f"{sentences[0]} [{source_id}]")
            if lead:
                return " ".join(lead)
        question = _terms(raw_question)
        scored: list[tuple[float, int, str]] = []
        for source_id, body in _SOURCE.findall(prompt):
            for sentence in split_sentences(" ".join(body.split())):
                overlap = len(question & _terms(sentence))
                if question and overlap / len(question) >= 0.34:
                    scored.append((overlap / len(question), int(source_id), sentence))
        if not scored:
            return (
                f"{INSUFFICIENT_CONTEXT_SENTINEL} The provided sources don't contain information "
                "that answers this question."
            )
        best = sorted(scored, key=lambda item: (-item[0], item[1]))[:3]
        return " ".join(f"{sentence} [{source_id}]" for _, source_id, sentence in best)

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        text = self._answer(messages)
        return LLMResult(
            text=text, usage=self._usage(messages, text), model=self.model, finish_reason="stop"
        )

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        text = self._answer(messages)
        for piece in re.findall(r"\S+\s*", text):
            yield LLMDelta(piece)
        yield LLMStreamEnd(
            usage=self._usage(messages, text), model=self.model, finish_reason="stop"
        )

    @staticmethod
    def _usage(messages: list[ChatMessage], text: str) -> LLMUsage:
        return LLMUsage(
            input_tokens=sum(estimate_tokens(m.content) for m in messages),
            output_tokens=estimate_tokens(text),
        )


# --------------------------------------------------------------------------- #
# Structured extraction (ADR 0034): rule-based, so development works offline.
# --------------------------------------------------------------------------- #


def _stems(text: str) -> set[str]:
    """Crude stemming for matching field names to sentences ("governs" ~ "governed")."""
    out = set()
    for word in _terms(text):
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out


def _clean(sentence: str) -> str:
    return " ".join(sentence.replace("**", "").replace("*", "").split()).strip(" -")


def _iso(sentence: str) -> str | None:
    m = _ISO.search(sentence)
    if m:
        return m.group(0)
    m = _DATE.search(sentence)
    if not m:
        return None
    month = _MONTHS.split("|").index(m.group(2).lower()) + 1
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"


def _cite(value: str | None, source: int, *, date: str | None = None) -> dict[str, Any] | None:
    if not value:
        return None
    cited: dict[str, Any] = {"value": value[:300], "source": source}
    if date is not None:
        cited["date"] = date
    return cited


def _extract(spec: dict[str, Any], sources: list[tuple[str, str]]) -> str:
    fields: list[dict[str, Any]] = spec.get("fields", [])
    sentences = [
        (int(sid), _clean(sentence))
        for sid, body in sources
        for sentence in split_sentences(" ".join(body.split()))
    ]
    if spec.get("mode") == "fields":
        values: dict[str, Any] = {}
        for field in fields:
            terms = _stems(f"{field['label']} {field.get('description', '')}")
            best: tuple[int, int, str] | None = None
            for sid, sentence in sentences:
                score = len(terms & _stems(sentence))
                if field.get("type") == "date" and _iso(sentence):
                    score += 1
                if score >= 2 and (best is None or score > best[0]):
                    best = (score, sid, sentence)
            if best is None:
                values[field["key"]] = None
                continue
            _, sid, sentence = best
            date = _iso(sentence) if field.get("type") == "date" else None
            values[field["key"]] = _cite(sentence, sid, date=date)
        return json.dumps({"values": values})

    items = []
    for sid, sentence in sentences:
        duty, when = _DUTY.search(sentence), _WHEN.search(sentence)
        if not (duty and (when or _iso(sentence))):
            continue
        item: dict[str, Any] = {}
        for i, field in enumerate(fields):
            key, kind = field["key"], field.get("type")
            if i == 0:
                item[key] = _cite(sentence, sid)
            elif kind == "date":
                date = _iso(sentence)
                written = _DATE.search(sentence) or _ISO.search(sentence)
                item[key] = _cite(written.group(0) if written else None, sid, date=date)
            elif "party" in key or "who" in field["label"].lower():
                subject = _SUBJECT.search(sentence)
                item[key] = _cite(subject.group(1).strip() if subject else None, sid)
            elif "recur" in key:
                recur = _RECUR.search(sentence)
                item[key] = _cite(recur.group(0) if recur else None, sid)
            elif "deadline" in key or "when" in field["label"].lower():
                item[key] = _cite(when.group(0).strip() if when else None, sid)
            else:
                item[key] = None
        items.append(item)
    return json.dumps({"items": items})
