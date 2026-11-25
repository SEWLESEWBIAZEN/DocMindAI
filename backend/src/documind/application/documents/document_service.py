"""Document management use cases. All methods are scoped to a WorkspaceContext."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.documents.uploads import InspectedUpload, inspect_upload
from documind.application.identity.common import (
    AuditRecorder,
    RequestMeta,
    audit_repository,
    utcnow,
)
from documind.application.ports import (
    JobDispatcher,
    MalwareScanner,
    ObjectStorageProvider,
    ScannerUnavailableError,
)
from documind.application.processing.jobs import new_job
from documind.core.config import Settings
from documind.domain.documents.files import (
    MEDIA_TYPES,
    DocumentFormat,
    clean_title,
    title_from_filename,
)
from documind.domain.documents.status import DocumentStatus
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import (
    WorkspaceContext,
    ensure_can_archive,
    ensure_recent_mfa,
)
from documind.infrastructure.database.models import (
    Document,
    DocumentCollectionItem,
    DocumentVersion,
)
from documind.infrastructure.database.repositories.documents import (
    CollectionRepository,
    DocumentFilter,
    DocumentRepository,
)
from documind.infrastructure.database.repositories.processing import ProcessingJobRepository

logger = logging.getLogger(__name__)

DOCUMENT_NOT_FOUND = "Document not found."


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    id: uuid.UUID
    title: str
    original_filename: str
    format: DocumentFormat
    media_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    processing_error: str | None
    page_count: int | None
    chunk_count: int | None
    processed_at: datetime | None
    version_count: int
    current_version_id: uuid.UUID | None
    collection_ids: list[uuid.UUID]
    uploaded_by_user_id: uuid.UUID | None
    archived_at: datetime | None
    archived_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: list[DocumentInfo]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class VersionInfo:
    id: uuid.UUID
    version_number: int
    original_filename: str
    format: DocumentFormat
    size_bytes: int
    checksum_sha256: str
    scan_status: str
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime
    is_current: bool


@dataclass(frozen=True, slots=True)
class SignedDownload:
    url: str
    expires_in: int
    filename: str


def _object_key(workspace_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID) -> str:
    """Storage keys are built only from server-generated IDs (no user input)."""
    return f"workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}"


def _download_media_type(fmt: DocumentFormat) -> str:
    return MEDIA_TYPES[fmt]


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage: ObjectStorageProvider,
        scanner: MalwareScanner,
        meta: RequestMeta,
        dispatcher: JobDispatcher,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._scanner = scanner
        self._dispatcher = dispatcher
        self._jobs = ProcessingJobRepository(session)
        self._documents = DocumentRepository(session)
        self._collections = CollectionRepository(session)
        self._audit = AuditRecorder(audit_repository(session), meta)

    # ------------------------------------------------------------------ helpers

    def _info(self, doc: Document, collection_ids: list[uuid.UUID] | None = None) -> DocumentInfo:
        return DocumentInfo(
            id=doc.id,
            title=doc.title,
            original_filename=doc.original_filename,
            format=DocumentFormat(doc.format),
            media_type=doc.media_type,
            size_bytes=doc.size_bytes,
            checksum_sha256=doc.checksum_sha256,
            status=DocumentStatus(doc.status),
            processing_error=doc.processing_error,
            page_count=doc.page_count,
            chunk_count=doc.chunk_count,
            processed_at=doc.processed_at,
            version_count=doc.version_count,
            current_version_id=doc.current_version_id,
            collection_ids=collection_ids or [],
            uploaded_by_user_id=doc.uploaded_by_user_id,
            archived_at=doc.archived_at,
            archived_by_user_id=doc.archived_by_user_id,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )

    @staticmethod
    def _ensure_active(doc: Document) -> None:
        if doc.archived_at is not None:
            raise ConflictError(
                "This document is archived. Restore it to make changes.", code="document_archived"
            )

    async def _require(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, *, for_update: bool = False
    ) -> Document:
        doc = await self._documents.get(ctx.workspace_id, document_id, for_update=for_update)
        if doc is None:
            raise NotFoundError(DOCUMENT_NOT_FOUND)
        return doc

    async def _inspect_and_scan(
        self, file: BinaryIO, filename: str | None
    ) -> tuple[InspectedUpload, str]:
        inspected = await asyncio.to_thread(
            inspect_upload, file, filename, max_bytes=self._settings.max_upload_bytes
        )
        try:
            result = await self._scanner.scan(file)
        except ScannerUnavailableError as exc:
            logger.error("Malware scanner unavailable; rejecting upload")
            raise ServiceUnavailableError(
                "File scanning is temporarily unavailable. Try again shortly."
            ) from exc
        if not result.clean:
            logger.warning(
                "Upload blocked by malware scanner", extra={"signature": result.signature}
            )
            raise UnsupportedMediaTypeError(
                "This file was flagged as unsafe and can't be uploaded.", code="malware_detected"
            )
        return inspected, "clean" if result.scanner != "none" else "not_scanned"

    async def _ensure_not_duplicate(self, ctx: WorkspaceContext, checksum: str) -> None:
        existing = await self._documents.by_checksum(ctx.workspace_id, checksum)
        if existing is not None:
            where = " (archived; restore it instead)" if existing.archived_at else ""
            raise ConflictError(
                f"This file is already in the workspace as “{existing.title}”{where}.",
                code="duplicate_document",
            )

    async def _store(self, key: str, file: BinaryIO, inspected: InspectedUpload) -> None:
        await self._storage.upload(
            key,
            file,
            content_type=inspected.media_type,
            metadata={"sha256": inspected.checksum_sha256},
        )

    async def _discard(self, key: str) -> None:
        try:
            await self._storage.delete(key)
        except Exception:
            logger.exception("Failed to delete orphaned object", extra={"key": key})

    # ------------------------------------------------------------------ upload

    async def upload(
        self,
        ctx: WorkspaceContext,
        file: BinaryIO,
        filename: str | None,
        *,
        title: str | None = None,
        collection_id: uuid.UUID | None = None,
    ) -> DocumentInfo:
        ctx.require(Permission.DOCUMENTS_WRITE)
        inspected, scan_status = await self._inspect_and_scan(file, filename)
        final_title = clean_title(title) if title else title_from_filename(inspected.filename)
        if (
            collection_id is not None
            and await self._collections.get(ctx.workspace_id, collection_id) is None
        ):
            raise NotFoundError("Collection not found.")
        await self._ensure_not_duplicate(ctx, inspected.checksum_sha256)

        document_id, version_id = uuid.uuid4(), uuid.uuid4()
        key = _object_key(ctx.workspace_id, document_id, version_id)
        await self._store(key, file, inspected)
        try:
            doc = Document(
                id=document_id,
                workspace_id=ctx.workspace_id,
                title=final_title,
                original_filename=inspected.filename,
                format=inspected.format.value,
                media_type=inspected.media_type,
                size_bytes=inspected.size_bytes,
                checksum_sha256=inspected.checksum_sha256,
                status=DocumentStatus.PENDING.value,
                uploaded_by_user_id=ctx.user_id,
            )
            self._documents.add(doc)
            await self._session.flush()
            self._documents.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    workspace_id=ctx.workspace_id,
                    version_number=1,
                    storage_key=key,
                    original_filename=inspected.filename,
                    format=inspected.format.value,
                    media_type=inspected.media_type,
                    size_bytes=inspected.size_bytes,
                    checksum_sha256=inspected.checksum_sha256,
                    scan_status=scan_status,
                    uploaded_by_user_id=ctx.user_id,
                )
            )
            await self._session.flush()
            doc.current_version_id = version_id
            if collection_id is not None:
                self._documents.add(
                    DocumentCollectionItem(
                        collection_id=collection_id,
                        document_id=document_id,
                        workspace_id=ctx.workspace_id,
                        added_by_user_id=ctx.user_id,
                    )
                )
            # Transactional outbox: the job is committed with the document.
            job = new_job(
                doc, self._settings, now=utcnow(), requested_by=ctx.user_id, dispatched=True
            )
            self._jobs.add(job)
            self._audit.record(
                "document.uploaded",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="document",
                target_id=document_id,
                details={
                    "title": final_title,
                    "format": inspected.format.value,
                    "size_bytes": inspected.size_bytes,
                },
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            await self._discard(key)
            raise ConflictError(
                "This file is already in the workspace.", code="duplicate_document"
            ) from exc
        except BaseException:
            await self._session.rollback()
            await self._discard(key)
            raise
        await self._dispatcher.dispatch(job.id, workspace_id=job.workspace_id)
        await self._session.refresh(doc)
        return self._info(doc, [collection_id] if collection_id else [])

    async def upload_version(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, file: BinaryIO, filename: str | None
    ) -> DocumentInfo:
        ctx.require(Permission.DOCUMENTS_WRITE)
        await self._require(ctx, document_id)
        inspected, scan_status = await self._inspect_and_scan(file, filename)
        doc = await self._require(ctx, document_id, for_update=True)
        self._ensure_active(doc)
        if doc.checksum_sha256 == inspected.checksum_sha256:
            raise ConflictError(
                "This file is identical to the current version.", code="duplicate_document"
            )
        await self._ensure_not_duplicate(ctx, inspected.checksum_sha256)

        version_id = uuid.uuid4()
        key = _object_key(ctx.workspace_id, document_id, version_id)
        await self._store(key, file, inspected)
        try:
            number = doc.version_count + 1
            self._documents.add(
                DocumentVersion(
                    id=version_id,
                    document_id=doc.id,
                    workspace_id=ctx.workspace_id,
                    version_number=number,
                    storage_key=key,
                    original_filename=inspected.filename,
                    format=inspected.format.value,
                    media_type=inspected.media_type,
                    size_bytes=inspected.size_bytes,
                    checksum_sha256=inspected.checksum_sha256,
                    scan_status=scan_status,
                    uploaded_by_user_id=ctx.user_id,
                )
            )
            await self._session.flush()
            doc.current_version_id = version_id
            doc.version_count = number
            doc.original_filename = inspected.filename
            doc.format = inspected.format.value
            doc.media_type = inspected.media_type
            doc.size_bytes = inspected.size_bytes
            doc.checksum_sha256 = inspected.checksum_sha256
            # The new content must be processed again; an in-flight job for the
            # previous version is superseded (its results are discarded).
            doc.status = DocumentStatus.PENDING.value
            doc.processing_error = None
            doc.page_count = None
            await self._supersede_active_job(doc.id)
            job = new_job(
                doc, self._settings, now=utcnow(), requested_by=ctx.user_id, dispatched=True
            )
            self._jobs.add(job)
            self._audit.record(
                "document.version_uploaded",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="document",
                target_id=doc.id,
                details={"version": number, "format": inspected.format.value},
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            await self._discard(key)
            raise ConflictError(
                "This file is already in the workspace.", code="duplicate_document"
            ) from exc
        except BaseException:
            await self._session.rollback()
            await self._discard(key)
            raise
        await self._dispatcher.dispatch(job.id, workspace_id=job.workspace_id)
        return await self.get(ctx, document_id)

    async def _supersede_active_job(self, document_id: uuid.UUID) -> None:
        active = await self._jobs.active_for_document(document_id)
        if active is not None:
            active.status = "superseded"
            active.finished_at = utcnow()
            active.lease_expires_at = None
            await self._session.flush()

    async def reprocess(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> uuid.UUID:
        """Queue the current version for processing again (e.g. after a failure)."""
        ctx.require(Permission.DOCUMENTS_WRITE)
        doc = await self._require(ctx, document_id, for_update=True)
        if await self._jobs.active_for_document(doc.id) is not None:
            raise ConflictError(
                "This document is already queued or being processed.",
                code="processing_in_progress",
            )
        job = new_job(doc, self._settings, now=utcnow(), requested_by=ctx.user_id, dispatched=True)
        self._jobs.add(job)
        doc.status = DocumentStatus.PENDING.value
        doc.processing_error = None
        self._audit.record(
            "document.reprocess_requested",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="document",
            target_id=doc.id,
            details={"job_id": str(job.id)},
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:  # lost a race with another reprocess request
            await self._session.rollback()
            raise ConflictError(
                "This document is already queued or being processed.",
                code="processing_in_progress",
            ) from exc
        await self._dispatcher.dispatch(job.id, workspace_id=job.workspace_id)
        return job.id

    # ------------------------------------------------------------------ read

    async def search(self, ctx: WorkspaceContext, f: DocumentFilter) -> DocumentPage:
        ctx.require(Permission.DOCUMENTS_READ)
        if (
            f.collection_id
            and await self._collections.get(ctx.workspace_id, f.collection_id) is None
        ):
            raise NotFoundError("Collection not found.")
        docs, total = await self._documents.search(ctx.workspace_id, f)
        collections = await self._documents.collection_ids(ctx.workspace_id, [d.id for d in docs])
        return DocumentPage(
            items=[self._info(d, collections.get(d.id)) for d in docs],
            total=total,
            limit=f.limit,
            offset=f.offset,
        )

    async def get(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> DocumentInfo:
        ctx.require(Permission.DOCUMENTS_READ)
        doc = await self._require(ctx, document_id)
        collections = await self._documents.collection_ids(ctx.workspace_id, [doc.id])
        return self._info(doc, collections.get(doc.id))

    async def versions(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> list[VersionInfo]:
        ctx.require(Permission.DOCUMENTS_READ)
        doc = await self._require(ctx, document_id)
        return [
            VersionInfo(
                id=v.id,
                version_number=v.version_number,
                original_filename=v.original_filename,
                format=DocumentFormat(v.format),
                size_bytes=v.size_bytes,
                checksum_sha256=v.checksum_sha256,
                scan_status=v.scan_status,
                uploaded_by_user_id=v.uploaded_by_user_id,
                created_at=v.created_at,
                is_current=v.id == doc.current_version_id,
            )
            for v in await self._documents.versions(ctx.workspace_id, document_id)
        ]

    async def download_url(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, version_id: uuid.UUID | None = None
    ) -> SignedDownload:
        ctx.require(Permission.DOCUMENTS_READ)
        doc = await self._require(ctx, document_id)
        target = version_id or doc.current_version_id
        version = (
            await self._documents.get_version(ctx.workspace_id, document_id, target)
            if target
            else None
        )
        if version is None:
            raise NotFoundError("Version not found.")
        ttl = self._settings.signed_url_ttl_seconds
        url = self._storage.generate_signed_url(
            version.storage_key,
            expires_in=ttl,
            download_filename=version.original_filename,
            content_type=_download_media_type(DocumentFormat(version.format)),
        )
        self._audit.record(
            "document.download_url_issued",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="document",
            target_id=doc.id,
            details={"version": version.version_number},
        )
        await self._session.commit()
        return SignedDownload(url=url, expires_in=ttl, filename=version.original_filename)

    # ------------------------------------------------------------------ write

    async def rename(
        self, ctx: WorkspaceContext, document_id: uuid.UUID, title: str
    ) -> DocumentInfo:
        ctx.require(Permission.DOCUMENTS_WRITE)
        new_title = clean_title(title)
        doc = await self._require(ctx, document_id, for_update=True)
        self._ensure_active(doc)
        old = doc.title
        doc.title = new_title
        self._audit.record(
            "document.renamed",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="document",
            target_id=doc.id,
            details={"from": old, "to": new_title},
        )
        await self._session.commit()
        return await self.get(ctx, document_id)

    async def archive(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> DocumentInfo:
        """Hide a document and make it read-only. Members: own uploads; admins: any."""
        doc = await self._require(ctx, document_id, for_update=True)
        ensure_can_archive(ctx, doc.uploaded_by_user_id)
        if doc.archived_at is None:
            doc.archived_at = utcnow()
            doc.archived_by_user_id = ctx.user_id
            self._audit.record(
                "document.archived",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="document",
                target_id=doc.id,
                details={"title": doc.title},
            )
            await self._session.commit()
        return await self.get(ctx, document_id)

    async def restore(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> DocumentInfo:
        doc = await self._require(ctx, document_id, for_update=True)
        ensure_can_archive(ctx, doc.uploaded_by_user_id)
        if doc.archived_at is not None:
            doc.archived_at = None
            doc.archived_by_user_id = None
            self._audit.record(
                "document.restored",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="document",
                target_id=doc.id,
                details={"title": doc.title},
            )
            await self._session.commit()
        return await self.get(ctx, document_id)

    async def delete(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> None:
        """Permanent deletion: admins/owners only, after a recent 2FA check (ADR 0013)."""
        ctx.require(Permission.DOCUMENTS_DELETE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        doc = await self._require(ctx, document_id, for_update=True)
        await self._delete(ctx, doc, "document.deleted", {"mfa_verified": True})

    async def ids_by_checksum(
        self, ctx: WorkspaceContext, checksums: frozenset[str]
    ) -> list[uuid.UUID]:
        ctx.require(Permission.DOCUMENTS_READ)
        return [d.id for d in await self._documents.by_checksums(ctx.workspace_id, checksums)]

    async def checksums_present(
        self, ctx: WorkspaceContext, checksums: frozenset[str]
    ) -> list[str]:
        ctx.require(Permission.DOCUMENTS_READ)
        docs = await self._documents.by_checksums(ctx.workspace_id, checksums)
        return [d.checksum_sha256 for d in docs]

    async def delete_sample(self, ctx: WorkspaceContext, document_id: uuid.UUID) -> bool:
        """Remove an unchanged sample document (ADR 0033). No 2FA: it holds no user data.

        Returns False (and keeps the document) if it was changed, e.g. a version was added.
        """
        from documind.application.onboarding.samples import is_sample

        ctx.require(Permission.DOCUMENTS_WRITE)
        doc = await self._require(ctx, document_id, for_update=True)
        versions = await self._documents.versions(ctx.workspace_id, document_id)
        if not is_sample(doc.checksum_sha256, len(versions)):
            await self._session.rollback()
            return False
        await self._delete(ctx, doc, "document.sample_removed", {})
        return True

    async def _delete(
        self, ctx: WorkspaceContext, doc: Document, action: str, extra: dict[str, object]
    ) -> None:
        document_id = doc.id
        keys = [
            v.storage_key for v in await self._documents.versions(ctx.workspace_id, document_id)
        ]
        title = doc.title
        await self._documents.delete(doc)
        self._audit.record(
            action,
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="document",
            target_id=document_id,
            details={"title": title, "versions": len(keys), **extra},
        )
        await self._session.commit()
        # After commit: a failed object delete leaves an orphan in a private
        # bucket (logged for cleanup), never a document pointing at nothing.
        for key in keys:
            await self._discard(key)
