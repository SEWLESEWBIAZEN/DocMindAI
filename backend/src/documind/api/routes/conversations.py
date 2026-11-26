"""Conversations: private, persistent chats over a workspace's documents."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from documind.api.dependencies import (
    AnswerQualityServiceDep,
    ConversationServiceDep,
    WorkspaceCtx,
    admit_answer,
    limit_ai_requests,
)
from documind.api.schemas.conversations import (
    AnswerQualityResponse,
    ConversationDetailResponse,
    ConversationPageResponse,
    ConversationResponse,
    CreateConversationRequest,
    MessageCitationResponse,
    MessageFeedbackRequest,
    MessagePageResponse,
    MessageResponse,
    RenameConversationRequest,
    ReplyResponse,
    SendMessageRequest,
    SourcesRequest,
)
from documind.api.sse import event_stream, wants_event_stream
from documind.application.conversations.service import StartedReply, collect

router = APIRouter(prefix="/workspaces/{workspace_id}/conversations", tags=["conversations"])
quality_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["conversations"])

_STREAM_DOC = (
    "Send `Accept: text/event-stream` to stream the answer as Server-Sent Events "
    "(`message.created`, `answer.delta`…, then `answer.completed`, `answer.cancelled` or "
    "`answer.error`). Otherwise the call waits and returns the finished answer. "
    "Disconnecting cancels generation; the partial answer is saved as `cancelled`, "
    "never as `completed`."
)


async def _reply(started: StartedReply, accept: str | None) -> Response:
    if wants_event_stream(accept):
        return event_stream(started)
    final = await collect(started)
    body = ReplyResponse(
        user_message=MessageResponse.of(started.user_message) if started.user_message else None,
        assistant_message=MessageResponse.of(final),
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status.HTTP_201_CREATED)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ConversationDetailResponse)
async def create_conversation(
    body: CreateConversationRequest, ctx: WorkspaceCtx, conversations: ConversationServiceDep
) -> ConversationDetailResponse:
    detail = await conversations.create(
        ctx, title=body.title, document_ids=body.document_ids, collection_ids=body.collection_ids
    )
    return ConversationDetailResponse.of_detail(detail)


@router.get("", response_model=ConversationPageResponse, summary="Your conversations, newest first")
async def list_conversations(
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
    q: Annotated[str | None, Query(max_length=200, description="Match the title")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ConversationPageResponse:
    items, total = await conversations.list_conversations(ctx, query=q, offset=offset, limit=limit)
    return ConversationPageResponse(
        items=[ConversationResponse.of(c) for c in items], total=total, limit=limit, offset=offset
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID, ctx: WorkspaceCtx, conversations: ConversationServiceDep
) -> ConversationDetailResponse:
    return ConversationDetailResponse.of_detail(await conversations.get(ctx, conversation_id))


@router.patch("/{conversation_id}", response_model=ConversationDetailResponse, summary="Rename")
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> ConversationDetailResponse:
    detail = await conversations.rename(ctx, conversation_id, body.title)
    return ConversationDetailResponse.of_detail(detail)


@router.put(
    "/{conversation_id}/sources",
    response_model=ConversationDetailResponse,
    summary="Select the documents and collections this conversation answers from",
)
async def set_conversation_sources(
    conversation_id: uuid.UUID,
    body: SourcesRequest,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> ConversationDetailResponse:
    detail = await conversations.set_sources(
        ctx, conversation_id, document_ids=body.document_ids, collection_ids=body.collection_ids
    )
    return ConversationDetailResponse.of_detail(detail)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID, ctx: WorkspaceCtx, conversations: ConversationServiceDep
) -> Response:
    await conversations.delete(ctx, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{conversation_id}/messages", response_model=MessagePageResponse)
async def list_messages(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
    before: Annotated[
        int | None, Query(ge=1, description="Load messages before this sequence number")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_superseded: Annotated[
        bool, Query(description="Include answers replaced by regeneration")
    ] = False,
) -> MessagePageResponse:
    items, has_more = await conversations.messages(
        ctx,
        conversation_id,
        before_sequence=before,
        limit=limit,
        include_superseded=include_superseded,
    )
    return MessagePageResponse(
        items=[MessageResponse.of(m) for m in items],
        has_more=has_more,
        next_before=items[0].sequence if has_more and items else None,
    )


@router.post(
    "/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=ReplyResponse,
    summary="Ask a question in this conversation",
    description=_STREAM_DOC,
    dependencies=[Depends(limit_ai_requests), Depends(admit_answer)],
    response_class=StreamingResponse,
    responses={201: {"content": {"application/json": {}, "text/event-stream": {}}}},
)
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    return await _reply(await conversations.send(ctx, conversation_id, body.content), accept)


@router.get("/{conversation_id}/messages/{message_id}", response_model=MessageResponse)
async def get_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> MessageResponse:
    return MessageResponse.of(await conversations.message(ctx, conversation_id, message_id))


@router.get(
    "/{conversation_id}/messages/{message_id}/citations",
    response_model=list[MessageCitationResponse],
)
async def get_message_citations(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> list[MessageCitationResponse]:
    message = await conversations.message(ctx, conversation_id, message_id)
    return [MessageCitationResponse.of(c) for c in message.citations]


@router.post(
    "/{conversation_id}/messages/{message_id}/regenerate",
    status_code=status.HTTP_201_CREATED,
    response_model=ReplyResponse,
    summary="Answer the latest question again (replaces the latest answer)",
    description=_STREAM_DOC,
    dependencies=[Depends(limit_ai_requests), Depends(admit_answer)],
    response_class=StreamingResponse,
    responses={201: {"content": {"application/json": {}, "text/event-stream": {}}}},
)
async def regenerate_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    started = await conversations.regenerate(ctx, conversation_id, message_id)
    return await _reply(started, accept)


@router.post(
    "/{conversation_id}/messages/{message_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
    summary="Stop generating an answer",
)
async def cancel_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> MessageResponse:
    return MessageResponse.of(await conversations.cancel(ctx, conversation_id, message_id))


@router.put(
    "/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageResponse,
    summary="Rate an answer (thumbs up or down)",
    description="Rating again replaces your earlier rating. Only finished answers can be rated.",
)
async def rate_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: MessageFeedbackRequest,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> MessageResponse:
    message = await conversations.rate(
        ctx,
        conversation_id,
        message_id,
        rating=body.rating,
        reasons=body.reasons,
        comment=body.comment,
        shared=body.shared,
    )
    return MessageResponse.of(message)


@router.delete(
    "/{conversation_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove your rating",
)
async def unrate_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    ctx: WorkspaceCtx,
    conversations: ConversationServiceDep,
) -> Response:
    await conversations.unrate(ctx, conversation_id, message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@quality_router.get(
    "/answer-quality",
    response_model=AnswerQualityResponse,
    summary="How members rate answers (admins)",
    description=(
        "Counts for the whole workspace. Questions and answers appear only where the "
        "member chose to share them."
    ),
)
async def answer_quality(
    ctx: WorkspaceCtx,
    quality: AnswerQualityServiceDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AnswerQualityResponse:
    return AnswerQualityResponse.of(await quality.summary(ctx, days=days))
