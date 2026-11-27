"""Operational gauges in Prometheus text format (Phase 10 observability).

Request-level metrics (rates, latencies, status codes) come from the
structured access logs. These gauges describe system state that alerts need:
dependencies, the processing queue, failures, data planes, AI keys and backups.
Computed per scrape from PostgreSQL and Redis, so every API process reports
the same values.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import func, select

from documind.application.identity.common import utcnow
from documind.domain.health import ComponentStatus
from documind.infrastructure.database.models import (
    Message,
    ProcessingJob,
    UsageRecord,
    WorkspaceAIProvider,
)
from documind.worker.celery_factory import QUEUE

BACKUP_KEY = "ops:backup:last_success"


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._seen: set[str] = set()

    def gauge(self, name: str, help_text: str, value: float, **labels: str) -> None:
        if name not in self._seen:
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} gauge")
            self._seen.add(name)
        label = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        self.lines.append(f"{name}{{{label}}} {value}" if label else f"{name} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


async def render(container: Any) -> str:
    out = _Out()
    out.gauge("documind_up", "The API process is serving.", 1)

    report = await container.health_service.check_readiness()
    for c in report.components:
        out.gauge(
            "documind_dependency_up",
            "Dependency health (1 ok, 0 failing).",
            1 if c.status is ComponentStatus.OK else 0,
            component=c.name,
        )

    depth = await container.redis.llen(QUEUE)
    out.gauge("documind_queue_depth", "Messages waiting in the processing queue.", float(depth))

    now = utcnow()
    since = now - timedelta(hours=24)
    async with container.session_factory() as session:
        statuses = (
            await session.execute(
                select(ProcessingJob.status, func.count())
                .where(ProcessingJob.updated_at >= since)
                .group_by(ProcessingJob.status)
            )
        ).all()
        for status, count in statuses:
            out.gauge(
                "documind_processing_jobs_24h",
                "Processing jobs updated in the last 24 hours, by status (shared data plane).",
                float(count),
                status=status,
            )
        oldest = (
            await session.execute(
                select(func.min(ProcessingJob.created_at)).where(ProcessingJob.status == "queued")
            )
        ).scalar_one_or_none()
        out.gauge(
            "documind_oldest_queued_job_seconds",
            "Age of the oldest queued job (shared data plane).",
            (now - oldest).total_seconds() if oldest else 0.0,
        )
        failed_answers = (
            await session.execute(
                select(func.count()).where(
                    Message.role == "assistant",
                    Message.status == "failed",
                    Message.updated_at >= since,
                )
            )
        ).scalar_one()
        out.gauge(
            "documind_failed_answers_24h",
            "Answers that failed in the last 24 hours.",
            float(failed_answers),
        )
        invalid_keys = (
            await session.execute(
                select(func.count())
                .select_from(WorkspaceAIProvider)
                .where(WorkspaceAIProvider.status == "invalid")
            )
        ).scalar_one()
        out.gauge(
            "documind_ai_keys_invalid",
            "Workspace AI keys rejected by their provider.",
            float(invalid_keys),
        )
        usage = (
            await session.execute(
                select(
                    UsageRecord.capability,
                    UsageRecord.billed_to,
                    func.coalesce(
                        func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
                    ),
                )
                .where(UsageRecord.occurred_at >= since)
                .group_by(UsageRecord.capability, UsageRecord.billed_to)
            )
        ).all()
        for capability, billed_to, tokens in usage:
            out.gauge(
                "documind_ai_tokens_24h",
                "AI tokens used in the last 24 hours.",
                float(tokens),
                capability=capability,
                billed_to=billed_to,
            )

    planes = await container.infrastructure().isolated_planes()
    up = 0
    for plane, _ in planes:
        try:
            async with plane.sessions() as data:
                await data.connection()
            up += 1
        except Exception:  # noqa: S112 - counted as down
            continue
    out.gauge(
        "documind_dataplanes",
        "Dedicated and external data planes.",
        float(len(planes)),
        state="total",
    )
    out.gauge("documind_dataplanes", "Dedicated and external data planes.", float(up), state="up")

    try:
        last_backup = await container.redis.get(BACKUP_KEY)
    except (RedisError, OSError):  # Redis down: still report everything else
        last_backup = None
    out.gauge(
        "documind_rate_limiter_degraded",
        "1 while Redis is down and the per-process rate-limit fallback is in use.",
        1.0 if getattr(container.rate_limiter, "degraded", False) else 0.0,
    )
    out.gauge(
        "documind_backup_last_success_timestamp",
        "Unix time of the last successful backup (0: never).",
        float(last_backup or 0),
    )
    tls = tls_certificate_state(container.settings.tls_certificate_file)
    if tls is not None:
        expiry, self_signed = tls
        out.gauge(
            "documind_tls_certificate_expiry_timestamp",
            "Unix time the served TLS certificate expires (0: unreadable).",
            expiry,
        )
        out.gauge(
            "documind_tls_certificate_self_signed",
            "1 while the temporary self-signed certificate is served (no Let's Encrypt yet).",
            1.0 if self_signed else 0.0,
        )
    out.gauge("documind_scrape_timestamp", "When these values were computed.", time.time())
    return out.text()


def tls_certificate_state(path: str | None) -> tuple[float, bool] | None:
    """(expiry as Unix time, self-signed?) of a PEM certificate; (0, False) if unreadable."""
    if not path:
        return None
    from cryptography import x509

    try:
        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return 0.0, False
    return cert.not_valid_after_utc.timestamp(), cert.issuer == cert.subject
