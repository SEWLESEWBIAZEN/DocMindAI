"""Document and collection endpoints (workspace-scoped)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from documind.api.dependencies import CollectionServiceDep, DocumentServiceDep, WorkspaceCtx
from documind.api.schemas.documents import (
    CollectionResponse,
    CreateCollectionRequest,
    DocumentPageResponse,
    DocumentResponse,
    DocumentVersionResponse,
    DownloadUrlResponse,
    RenameDocumentRequest,
    UpdateCollectionRequest,
)
from documind.application.documents.collection_service import CollectionInfo
from documind.application.documents.document_service import DocumentInfo
from documind.domain.documents.files import SUPPORTED_EXTENSIONS, DocumentFormat
from documind.domain.documents.status import DocumentStatus
from documind.infrastructure.database.repositories.documents import DocumentFilter

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["documents"])


def _doc(d: DocumentInfo) -> DocumentResponse:
    return DocumentResponse(**{f: getattr(d, f) for f in DocumentResponse.model_fields})


def _collection(c: CollectionInfo) -> CollectionResponse:
    return CollectionResponse(**{f: getattr(c, f) for f in CollectionResponse.model_fields})


_UPLOAD_DESCRIPTION = (
    f"Multipart upload. Supported: {', '.join(SUPPORTED_EXTENSIONS)}. The file's contents must "
    "match its extension. Duplicate content in the same workspace is rejected (409)."
)


# --- Documents -----------------------------------------------------------------


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentResponse,
    description=_UPLOAD_DESCRIPTION,
)
async def upload_document(
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
    file: Annotated[UploadFile, File(description="The document to upload")],
    title: Annotated[str | None, Form(max_length=500)] = None,
    collection_id: Annotated[uuid.UUID | None, Form()] = None,
) -> DocumentResponse:
    info = await documents.upload(
        ctx, file.file, file.filename, title=title, collection_id=collection_id
    )
    return _doc(info)


@router.get("/documents", response_model=DocumentPageResponse, summary="List and search documents")
async def list_documents(
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
    q: Annotated[str | None, Query(max_length=200, description="Match title or filename")] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    format_filter: Annotated[DocumentFormat | None, Query(alias="format")] = None,
    collection_id: uuid.UUID | None = None,
    uploaded_by: uuid.UUID | None = None,
    archived: Annotated[
        Literal["exclude", "only", "include"],
        Query(description="Archived documents are hidden unless requested"),
    ] = "exclude",
    sort: Literal["created_at", "updated_at", "title", "size_bytes"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> DocumentPageResponse:
    page = await documents.search(
        ctx,
        DocumentFilter(
            query=q or None,
            status=status_filter.value if status_filter else None,
            format=format_filter.value if format_filter else None,
            collection_id=collection_id,
            uploaded_by=uploaded_by,
            archived=archived,
            sort=sort,
            descending=order == "desc",
            limit=limit,
            offset=offset,
        ),
    )
    return DocumentPageResponse(
        items=[_doc(d) for d in page.items], total=page.total, limit=page.limit, offset=page.offset
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID, ctx: WorkspaceCtx, documents: DocumentServiceDep
) -> DocumentResponse:
    return _doc(await documents.get(ctx, document_id))


@router.patch("/documents/{document_id}", response_model=DocumentResponse)
async def rename_document(
    document_id: uuid.UUID,
    body: RenameDocumentRequest,
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
) -> DocumentResponse:
    return _doc(await documents.rename(ctx, document_id, body.title))


@router.post(
    "/documents/{document_id}/archive",
    response_model=DocumentResponse,
    summary="Archive (members: own uploads; admins: any). Read-only and hidden by default.",
)
async def archive_document(
    document_id: uuid.UUID, ctx: WorkspaceCtx, documents: DocumentServiceDep
) -> DocumentResponse:
    return _doc(await documents.archive(ctx, document_id))


@router.post("/documents/{document_id}/restore", response_model=DocumentResponse)
async def restore_document(
    document_id: uuid.UUID, ctx: WorkspaceCtx, documents: DocumentServiceDep
) -> DocumentResponse:
    return _doc(await documents.restore(ctx, document_id))


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently delete (admins/owners; requires a recent 2FA step-up token)",
    description=(
        "Call POST /auth/mfa/verify first and use the returned access token. Errors: 403 "
        "`mfa_enrollment_required` (2FA not set up) or `mfa_step_up_required` (no recent check)."
    ),
)
async def delete_document(
    document_id: uuid.UUID, ctx: WorkspaceCtx, documents: DocumentServiceDep
) -> Response:
    await documents.delete(ctx, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/documents/{document_id}/download-url",
    response_model=DownloadUrlResponse,
    summary="Short-lived signed URL that downloads the file",
)
async def download_url(
    document_id: uuid.UUID,
    response: Response,
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
    version_id: uuid.UUID | None = None,
) -> DownloadUrlResponse:
    signed = await documents.download_url(ctx, document_id, version_id)
    response.headers["Cache-Control"] = "no-store"
    return DownloadUrlResponse(
        url=signed.url, expires_in=signed.expires_in, filename=signed.filename
    )


@router.get("/documents/{document_id}/versions", response_model=list[DocumentVersionResponse])
async def list_versions(
    document_id: uuid.UUID, ctx: WorkspaceCtx, documents: DocumentServiceDep
) -> list[DocumentVersionResponse]:
    return [
        DocumentVersionResponse(**{f: getattr(v, f) for f in DocumentVersionResponse.model_fields})
        for v in await documents.versions(ctx, document_id)
    ]


@router.post(
    "/documents/{document_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentResponse,
    summary="Upload a new version (the document is reprocessed)",
)
async def upload_version(
    document_id: uuid.UUID,
    ctx: WorkspaceCtx,
    documents: DocumentServiceDep,
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    return _doc(await documents.upload_version(ctx, document_id, file.file, file.filename))


# --- Collections ---------------------------------------------------------------


@router.get("/collections", response_model=list[CollectionResponse], tags=["collections"])
async def list_collections(
    ctx: WorkspaceCtx, collections: CollectionServiceDep
) -> list[CollectionResponse]:
    return [_collection(c) for c in await collections.list(ctx)]


@router.post(
    "/collections",
    status_code=status.HTTP_201_CREATED,
    response_model=CollectionResponse,
    tags=["collections"],
)
async def create_collection(
    body: CreateCollectionRequest, ctx: WorkspaceCtx, collections: CollectionServiceDep
) -> CollectionResponse:
    return _collection(await collections.create(ctx, body.name, body.description))


@router.get("/collections/{collection_id}", response_model=CollectionResponse, tags=["collections"])
async def get_collection(
    collection_id: uuid.UUID, ctx: WorkspaceCtx, collections: CollectionServiceDep
) -> CollectionResponse:
    return _collection(await collections.get(ctx, collection_id))


@router.patch(
    "/collections/{collection_id}", response_model=CollectionResponse, tags=["collections"]
)
async def update_collection(
    collection_id: uuid.UUID,
    body: UpdateCollectionRequest,
    ctx: WorkspaceCtx,
    collections: CollectionServiceDep,
) -> CollectionResponse:
    return _collection(
        await collections.update(
            ctx,
            collection_id,
            name=body.name,
            description=body.description,
            description_set="description" in body.model_fields_set,
        )
    )


@router.delete(
    "/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["collections"],
    summary="Delete a collection (its documents are kept)",
)
async def delete_collection(
    collection_id: uuid.UUID, ctx: WorkspaceCtx, collections: CollectionServiceDep
) -> Response:
    await collections.delete(ctx, collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/collections/{collection_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["collections"],
    summary="Add a document to a collection (idempotent)",
)
async def add_to_collection(
    collection_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: WorkspaceCtx,
    collections: CollectionServiceDep,
) -> Response:
    await collections.add_document(ctx, collection_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/collections/{collection_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["collections"],
)
async def remove_from_collection(
    collection_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: WorkspaceCtx,
    collections: CollectionServiceDep,
) -> Response:
    await collections.remove_document(ctx, collection_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
