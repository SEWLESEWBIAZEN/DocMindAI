"""Runs an extraction in the background, one document at a time (ADR 0034)."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.application.extraction.prompts import Window, messages, windows
from documind.application.identity.common import utcnow
from documind.application.ports import ChatMessage, LLMProvider
from documind.core.config import Settings
from documind.domain.extraction.parsing import (
    Cited,
    Row,
    UnreadableReplyError,
    dedupe_items,
    merge_fields,
    parse_fields,
    parse_items,
)
from documind.domain.extraction.templates import TEMPLATES, Field
from documind.domain.processing.errors import ProcessingError, ProviderAccessError
from documind.infrastructure.database.models import ExtractionResult, ExtractionRun
from documind.infrastructure.database.repositories.retrieval import RetrievalRepository

logger = logging.getLogger(__name__)

LLMFor = Callable[[uuid.UUID, uuid.UUID | None], Awaitable[LLMProvider]]
_MAX_CHUNKS = 5000


@dataclass(slots=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


def excerpt(text: str, value: str, width: int = 280) -> str:
    """The part of a chunk around a value (or its start), for showing the source."""
    flat = " ".join(text.split())
    probe = re.escape(" ".join(value.split())[:40])
    match = re.search(probe, flat, re.IGNORECASE) if probe else None
    if match is None:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, match.start() - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _stored(cited: Cited, window: Window) -> dict[str, Any]:
    chunk = window.chunks[cited.source - 1]
    return {
        "value": cited.value,
        "date": cited.date.isoformat() if cited.date else None,
        "chunk_id": str(chunk.chunk_id),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "excerpt": excerpt(chunk.text, cited.value),
    }


class ExtractionRunner:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], llm_for: LLMFor, settings: Settings
    ):
        self._sessions = sessions
        self._llm_for = llm_for
        self._settings = settings

    async def run(self, run_id: uuid.UUID) -> str:
        async with self._sessions() as s:
            run = await s.get(ExtractionRun, run_id, with_for_update=True)
            if run is None or run.status != "queued":
                return "skipped"
            run.status = "running"
            run.started_at = run.heartbeat_at = utcnow()
            workspace_id, user_id = run.workspace_id, run.created_by_user_id
            fields = tuple(Field(**f) for f in run.fields)
            mode = run.mode
            template = TEMPLATES.get(run.template_key)
            noun = template.item_noun if template else "item"
            document_ids = [uuid.UUID(d) for d in run.document_ids]
            await s.commit()

        usage = _Usage()
        try:
            llm = await self._llm_for(workspace_id, user_id)
            for index, document_id in enumerate(document_ids):
                await self._document(
                    run_id, workspace_id, index, document_id, llm, fields, mode, noun, usage
                )
        except ProviderAccessError as exc:
            return await self._finish(run_id, usage, "failed", exc.code)
        except ProcessingError as exc:
            return await self._finish(
                run_id, usage, "failed", getattr(exc, "code", None) or "llm_error"
            )
        except Exception:
            logger.exception("extraction failed", extra={"run_id": str(run_id)})
            return await self._finish(run_id, usage, "failed", "extraction_failed")
        return await self._finish(run_id, usage, "completed", None)

    async def _generate(self, llm: LLMProvider, prompt: list[ChatMessage], usage: _Usage) -> str:
        result = await llm.generate(
            prompt, max_output_tokens=self._settings.extraction_max_output_tokens, temperature=None
        )
        usage.input_tokens += result.usage.input_tokens
        usage.output_tokens += result.usage.output_tokens
        usage.model = result.model or usage.model
        return result.text

    async def _window(
        self,
        llm: LLMProvider,
        title: str,
        window: Window,
        fields: tuple[Field, ...],
        mode: str,
        noun: str,
        usage: _Usage,
    ) -> list[Row] | None:
        prompt = messages(
            document_title=title,
            window=window,
            mode="fields" if mode == "fields" else "items",
            fields=fields,
            item_noun=noun,
        )
        reply = await self._generate(llm, prompt, usage)
        for attempt in range(2):
            try:
                if mode == "fields":
                    return [parse_fields(reply, fields, len(window.chunks))]
                return parse_items(reply, fields, len(window.chunks))
            except UnreadableReplyError:
                if attempt:
                    return None
                retry = [
                    *prompt,
                    ChatMessage("assistant", reply[:4000]),
                    ChatMessage("user", "That wasn't valid JSON. Reply with the JSON object only."),
                ]
                reply = await self._generate(llm, retry, usage)
        return None  # pragma: no cover

    async def _document(
        self,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        index: int,
        document_id: uuid.UUID,
        llm: LLMProvider,
        fields: tuple[Field, ...],
        mode: str,
        noun: str,
        usage: _Usage,
    ) -> None:
        warning: str | None = None
        stored: list[dict[str, Any]] = []
        async with self._sessions() as s:
            repo = RetrievalRepository(s)
            found = (await repo.documents(workspace_id, {document_id})).get(document_id)
            if found is None:
                warning = "not_found"
            elif found.archived or found.status != "ready":
                warning = "not_ready"
            else:
                chunks = await repo.document_chunks(
                    workspace_id, document_id, page_start=None, page_end=None, limit=_MAX_CHUNKS
                )
                parts, truncated = windows(
                    chunks,
                    max_tokens=self._settings.extraction_window_tokens,
                    max_windows=self._settings.extraction_max_calls_per_document,
                )
                if truncated:
                    warning = "partial"
                rows: list[tuple[Row, Window]] = []
                unreadable = 0
                for window in parts:
                    found_rows = await self._window(
                        llm, found.title, window, fields, mode, noun, usage
                    )
                    if found_rows is None:
                        unreadable += 1
                        continue
                    rows.extend((row, window) for row in found_rows)
                if unreadable and not rows:
                    warning = "unreadable"
                if mode == "fields":
                    merged = merge_fields([r for r, _ in rows], fields)
                    where = {
                        k: next(w for r, w in rows if k in r and r[k] is v)
                        for k, v in merged.items()
                    }
                    stored = [{k: _stored(v, where[k]) for k, v in merged.items()}]
                else:
                    kept = dedupe_items([r for r, _ in rows], fields)
                    ids = {id(r) for r in kept}
                    stored = [
                        {k: _stored(v, w) for k, v in r.items()} for r, w in rows if id(r) in ids
                    ]

        async with self._sessions() as s:
            run = await s.get(ExtractionRun, run_id, with_for_update=True)
            if run is None:
                return
            for position, values in enumerate(stored):
                s.add(
                    ExtractionResult(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        position=index * 10_000 + position,
                        values=values,
                    )
                )
            if warning:
                run.warnings = {**run.warnings, str(document_id): warning}
            run.documents_done = index + 1
            run.heartbeat_at = utcnow()
            run.input_tokens, run.output_tokens = usage.input_tokens, usage.output_tokens
            run.model = usage.model
            await s.commit()

    async def _finish(self, run_id: uuid.UUID, usage: _Usage, status: str, code: str | None) -> str:
        async with self._sessions() as s:
            run = await s.get(ExtractionRun, run_id, with_for_update=True)
            if run is not None:
                run.status = status
                run.error_code = code
                run.completed_at = utcnow()
                run.input_tokens, run.output_tokens = usage.input_tokens, usage.output_tokens
                run.model = usage.model or run.model
                await s.commit()
        return status
