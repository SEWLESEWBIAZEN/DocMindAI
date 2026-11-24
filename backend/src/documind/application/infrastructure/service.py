"""Organization infrastructure settings (MI-1 to MI-5, ADR 0021).

Lifecycle of a profile version: draft → validated (or failed) → active →
retired. Versions are immutable; secrets go to the credential vault and the
configuration only references them. Activation needs owner rights, a fresh
2FA step-up and a successful validation of that exact version. Moving a
non-empty workspace to another database or bucket is refused with
``migration_required`` and recorded as a planned migration (MI-8): data only
moves once an owner approves it, and runs in ``migration.py`` (ADR 0025).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.billing.service import BillingService
from documind.application.credentials.vault import CredentialVault
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.infrastructure.resolver import InfrastructureResolver, verify
from documind.core.config import Settings
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import (
    WorkspaceContext,
    WorkspaceKind,
    ensure_recent_mfa,
    ensure_shared_workspace,
)
from documind.domain.infrastructure.profiles import (
    PLATFORM_DEFAULT,
    STORAGE_PROVIDERS,
    InfrastructureConfig,
    ProfileError,
    check_entitlements,
    parse_config,
    requires_migration,
)
from documind.infrastructure.database.dataplane_runtime import (
    create_dataplane_engine,
    dataplane_sessions,
    register_tenant,
)
from documind.infrastructure.database.models import (
    Conversation,
    Document,
    DocumentCollection,
    InfrastructureEvent,
    InfrastructureMigration,
    InfrastructureProfile,
    InfrastructureProfileVersion,
    WorkerRegistration,
    WorkspaceInfrastructure,
)
from documind.infrastructure.database.provisioning import (
    DedicatedProvisioner,
    ProvisioningUnavailableError,
    validate_database,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository
from documind.infrastructure.security.vault import EnvelopeCipher, VaultError
from documind.infrastructure.storage.factory import validate_storage

WORKER_SCOPES = ("jobs:claim", "jobs:complete", "documents:read")


@dataclass(frozen=True, slots=True)
class VersionView:
    id: uuid.UUID
    version: int
    status: str
    config: dict[str, Any]
    checksum: str
    validation: dict[str, Any]
    note: str | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None
    validated_at: datetime | None
    activated_at: datetime | None


@dataclass(frozen=True, slots=True)
class InfrastructureOverview:
    workspace_kind: str
    active_version_id: uuid.UUID | None
    active_config: dict[str, Any]
    versions: list[VersionView]
    entitlements: dict[str, bool]
    storage_providers: list[dict[str, str]]
    dedicated_available: bool
    events: list[dict[str, Any]]
    workers: list[dict[str, Any]]
    migrations: list[dict[str, Any]]
    has_data: bool


@dataclass(frozen=True, slots=True)
class IssuedWorkerToken:
    id: uuid.UUID
    name: str
    token: str  # shown once


def _view(row: InfrastructureProfileVersion) -> VersionView:
    return VersionView(
        row.id,
        row.version,
        row.status,
        dict(row.config),
        row.checksum,
        dict(row.validation or {}),
        row.note,
        row.created_at,
        row.created_by_user_id,
        row.validated_at,
        row.activated_at,
    )


class InfrastructureService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        cipher: EnvelopeCipher | None,
        resolver: InfrastructureResolver,
        meta: RequestMeta,
        *,
        provisioner: DedicatedProvisioner | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._vault = CredentialVault(session, cipher)
        self._resolver = resolver
        self._audit = AuditRecorder(AuditLogRepository(session), meta)
        self._provisioner = provisioner or DedicatedProvisioner(settings)

    # ---------------------------------------------------------------- guards

    def _require(self, ctx: WorkspaceContext) -> None:
        ctx.require(Permission.INFRASTRUCTURE_MANAGE)
        ensure_shared_workspace(ctx.workspace_kind)

    def _step_up(self, ctx: WorkspaceContext) -> None:
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)

    def _event(
        self,
        ctx: WorkspaceContext,
        kind: str,
        version_id: uuid.UUID | None,
        *,
        ok: bool | None,
        **details: Any,
    ) -> None:
        self._s.add(
            InfrastructureEvent(
                workspace_id=ctx.workspace_id,
                version_id=version_id,
                kind=kind,
                ok=ok,
                details=details,
            )
        )

    async def _profile(
        self, ctx: WorkspaceContext, *, create: bool
    ) -> InfrastructureProfile | None:
        row = (
            await self._s.execute(
                select(InfrastructureProfile).where(
                    InfrastructureProfile.workspace_id == ctx.workspace_id
                )
            )
        ).scalar_one_or_none()
        if row is None and create:
            row = InfrastructureProfile(
                workspace_id=ctx.workspace_id, created_by_user_id=ctx.user_id
            )
            self._s.add(row)
            await self._s.flush()
        return row

    async def _version(
        self, ctx: WorkspaceContext, version_id: uuid.UUID, *, lock: bool = False
    ) -> InfrastructureProfileVersion:
        stmt = select(InfrastructureProfileVersion).where(
            InfrastructureProfileVersion.id == version_id,
            InfrastructureProfileVersion.workspace_id == ctx.workspace_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Infrastructure version not found.")
        return row

    async def _active(
        self, ctx: WorkspaceContext
    ) -> tuple[InfrastructureProfileVersion | None, InfrastructureConfig]:
        pin = await self._s.get(WorkspaceInfrastructure, ctx.workspace_id)
        if pin is None:
            return None, PLATFORM_DEFAULT
        row = await self._s.get(InfrastructureProfileVersion, pin.version_id)
        if row is None:  # pragma: no cover - FK RESTRICT
            return None, PLATFORM_DEFAULT
        return row, verify(row)

    async def _migration_busy(self, ctx: WorkspaceContext) -> bool:
        busy = (
            await self._s.execute(
                select(func.count())
                .select_from(InfrastructureMigration)
                .where(
                    InfrastructureMigration.workspace_id == ctx.workspace_id,
                    InfrastructureMigration.status.in_(("approved", "running")),
                )
            )
        ).scalar_one()
        return bool(busy)

    async def _has_data(self, ctx: WorkspaceContext) -> bool:
        # Read-only counts: safe while a migration holds the workspace (MI-8).
        infra = await self._resolver.resolve(ctx.workspace_id, ignore_migration=True)
        async with infra.data_plane.sessions() as data:
            docs = (
                await data.execute(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.workspace_id == ctx.workspace_id)
                )
            ).scalar_one()
            convs = (
                await data.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.workspace_id == ctx.workspace_id)
                )
            ).scalar_one()
            collections = (
                await data.execute(
                    select(func.count())
                    .select_from(DocumentCollection)
                    .where(DocumentCollection.workspace_id == ctx.workspace_id)
                )
            ).scalar_one()
        return bool(docs or convs or collections)

    async def _reveal(self, ctx: WorkspaceContext, credential_id: uuid.UUID | None) -> str:
        if credential_id is None:
            raise ValidationFailedError(
                "A required credential is missing.", code="credential_missing"
            )
        row = await self._vault.get(ctx.workspace_id, credential_id)
        if row is None:
            raise ValidationFailedError(
                "A required credential is missing.", code="credential_missing"
            )
        try:
            return self._vault.reveal(row)
        except VaultError as exc:
            raise ConflictError(
                "A saved credential can't be read. Enter it again.", code="credential_unreadable"
            ) from exc

    # ----------------------------------------------------------------- reads

    async def overview(self, ctx: WorkspaceContext) -> InfrastructureOverview:
        ctx.require(Permission.INFRASTRUCTURE_MANAGE)
        ents = await BillingService(self._s).entitlements(ctx.workspace_id)
        active, config = (None, PLATFORM_DEFAULT)
        versions: list[VersionView] = []
        if ctx.workspace_kind is WorkspaceKind.ORGANIZATION:
            active, config = await self._active(ctx)
            versions = [
                _view(r)
                for r in (
                    await self._s.execute(
                        select(InfrastructureProfileVersion)
                        .where(InfrastructureProfileVersion.workspace_id == ctx.workspace_id)
                        .order_by(InfrastructureProfileVersion.version.desc())
                    )
                ).scalars()
            ]
        events = [
            {
                "kind": e.kind,
                "ok": e.ok,
                "version_id": e.version_id,
                "details": e.details,
                "created_at": e.created_at,
            }
            for e in (
                await self._s.execute(
                    select(InfrastructureEvent)
                    .where(InfrastructureEvent.workspace_id == ctx.workspace_id)
                    .order_by(InfrastructureEvent.id.desc())
                    .limit(30)
                )
            ).scalars()
        ]
        workers = [
            {
                "id": w.id,
                "name": w.name,
                "status": w.status,
                "token_prefix": w.token_prefix,
                "scopes": list(w.scopes),
                "created_at": w.created_at,
                "last_seen_at": w.last_seen_at,
                "revoked_at": w.revoked_at,
            }
            for w in (
                await self._s.execute(
                    select(WorkerRegistration)
                    .where(WorkerRegistration.workspace_id == ctx.workspace_id)
                    .order_by(WorkerRegistration.created_at.desc())
                )
            ).scalars()
        ]
        migrations = [
            {
                "id": m.id,
                "status": m.status,
                "from_version_id": m.from_version_id,
                "to_version_id": m.to_version_id,
                "plan": m.plan,
                "created_at": m.created_at,
            }
            for m in (
                await self._s.execute(
                    select(InfrastructureMigration)
                    .where(InfrastructureMigration.workspace_id == ctx.workspace_id)
                    .order_by(InfrastructureMigration.created_at.desc())
                    .limit(20)
                )
            ).scalars()
        ]
        return InfrastructureOverview(
            workspace_kind=ctx.workspace_kind.value,
            active_version_id=active.id if active else None,
            active_config=config.to_dict(),
            versions=versions,
            entitlements={
                "custom_storage": ents.custom_storage,
                "dedicated_database": ents.dedicated_database,
                "external_database": ents.external_database,
                "customer_workers": ents.customer_workers,
            },
            storage_providers=[
                {"key": p.key, "name": p.name, "status": p.status, "description": p.description}
                for p in STORAGE_PROVIDERS.values()
            ],
            dedicated_available=self._provisioner.available,
            events=events,
            workers=workers,
            migrations=migrations,
            has_data=await self._has_data(ctx),
        )

    # --------------------------------------------------------------- writes

    async def create_version(
        self,
        ctx: WorkspaceContext,
        raw: dict[str, Any],
        *,
        database_password: str | None = None,
        storage_secret_key: str | None = None,
        note: str | None = None,
    ) -> VersionView:
        """A new immutable draft. Secrets are encrypted in the vault (2FA step-up)."""
        self._require(ctx)
        self._step_up(ctx)
        try:
            config = parse_config(raw)
            check_entitlements(config, await BillingService(self._s).entitlements(ctx.workspace_id))
        except ProfileError as exc:
            raise ValidationFailedError(str(exc), code="invalid_infrastructure") from exc
        _, current = await self._active(ctx)

        db = config.database
        if db.mode == "dedicated":
            if current.database.mode == "dedicated" and database_password is None:
                db = current.database  # keep the provisioned database
            else:
                try:
                    provisioned, password = await self._provisioner.provision(ctx.workspace_id)
                except ProvisioningUnavailableError as exc:
                    raise ServiceUnavailableError(
                        "Dedicated databases aren't available on this server.",
                        code="dedicated_database_unavailable",
                    ) from exc
                cred = self._vault.store(
                    ctx.workspace_id, "database_password", password, created_by=ctx.user_id
                )
                del password
                db = replace(provisioned, password_credential_id=cred.id)
        elif db.mode == "external":
            if database_password:
                cred = self._vault.store(
                    ctx.workspace_id, "database_password", database_password, created_by=ctx.user_id
                )
                db = replace(db, password_credential_id=cred.id)
            elif (
                current.database.location() == db.location()
                and current.database.password_credential_id
            ):
                db = replace(db, password_credential_id=current.database.password_credential_id)
            else:
                raise ValidationFailedError(
                    "Enter the database password.", code="credential_missing"
                )

        st = config.storage
        if st.provider != "documind":
            if storage_secret_key:
                cred = self._vault.store(
                    ctx.workspace_id, "storage_secret", storage_secret_key, created_by=ctx.user_id
                )
                st = replace(st, secret_credential_id=cred.id)
            elif (
                current.storage.location() == st.location()
                and current.storage.access_key_id == st.access_key_id
                and current.storage.secret_credential_id
            ):
                st = replace(st, secret_credential_id=current.storage.secret_credential_id)
            else:
                raise ValidationFailedError(
                    "Enter the storage secret key.", code="credential_missing"
                )
        config = InfrastructureConfig(db, st, config.processing)

        profile = await self._profile(ctx, create=True)
        assert profile is not None  # noqa: S101 - created above
        latest = (
            await self._s.execute(
                select(func.coalesce(func.max(InfrastructureProfileVersion.version), 0)).where(
                    InfrastructureProfileVersion.profile_id == profile.id
                )
            )
        ).scalar_one()
        row = InfrastructureProfileVersion(
            profile_id=profile.id,
            workspace_id=ctx.workspace_id,
            version=int(latest) + 1,
            config=config.to_dict(),
            checksum=config.checksum(),
            status="draft",
            note=(note or None) and note[:255],
            created_by_user_id=ctx.user_id,
        )
        self._s.add(row)
        await self._s.flush()
        self._audit.record(
            "infrastructure.version_created",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="infrastructure_version",
            target_id=row.id,
            details={"version": row.version, "config": row.config},
        )
        self._event(ctx, "version_created", row.id, ok=None, version=row.version)
        await self._s.commit()
        return _view(row)

    async def validate(self, ctx: WorkspaceContext, version_id: uuid.UUID) -> VersionView:
        self._require(ctx)
        row = await self._version(ctx, version_id, lock=True)
        if row.status in ("active", "retired"):
            raise ConflictError("Only drafts can be validated.", code="version_not_draft")
        config = verify(row)
        results: dict[str, Any] = {}
        ok = True
        db = config.database
        if db.mode != "shared":
            password = await self._reveal(ctx, db.password_credential_id)
            check = await validate_database(
                self._settings, db, password, check_network=db.mode == "external"
            )
            del password
            results["database"] = {
                "ok": check.ok,
                "code": check.code,
                "message": check.message,
                **check.details,
            }
            ok = ok and check.ok
        else:
            results["database"] = {"ok": True, "mode": "shared"}
        st = config.storage
        if st.provider != "documind":
            secret = await self._reveal(ctx, st.secret_credential_id)
            scheck = await validate_storage(self._settings, st, secret)
            del secret
            results["storage"] = {
                "ok": scheck.ok,
                "code": scheck.code,
                "message": scheck.message,
                **(scheck.details or {}),
            }
            ok = ok and scheck.ok
        else:
            results["storage"] = {"ok": True, "provider": "documind"}
        results["processing"] = await self._check_processing(ctx, config)
        ok = ok and bool(results["processing"]["ok"])
        row.validation = {**results, "checked_at": utcnow().isoformat()}
        row.status = "validated" if ok else "failed"
        row.validated_at = utcnow()
        self._event(ctx, "validated" if ok else "validation_failed", row.id, ok=ok, results=results)
        self._audit.record(
            "infrastructure.version_validated",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="infrastructure_version",
            target_id=row.id,
            details={"ok": ok, "codes": {k: v.get("code") for k, v in results.items()}},
        )
        await self._s.commit()
        return _view(row)

    async def _check_processing(
        self, ctx: WorkspaceContext, config: InfrastructureConfig
    ) -> dict[str, Any]:
        """Customer-hosted processing (MI-7): the workers read the customer's own
        database and bucket, so the database must be external, and a registered
        worker must have checked in recently."""
        mode = config.processing.mode
        if mode == "documind":
            return {"ok": True, "mode": mode}
        if config.database.mode != "external":
            return {
                "ok": False,
                "code": "external_database_required",
                "message": "Customer-hosted workers process documents in your own database: "
                "choose an external database first.",
            }
        recent = utcnow() - timedelta(minutes=15)
        seen = (
            await self._s.execute(
                select(func.count())
                .select_from(WorkerRegistration)
                .where(
                    WorkerRegistration.workspace_id == ctx.workspace_id,
                    WorkerRegistration.status == "active",
                    WorkerRegistration.last_seen_at >= recent,
                )
            )
        ).scalar_one()
        if not seen:
            return {
                "ok": False,
                "code": "no_active_worker",
                "message": "Start a registered worker (documind-worker) so it checks in, "
                "then validate again.",
            }
        return {"ok": True, "mode": mode, "active_workers": int(seen)}

    async def activate(self, ctx: WorkspaceContext, version_id: uuid.UUID) -> VersionView:
        self._require(ctx)
        self._step_up(ctx)
        row = await self._version(ctx, version_id, lock=True)
        if row.status != "validated":
            raise ConflictError(
                "Validate this version before activating it.", code="version_not_validated"
            )
        target = verify(row)
        if await self._migration_busy(ctx):
            raise ConflictError(
                "A data migration is under way. Wait for it to finish.", code="migration_busy"
            )
        active, current = await self._active(ctx)
        moves = requires_migration(current, target)
        if moves and await self._has_data(ctx):
            plan = InfrastructureMigration(
                workspace_id=ctx.workspace_id,
                from_version_id=active.id if active else None,
                to_version_id=row.id,
                status="planned",
                plan={"moves": moves, "approval_required": True},
                requested_by_user_id=ctx.user_id,
            )
            self._s.add(plan)
            self._event(ctx, "migration_planned", row.id, ok=None, moves=moves)
            await self._s.commit()
            raise ConflictError(
                "This workspace already has documents or conversations, and this version moves its "
                f"{' and '.join(moves)}. That needs a data migration, which DocuMind plans but "
                "doesn't run without your explicit approval. A migration plan was recorded: "
                "approve it under Data migrations to move the data.",
                code="migration_required",
            )
        if target.database.mode != "shared":
            password = await self._reveal(ctx, target.database.password_credential_id)
            engine = create_dataplane_engine(self._settings, target.database, password, pool_size=1)
            del password
            try:
                await register_tenant(dataplane_sessions(engine), ctx.workspace_id)
            finally:
                await engine.dispose()
        now = utcnow()
        if active is not None:
            active.status = "retired"
            # Rotation: secrets only the retired version used are revoked (destroyed).
            keep = {target.database.password_credential_id, target.storage.secret_credential_id}
            for old in (
                current.database.password_credential_id,
                current.storage.secret_credential_id,
            ):
                if old is not None and old not in keep:
                    cred = await self._vault.get(ctx.workspace_id, old)
                    if cred is not None and cred.status == "active":
                        self._vault.revoke(cred)
        pin = await self._s.get(WorkspaceInfrastructure, ctx.workspace_id, with_for_update=True)
        if pin is None:
            self._s.add(WorkspaceInfrastructure(workspace_id=ctx.workspace_id, version_id=row.id))
        else:
            pin.version_id = row.id
        row.status = "active"
        row.activated_at = now
        row.activated_by_user_id = ctx.user_id
        self._event(
            ctx,
            "activated",
            row.id,
            ok=True,
            moves=moves,
            previous=str(active.id) if active else None,
        )
        self._audit.record(
            "infrastructure.version_activated",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="infrastructure_version",
            target_id=row.id,
            details={"version": row.version, "previous": str(active.id) if active else None},
        )
        await self._s.commit()
        self._resolver.invalidate(ctx.workspace_id)
        return _view(row)

    async def cancel_migration(self, ctx: WorkspaceContext, migration_id: uuid.UUID) -> None:
        self._require(ctx)
        result = await self._s.execute(
            update(InfrastructureMigration)
            .where(
                InfrastructureMigration.id == migration_id,
                InfrastructureMigration.workspace_id == ctx.workspace_id,
                InfrastructureMigration.status.in_(("planned", "approved")),
            )
            .values(status="cancelled")
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError("No pending migration with that id.")
        self._audit.record(
            "infrastructure.migration_cancelled",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="infrastructure_migration",
            target_id=migration_id,
        )
        await self._s.commit()

    async def approve_migration(self, ctx: WorkspaceContext, migration_id: uuid.UUID) -> None:
        """The customer's explicit consent to move data (owner, fresh 2FA)."""
        self._require(ctx)
        self._step_up(ctx)
        migration = (
            await self._s.execute(
                select(InfrastructureMigration)
                .where(
                    InfrastructureMigration.id == migration_id,
                    InfrastructureMigration.workspace_id == ctx.workspace_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if migration is None or migration.status != "planned":
            raise NotFoundError("No planned migration with that id.")
        target = await self._version(ctx, migration.to_version_id, lock=True)
        if target.status != "validated":
            raise ConflictError(
                "Validate the target version again before approving the migration.",
                code="version_not_validated",
            )
        active, current = await self._active(ctx)
        if (active.id if active else None) != migration.from_version_id or requires_migration(
            current, verify(target)
        ) != list(migration.plan.get("moves", [])):
            raise ConflictError(
                "The workspace's infrastructure changed since this plan was made. "
                "Activate the version again to get a new plan.",
                code="migration_plan_stale",
            )
        if await self._migration_busy(ctx):
            raise ConflictError("Another migration is already under way.", code="migration_busy")
        migration.status = "approved"
        migration.plan = {
            **migration.plan,
            "approved_by": str(ctx.user_id),
            "approved_at": utcnow().isoformat(),
        }
        self._event(ctx, "migration_approved", target.id, ok=None, migration=str(migration.id))
        self._audit.record(
            "infrastructure.migration_approved",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="infrastructure_migration",
            target_id=migration.id,
            details={"moves": migration.plan.get("moves", [])},
        )
        await self._s.commit()

    async def authorize_cleanup(self, ctx: WorkspaceContext, migration_id: uuid.UUID) -> None:
        """Deleting the retained source copy is irreversible: owner and fresh 2FA."""
        self._require(ctx)
        self._step_up(ctx)
        found = (
            await self._s.execute(
                select(InfrastructureMigration.id).where(
                    InfrastructureMigration.id == migration_id,
                    InfrastructureMigration.workspace_id == ctx.workspace_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError("Migration not found.")

    # -------------------------------------------------------------- workers

    async def register_worker(self, ctx: WorkspaceContext, name: str) -> IssuedWorkerToken:
        """MI-7 foundation: tokens are random, shown once, stored as SHA-256."""
        self._require(ctx)
        self._step_up(ctx)
        ents = await BillingService(self._s).entitlements(ctx.workspace_id)
        if not ents.customer_workers:
            raise PermissionDeniedError(
                "Your plan doesn't include customer-hosted processing.", code="not_in_plan"
            )
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValidationFailedError("Give the worker a name (up to 100 characters).")
        token = f"dmw_{secrets.token_urlsafe(32)}"
        row = WorkerRegistration(
            workspace_id=ctx.workspace_id,
            name=name,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            token_prefix=token[:12],
            scopes=list(WORKER_SCOPES),
            created_by_user_id=ctx.user_id,
        )
        self._s.add(row)
        await self._s.flush()
        self._audit.record(
            "infrastructure.worker_registered",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="worker",
            target_id=row.id,
            details={"name": name, "prefix": row.token_prefix},
        )
        await self._s.commit()
        return IssuedWorkerToken(row.id, name, token)

    async def revoke_worker(self, ctx: WorkspaceContext, worker_id: uuid.UUID) -> None:
        self._require(ctx)
        row = (
            await self._s.execute(
                select(WorkerRegistration).where(
                    WorkerRegistration.id == worker_id,
                    WorkerRegistration.workspace_id == ctx.workspace_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Worker not found.")
        if row.status != "revoked":
            row.status = "revoked"
            row.revoked_at = utcnow()
            self._audit.record(
                "infrastructure.worker_revoked",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="worker",
                target_id=row.id,
            )
            await self._s.commit()


async def authenticate_worker(session: AsyncSession, token: str) -> WorkerRegistration | None:
    """Look up an active worker by token (constant-time via the hash index)."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = (
        await session.execute(
            select(WorkerRegistration).where(WorkerRegistration.token_hash == digest)
        )
    ).scalar_one_or_none()
    if row is None or row.status != "active":
        return None
    return row
