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
import getpass
import json
import sys
import uuid
from typing import Any

from sqlalchemy import select

from documind.application.billing.service import BillingService
from documind.application.credentials.vault import CredentialVault
from documind.application.identity.common import utcnow
from documind.application.infrastructure.resolver import InfrastructureTargets
from documind.application.infrastructure.sweeps import sweep_processing
from documind.application.processing.jobs import new_job
from documind.application.processing.orchestrator import ProcessingOrchestrator
from documind.container import Container, build_container, processing_ai
from documind.core.config import get_settings
from documind.core.logging import configure_logging
from documind.domain.billing.plans import PLANS
from documind.infrastructure.database.models import Document
from documind.infrastructure.database.repositories.processing import (
    IndexRepository,
    ProcessingJobRepository,
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
    result = await sweep_processing(
        container.infrastructure(), container.settings, container.job_dispatcher
    )
    _print(dataclasses.asdict(result))
    return 0


async def _process(
    container: Container, job_id: uuid.UUID, workspace_id: uuid.UUID | None = None
) -> int:
    orchestrator = ProcessingOrchestrator(
        container.session_factory,
        container.settings,
        container.storage,
        container.extractor,
        worker="cli",
        ai=processing_ai(container),
        targets=InfrastructureTargets(container.infrastructure()),
    )
    outcome = await orchestrator.run(job_id, workspace_id=workspace_id)
    _print(
        {"job_id": job_id, "outcome": outcome.status, "retry_in_seconds": outcome.retry_in_seconds}
    )
    return 0 if outcome.status in {"succeeded", "skipped"} else 1


async def _reindex(container: Container, workspace_id: uuid.UUID | None, include_all: bool) -> int:
    """Re-embed documents whose chunks use another model than their workspace's.

    Each workspace is compared with its own embedding model (its AI key may
    differ from the platform's, ADR 0019).
    """
    async with container.session_factory() as session:
        if workspace_id is not None:
            workspaces = [workspace_id]
        else:
            workspaces = list(
                (
                    await session.execute(
                        select(Document.workspace_id).where(Document.status == "ready").distinct()
                    )
                ).scalars()
            )
    queued: list[tuple[uuid.UUID, uuid.UUID]] = []
    models: dict[str, int] = {}
    for ws in workspaces:
        model = (await container.ai_resolver().resolve(ws)).embeddings.model
        async with container.session_factory() as session:
            jobs = ProcessingJobRepository(session)
            documents = await IndexRepository(session).documents_needing_reindex(
                model, workspace_id=ws, include_all=include_all
            )
            for doc in documents:
                if await jobs.active_for_document(doc.id) is None:
                    job = new_job(
                        doc, container.settings, now=utcnow(), requested_by=None, dispatched=True
                    )
                    jobs.add(job)
                    doc.status = "pending"
                    queued.append((job.id, ws))
                    models[model] = models.get(model, 0) + 1
            await session.commit()
    for job_id, ws in queued:
        await container.job_dispatcher.dispatch(job_id, workspace_id=ws)
    _print({"workspaces": len(workspaces), "queued": len(queued), "by_model": models})
    return 0


async def _rewrap(container: Container) -> int:
    async with container.session_factory() as session:
        done, current = await CredentialVault(session, container.cipher).rewrap_all()
    _print({"rewrapped": done, "already_current": current})
    return 0


async def _plans() -> int:
    _print(
        [{"key": p.key.value, "name": p.name, **p.entitlements.to_dict()} for p in PLANS.values()]
    )
    return 0


async def _plan_assign(
    container: Container,
    workspace_id: uuid.UUID,
    plan: str,
    overrides: list[str],
    note: str | None,
) -> int:
    parsed: dict[str, object] = {}
    for item in overrides:
        name, _, raw = item.partition("=")
        parsed[name] = json.loads(raw) if raw else None
    async with container.session_factory() as session:
        row = await BillingService(session).assign(
            workspace_id,
            plan,
            overrides=parsed,
            operator=f"cli:{getpass.getuser()}",
            note=note,
        )
    if container.entitlements is not None:
        container.entitlements.invalidate(workspace_id)
    _print({"workspace_id": workspace_id, "plan": row.plan_key, "overrides": row.overrides})
    return 0


def _objects(command: str, directory: str, bucket: str | None) -> int:
    from pathlib import Path

    from documind.ops.objects import export_objects, import_objects

    settings = get_settings()
    client = create_s3_client(settings)
    target = bucket or settings.s3_bucket
    try:
        if command == "export":
            manifest = export_objects(client, target, Path(directory))
            _print({"bucket": target, "exported": len(manifest["objects"])})
        else:
            ensure_private_bucket(client, target, settings.s3_region)
            _print({"bucket": target, "imported": import_objects(client, target, Path(directory))})
    finally:
        client.close()
    return 0


def _backup(command: str, source: str, target: str, passphrase_file: str) -> int:
    from documind.ops.backup_crypto import BackupIntegrityError, passphrase_from, seal, unseal

    passphrase = passphrase_from(passphrase_file)
    with open(source, "rb") as src, open(target, "wb") as dst:
        try:
            (seal if command == "seal" else unseal)(src, dst, passphrase)
        except BackupIntegrityError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
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
    process.add_argument(
        "--workspace", type=uuid.UUID, default=None, help="for jobs in a dedicated data plane"
    )
    reindex = sub.add_parser("reindex", help="re-embed documents indexed with another model")
    reindex.add_argument("--workspace", type=uuid.UUID, default=None)
    reindex.add_argument("--all", action="store_true", help="re-index every ready document")
    creds = sub.add_parser("credentials", help="credential vault maintenance (ADR 0022)")
    creds_sub = creds.add_subparsers(dest="credentials_command", required=True)
    creds_sub.add_parser(
        "rewrap", help="re-wrap every credential with the first key in CREDENTIAL_ENCRYPTION_KEYS"
    )
    objects = sub.add_parser("objects", help="export or import object storage (backups)")
    objects.add_argument("objects_command", choices=["export", "import"])
    objects.add_argument("directory")
    objects.add_argument("--bucket", default=None, help="defaults to S3_BUCKET")
    backup = sub.add_parser("backup", help="encrypt or decrypt a backup archive")
    backup.add_argument("backup_command", choices=["seal", "unseal"])
    backup.add_argument("source")
    backup.add_argument("target")
    backup.add_argument("--passphrase-file", required=True)
    plan = sub.add_parser("plan", help="list plans or assign one to a workspace (audited)")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_sub.add_parser("list", help="show the plan catalog")
    assign = plan_sub.add_parser("assign", help="assign a plan to a workspace")
    assign.add_argument("workspace_id", type=uuid.UUID)
    assign.add_argument("plan", choices=[p.value for p in PLANS])
    assign.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="entitlement override, e.g. --override max_members=100 (repeatable)",
    )
    assign.add_argument("--note", default=None)
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
    if args.command == "backup":
        return _backup(args.backup_command, args.source, args.target, args.passphrase_file)
    if args.command == "objects":
        return _objects(args.objects_command, args.directory, args.bucket)
    if args.command == "credentials":
        return asyncio.run(_with_container(_rewrap))
    if args.command == "plan":
        if args.plan_command == "list":
            return asyncio.run(_plans())
        return asyncio.run(
            _with_container(
                lambda c: _plan_assign(c, args.workspace_id, args.plan, args.override, args.note)
            )
        )
    if args.command == "process":
        return asyncio.run(_with_container(lambda c: _process(c, args.job_id, args.workspace)))
    return asyncio.run(_with_container(lambda c: _reindex(c, args.workspace, args.all)))


if __name__ == "__main__":
    sys.exit(main())
