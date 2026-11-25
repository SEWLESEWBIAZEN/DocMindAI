"""Pricing, Stripe subscriptions and plan-gated infrastructure (ADR 0032).

Stripe is replaced by a fake gateway that keeps subscriptions in memory, but
webhook signatures are verified with Stripe's real code and a test secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import stripe
from pydantic import SecretStr

from documind.application.billing.gateway import (
    BillingGatewayError,
    InvalidWebhookError,
    SubscriptionState,
)
from documind.application.billing.subscriptions import SubscriptionService
from documind.container import build_container
from tests.integration.conftest import Api
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    activate_new,
    customer_bucket,
    external_db,
    ready,
    url,
)
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test_documind"
PRICES = {
    "stripe_price_pro_monthly": "price_pro_m",
    "stripe_price_pro_annual": "price_pro_y",
    "stripe_price_business_monthly": "price_biz_m",
    "stripe_price_business_annual": "price_biz_y",
}


class FakeStripe:
    """In-memory BillingGateway. ``complete`` plays the customer finishing Checkout."""

    def __init__(self) -> None:
        self.customers: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, SubscriptionState] = {}
        self.checkouts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail = False

    async def create_customer(self, *, email: str, name: str, workspace_id: uuid.UUID) -> str:
        if self.fail:
            raise BillingGatewayError("down")
        cid = f"cus_{uuid.uuid4().hex[:10]}"
        self.customers[cid] = {"email": email, "name": name, "workspace_id": workspace_id}
        return cid

    async def checkout_url(self, **kwargs: Any) -> str:
        if self.fail:
            raise BillingGatewayError("down")
        self.checkouts.append(kwargs)
        return f"https://checkout.stripe.test/c/{len(self.checkouts)}"

    async def portal_url(self, *, customer_id: str, return_url: str) -> str:
        return f"https://billing.stripe.test/p/{customer_id}?return={return_url}"

    async def subscription(self, subscription_id: str) -> SubscriptionState:
        return self.subscriptions[subscription_id]

    async def update_subscription(
        self, subscription_id: str, *, price_id: str | None = None, seats: int | None = None
    ) -> SubscriptionState:
        self.updates.append({"id": subscription_id, "price_id": price_id, "seats": seats})
        current = self.subscriptions[subscription_id]
        updated = replace(
            current,
            price_id=price_id or current.price_id,
            seats=seats if seats is not None else current.seats,
        )
        self.subscriptions[subscription_id] = updated
        return updated

    def verify_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            stripe.Webhook.construct_event(payload, signature, WEBHOOK_SECRET)
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise InvalidWebhookError(str(exc)) from exc
        event: dict[str, Any] = json.loads(payload)
        return event

    # --- test helpers -------------------------------------------------------

    def complete(self, n: int = -1, status: str = "active") -> SubscriptionState:
        checkout = self.checkouts[n]
        sid = f"sub_{uuid.uuid4().hex[:10]}"
        state = SubscriptionState(
            id=sid,
            customer_id=checkout["customer_id"],
            status=status,
            price_id=checkout["price_id"],
            seats=checkout["seats"],
            current_period_end=datetime(2026, 12, 25, tzinfo=UTC),
            cancel_at_period_end=False,
            workspace_id=checkout["workspace_id"],
        )
        self.subscriptions[sid] = state
        return state

    def set(self, sid: str, **changes: Any) -> SubscriptionState:
        self.subscriptions[sid] = replace(self.subscriptions[sid], **changes)
        return self.subscriptions[sid]


def signed(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    ts = int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def webhook(api: Api, kind: str, obj: dict[str, Any], *, event_id: str | None = None) -> Any:
    payload = json.dumps(
        {"id": event_id or f"evt_{uuid.uuid4().hex}", "type": kind, "data": {"object": obj}}
    ).encode()
    return api.client.post(
        "/api/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": signed(payload), "Content-Type": "application/json"},
    )


def billing(w: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{w}/billing{tail}"


@pytest.fixture
def fake() -> FakeStripe:
    return FakeStripe()


def billing_settings() -> dict[str, Any]:
    return {
        "stripe_secret_key": SecretStr("sk_test_documind"),
        "stripe_webhook_secret": SecretStr(WEBHOOK_SECRET),
        "sales_email": "sales@example.com",
        **PRICES,
    }


@pytest.fixture
def bapi(make_api: Any, fake: FakeStripe) -> Api:
    api: Api = make_api(
        {"billing_gateway": fake},
        infrastructure_allowed_private_hosts=["localhost", "127.0.0.1"],
        **billing_settings(),
    )
    return api


def subscribe(api: Api, fake: FakeStripe, w: str, h: dict[str, str], plan: str = "pro") -> str:
    r = api.client.post(billing(w, "/checkout"), json={"plan": plan}, headers=h)
    assert r.status_code == 201, r.text
    state = fake.complete()
    applied = webhook(api, "checkout.session.completed", {"subscription": state.id})
    assert applied.json() == {"result": "applied"}, applied.text
    return state.id


def plan_of(api: Api, w: str) -> tuple[str, str | None]:
    row = sql(
        api,
        "SELECT plan_key, assigned_by FROM workspace_plans WHERE workspace_id = :w",
        w=uuid.UUID(w),
    )
    return (row[0].plan_key, row[0].assigned_by) if row else ("free", None)


# --------------------------------------------------------------------------- tests


def test_public_catalog_lists_prices_and_infrastructure(bapi: Api, make_api: Any) -> None:
    body = bapi.client.get("/api/v1/plans").json()
    plans = {p["key"]: p for p in body["plans"]}
    assert body["billing_enabled"] is True
    assert body["annual_months_charged"] == 10
    assert plans["free"]["monthly_cents_per_member"] == 0
    assert plans["pro"]["monthly_cents_per_member"] == 1900
    assert plans["pro"]["annual_cents_per_member"] == 19000
    assert plans["business"]["monthly_cents_per_member"] == 9900
    assert plans["enterprise"]["contact_sales"] is True
    assert plans["enterprise"]["self_serve"] is False
    assert plans["pro"]["entitlements"]["custom_storage"] is True
    assert plans["pro"]["entitlements"]["external_database"] is False
    assert plans["business"]["entitlements"]["dedicated_database"] is True
    assert plans["enterprise"]["entitlements"]["customer_workers"] is True
    assert set(body["infrastructure_features"]) == {
        "custom_storage",
        "dedicated_database",
        "external_database",
        "customer_workers",
    }
    # Without Stripe the catalog still shows prices, but nothing is self-serve.
    plain = make_api().client.get("/api/v1/plans").json()
    assert plain["billing_enabled"] is False
    assert not any(p["self_serve"] for p in plain["plans"])


def test_checkout_webhook_assigns_plan_and_is_idempotent(bapi: Api, fake: FakeStripe) -> None:
    h = bapi.signup("owner@example.com")
    w = ws(bapi, h)
    r = bapi.client.post(
        billing(w, "/checkout"), json={"plan": "pro", "interval": "year"}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.test/")
    checkout = fake.checkouts[-1]
    assert checkout["price_id"] == "price_pro_y"
    assert checkout["seats"] == 1
    assert checkout["success_url"].endswith(f"/w/{w}/billing?checkout=success")
    assert next(iter(fake.customers.values()))["email"] == "owner@example.com"
    # Nothing changes until Stripe confirms.
    assert plan_of(bapi, w) == ("free", None)

    state = fake.complete()
    event_id = "evt_checkout_1"
    obj = {"subscription": state.id, "customer": state.customer_id}
    assert webhook(bapi, "checkout.session.completed", obj, event_id=event_id).json() == {
        "result": "applied"
    }
    assert plan_of(bapi, w) == ("pro", "stripe")
    # Redelivery is a no-op.
    assert webhook(bapi, "checkout.session.completed", obj, event_id=event_id).json() == {
        "result": "duplicate"
    }
    overview = bapi.client.get(billing(w), headers=h).json()
    assert overview["plan_key"] == "pro"
    assert overview["managed_by"] == "stripe"
    assert overview["status"] == "active"
    assert overview["interval"] == "year"
    assert overview["seats"] == 1
    assert overview["can_manage_payment"] is True
    actions = [
        r.action
        for r in sql(bapi, "SELECT action FROM audit_logs WHERE workspace_id = :w", w=uuid.UUID(w))
    ]
    assert "billing.checkout_started" in actions
    assert "workspace.plan_assigned" in actions
    # The new plan applies at once (no stale entitlement cache): Pro allows 25 members.
    plan = bapi.client.get(f"/api/v1/workspaces/{w}/plan", headers=h).json()
    assert plan["plan_key"] == "pro"

    # A second checkout is refused: change the plan instead.
    again = bapi.client.post(billing(w, "/checkout"), json={"plan": "business"}, headers=h)
    assert again.status_code == 409
    assert again.json()["code"] == "subscription_exists"
    portal = bapi.client.post(billing(w, "/portal"), headers=h).json()["url"]
    assert portal.startswith("https://billing.stripe.test/p/cus_")


def test_webhook_signature_is_required(bapi: Api) -> None:
    payload = json.dumps({"id": "evt_x", "type": "customer.subscription.updated"}).encode()
    for signature in (None, "t=1,v1=deadbeef", signed(payload, "whsec_wrong")):
        headers = {"Stripe-Signature": signature} if signature else {}
        r = bapi.client.post("/api/v1/billing/webhook", content=payload, headers=headers)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "invalid_signature"
    assert sql(bapi, "SELECT count(*) AS n FROM billing_events")[0].n == 0


def test_change_plan_seats_past_due_and_cancellation(bapi: Api, fake: FakeStripe) -> None:
    h = bapi.signup("owner@example.com")
    org = str(bapi.create_org(h, "Acme")["id"])
    sid = subscribe(bapi, fake, org, h, "pro")
    assert plan_of(bapi, org) == ("pro", "stripe")

    changed = bapi.client.post(
        billing(org, "/plan"), json={"plan": "business", "interval": "month"}, headers=h
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["plan_key"] == "business"
    assert fake.updates[-1] == {"id": sid, "price_id": "price_biz_m", "seats": 1}
    assert plan_of(bapi, org) == ("business", "stripe")

    # Seats follow the member count.
    bapi.add_member(org, h, "bob@example.com", "member")
    bapi.add_member(org, h, "cy@example.com", "viewer")
    assert sync_seats(bapi, fake) == 1
    assert fake.subscriptions[sid].seats == 3
    assert sync_seats(bapi, fake) == 0

    # Payment trouble: the plan stays while Stripe retries; owners are told.
    bapi.mail.sent.clear()
    fake.set(sid, status="past_due")
    webhook(bapi, "customer.subscription.updated", {"id": sid, "customer": "x"})
    assert plan_of(bapi, org) == ("business", "stripe")
    assert [m.subject for m in bapi.mail.to("owner@example.com")] == ["Acme: payment failed"]
    assert bapi.mail.to("bob@example.com") == []

    # Cancelled: back to Free, keeping any operator overrides.
    fake.set(sid, status="canceled")
    r = webhook(bapi, "customer.subscription.deleted", {"id": sid, "customer": "x"})
    assert r.json() == {"result": "applied"}
    assert plan_of(bapi, org) == ("free", "stripe")
    overview = bapi.client.get(billing(org), headers=h).json()
    assert overview["managed_by"] == "free"
    assert overview["status"] == "canceled"
    # A new checkout is allowed again.
    assert (
        bapi.client.post(billing(org, "/checkout"), json={"plan": "pro"}, headers=h).status_code
        == 201
    )


def sync_seats(api: Api, fake: FakeStripe) -> int:
    async def go() -> int:
        c = build_container(api.settings)
        try:
            async with c.session_factory() as session:
                return await SubscriptionService(session, api.settings, fake).sync_seats()
        finally:
            await c.aclose()

    return asyncio.run(go())


def test_custom_infrastructure_is_part_of_the_plans(
    bapi: Api,
    fake: FakeStripe,
    customer_bucket: dict[str, Any],  # noqa: F811
    external_db: dict[str, Any],  # noqa: F811
) -> None:
    h = bapi.signup("owner@example.com")
    org = str(bapi.create_org(h, "Acme")["id"])
    secret, _ = bapi.enable_mfa(h)
    elevated = bapi.step_up(h, secret)

    # Free: no custom infrastructure, and the error names the plan that has it.
    refused = bapi.client.post(
        url(org, "/versions"), json={"storage": customer_bucket}, headers=elevated
    )
    assert refused.status_code == 422
    assert "It's included from the Pro plan" in refused.json()["detail"]

    # Business (bought online) includes your own storage and database.
    subscribe(bapi, fake, org, h, "business")
    elevated = bapi.step_up(h, secret)
    activate_new(bapi, org, elevated, storage=customer_bucket, database=external_db)
    doc = ready(bapi, org, h)

    overview = bapi.client.get(billing(org), headers=h).json()
    assert overview["features_in_use"] == ["custom_storage", "external_database"]
    assert overview["over_plan"] == []
    assert set(overview["blocked_plans"]) == {"free", "pro"}
    assert "your own (external) database" in overview["blocked_plans"]["pro"]

    # Pro lacks external databases: switching down is refused with a clear reason.
    down = bapi.client.post(billing(org, "/plan"), json={"plan": "pro"}, headers=h)
    assert down.status_code == 409
    assert down.json()["code"] == "plan_lacks_infrastructure"
    assert "Business or higher" in down.json()["detail"]
    assert plan_of(bapi, org)[0] == "business"

    # A cancellation can't be refused: the data keeps working, owners are told.
    sid = next(iter(fake.subscriptions))
    fake.set(sid, status="canceled")
    bapi.mail.sent.clear()
    webhook(bapi, "customer.subscription.deleted", {"id": sid, "customer": "x"})
    assert plan_of(bapi, org)[0] == "free"
    overview = bapi.client.get(billing(org), headers=h).json()
    assert overview["over_plan"] == ["custom_storage", "external_database"]
    (notice,) = bapi.mail.to("owner@example.com")
    assert "your own object storage and your own (external) database" in notice.text
    got = bapi.client.get(f"/api/v1/workspaces/{org}/documents/{doc}", headers=h)
    assert got.status_code == 200
    actions = {
        r.action
        for r in sql(
            bapi, "SELECT action FROM audit_logs WHERE workspace_id = :w", w=uuid.UUID(org)
        )
    }
    assert "billing.over_plan" in actions


def test_access_contract_plans_and_disabled_billing(
    bapi: Api, fake: FakeStripe, make_api: Any
) -> None:
    owner = bapi.signup("owner@example.com")
    org = str(bapi.create_org(owner, "Acme")["id"])
    admin = bapi.add_member(org, owner, "ann@example.com", "admin")
    for method, tail in (("GET", ""), ("POST", "/checkout"), ("POST", "/portal")):
        r = bapi.client.request(method, billing(org, tail), json={"plan": "pro"}, headers=admin)
        assert r.status_code == 403, (tail, r.text)
    # Enterprise isn't sold online.
    r = bapi.client.post(billing(org, "/checkout"), json={"plan": "enterprise"}, headers=owner)
    assert r.status_code == 422
    # A plan set by an operator (a contract) isn't changed online.
    assign(bapi, org, "business")
    r = bapi.client.post(billing(org, "/checkout"), json={"plan": "pro"}, headers=owner)
    assert r.status_code == 409
    assert r.json()["code"] == "plan_managed_by_contract"
    assert bapi.client.get(billing(org), headers=owner).json()["managed_by"] == "contract"
    # Stripe trouble is a clear 503, not a crash.
    assign(bapi, org, "free")
    fake.fail = True
    r = bapi.client.post(billing(org, "/checkout"), json={"plan": "pro"}, headers=owner)
    assert r.status_code == 503
    assert r.json()["code"] == "billing_unavailable"

    plain = make_api()
    h = plain.signup("solo@example.com")
    w = ws(plain, h)
    r = plain.client.post(billing(w, "/checkout"), json={"plan": "pro"}, headers=h)
    assert r.status_code == 503
    assert r.json()["code"] == "billing_disabled"
    overview = plain.client.get(billing(w), headers=h).json()
    assert overview["enabled"] is False
    assert overview["plan_key"] == "free"
