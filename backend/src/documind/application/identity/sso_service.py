"""Single sign-on with OpenID Connect, per organization (ADR 0036).

Safety rules:

* An organization's SSO only works after an operator confirms it owns the email
  domains (``documind-manage sso approve``). Otherwise an admin could point a
  domain they don't own at their own identity provider and sign in as anyone
  with an address there.
* The callback is bound to the browser that started it (single-use state in
  Redis plus an HttpOnly nonce cookie), and the ID token is checked: signature
  from the provider's keys, issuer, audience, expiry, nonce, verified email in
  one of the organization's domains.
* New people are created on first sign-in with a verified email and no
  usable password; existing accounts with that email are linked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.credentials.vault import CredentialVault
from documind.application.identity.auth_service import AuthResult, AuthService
from documind.application.identity.common import AuditRecorder, RequestMeta, utcnow
from documind.application.ports import PasswordHasher
from documind.core.config import Settings
from documind.domain.billing.plans import PlanKey
from documind.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission, RoleKey
from documind.domain.identity.policies import WorkspaceContext, WorkspaceKind, ensure_recent_mfa
from documind.infrastructure.database.models import (
    RefreshToken,
    Role,
    SsoConnection,
    User,
    Workspace,
    WorkspaceMember,
    WorkspacePlan,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository
from documind.infrastructure.security.vault import EnvelopeCipher
from documind.infrastructure.sso.oidc import OidcClient, OidcError, is_google

STATE_TTL_SECONDS = 600
NONCE_COOKIE = "documind_sso_nonce"
CALLBACK_PATH = "/api/v1/auth/sso/callback"
SSO_PLANS = frozenset({PlanKey.BUSINESS.value, PlanKey.ENTERPRISE.value})
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
#: Shared mail providers can't be claimed by one organization.
PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
    }
)


def _key(state: str) -> str:
    return f"sso:oidc:{hashlib.sha256(state.encode()).hexdigest()}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


@dataclass(frozen=True, slots=True)
class SsoStart:
    url: str
    nonce: str


@dataclass(frozen=True, slots=True)
class SsoFinish:
    path: str
    session: AuthResult | None = None


class SsoService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        oidc: OidcClient,
        redis: Redis,
        cipher: EnvelopeCipher | None,
        hasher: PasswordHasher,
        auth: AuthService,
        meta: RequestMeta | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._oidc = oidc
        self._redis = redis
        self._vault = CredentialVault(session, cipher)
        self._hasher = hasher
        self._auth = auth
        self._audit = AuditRecorder(AuditLogRepository(session), meta or RequestMeta())

    def redirect_uri(self) -> str:
        return f"{self._settings.public_app_url.rstrip('/')}{CALLBACK_PATH}"

    # --------------------------------------------------------- admin setup

    async def get(self, ctx: WorkspaceContext) -> SsoConnection | None:
        ctx.require(Permission.WORKSPACE_UPDATE)
        return (
            await self._s.execute(
                select(SsoConnection).where(SsoConnection.workspace_id == ctx.workspace_id)
            )
        ).scalar_one_or_none()

    async def save(
        self,
        ctx: WorkspaceContext,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        domains: list[str],
        default_role: str,
        enforced: bool,
    ) -> SsoConnection:
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        if ctx.workspace_kind is not WorkspaceKind.ORGANIZATION:
            raise ValidationFailedError("Single sign-on is for organizations.", code="sso_org_only")
        plan = await self._s.get(WorkspacePlan, ctx.workspace_id)
        if plan is None or plan.plan_key not in SSO_PLANS:
            raise PermissionDeniedError(
                "Single sign-on is included from the Business plan.", code="sso_plan_required"
            )
        cleaned = sorted({d.strip().lower() for d in domains if d.strip()})
        if not cleaned or len(cleaned) > 20:
            raise ValidationFailedError("Add 1 to 20 email domains.", code="invalid_domains")
        for d in cleaned:
            if not _DOMAIN.match(d) or d in PUBLIC_DOMAINS:
                raise ValidationFailedError(
                    f"'{d}' can't be used for single sign-on.", code="invalid_domains"
                )
        if default_role not in ("member", "viewer"):
            raise ValidationFailedError(
                "New members join as member or viewer.", code="invalid_role"
            )
        try:
            await self._oidc.discover(issuer)
        except OidcError as exc:
            raise ValidationFailedError(
                f"Couldn't use that issuer: {exc}", code="invalid_issuer"
            ) from exc
        taken = (
            await self._s.execute(
                select(SsoConnection).where(
                    SsoConnection.workspace_id != ctx.workspace_id,
                    SsoConnection.status == "active",
                )
            )
        ).scalars()
        for other in taken:
            if set(other.domains) & set(cleaned):
                raise ConflictError(
                    "Another organization already signs in with one of these domains.",
                    code="domain_taken",
                )
        row = await self.get(ctx)
        if row is None:
            if not client_secret:
                raise ValidationFailedError("Enter the client secret.", code="invalid_secret")
            row = SsoConnection(
                id=uuid.uuid4(),
                workspace_id=ctx.workspace_id,
                created_by_user_id=ctx.user_id,
                status="pending_verification",
            )
            self._s.add(row)
        identity_changed = (
            row.issuer != issuer.rstrip("/")
            or row.client_id != client_id
            or set(row.domains or []) != set(cleaned)
        )
        row.issuer, row.client_id, row.domains = issuer.rstrip("/"), client_id.strip(), cleaned
        row.default_role, row.enforced = default_role, enforced
        if client_secret:
            old = (
                await self._vault.get(ctx.workspace_id, row.client_secret_credential_id)
                if row.client_secret_credential_id
                else None
            )
            cred = self._vault.store(
                ctx.workspace_id, "sso_client_secret", client_secret, created_by=ctx.user_id
            )
            await self._s.flush()
            row.client_secret_credential_id = cred.id
            if old is not None:
                self._vault.revoke(old)
        if identity_changed:
            row.status, row.verified_by, row.verification_note = "pending_verification", None, None
        self._audit.record(
            "sso.configured",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="sso",
            target_id=row.id,
            details={"domains": cleaned, "enforced": enforced, "status": row.status},
        )
        await self._s.commit()
        await self._s.refresh(row)
        return row

    async def scim_token(self, ctx: WorkspaceContext) -> str:
        """A new SCIM bearer token (shown once; replaces the previous one)."""
        ctx.require(Permission.WORKSPACE_UPDATE)
        ensure_recent_mfa(ctx, utcnow(), self._settings.mfa_step_up_ttl_seconds)
        row = await self.get(ctx)
        if row is None or row.status != "active":
            raise ConflictError("Set up and verify single sign-on first.", code="sso_not_active")
        token = "dmscim_" + secrets.token_urlsafe(32)
        row.scim_token_hash, row.scim_token_prefix = _digest(token), token[:14]
        self._audit.record(
            "sso.scim_token_created",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            target_type="sso",
            target_id=row.id,
        )
        await self._s.commit()
        return token

    # --------------------------------------------------------- operators

    async def decide(
        self, connection_id: uuid.UUID, *, approve: bool, operator: str, note: str
    ) -> SsoConnection:
        row = await self._s.get(SsoConnection, connection_id)
        if row is None:
            raise NotFoundError("SSO connection not found.")
        if approve:
            for other in (
                await self._s.execute(
                    select(SsoConnection).where(
                        SsoConnection.id != row.id, SsoConnection.status == "active"
                    )
                )
            ).scalars():
                if set(other.domains) & set(row.domains):
                    raise ConflictError("A domain is already active for another organization.")
        row.status = "active" if approve else "disabled"
        row.verified_by, row.verification_note = operator[:128], note[:500]
        if not approve:
            # Sessions started through this provider end now.
            await self._s.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.sso_workspace_id == row.workspace_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow(), revoked_reason="sso_disabled")
            )
        self._audit.record(
            "sso.verified" if approve else "sso.disabled",
            actor_user_id=None,
            workspace_id=row.workspace_id,
            target_type="sso",
            target_id=row.id,
            details={"operator": operator, "note": note},
        )
        await self._s.commit()
        return row

    # --------------------------------------------------------- sign-in

    async def for_email(self, email: str) -> SsoConnection | None:
        domain = domain_of(email)
        return (
            await self._s.execute(
                select(SsoConnection).where(
                    SsoConnection.status == "active", SsoConnection.domains.contains([domain])
                )
            )
        ).scalar_one_or_none()

    async def start(self, email: str) -> SsoStart:
        row = await self.for_email(email)
        if row is None:
            raise NotFoundError(
                "Single sign-on isn't set up for that email address.", code="sso_not_found"
            )
        try:
            config = await self._oidc.discover(row.issuer)
        except OidcError as exc:
            raise ValidationFailedError(str(exc), code="sso_unavailable") from exc
        state, nonce, browser = (secrets.token_urlsafe(32) for _ in range(3))
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        await self._redis.set(
            _key(state),
            json.dumps(
                {
                    "connection": str(row.id),
                    "verifier": verifier,
                    "nonce": nonce,
                    "browser": _digest(browser),
                }
            ),
            ex=STATE_TTL_SECONDS,
        )
        url = self._oidc.authorize_url(
            config,
            client_id=row.client_id,
            redirect_uri=self.redirect_uri(),
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            login_hint=email,
            hosted_domain=domain_of(email) if is_google(row.issuer) else None,
        )
        return SsoStart(url, browser)

    async def finish(
        self, *, state: str | None, code: str | None, error: str | None, browser: str | None
    ) -> SsoFinish:
        raw = await self._redis.getdel(_key(state)) if state else None
        if raw is None:
            return SsoFinish("/login?sso_error=expired")
        pending: dict[str, Any] = json.loads(raw)
        if not browser or not hmac.compare_digest(_digest(browser), pending["browser"]):
            return SsoFinish("/login?sso_error=browser")
        if error or not code:
            return SsoFinish("/login?sso_error=denied")
        row = await self._s.get(SsoConnection, uuid.UUID(pending["connection"]))
        if row is None or row.status != "active" or row.client_secret_credential_id is None:
            return SsoFinish("/login?sso_error=unavailable")
        cred = await self._vault.get(row.workspace_id, row.client_secret_credential_id)
        if cred is None:
            return SsoFinish("/login?sso_error=unavailable")
        try:
            config = await self._oidc.discover(row.issuer)
            identity = await self._oidc.identity(
                config,
                client_id=row.client_id,
                client_secret=self._vault.reveal(cred),
                code=code,
                code_verifier=pending["verifier"],
                redirect_uri=self.redirect_uri(),
                nonce=pending["nonce"],
            )
        except OidcError:
            self._audit.record(
                "auth.login_sso_failed",
                actor_user_id=None,
                workspace_id=row.workspace_id,
                target_type="sso",
                target_id=row.id,
            )
            await self._s.commit()
            return SsoFinish("/login?sso_error=failed")
        if domain_of(identity.email) not in row.domains:
            return SsoFinish("/login?sso_error=domain")
        # Google: only accounts managed by the organization's Google Workspace. A
        # personal Google account can be registered with a work address, and it
        # keeps that address after the person leaves the company.
        if is_google(row.issuer) and identity.hosted_domain not in row.domains:
            return SsoFinish("/login?sso_error=domain")
        user = await self.provision(row, identity.email, identity.name)
        if not user.is_active:
            return SsoFinish("/login?sso_error=inactive")
        session = await self._auth.sso_session(user, row.workspace_id)
        return SsoFinish(f"/sso/complete?workspace={row.workspace_id}", session)

    async def provision(self, row: SsoConnection, email: str, name: str | None) -> User:
        """The user for this email, created if new, and a member of the organization."""
        user = (await self._s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=email,
                # No usable password: they sign in with SSO (or reset one later).
                password_hash=await self._hasher.hash(secrets.token_urlsafe(32)),
                display_name=(name or email.split("@")[0])[:100],
                email_verified_at=utcnow(),
            )
            self._s.add(user)
            await self._s.flush()
            personal = Workspace(
                id=uuid.uuid4(),
                kind=WorkspaceKind.PERSONAL.value,
                name="Personal",
                personal_owner_id=user.id,
            )
            self._s.add(personal)
            await self._s.flush()
            owner = await self._role(RoleKey.OWNER)
            self._s.add(
                WorkspaceMember(workspace_id=personal.id, user_id=user.id, role_id=owner.id)
            )
            user.default_workspace_id = row.workspace_id
            self._audit.record(
                "user.provisioned",
                actor_user_id=None,
                workspace_id=row.workspace_id,
                target_type="user",
                target_id=user.id,
                details={"via": "sso"},
            )
        elif user.email_verified_at is None:
            user.email_verified_at = utcnow()  # the organization's IdP vouches for it
        member = (
            await self._s.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == row.workspace_id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            role = await self._role(RoleKey(row.default_role))
            self._s.add(
                WorkspaceMember(workspace_id=row.workspace_id, user_id=user.id, role_id=role.id)
            )
            self._audit.record(
                "member.provisioned",
                actor_user_id=None,
                workspace_id=row.workspace_id,
                target_type="user",
                target_id=user.id,
                details={"role": row.default_role},
            )
        await self._s.commit()
        return user

    async def _role(self, key: RoleKey) -> Role:
        return (await self._s.execute(select(Role).where(Role.key == key.value))).scalar_one()


async def sso_required_for(session: AsyncSession, email: str) -> SsoConnection | None:
    """An active, enforced connection whose organization this email belongs to."""
    domain = domain_of(email)
    row = (
        await session.execute(
            select(SsoConnection).where(
                SsoConnection.status == "active",
                SsoConnection.enforced.is_(True),
                SsoConnection.domains.contains([domain]),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    member = (
        await session.execute(
            select(WorkspaceMember.id)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == row.workspace_id, User.email == email)
        )
    ).first()
    return row if member is not None else None
