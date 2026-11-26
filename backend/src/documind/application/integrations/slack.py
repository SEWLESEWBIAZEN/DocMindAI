"""The Slack app: install, link accounts, answer questions (ADR 0037).

- Slack calls our command and event URLs. Every request is checked against
  the signing secret and a five-minute clock window before anything is read.
- A Slack user is linked to a DocuMind account with a short code made in
  DocuMind while signed in, then typed into Slack (`/documind link CODE`).
  The code proves the account, so nobody can link someone else's.
- Slash commands answer privately (only the asker sees it), with everything
  the asker may see. @mentions answer in the channel's thread, so they use
  only documents everyone in the workspace may see: never a restricted
  collection.
- Slack must hear back within three seconds, so answers are produced by the
  worker and posted afterwards.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.credentials.vault import CredentialVault
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.integrations.api_keys import require_integrations_plan
from documind.application.ports import RateLimiter
from documind.core.config import Settings
from documind.domain.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext, ensure_recent_mfa
from documind.infrastructure.database.models import (
    SlackInstallation,
    SlackUserLink,
    WorkspaceMember,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository
from documind.infrastructure.integrations.slack import SlackClient, SlackError
from documind.infrastructure.security.vault import EnvelopeCipher

NONCE_COOKIE = "documind_slack_nonce"
CALLBACK_PATH = "/api/v1/integrations/slack/callback"
STATE_TTL_SECONDS = 600
LINK_TTL_SECONDS = 600
SIGNATURE_WINDOW_SECONDS = 300
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE = re.compile(r"^[A-Z2-9]{4}-?[A-Z2-9]{4}$")
_MENTION = re.compile(r"<@[A-Z0-9]+(\|[^>]*)?>")
MAX_QUESTION = 2000

HELP = (
    "*DocuMind* answers questions from your workspace's documents, with sources.\n"
    "• `/documind <question>`: a private answer, only you see it\n"
    "• `@DocuMind <question>` in a channel: an answer in the thread, from documents "
    "everyone in the workspace can see\n"
    "• `/documind link CODE`: link your DocuMind account (get the code in DocuMind, "
    "under Integrations)\n"
    "• `/documind unlink`: unlink it"
)
NOT_LINKED = (
    "Link your DocuMind account first: in DocuMind, open *Integrations → Slack*, choose "
    "*Link my Slack account*, then run `/documind link CODE` here."
)

#: Queues the worker job that answers. Set by the container (tests capture it).
Dispatch = Callable[[dict[str, Any]], Awaitable[bool]]


def _key(state: str) -> str:
    return f"slack:oauth:{state}"


def _link_key(installation_id: uuid.UUID, code: str) -> str:
    return f"slack:link:{installation_id}:{code.replace('-', '')}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_app_url.rstrip('/')}{CALLBACK_PATH}"


def verify_signature(
    secret: str, *, timestamp: str | None, body: bytes, signature: str | None, now: float
) -> None:
    """Slack's request signing: v0=HMAC-SHA256(secret, "v0:{ts}:{body}")."""
    if not timestamp or not signature:
        raise AuthenticationError("Missing Slack signature.", code="slack_signature_invalid")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise AuthenticationError("Bad Slack timestamp.", code="slack_signature_invalid") from exc
    if abs(now - ts) > SIGNATURE_WINDOW_SECONDS:
        raise AuthenticationError("Stale Slack request.", code="slack_signature_invalid")
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise AuthenticationError("Bad Slack signature.", code="slack_signature_invalid")


def new_link_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _ephemeral(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "text": text}


@dataclass(frozen=True, slots=True)
class Authorization:
    url: str
    nonce: str


class SlackService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        client: SlackClient | None,
        redis: Redis,
        cipher: EnvelopeCipher | None,
        rate_limiter: RateLimiter,
        dispatch: Dispatch | None,
        meta: RequestMeta,
    ) -> None:
        self._s = session
        self._settings = settings
        self._client = client
        self._redis = redis
        self._vault = CredentialVault(session, cipher)
        self._limiter = rate_limiter
        self._dispatch = dispatch
        self._audit = AuditRecorder(AuditLogRepository(session), meta)

    # ------------------------------------------------------------ admins

    def available(self) -> bool:
        return self._client is not None and self._settings.slack_enabled

    def _require_client(self) -> SlackClient:
        if self._client is None or not self._settings.slack_enabled:
            raise ServiceUnavailableError(
                "Slack isn't set up on this DocuMind server.", code="slack_not_configured"
            )
        return self._client

    async def installation(self, workspace_id: uuid.UUID) -> SlackInstallation | None:
        return (
            await self._s.execute(
                select(SlackInstallation).where(SlackInstallation.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()

    async def authorize(self, ctx: WorkspaceContext) -> Authorization:
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        await require_integrations_plan(self._s, ctx.workspace_id)
        client = self._require_client()
        if await self.installation(ctx.workspace_id) is not None:
            raise ConflictError("Slack is already connected.", code="slack_connected")
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        await self._redis.set(
            _key(state),
            json.dumps(
                {
                    "workspace_id": str(ctx.workspace_id),
                    "user_id": str(ctx.user_id),
                    "nonce": _digest(nonce),
                }
            ),
            ex=STATE_TTL_SECONDS,
        )
        return Authorization(
            client.authorize_url(state=state, redirect_uri=redirect_uri(self._settings)), nonce
        )

    async def callback(
        self, *, state: str | None, code: str | None, error: str | None, nonce: str | None
    ) -> str:
        """Finish installing. Returns the app path to send the browser to."""
        raw = await self._redis.getdel(_key(state)) if state else None
        if raw is None:
            return "/app?slack_error=expired"
        pending: dict[str, Any] = json.loads(raw)
        workspace_id = uuid.UUID(pending["workspace_id"])
        user_id = uuid.UUID(pending["user_id"])
        back = f"/w/{workspace_id}/integrations"
        if not nonce or not hmac.compare_digest(_digest(nonce), pending["nonce"]):
            return f"{back}?slack_error=browser"
        if error or not code:
            return f"{back}?slack_error=denied"
        if not await self._is_member(workspace_id, user_id):
            return "/app?slack_error=denied"
        client = self._require_client()
        try:
            install = await client.exchange_code(
                code=code, redirect_uri=redirect_uri(self._settings)
            )
        except SlackError:
            return f"{back}?slack_error=failed"
        other = (
            await self._s.execute(
                select(SlackInstallation).where(SlackInstallation.team_id == install.team_id)
            )
        ).scalar_one_or_none()
        if other is not None and other.workspace_id != workspace_id:
            return f"{back}?slack_error=team_taken"
        cred = self._vault.store(
            workspace_id, "slack_bot_token", install.bot_token, created_by=user_id
        )
        await self._s.flush()
        row = other or await self.installation(workspace_id)
        if row is None:
            row = SlackInstallation(id=uuid.uuid4(), workspace_id=workspace_id)
            self._s.add(row)
        elif row.bot_token_credential_id:
            old = await self._vault.get(workspace_id, row.bot_token_credential_id)
            if old is not None:
                self._vault.revoke(old)
        row.team_id, row.team_name = install.team_id, (install.team_name or "")[:200] or None
        row.bot_user_id, row.bot_token_credential_id = install.bot_user_id, cred.id
        row.status, row.installed_by_user_id = "active", user_id
        self._audit.record(
            "slack.installed",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            target_type="slack",
            target_id=row.id,
            details={"team": install.team_name},
        )
        await self._s.commit()
        return f"{back}?slack=connected"

    async def disconnect(self, ctx: WorkspaceContext) -> None:
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        row = await self.installation(ctx.workspace_id)
        if row is None:
            return
        await self._remove(row, actor=ctx.user_id, revoke_at_slack=True)

    async def _remove(
        self, row: SlackInstallation, *, actor: uuid.UUID | None, revoke_at_slack: bool
    ) -> None:
        if row.bot_token_credential_id:
            cred = await self._vault.get(row.workspace_id, row.bot_token_credential_id)
            if cred is not None:
                if revoke_at_slack and self._client is not None:
                    # Already revoked or unreachable: it's destroyed here anyway.
                    with contextlib.suppress(SlackError):
                        await self._client.revoke(self._vault.reveal(cred))
                self._vault.revoke(cred)
        self._audit.record(
            "slack.removed",
            actor_user_id=actor,
            workspace_id=row.workspace_id,
            target_type="slack",
            target_id=row.id,
            details={"team": row.team_name},
        )
        await self._s.delete(row)  # links go with it
        await self._s.commit()

    # ------------------------------------------------------------ members

    async def link_code(self, ctx: WorkspaceContext) -> str:
        """A code the signed-in person types into Slack to link their Slack account."""
        ctx.require(Permission.CONVERSATIONS_USE)
        row = await self.installation(ctx.workspace_id)
        if row is None:
            raise ConflictError("Slack isn't connected to this workspace.", code="slack_missing")
        await self._limiter.hit("slack:code", str(ctx.user_id), 10, 3600)
        code = new_link_code()
        await self._redis.set(_link_key(row.id, code), str(ctx.user_id), ex=LINK_TTL_SECONDS)
        return code

    async def linked(self, ctx: WorkspaceContext) -> bool:
        row = await self.installation(ctx.workspace_id)
        if row is None:
            return False
        found = await self._s.execute(
            select(SlackUserLink.slack_user_id).where(
                SlackUserLink.installation_id == row.id, SlackUserLink.user_id == ctx.user_id
            )
        )
        return found.first() is not None

    async def unlink_me(self, ctx: WorkspaceContext) -> None:
        row = await self.installation(ctx.workspace_id)
        if row is None:
            return
        await self._s.execute(
            delete(SlackUserLink).where(
                SlackUserLink.installation_id == row.id, SlackUserLink.user_id == ctx.user_id
            )
        )
        await self._s.commit()

    async def _is_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        found = await self._s.execute(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
            )
        )
        return found.first() is not None

    # ------------------------------------------------------- Slack calls us

    def verify(self, *, timestamp: str | None, body: bytes, signature: str | None) -> None:
        self._require_client()
        secret = self._settings.slack_signing_secret
        if secret is None:  # pragma: no cover - slack_enabled checks it
            raise ServiceUnavailableError("Slack isn't set up.", code="slack_not_configured")
        verify_signature(
            secret.get_secret_value(),
            timestamp=timestamp,
            body=body,
            signature=signature,
            now=time.time(),
        )

    async def _active(self, team_id: str | None) -> SlackInstallation | None:
        if not team_id:
            return None
        return (
            await self._s.execute(
                select(SlackInstallation).where(
                    SlackInstallation.team_id == team_id, SlackInstallation.status == "active"
                )
            )
        ).scalar_one_or_none()

    async def _link_for(self, row: SlackInstallation, slack_user: str) -> SlackUserLink | None:
        return await self._s.get(SlackUserLink, (row.id, slack_user))

    async def command(self, form: dict[str, str]) -> dict[str, Any]:
        """A slash command. The reply is shown only to the person who typed it."""
        row = await self._active(form.get("team_id"))
        if row is None:
            return _ephemeral("DocuMind isn't connected to this Slack workspace.")
        slack_user = form.get("user_id", "")
        text = " ".join(form.get("text", "").split())
        word, _, rest = text.partition(" ")
        if not text or word.lower() == "help":
            return _ephemeral(HELP)
        if word.lower() == "link":
            return _ephemeral(await self._link(row, slack_user, rest))
        if word.lower() == "unlink":
            await self._s.execute(
                delete(SlackUserLink).where(
                    SlackUserLink.installation_id == row.id,
                    SlackUserLink.slack_user_id == slack_user,
                )
            )
            await self._s.commit()
            return _ephemeral("Unlinked. Link again any time with `/documind link CODE`.")
        link = await self._link_for(row, slack_user)
        if link is None:
            return _ephemeral(NOT_LINKED)
        try:
            await self._limiter.hit(
                "ai:user",
                str(link.user_id),
                self._settings.rate_limit_ai_per_user,
                self._settings.rate_limit_ai_window_seconds,
            )
        except RateLimitedError:
            return _ephemeral("You've asked a lot of questions just now. Try again in a minute.")
        await self._queue(
            {
                "kind": "command",
                "installation_id": str(row.id),
                "slack_user_id": slack_user,
                "question": text[:MAX_QUESTION],
                "response_url": form.get("response_url", ""),
            }
        )
        return _ephemeral(f"Looking that up: _{_escape(text[:200])}_")

    async def _link(self, row: SlackInstallation, slack_user: str, code: str) -> str:
        code = code.strip().upper()
        try:
            await self._limiter.hit("slack:link", f"{row.team_id}:{slack_user}", 5, 600)
        except RateLimitedError:
            return "Too many tries. Wait ten minutes and get a new code."
        if not slack_user or not _CODE.match(code):
            return "That isn't a link code. It looks like `ABCD-EFGH`."
        raw = await self._redis.getdel(_link_key(row.id, code))
        if raw is None:
            return "That code is wrong or has expired. Get a new one in DocuMind."
        user_id = uuid.UUID(raw.decode() if isinstance(raw, bytes) else str(raw))
        if not await self._is_member(row.workspace_id, user_id):
            return "That account isn't a member of this DocuMind workspace."
        await self._s.execute(
            delete(SlackUserLink).where(
                SlackUserLink.installation_id == row.id,
                SlackUserLink.slack_user_id == slack_user,
            )
        )
        self._s.add(
            SlackUserLink(installation_id=row.id, slack_user_id=slack_user, user_id=user_id)
        )
        self._audit.record(
            "slack.user_linked",
            actor_user_id=user_id,
            workspace_id=row.workspace_id,
            target_type="slack",
            target_id=row.id,
            details={"slack_user": slack_user},
        )
        await self._s.commit()
        return "Linked. Ask away: `/documind what is our refund policy?`"

    async def event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind = payload.get("type")
        if kind == "url_verification":
            return {"challenge": str(payload.get("challenge", ""))}
        if kind != "event_callback":
            return None
        event_id = str(payload.get("event_id", ""))
        if event_id and not await self._redis.set(f"slack:event:{event_id}", "1", nx=True, ex=3600):
            return None  # Slack retried something we already have
        row = await self._active(payload.get("team_id"))
        if row is None:
            return None
        event = payload.get("event") or {}
        etype = event.get("type")
        if etype in ("app_uninstalled", "tokens_revoked"):
            await self._remove(row, actor=None, revoke_at_slack=False)
            return None
        if etype != "app_mention" or event.get("bot_id") or not event.get("user"):
            return None
        question = " ".join(_MENTION.sub(" ", str(event.get("text", ""))).split())
        await self._queue(
            {
                "kind": "mention",
                "installation_id": str(row.id),
                "slack_user_id": str(event["user"]),
                "question": question[:MAX_QUESTION],
                "channel": str(event.get("channel", "")),
                "thread_ts": str(event.get("thread_ts") or event.get("ts") or ""),
            }
        )
        return None

    async def _queue(self, job: dict[str, Any]) -> None:
        if self._dispatch is None:  # pragma: no cover - set by the container
            raise ServiceUnavailableError("Answers are unavailable.", code="slack_unavailable")
        await self._dispatch(job)


# ------------------------------------------------------------- answering


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_answer(answer: Any, *, app_url: str, workspace_id: uuid.UUID, public: bool) -> str:
    """Slack mrkdwn: the answer, then numbered sources linking into DocuMind."""
    lines = [_escape(answer.answer)]
    if answer.citations:
        lines.append("")
        lines.append("*Sources*")
        base = f"{app_url.rstrip('/')}/w/{workspace_id}/documents"
        for c in answer.citations:
            title = _escape(c.document_name).replace("|", "/")
            where = f", p. {c.page_start}" if c.page_start else ""
            lines.append(f"[{c.number}] <{base}/{c.document_id}|{title}>{where}")
    if public:
        lines.append("")
        lines.append("_From documents everyone in the workspace can see._")
    return "\n".join(lines)


@dataclass
class AnswerDeps:
    """What the worker provides to answer one Slack question."""

    session: AsyncSession
    settings: Settings
    client: SlackClient
    cipher: EnvelopeCipher | None
    #: Workspace context for a user, enforcing membership and 2FA policy.
    context_for: Callable[[uuid.UUID, uuid.UUID], Awaitable[WorkspaceContext]]
    #: Answers a question for a context (quota checked inside).
    answer: Callable[[WorkspaceContext, str], Awaitable[Any]]


async def answer_job(job: dict[str, Any], deps: AnswerDeps) -> str:
    """Answer one queued Slack question and post the reply. Returns an outcome label."""
    row = await deps.session.get(SlackInstallation, uuid.UUID(job["installation_id"]))
    if row is None or row.status != "active" or row.bot_token_credential_id is None:
        return "not_installed"
    vault = CredentialVault(deps.session, deps.cipher)
    cred = await vault.get(row.workspace_id, row.bot_token_credential_id)
    if cred is None:
        return "not_installed"
    token = vault.reveal(cred)
    public = job["kind"] == "mention"

    async def reply(text: str) -> None:
        if public:
            await deps.client.post_message(
                token, channel=job["channel"], text=text, thread_ts=job.get("thread_ts") or None
            )
        else:
            await deps.client.respond(
                job["response_url"],
                {"response_type": "ephemeral", "replace_original": True, "text": text},
            )

    link = await deps.session.get(SlackUserLink, (row.id, job["slack_user_id"]))
    try:
        if link is None:
            await reply(NOT_LINKED)
            return "not_linked"
        if not job.get("question"):
            await reply(HELP)
            return "help"
        try:
            ctx = await deps.context_for(link.user_id, row.workspace_id)
            ctx.require(Permission.CONVERSATIONS_USE)
            if public:
                ctx = dataclasses.replace(ctx, public_only=True)
            result = await deps.answer(ctx, job["question"])
        except PermissionDeniedError as exc:
            await reply(_escape(exc.message))
            return "denied"
        except DomainError as exc:
            await reply(_escape(exc.message))
            return "failed"
        await reply(
            format_answer(
                result,
                app_url=deps.settings.public_app_url,
                workspace_id=row.workspace_id,
                public=public,
            )
        )
        return "answered"
    except SlackError:
        return "reply_failed"
