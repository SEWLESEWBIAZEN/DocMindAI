"""Answer feedback and the admin quality summary (ADR 0038).

Members rate their own answers. Admins see counts for the whole workspace, and
the question and answer only where the member ticked "share with admins":
conversations are private (ADR 0016), so consent is per answer.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import utcnow
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models.conversations import FEEDBACK_REASONS
from documind.infrastructure.database.repositories.feedback import FeedbackRepository

MAX_COMMENT = 1000
SHARED_LIMIT = 50
EXCERPT = 2000


def clean_feedback(
    rating: str, reasons: Sequence[str], comment: str | None
) -> tuple[list[str], str | None]:
    """Reasons only explain a thumbs down; unknown ones are refused by the API schema."""
    kept = [r for r in dict.fromkeys(reasons) if r in FEEDBACK_REASONS] if rating == "down" else []
    text = (comment or "").strip()[:MAX_COMMENT] or None
    return kept, text


@dataclass(frozen=True, slots=True)
class ModelRating:
    model: str
    up: int
    down: int


@dataclass(frozen=True, slots=True)
class SharedFeedback:
    message_id: uuid.UUID
    rating: str
    reasons: tuple[str, ...]
    comment: str | None
    question: str
    answer: str
    answer_status: str | None
    citation_count: int
    model: str | None
    prompt_version: str | None
    rated_at: datetime


@dataclass(frozen=True, slots=True)
class QualitySummary:
    days: int
    answers: int
    insufficient_context: int
    rated: int
    up: int
    down: int
    reasons: dict[str, int]
    models: tuple[ModelRating, ...]
    shared: tuple[SharedFeedback, ...] = field(default_factory=tuple)


class AnswerQualityService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = FeedbackRepository(session)

    async def summary(self, ctx: WorkspaceContext, *, days: int) -> QualitySummary:
        ctx.require(Permission.USAGE_READ)
        since = utcnow() - timedelta(days=days)
        answers, insufficient = await self._repo.answer_counts(ctx.workspace_id, since)
        ratings = await self._repo.ratings(ctx.workspace_id, since)
        reasons: Counter[str] = Counter(dict.fromkeys(FEEDBACK_REASONS, 0))
        per_model: dict[str, Counter[str]] = {}
        for rating, why, model in ratings:
            per_model.setdefault(model or "unknown", Counter())[rating] += 1
            if rating == "down":
                reasons.update(why)
        up = sum(1 for r, _, _ in ratings if r == "up")
        shared = await self._repo.shared(ctx.workspace_id, since, limit=SHARED_LIMIT)
        return QualitySummary(
            days=days,
            answers=answers,
            insufficient_context=insufficient,
            rated=len(ratings),
            up=up,
            down=len(ratings) - up,
            reasons=dict(reasons),
            models=tuple(
                ModelRating(model, c["up"], c["down"]) for model, c in sorted(per_model.items())
            ),
            shared=tuple(
                SharedFeedback(
                    message_id=f.message_id,
                    rating=f.rating,
                    reasons=tuple(f.reasons or ()),
                    comment=f.comment,
                    question=question[:EXCERPT],
                    answer=m.content[:EXCERPT],
                    answer_status=m.answer_status,
                    citation_count=n,
                    model=m.model,
                    prompt_version=m.prompt_version,
                    rated_at=f.updated_at,
                )
                for f, m, question, n in shared
            ),
        )
