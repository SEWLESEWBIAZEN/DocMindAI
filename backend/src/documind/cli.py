"""Operational commands: ``documind-manage <command>``.

Commands:
  bootstrap-storage   Create the private application bucket if it is missing.
  check               Run all dependency health checks; exit 1 if any fails.
  jobs                List processing jobs (optionally by --status).
  sweep               Run one recovery sweep (re-queue stuck jobs, re-dispatch).
  process JOB_ID      Run one processing job synchronously (debugging).
  reindex             Queue documents embedded with a different model than the
                      configured one (or --all) for re-processing.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import uuid
from typing import Any

from documind.application.identity.common import utcnow
from documind.application.processing.embedding_service import EmbeddingService
from documind.application.processing.jobs import new_job
from documind.application.processing.orchestrator import ProcessingOrchestrator
from documind.application.processing.sweeper import ProcessingSweeper
from documind.container import Container, build_container
from documind.core.config import get_settings
from documind.core.logging import configure_logging
from documind.infrastructure.database.repositories.processing import (
    IndexRepository,
    ProcessingJobRepository,
    SessionEmbeddingCache,
)
from documind.infrastructure.storage.bootstrap import ensure_private_bucket
from documind.infrastructure.storage.client import create_s3_client


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))  # noqa: T201


def _bootstrap_storage() -> int:
    settings = get_settings()
    client = create_s3_client(settings)
    try:
        result = ensure_private_bucket(client, settings.s3_bucket, settings.s3_region)
    finally:
        client.close()
    state = "created" if result.created else "already exists"
    print(f"Bucket {result.bucket!r} {state} and is private.")  # noqa: T201
    return 0


async def _with_container(fn: Any) -> int:
    container = build_container(get_settings())
    try:
        result: int = await fn(container)
        return result
    finally:
        await container.aclose()


async def _check(container: Container) -> int:
    report = await container.health_service.check_readiness()
    _print(
        {
            "status": report.status.value,
            "components": {
                c.name: {
                    "status": c.status.value,
                    "latency_ms": c.latency_ms,
                    "error": c.error,
                    **c.details,
                }
                for c in report.components
            },
        }
    )
    return 0 if report.is_healthy else 1


async def _jobs(container: Container, status: str | None, limit: int) -> int:
    async with container.session_factory() as session:
        jobs = await ProcessingJobRepository(session).list(status=status, limit=limit)
    _print(
        [
            {
                "id": j.id,
                "document_id": j.document_id,
                "status": j.status,
                "stage": j.stage,
                "attempts": f"{j.attempts}/{j.max_attempts}",
                "error": j.error_code,
                "updated_at": j.updated_at,
            }
            for j in jobs
        ]
    )
    return 0


async def _sweep(container: Container) -> int:
    result = await ProcessingSweeper(
        container.session_factory, container.settings, container.job_dispatcher
    ).sweep()
    _print(dataclasses.asdict(result))
    return 0


async def _process(container: Container, job_id: uuid.UUID) -> int:
    embeddings = EmbeddingService(
        container.embedding_provider,
        SessionEmbeddingCache(container.session_factory),
        batch_size=container.settings.embedding_batch_size,
    )
    orchestrator = ProcessingOrchestrator(
        container.session_factory,
        container.settings,
        container.storage,
        container.extractor,
        embeddings,
        worker="cli",
    )
    outcome = await orchestrator.run(job_id)
    _print(
        {"job_id": job_id, "outcome": outcome.status, "retry_in_seconds": outcome.retry_in_seconds}
    )
    return 0 if outcome.status in {"succeeded", "skipped"} else 1


async def _reindex(container: Container, workspace_id: uuid.UUID | None, include_all: bool) -> int:
    model = container.embedding_provider.model
    queued: list[uuid.UUID] = []
    async with container.session_factory() as session:
        jobs = ProcessingJobRepository(session)
        documents = await IndexRepository(session).documents_needing_reindex(
            model, workspace_id=workspace_id, include_all=include_all
        )
        for doc in documents:
            if await jobs.active_for_document(doc.id) is None:
                job = new_job(
                    doc, container.settings, now=utcnow(), requested_by=None, dispatched=True
                )
                jobs.add(job)
                doc.status = "pending"
                queued.append(job.id)
        await session.commit()
    for job_id in queued:
        await container.job_dispatcher.dispatch(job_id)
    _print({"model": model, "queued": len(queued)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="documind-manage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap-storage", help="create the private application bucket")
    sub.add_parser("check", help="check dependency health")
    jobs = sub.add_parser("jobs", help="list processing jobs")
    jobs.add_argument(
        "--status",
        choices=["queued", "running", "succeeded", "failed", "dead", "superseded", "cancelled"],
    )
    jobs.add_argument("--limit", type=int, default=50)
    sub.add_parser("sweep", help="re-queue stuck jobs and re-dispatch queued ones")
    process = sub.add_parser("process", help="run one processing job synchronously")
    process.add_argument("job_id", type=uuid.UUID)
    reindex = sub.add_parser("reindex", help="re-embed documents indexed with another model")
    reindex.add_argument("--workspace", type=uuid.UUID, default=None)
    reindex.add_argument("--all", action="store_true", help="re-index every ready document")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "bootstrap-storage":
        return _bootstrap_storage()
    if args.command == "check":
        return asyncio.run(_with_container(_check))
    if args.command == "jobs":
        return asyncio.run(_with_container(lambda c: _jobs(c, args.status, args.limit)))
    if args.command == "sweep":
        return asyncio.run(_with_container(_sweep))
    if args.command == "process":
        return asyncio.run(_with_container(lambda c: _process(c, args.job_id)))
    return asyncio.run(_with_container(lambda c: _reindex(c, args.workspace, args.all)))


if __name__ == "__main__":
    sys.exit(main())
