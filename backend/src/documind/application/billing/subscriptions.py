"""Self-serve subscriptions with Stripe (ADR 0032).

``workspace_plans`` stays the single place that decides what a workspace may
do. Stripe only *drives* it: webhooks (and in-app plan changes) re-read the
subscription from Stripe and assign the matching plan through the audited
``BillingService.assign``. Card details never reach DocuMind.

Custom infrastructure is part of the plans (custom storage from Pro, dedicated
and external databases from Business, own workers on Enterprise). A plan change
that would drop a feature the workspace is using is refused; a cancellation
can't be refused, so the workspace keeps working and its owners are told to
move the data back or upgrade.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.billing.gateway import (
    BillingGateway,
    BillingGatewayError,
    InvalidWebhookError,
    SubscriptionState,
)
from documind.application.billing.service import BillingService
from documind.application.identity.common import AuditRecorder, RequestMeta
from documind.application.infrastructure.resolver import verify
from documind.application.ports import EmailMessage, EmailSender
from documind.core.config import Settings
from documind.domain.billing.plans import (
    ANNUAL_MONTHS_CHARGED,
    INFRASTRUCTURE_FEATURES,
    PLANS,
    BillingInterval,
    Plan,
    PlanKey,
    cheapest_plan_with,
)
from documind.domain.errors import ConflictError, ServiceUnavailableError, ValidationFailedError
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.domain.infrastructure.profiles import PLATFORM_DEFAULT, features_in_use
from documind.infrastructure.database.models import (
    BillingEvent,
    BillingSubscription,
    InfrastructureProfileVersion,
    Role,
    User,
    Workspace,
    WorkspaceInfrastructure,
    WorkspaceMember,
    WorkspacePlan,
)
from documind.infrastructure.database.repositories.identity import AuditLogRepository

logger = logging.getLogger(__name__)

#: Subscription states in which the paid plan applies (past_due: Stripe is retrying payment).
LIVE = frozenset({"active", "trialing", "past_due"})
#: States in which the subscription is over: back to Free.
ENDED = frozenset({"canceled", "unpaid", "incomplete_expired"})
#: The ``assigned_by`` used for plans set from Stripe.
STRIPE = "stripe"

ManagedBy = Literal["free", "stripe", "contract"]


@dataclass(frozen=True, slots=True)
class PlanPrice:
    plan: Plan
    interval: BillingInterval


@dataclass(frozen=True, slots=True)
class BillingOverview:
    enabled: bool
    plan: Plan
    managed_by: ManagedBy
    status: str | None
    interval: str | None
    seats: int
    members: int
    current_period_end: datetime | None
    cancel_at_period_end: bool
    features_in_use: list[str]
    #: Features in use that the current plan doesn't include.
    over_plan: list[str]
    sales_email: str | None
    can_manage_payment: bool
    #: Plans the workspace could switch to, with the reason when it can't.
    blocked_plans: dict[str, str] = field(default_factory=dict)


def price_table(settings: Settings) -> dict[str, PlanPrice]:
    """Stripe price id → plan and interval."""
    table: dict[str, PlanPrice] = {}
    for name, price_id in settings.stripe_prices.items():
        if not price_id:
            continue
        key, interval = name.split("_")
        table[price_id] = PlanPrice(
            PLANS[PlanKey(key)],
            BillingInterval.YEAR if interval == "annual" else BillingInterval.MONTH,
        )
    return table


def price_id_for(settings: Settings, plan: PlanKey, interval: BillingInterval) -> str | None:
    suffix = "annual" if interval is BillingInterval.YEAR else "monthly"
    return settings.stripe_prices.get(f"{plan.value}_{suffix}")


def catalog(settings: Settings) -> list[dict[str, Any]]:
    """Public plan catalog: prices, limits and features (pricing page)."""
    return [
        {
            "key": plan.key.value,
            "name": plan.name,
            "description": plan.description,
            "monthly_cents_per_member": plan.pricing.monthly_cents_per_member,
            "annual_cents_per_member": plan.pricing.annual_cents_per_member,
            "self_serve": plan.pricing.self_serve and settings.billing_enabled,
            "contact_sales": plan.pricing.monthly_cents_per_member is None,
            "entitlements": plan.entitlements.to_dict(),
        }
        for plan in PLANS.values()
    ]


def annual_months_charged() -> int:
    return ANNUAL_MONTHS_CHARGED


def describe_features(features: list[str]) -> str:
    names = [INFRASTRUCTURE_FEATURES[f] for f in features]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _blocking_reason(plan: Plan, used: list[str]) -> str | None:
    missing = [f for f in used if not getattr(plan.entitlements, f)]
    if not missing:
        return None
    needs = max(
        (cheapest_plan_with(f) or PlanKey.ENTERPRISE for f in missing),
        key=lambda k: list(PlanKey).index(k),
    )
    return (
        f"This workspace uses {describe_features(missing)}, which the {plan.name} plan doesn't "
        f"include ({PLANS[needs].name} or higher does). Move that data back to DocuMind on the "
        "Infrastructure page first, or choose a higher plan."
    )


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        gateway: BillingGateway | None,
        meta: RequestMeta | None = None,
        *,
        email: EmailSender | None = None,
        on_plan_change: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._gateway = gateway if settings.billing_enabled or gateway is not None else None
        self._meta = meta or RequestMeta()
        self._audit = AuditRecorder(AuditLogRepository(session), self._meta)
        self._email = email
        self._on_plan_change = on_plan_change

    # ------------------------------------------------------------- helpers

    def _require_gateway(self) -> BillingGateway:
        if self._gateway is None:
            raise ServiceUnavailableError(
                "Online payments aren't set up on this server. Contact us to change your plan.",
                code="billing_disabled",
            )
        return self._gateway

    async def _features_in_use(self, workspace_id: uuid.UUID) -> list[str]:
        pin = await self._s.get(WorkspaceInfrastructure, workspace_id)
        if pin is None:
            return features_in_use(PLATFORM_DEFAULT)
        row = await self._s.get(InfrastructureProfileVersion, pin.version_id)
        return features_in_use(verify(row)) if row is not None else []

    async def _members(self, workspace_id: uuid.UUID) -> int:
        count = (
            await self._s.execute(
                select(func.count())
                .select_from(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == workspace_id)
            )
        ).scalar_one()
        return int(count)

    async def _managed_by(self, workspace_id: uuid.UUID, plan: Plan) -> ManagedBy:
        if not plan.paid:
            return "free"
        row = await self._s.get(WorkspacePlan, workspace_id)
        return "stripe" if row is not None and row.assigned_by == STRIPE else "contract"

    def _self_serve(self, plan_key: str) -> Plan:
        try:
            plan = PLANS[PlanKey(plan_key)]
        except ValueError as exc:
            raise ValidationFailedError(f"Unknown plan '{plan_key}'.") from exc
        if not plan.pricing.self_serve:
            raise ValidationFailedError(
                f"The {plan.name} plan can't be bought online. Contact sales.",
                code="plan_not_self_serve",
            )
        return plan

    async def _check_fits(self, workspace_id: uuid.UUID, plan: Plan) -> int:
        """Infrastructure and member count must fit the plan. Returns the seat count."""
        reason = _blocking_reason(plan, await self._features_in_use(workspace_id))
        if reason:
            raise ConflictError(reason, code="plan_lacks_infrastructure")
        seats = max(1, await self._members(workspace_id))
        limit = plan.entitlements.max_members
        if limit is not None and seats > limit:
            raise ConflictError(
                f"The {plan.name} plan allows {limit} members; this workspace has {seats}.",
                code="too_many_members",
            )
        return seats

    def _urls(self, workspace_id: uuid.UUID) -> str:
        return f"{self._settings.public_app_url.rstrip('/')}/w/{workspace_id}/billing"

    # ---------------------------------------------------------------- reads

    async def overview(self, ctx: WorkspaceContext) -> BillingOverview:
        ctx.require(Permission.BILLING_MANAGE)
        ws = ctx.workspace_id
        plan, ents, _ = await BillingService(self._s).plan(ws)
        row = await self._s.get(BillingSubscription, ws)
        used = await self._features_in_use(ws)
        blocked = {
            p.key.value: reason
            for p in PLANS.values()
            if (reason := _blocking_reason(p, used)) is not None
        }
        return BillingOverview(
            enabled=self._gateway is not None,
            plan=plan,
            managed_by=await self._managed_by(ws, plan),
            status=row.status if row and row.status != "none" else None,
            interval=row.interval if row else None,
            seats=row.seats if row else 0,
            members=await self._members(ws),
            current_period_end=row.current_period_end if row else None,
            cancel_at_period_end=bool(row and row.cancel_at_period_end),
            features_in_use=used,
            over_plan=[f for f in used if not getattr(ents, f)],
            sales_email=self._settings.sales_email,
            can_manage_payment=self._gateway is not None and row is not None,
            blocked_plans=blocked,
        )

    # --------------------------------------------------------------- writes

    async def checkout(self, ctx: WorkspaceContext, plan_key: str, interval: str) -> str:
        """Start Stripe Checkout for a first subscription. Returns the hosted page URL."""
        ctx.require(Permission.BILLING_MANAGE)
        gateway = self._require_gateway()
        plan = self._self_serve(plan_key)
        period = BillingInterval(interval)
        ws = ctx.workspace_id
        row = await self._s.get(BillingSubscription, ws)
        if row is not None and row.status in LIVE:
            raise ConflictError(
                "This workspace already has a subscription. Change the plan instead.",
                code="subscription_exists",
            )
        current, _, _ = await BillingService(self._s).plan(ws)
        if await self._managed_by(ws, current) == "contract":
            raise ConflictError(
                "This workspace's plan is managed under a contract. Contact sales to change it.",
                code="plan_managed_by_contract",
            )
        seats = await self._check_fits(ws, plan)
        price = price_id_for(self._settings, plan.key, period)
        if price is None:  # pragma: no cover - settings validation requires all prices
            raise ServiceUnavailableError("This price isn't configured.", code="billing_disabled")
        try:
            if row is None:
                user = await self._s.get(User, ctx.user_id)
                workspace = await self._s.get(Workspace, ws)
                customer = await gateway.create_customer(
                    email=user.email if user else "",
                    name=workspace.name if workspace else str(ws),
                    workspace_id=ws,
                )
                row = BillingSubscription(workspace_id=ws, customer_id=customer, status="none")
                self._s.add(row)
                await self._s.commit()
            url = await gateway.checkout_url(
                customer_id=row.customer_id,
                price_id=price,
                seats=seats,
                workspace_id=ws,
                success_url=f"{self._urls(ws)}?checkout=success",
                cancel_url=f"{self._urls(ws)}?checkout=cancelled",
            )
        except BillingGatewayError as exc:
            raise _unavailable() from exc
        self._audit.record(
            "billing.checkout_started",
            actor_user_id=ctx.user_id,
            workspace_id=ws,
            target_type="workspace",
            target_id=ws,
            details={"plan": plan.key.value, "interval": period.value, "seats": seats},
        )
        await self._s.commit()
        return url

    async def change_plan(
        self, ctx: WorkspaceContext, plan_key: str, interval: str
    ) -> BillingOverview:
        """Switch an existing subscription to another self-serve plan (prorated)."""
        ctx.require(Permission.BILLING_MANAGE)
        gateway = self._require_gateway()
        plan = self._self_serve(plan_key)
        period = BillingInterval(interval)
        ws = ctx.workspace_id
        row = await self._s.get(BillingSubscription, ws)
        if row is None or row.subscription_id is None or row.status not in LIVE:
            raise ConflictError(
                "There's no active subscription to change. Choose a plan to subscribe.",
                code="no_subscription",
            )
        seats = await self._check_fits(ws, plan)
        if row.plan_key == plan.key.value and row.interval == period.value:
            return await self.overview(ctx)
        price = price_id_for(self._settings, plan.key, period)
        try:
            state = await gateway.update_subscription(
                row.subscription_id, price_id=price, seats=seats
            )
        except BillingGatewayError as exc:
            raise _unavailable() from exc
        previous = row.plan_key
        await self._apply(row, state)
        self._audit.record(
            "billing.plan_changed",
            actor_user_id=ctx.user_id,
            workspace_id=ws,
            target_type="workspace",
            target_id=ws,
            details={"from": previous, "to": plan.key.value, "interval": period.value},
        )
        await self._s.commit()
        return await self.overview(ctx)

    async def portal(self, ctx: WorkspaceContext) -> str:
        """Stripe's customer portal: payment methods, invoices, cancellation."""
        ctx.require(Permission.BILLING_MANAGE)
        gateway = self._require_gateway()
        row = await self._s.get(BillingSubscription, ctx.workspace_id)
        if row is None:
            raise ConflictError("There's no billing account yet.", code="no_subscription")
        try:
            return await gateway.portal_url(
                customer_id=row.customer_id, return_url=self._urls(ctx.workspace_id)
            )
        except BillingGatewayError as exc:
            raise _unavailable() from exc

    # -------------------------------------------------------------- webhooks

    async def handle_webhook(self, payload: bytes, signature: str | None) -> str:
        """Apply a Stripe event. Returns ``applied``, ``ignored`` or ``duplicate``."""
        gateway = self._require_gateway()
        try:
            event = gateway.verify_event(payload, signature or "")
        except InvalidWebhookError as exc:
            raise ValidationFailedError(
                "Invalid webhook signature.", code="invalid_signature"
            ) from exc
        event_id = str(event.get("id", ""))
        kind = str(event.get("type", ""))
        if not event_id:
            raise ValidationFailedError("Invalid webhook event.", code="invalid_event")
        if await self._s.get(BillingEvent, event_id) is not None:
            return "duplicate"
        obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
        workspace_id: uuid.UUID | None = None
        outcome = "ignored"
        sub_id: str | None = None
        if kind == "checkout.session.completed":
            sub_id = obj.get("subscription") if isinstance(obj.get("subscription"), str) else None
        elif kind.startswith("customer.subscription."):
            sub_id = obj.get("id")
        if sub_id:
            try:
                state = await gateway.subscription(sub_id)
            except BillingGatewayError as exc:
                # Not recorded as handled: Stripe retries the delivery.
                raise _unavailable() from exc
            row = await self._row_for(state)
            if row is None:
                logger.warning("stripe subscription for an unknown customer", extra={"event": kind})
            elif (
                row.subscription_id not in (None, state.id)
                and row.status in LIVE
                and (state.status not in LIVE)
            ):
                # An old subscription ending while a newer one is live: nothing to do.
                workspace_id = row.workspace_id
            else:
                await self._apply(row, state)
                workspace_id, outcome = row.workspace_id, "applied"
        self._s.add(BillingEvent(id=event_id[:255], type=kind[:64], workspace_id=workspace_id))
        try:
            await self._s.commit()
        except IntegrityError:
            await self._s.rollback()
            return "duplicate"
        return outcome

    async def _row_for(self, state: SubscriptionState) -> BillingSubscription | None:
        row = (
            await self._s.execute(
                select(BillingSubscription).where(
                    BillingSubscription.customer_id == state.customer_id
                )
            )
        ).scalar_one_or_none()
        if row is None and state.workspace_id is not None:
            row = await self._s.get(BillingSubscription, state.workspace_id)
        return row

    async def _apply(self, row: BillingSubscription, state: SubscriptionState) -> None:
        """Mirror the subscription and assign the plan it pays for."""
        ws = row.workspace_id
        previous_status = row.status
        mapped = price_table(self._settings).get(state.price_id or "")
        row.subscription_id = state.id
        row.status = state.status[:32]
        row.seats = state.seats
        row.current_period_end = state.current_period_end
        row.cancel_at_period_end = state.cancel_at_period_end
        if mapped is not None:
            row.plan_key, row.interval = mapped.plan.key.value, mapped.interval.value

        target: PlanKey | None = None
        if state.status in LIVE and mapped is not None:
            target = mapped.plan.key
        elif state.status in ENDED:
            target = PlanKey.FREE
        plan_row = await self._s.get(WorkspacePlan, ws)
        current = PlanKey(plan_row.plan_key) if plan_row else PlanKey.FREE
        if target is not None and target is not current:
            if current is not PlanKey.FREE and (plan_row is None or plan_row.assigned_by != STRIPE):
                # A contract plan set by an operator wins over a stray subscription.
                logger.warning(
                    "stripe subscription ignored: plan is managed by contract",
                    extra={"workspace_id": str(ws)},
                )
            else:
                await BillingService(self._s, self._meta).assign(
                    ws,
                    target.value,
                    overrides=plan_row.overrides if plan_row else None,
                    operator=STRIPE,
                    note=f"subscription {state.id}: {state.status}"[:255],
                )
                if self._on_plan_change is not None:
                    self._on_plan_change(ws)
                if target is PlanKey.FREE:
                    over = await self._features_in_use(ws)
                    if over:
                        self._audit.record(
                            "billing.over_plan",
                            actor_user_id=None,
                            workspace_id=ws,
                            target_type="workspace",
                            target_id=ws,
                            details={"features": over},
                        )
                        await self._notify(ws, _downgraded_email, over)
        if state.status == "past_due" and previous_status != "past_due":
            await self._notify(ws, _payment_failed_email, [])

    # ------------------------------------------------------------ seat sync

    async def sync_seats(self) -> int:
        """Keep each subscription's quantity equal to the member count. Returns changes."""
        if self._gateway is None:
            return 0
        rows = (
            await self._s.execute(
                select(BillingSubscription).where(
                    BillingSubscription.status.in_(LIVE),
                    BillingSubscription.subscription_id.is_not(None),
                )
            )
        ).scalars()
        changed = 0
        for row in list(rows):
            seats = max(1, await self._members(row.workspace_id))
            if seats == row.seats or row.subscription_id is None:
                continue
            try:
                state = await self._gateway.update_subscription(row.subscription_id, seats=seats)
            except BillingGatewayError:
                logger.warning("seat sync failed", extra={"workspace_id": str(row.workspace_id)})
                continue
            await self._apply(row, state)
            await self._s.commit()
            changed += 1
        return changed

    # ---------------------------------------------------------------- email

    async def _notify(
        self,
        workspace_id: uuid.UUID,
        build: Callable[[str, str, str, str, list[str]], EmailMessage],
        features: list[str],
    ) -> None:
        if self._email is None:
            return
        workspace = await self._s.get(Workspace, workspace_id)
        owners = (
            await self._s.execute(
                select(User.email, User.display_name)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .join(Role, Role.id == WorkspaceMember.role_id)
                .where(WorkspaceMember.workspace_id == workspace_id, Role.key == "owner")
            )
        ).all()
        url = self._urls(workspace_id)
        for address, name in owners:
            try:
                await self._email.send(
                    build(address, name, workspace.name if workspace else "", url, features)
                )
            except Exception:
                logger.warning("billing notice not sent", exc_info=True)


def _unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "The payment service is unavailable. Please try again shortly.",
        code="billing_unavailable",
    )


def _mail(to: str, subject: str, heading: str, paragraphs: list[str], url: str) -> EmailMessage:
    text = "\n\n".join(paragraphs) + f"\n\n{url}"
    body = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    html = (
        f'<div style="font-family:system-ui,sans-serif;max-width:560px">'
        f'<h1 style="font-size:20px">{escape(heading)}</h1>{body}'
        f'<p><a href="{escape(url)}">Open billing</a></p></div>'
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def _downgraded_email(
    to: str, name: str, workspace: str, url: str, features: list[str]
) -> EmailMessage:
    return _mail(
        to,
        f"{workspace}: subscription ended",
        "Your subscription has ended",
        [
            f"Hi {name},",
            f"The subscription for {workspace} has ended, so it is on the Free plan now.",
            f"It still uses {describe_features(features)}, which the Free plan doesn't include. "
            "Nothing has been deleted and your documents keep working, but you can't change "
            "that setup until you subscribe again or move the data back to DocuMind on the "
            "Infrastructure page.",
        ],
        url,
    )


def _payment_failed_email(
    to: str, name: str, workspace: str, url: str, features: list[str]
) -> EmailMessage:
    del features
    return _mail(
        to,
        f"{workspace}: payment failed",
        "We couldn't take your payment",
        [
            f"Hi {name},",
            f"The latest payment for {workspace} didn't go through. Stripe will try again over "
            "the next few days. Update the payment method to keep your plan.",
        ],
        url,
    )
