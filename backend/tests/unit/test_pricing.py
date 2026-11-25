"""Plan prices, Stripe settings and infrastructure tiers (ADR 0032)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from pydantic import ValidationError

from documind.application.billing.gateway import InvalidWebhookError
from documind.application.billing.subscriptions import (
    _blocking_reason,
    catalog,
    price_id_for,
    price_table,
)
from documind.domain.billing.plans import (
    PLANS,
    BillingInterval,
    PlanKey,
    cheapest_plan_with,
)
from documind.domain.infrastructure.profiles import (
    PLATFORM_DEFAULT,
    ProfileError,
    check_entitlements,
    features_in_use,
    parse_config,
)
from documind.infrastructure.billing.stripe_gateway import StripeGateway, subscription_state
from tests.conftest import SettingsFactory

STRIPE = {
    "stripe_secret_key": "sk_test_x",
    "stripe_webhook_secret": "whsec_x",
    "stripe_price_pro_monthly": "p1",
    "stripe_price_pro_annual": "p2",
    "stripe_price_business_monthly": "p3",
    "stripe_price_business_annual": "p4",
}


def test_prices_per_member_with_two_months_free_annually() -> None:
    assert PLANS[PlanKey.FREE].pricing.monthly_cents_per_member == 0
    assert PLANS[PlanKey.PRO].pricing.monthly_cents_per_member == 1_900
    assert PLANS[PlanKey.PRO].pricing.cents_per_member(BillingInterval.YEAR) == 19_000
    assert PLANS[PlanKey.BUSINESS].pricing.monthly_cents_per_member == 9_900
    assert PLANS[PlanKey.BUSINESS].pricing.annual_cents_per_member == 99_000
    enterprise = PLANS[PlanKey.ENTERPRISE].pricing
    assert enterprise.monthly_cents_per_member is None
    assert enterprise.annual_cents_per_member is None
    assert not enterprise.self_serve
    assert [k for k, p in PLANS.items() if p.pricing.self_serve] == [PlanKey.PRO, PlanKey.BUSINESS]


def test_custom_infrastructure_tiers() -> None:
    assert cheapest_plan_with("custom_storage") is PlanKey.PRO
    assert cheapest_plan_with("dedicated_database") is PlanKey.BUSINESS
    assert cheapest_plan_with("external_database") is PlanKey.BUSINESS
    assert cheapest_plan_with("customer_workers") is PlanKey.ENTERPRISE
    # Every higher plan keeps what a lower one has.
    order = list(PlanKey)
    for feature in (
        "custom_storage",
        "dedicated_database",
        "external_database",
        "customer_workers",
    ):
        flags = [getattr(PLANS[k].entitlements, feature) for k in order]
        assert flags == sorted(flags)


def test_entitlement_errors_name_the_plan_that_includes_the_feature() -> None:
    config = parse_config(
        {
            "database": {"mode": "dedicated"},
            "storage": {
                "provider": "minio",
                "bucket": "acme-docs",
                "endpoint_url": "https://minio.acme.example",
                "region": "us-east-1",
                "access_key_id": "AKIAEXAMPLE",
            },
        }
    )
    assert features_in_use(config) == ["custom_storage", "dedicated_database"]
    assert features_in_use(PLATFORM_DEFAULT) == []
    with pytest.raises(ProfileError, match=r"your own object storage\. It's included from the Pro"):
        check_entitlements(config, PLANS[PlanKey.FREE].entitlements)
    with pytest.raises(
        ProfileError, match=r"a dedicated database\. It's included from the Business"
    ):
        check_entitlements(config, PLANS[PlanKey.PRO].entitlements)
    check_entitlements(config, PLANS[PlanKey.BUSINESS].entitlements)

    reason = _blocking_reason(PLANS[PlanKey.PRO], ["custom_storage", "dedicated_database"])
    assert reason is not None
    assert "a dedicated database" in reason
    assert "Business or higher" in reason
    assert _blocking_reason(PLANS[PlanKey.BUSINESS], ["dedicated_database"]) is None


def test_stripe_settings_all_or_nothing(make_settings: SettingsFactory) -> None:
    settings = make_settings
    assert not settings().billing_enabled
    full = settings(**STRIPE)
    assert full.billing_enabled
    table = price_table(full)
    assert table["p2"].plan.key is PlanKey.PRO
    assert table["p2"].interval is BillingInterval.YEAR
    assert price_id_for(full, PlanKey.BUSINESS, BillingInterval.MONTH) == "p3"
    with pytest.raises(ValidationError, match="partly configured"):
        settings(stripe_secret_key="sk_test_x")
    with pytest.raises(ValidationError, match="whsec_"):
        settings(**{**STRIPE, "stripe_webhook_secret": "nope"})
    with pytest.raises(ValidationError, match="different price"):
        settings(**{**STRIPE, "stripe_price_pro_annual": "p1"})
    # Blank values from compose mean "unset".
    assert not settings(**dict.fromkeys(STRIPE, "")).billing_enabled
    assert all(not p["self_serve"] for p in catalog(settings()))
    assert [p["key"] for p in catalog(full) if p["self_serve"]] == ["pro", "business"]


def test_subscription_state_reads_current_and_older_api_shapes() -> None:
    newer = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "cancel_at_period_end": True,
        "metadata": {"workspace_id": "6b4f4b8e-8e7c-4d1c-9a55-2f1d3b0e7a11"},
        "items": {
            "data": [
                {
                    "id": "si_1",
                    "quantity": 4,
                    "price": {"id": "p1"},
                    "current_period_end": 1_800_000_000,
                }
            ]
        },
    }
    state = subscription_state(newer)
    assert (state.price_id, state.seats, state.cancel_at_period_end) == ("p1", 4, True)
    assert state.current_period_end is not None
    assert state.current_period_end.year == 2027
    assert str(state.workspace_id) == "6b4f4b8e-8e7c-4d1c-9a55-2f1d3b0e7a11"
    older = {
        **newer,
        "current_period_end": 1_700_000_000,
        "metadata": {"workspace_id": "bad"},
        "customer": {"id": "cus_2"},
    }
    older["items"] = {"data": [{"id": "si_1", "quantity": 1, "price": {"id": "p2"}}]}
    state = subscription_state(older)
    assert state.customer_id == "cus_2"
    assert state.workspace_id is None
    assert state.current_period_end is not None
    assert state.current_period_end.year == 2023


def test_webhook_signature_verification() -> None:
    gateway = StripeGateway(secret_key="sk_test_x", webhook_secret="whsec_unit")
    payload = json.dumps({"id": "evt_1", "type": "ping"}).encode()
    ts = int(time.time())
    good = hmac.new(b"whsec_unit", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    assert gateway.verify_event(payload, f"t={ts},v1={good}")["id"] == "evt_1"
    with pytest.raises(InvalidWebhookError):
        gateway.verify_event(payload, f"t={ts},v1={'0' * 64}")
    with pytest.raises(InvalidWebhookError):
        gateway.verify_event(payload + b" ", f"t={ts},v1={good}")
    old = ts - 3600
    stale = hmac.new(b"whsec_unit", f"{old}.".encode() + payload, hashlib.sha256).hexdigest()
    with pytest.raises(InvalidWebhookError):  # replayed outside the tolerance window
        gateway.verify_event(payload, f"t={old},v1={stale}")
