"""Conversations: private, persistent, workspace-scoped chats over documents.

Access rule: a conversation is visible only to the member who created it,
inside its workspace, while they hold ``conversations.use``. Anything else
(another member, another workspace, a removed member) gets 404, the same as a
conversation that doesn't exist.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.conversations.streaming import (
    ReplyCompleted,
    ReplyEvent,
    ReplyFailed,
    ReplyRunner,
    ReplyStarted,
)
from documind.application.conversations.views import (
    ConversationDetail,
    ConversationInfo,
    MessageInfo,
    SelectedCollection,
    SelectedDocument,
    citation_info,
    conversation_info,
    message_info,
)
from documind.application.identity.common import (
    AuditRecorder,
    RequestMeta,
    audit_repository,
    utcnow,
)
from documind.application.ports import CancellationRegistry
from documind.application.rag.orchestrator import AnswerRequest, RAGOrchestrator
from documind.application.rag.retrieval import MAX_QUERY_CHARS
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.infrastructure.database.models import Conversation, Message
from documind.infrastructure.database.repositories.conversations import (
    ConversationRepository,
    MessageRepository,
)
from documind.infrastructure.database.repositories.retrieval import RetrievalRepository
from documind.infrastructure.database.repositories.visibility import viewer_of

TITLE_MAX = 200
AUTO_TITLE_CHARS = 80
DEFAULT_TITLE = "New conversation"
MAX_SOURCES = 100


@dataclass(frozen=True, slots=True)
class StartedReply:
    """Messages are committed; iterate ``events`` to generate the answer."""

    user_message: MessageInfo | None
    assistant_message: MessageInfo
    events: AsyncIterator[ReplyEvent]


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    if not cleaned:
        raise ValidationFailedError("Title can't be empty.")
    if len(cleaned) > TITLE_MAX:
        raise ValidationFailedError(f"Title must be at most {TITLE_MAX} characters.")
    return cleaned


def auto_title(question: str) -> str:
    text = " ".join(question.split())
    if len(text) <= AUTO_TITLE_CHARS:
        return text
    cut = text[:AUTO_TITLE_CHARS].rsplit(" ", 1)[0] or text[:AUTO_TITLE_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def _clean_question(content: str) -> str:
    text = content.strip()
    if not text:
        raise ValidationFailedError("Enter a question.", code="empty_message")
    if len(text) > MAX_QUERY_CHARS:
        raise ValidationFailedError(
            f"Messages are limited to {MAX_QUERY_CHARS} characters.", code="message_too_long"
        )
    return text


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        meta: RequestMeta,
        *,
        orchestrator: RAGOrchestrator,
        runner: ReplyRunner,
        cancellation: CancellationRegistry,
        stale_after_seconds: int,
    ) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._retrieval = RetrievalRepository(session)
        self._audit = AuditRecorder(audit_repository(session), meta)
        self._orchestrator = orchestrator
        self._runner = runner
        self._cancellation = cancellation
        self._stale = timedelta(seconds=stale_after_seconds)

    # ----------------------------------------------------------------- helpers

    async def _require(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, *, for_update: bool = False
    ) -> Conversation:
        ctx.require(Permission.CONVERSATIONS_USE)
        conversation = await self._conversations.get_owned(
            ctx.workspace_id, ctx.user_id, conversation_id, for_update=for_update
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    async def _validate_sources(
        self,
        ctx: WorkspaceContext,
        document_ids: Sequence[uuid.UUID],
        collection_ids: Sequence[uuid.UUID],
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        docs = list(dict.fromkeys(document_ids))
        cols = list(dict.fromkeys(collection_ids))
        if len(docs) > MAX_SOURCES or len(cols) > MAX_SOURCES:
            raise ValidationFailedError(f"Select at most {MAX_SOURCES} documents and collections.")
        if docs:
            ctx.require(Permission.DOCUMENTS_READ)
            found = await self._retrieval.documents(
                ctx.workspace_id, set(docs), viewer=viewer_of(ctx)
            )
            if found.keys() != set(docs):
                raise NotFoundError("Document not found.")
        if cols:
            ctx.require(Permission.DOCUMENTS_READ)
            if await self._retrieval.collection_ids(
                ctx.workspace_id, set(cols), viewer=viewer_of(ctx)
            ) != set(cols):
                raise NotFoundError("Collection not found.")
        return docs, cols

    async def _info(self, conversation: Conversation) -> ConversationInfo:
        sources = await self._conversations.source_ids(conversation.workspace_id, [conversation.id])
        docs, cols = sources[conversation.id]
        return conversation_info(conversation, docs, cols)

    async def _detail(self, conversation: Conversation) -> ConversationDetail:
        ws, cid = conversation.workspace_id, conversation.id
        docs = await self._conversations.selected_documents(ws, cid)
        cols = await self._conversations.selected_collections(ws, cid)
        return ConversationDetail(
            conversation=conversation_info(
                conversation, [d[0] for d in docs], [c[0] for c in cols]
            ),
            documents=tuple(SelectedDocument(*d) for d in docs),
            collections=tuple(SelectedCollection(*c) for c in cols),
        )

    async def _with_citations(
        self, ctx: WorkspaceContext, rows: list[Message]
    ) -> list[MessageInfo]:
        citations = await self._messages.citations(ctx.workspace_id, [m.id for m in rows])
        return [
            message_info(m, tuple(citation_info(c, ok) for c, ok in citations.get(m.id, [])))
            for m in rows
        ]

    async def _recover_stale(self, conversation: Conversation) -> None:
        await self._messages.fail_stale(utcnow() - self._stale, conversation_id=conversation.id)

    # ------------------------------------------------------------ conversations

    async def create(
        self,
        ctx: WorkspaceContext,
        *,
        title: str | None = None,
        document_ids: Sequence[uuid.UUID] = (),
        collection_ids: Sequence[uuid.UUID] = (),
    ) -> ConversationDetail:
        ctx.require(Permission.CONVERSATIONS_USE)
        docs, cols = await self._validate_sources(ctx, document_ids, collection_ids)
        conversation = Conversation(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            created_by_user_id=ctx.user_id,
            title=_clean_title(title) if title is not None else DEFAULT_TITLE,
            title_source="user" if title is not None else "auto",
            scope="selected" if docs or cols else "all",
        )
        self._conversations.add(conversation)
        await self._session.flush()
        await self._conversations.replace_sources(conversation, docs, cols)
        await self._session.commit()
        await self._session.refresh(conversation)
        return await self._detail(conversation)

    async def list_conversations(
        self, ctx: WorkspaceContext, *, query: str | None, offset: int, limit: int
    ) -> tuple[list[ConversationInfo], int]:
        ctx.require(Permission.CONVERSATIONS_USE)
        rows, total = await self._conversations.list_owned(
            ctx.workspace_id, ctx.user_id, query=query, offset=offset, limit=limit
        )
        sources = await self._conversations.source_ids(ctx.workspace_id, [c.id for c in rows])
        return [conversation_info(c, *sources[c.id]) for c in rows], total

    async def get(self, ctx: WorkspaceContext, conversation_id: uuid.UUID) -> ConversationDetail:
        return await self._detail(await self._require(ctx, conversation_id))

    async def rename(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, title: str
    ) -> ConversationDetail:
        conversation = await self._require(ctx, conversation_id)
        conversation.title = _clean_title(title)
        conversation.title_source = "user"
        await self._session.commit()
        await self._session.refresh(conversation)
        return await self._detail(conversation)

    async def set_sources(
        self,
        ctx: WorkspaceContext,
        conversation_id: uuid.UUID,
        *,
        document_ids: Sequence[uuid.UUID],
        collection_ids: Sequence[uuid.UUID],
    ) -> ConversationDetail:
        """Replace the selection. Empty lists mean "search the whole workspace"."""
        conversation = await self._require(ctx, conversation_id, for_update=True)
        docs, cols = await self._validate_sources(ctx, document_ids, collection_ids)
        await self._conversations.replace_sources(conversation, docs, cols)
        conversation.scope = "selected" if docs or cols else "all"
        conversation.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(conversation)
        return await self._detail(conversation)

    async def delete(self, ctx: WorkspaceContext, conversation_id: uuid.UUID) -> None:
        conversation = await self._require(ctx, conversation_id, for_update=True)
        streaming = await self._messages.streaming(conversation.id)
        await self._conversations.delete(conversation)
        self._audit.record(
            "conversation.deleted",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="conversation",
            target_id=conversation.id,
            details={},
        )
        await self._session.commit()
        if streaming is not None:  # stop generating an answer nobody can read
            await self._cancellation.request(streaming.id)

    # ----------------------------------------------------------------- messages

    async def messages(
        self,
        ctx: WorkspaceContext,
        conversation_id: uuid.UUID,
        *,
        before_sequence: int | None,
        limit: int,
        include_superseded: bool = False,
    ) -> tuple[list[MessageInfo], bool]:
        conversation = await self._require(ctx, conversation_id)
        rows, has_more = await self._messages.page(
            ctx.workspace_id,
            conversation.id,
            before_sequence=before_sequence,
            limit=limit,
            include_superseded=include_superseded,
        )
        return await self._with_citations(ctx, rows), has_more

    async def message(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> MessageInfo:
        conversation = await self._require(ctx, conversation_id)
        message = await self._messages.get(ctx.workspace_id, conversation.id, message_id)
        if message is None:
            raise NotFoundError("Message not found.")
        return (await self._with_citations(ctx, [message]))[0]

    async def _sources(self, conversation: Conversation) -> tuple[list[uuid.UUID] | None, ...]:
        if conversation.scope == "all":
            return None, None
        docs, cols = (
            await self._conversations.source_ids(conversation.workspace_id, [conversation.id])
        )[conversation.id]
        return docs, cols

    async def _start(
        self,
        ctx: WorkspaceContext,
        conversation: Conversation,
        *,
        question: str,
        history_before: int | None,
        user_message: Message | None,
        reply_to: Message,
    ) -> StartedReply:
        docs, cols = await self._sources(conversation)
        turns = await self._messages.turns(conversation.id, before_sequence=history_before)
        prepared = await self._orchestrator.prepare(
            ctx,
            AnswerRequest(question=question, document_ids=docs, collection_ids=cols, history=turns),
        )
        now = utcnow()
        sequence = await self._messages.max_sequence(conversation.id)
        if user_message is not None:
            user_message.sequence = sequence + 1
            sequence += 1
            self._messages.add(user_message)
        assistant = Message(
            id=uuid.uuid4(),
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            sequence=sequence + 1,
            role="assistant",
            content="",
            status="streaming",
            reply_to_id=reply_to.id,
            retrieval_query=prepared.retrieval_query,
            prompt_version=self._orchestrator.prompt_version,
        )
        self._messages.add(assistant)
        conversation.message_count += 2 if user_message is not None else 1
        conversation.last_message_at = now
        if conversation.title_source == "auto" and conversation.title == DEFAULT_TITLE:
            conversation.title = auto_title(question)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "An answer is still being generated in this conversation.",
                code="reply_in_progress",
            ) from exc
        await self._session.refresh(assistant)
        user_info = None
        if user_message is not None:
            await self._session.refresh(user_message)
            user_info = message_info(user_message)
        started = ReplyStarted(user_info, message_info(assistant))
        return StartedReply(
            user_info, started.assistant_message, self._runner.run(prepared, started)
        )

    async def _lock_for_reply(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID
    ) -> Conversation:
        ctx.require(Permission.DOCUMENTS_READ)
        conversation = await self._require(ctx, conversation_id, for_update=True)
        await self._recover_stale(conversation)
        if await self._messages.streaming(conversation.id) is not None:
            raise ConflictError(
                "An answer is still being generated in this conversation.",
                code="reply_in_progress",
            )
        return conversation

    async def send(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, content: str
    ) -> StartedReply:
        """Persist the question, prepare the answer, and return its event stream."""
        question = _clean_question(content)
        conversation = await self._lock_for_reply(ctx, conversation_id)
        user_message = Message(
            id=uuid.uuid4(),
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            sequence=0,  # assigned in _start
            role="user",
            content=question,
            status="completed",
            created_by_user_id=ctx.user_id,
            completed_at=utcnow(),
        )
        return await self._start(
            ctx,
            conversation,
            question=question,
            history_before=None,
            user_message=user_message,
            reply_to=user_message,
        )

    async def regenerate(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> StartedReply:
        """Answer the latest question again. Only the conversation's last answer qualifies."""
        conversation = await self._lock_for_reply(ctx, conversation_id)
        target = await self._messages.get(ctx.workspace_id, conversation.id, message_id)
        if target is None:
            raise NotFoundError("Message not found.")
        if target.role != "assistant" or target.reply_to_id is None:
            raise ConflictError("Only answers can be regenerated.", code="not_an_answer")
        if target.superseded_at is not None or (
            target.sequence != await self._messages.max_sequence(conversation.id)
        ):
            raise ConflictError(
                "Only the latest answer can be regenerated.", code="not_latest_answer"
            )
        question = await self._messages.get(ctx.workspace_id, conversation.id, target.reply_to_id)
        if question is None:  # pragma: no cover - FK guarantees it
            raise NotFoundError("Message not found.")
        target.superseded_at = utcnow()
        return await self._start(
            ctx,
            conversation,
            question=question.content,
            history_before=question.sequence,
            user_message=None,
            reply_to=question,
        )

    async def cancel(
        self, ctx: WorkspaceContext, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> MessageInfo:
        conversation = await self._require(ctx, conversation_id)
        message = await self._messages.get(ctx.workspace_id, conversation.id, message_id)
        if message is None:
            raise NotFoundError("Message not found.")
        if message.status != "streaming":
            raise ConflictError("This answer is not being generated.", code="not_streaming")
        try:
            await self._cancellation.request(message.id)
        except Exception as exc:
            raise ServiceUnavailableError("Couldn't cancel right now. Please try again.") from exc
        return message_info(message)


async def collect(started: StartedReply) -> MessageInfo:
    """Drive a reply to the end (non-streaming clients). Raises if it failed."""
    final: MessageInfo | None = None
    failure: ReplyFailed | None = None
    async for event in started.events:
        if isinstance(event, ReplyCompleted):
            final = event.message
        elif isinstance(event, ReplyFailed):
            failure = event
    if final is None:
        code = failure.code if failure else "reply_interrupted"
        detail = failure.detail if failure else "The answer was interrupted."
        raise ServiceUnavailableError(detail, code=code)
    return final
