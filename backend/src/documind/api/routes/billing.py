"""Plans, prices and self-serve subscriptions (ADR 0032)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import ContainerDep, SubscriptionServiceDep, WorkspaceCtx
from documind.application.billing.subscriptions import (
    BillingOverview,
    annual_months_charged,
    catalog,
)
from documind.domain.billing.plans import INFRASTRUCTURE_FEATURES

public_router = APIRouter(tags=["billing"])
router = APIRouter(prefix="/workspaces/{workspace_id}/billing", tags=["billing"])

PlanChoice = Literal["pro", "business"]
Interval = Literal["month", "year"]


class PlanCatalogEntry(BaseModel):
    key: str
    name: str
    description: str
    monthly_cents_per_member: int | None = Field(
        description="List price per member per month, in US cents. Null: by quote."
    )
    annual_cents_per_member: int | None = Field(
        description="Price per member per year when billed annually (two months free)."
    )
    self_serve: bool = Field(description="Can be bought online on this server.")
    contact_sales: bool
    entitlements: dict[str, Any]


class PlanCatalogResponse(BaseModel):
    currency: Literal["usd"] = "usd"
    annual_months_charged: int
    billing_enabled: bool
    sales_email: str | None
    infrastructure_features: dict[str, str]
    plans: list[PlanCatalogEntry]


class BillingOverviewResponse(BaseModel):
    enabled: bool
    plan_key: str
    plan_name: str
    managed_by: Literal["free", "stripe", "contract"]
    status: str | None
    interval: str | None
    seats: int
    members: int
    current_period_end: datetime | None
    cancel_at_period_end: bool
    features_in_use: list[str]
    over_plan: list[str] = Field(
        description="Infrastructure features in use that the current plan doesn't include."
    )
    blocked_plans: dict[str, str] = Field(
        description="Plans the workspace can't move to now, with the reason."
    )
    sales_email: str | None
    can_manage_payment: bool


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanChoice
    interval: Interval = "month"


class RedirectResponse(BaseModel):
    url: str


class WebhookResponse(BaseModel):
    result: Literal["applied", "ignored", "duplicate"]


def _overview(o: BillingOverview) -> BillingOverviewResponse:
    return BillingOverviewResponse(
        enabled=o.enabled,
        plan_key=o.plan.key.value,
        plan_name=o.plan.name,
        managed_by=o.managed_by,
        status=o.status,
        interval=o.interval,
        seats=o.seats,
        members=o.members,
        current_period_end=o.current_period_end,
        cancel_at_period_end=o.cancel_at_period_end,
        features_in_use=o.features_in_use,
        over_plan=o.over_plan,
        blocked_plans=o.blocked_plans,
        sales_email=o.sales_email,
        can_manage_payment=o.can_manage_payment,
    )


@public_router.get(
    "/plans", response_model=PlanCatalogResponse, summary="Plans and prices (public)"
)
async def list_plans(container: ContainerDep) -> PlanCatalogResponse:
    s = container.settings
    return PlanCatalogResponse(
        annual_months_charged=annual_months_charged(),
        billing_enabled=container.billing_gateway is not None,
        sales_email=s.sales_email,
        infrastructure_features=dict(INFRASTRUCTURE_FEATURES),
        plans=[PlanCatalogEntry(**p) for p in catalog(s)],
    )


@public_router.post(
    "/billing/webhook",
    response_model=WebhookResponse,
    summary="Stripe webhook endpoint (signature-verified)",
    include_in_schema=False,
)
async def stripe_webhook(
    request: Request,
    billing: SubscriptionServiceDep,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> WebhookResponse:
    result = await billing.handle_webhook(await request.body(), stripe_signature)
    return WebhookResponse(result=result)


@router.get("", response_model=BillingOverviewResponse, summary="Plan, subscription and seats")
async def billing_overview(
    ctx: WorkspaceCtx, billing: SubscriptionServiceDep
) -> BillingOverviewResponse:
    return _overview(await billing.overview(ctx))


@router.post(
    "/checkout",
    response_model=RedirectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start Stripe Checkout for a first subscription (owners)",
)
async def start_checkout(
    body: PlanRequest, ctx: WorkspaceCtx, billing: SubscriptionServiceDep
) -> RedirectResponse:
    return RedirectResponse(url=await billing.checkout(ctx, body.plan, body.interval))


@router.post(
    "/plan",
    response_model=BillingOverviewResponse,
    summary="Switch an existing subscription to another plan (prorated)",
)
async def change_plan(
    body: PlanRequest, ctx: WorkspaceCtx, billing: SubscriptionServiceDep
) -> BillingOverviewResponse:
    return _overview(await billing.change_plan(ctx, body.plan, body.interval))


@router.post(
    "/portal",
    response_model=RedirectResponse,
    summary="Stripe customer portal: payment method, invoices, cancellation",
)
async def open_portal(ctx: WorkspaceCtx, billing: SubscriptionServiceDep) -> RedirectResponse:
    return RedirectResponse(url=await billing.portal(ctx))
