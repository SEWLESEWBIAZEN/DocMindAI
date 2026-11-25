"""Stripe behind the BillingGateway port (ADR 0032).

Checkout and the customer portal are hosted by Stripe, so card details never
reach DocuMind. Webhook signatures are verified locally with the endpoint secret.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import stripe

from documind.application.billing.gateway import (
    BillingGatewayError,
    InvalidWebhookError,
    SubscriptionState,
)

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, default)


def subscription_state(sub: Any) -> SubscriptionState:
    """Read a Stripe subscription (object or plain dict) into the port's shape."""
    items = _get(_get(sub, "items"), "data", []) or []
    item = items[0] if items else None
    price = _get(item, "price")
    # Newer API versions keep the billing period on the item, older on the subscription.
    period_end = _get(item, "current_period_end") or _get(sub, "current_period_end")
    customer = _get(sub, "customer")
    metadata = _get(sub, "metadata") or {}
    raw_ws = _get(metadata, "workspace_id")
    try:
        workspace_id = uuid.UUID(str(raw_ws)) if raw_ws else None
    except ValueError:
        workspace_id = None
    return SubscriptionState(
        id=str(_get(sub, "id")),
        customer_id=str(customer if isinstance(customer, str) else _get(customer, "id")),
        status=str(_get(sub, "status")),
        price_id=str(_get(price, "id")) if price is not None else None,
        seats=int(_get(item, "quantity", 0) or 0),
        current_period_end=(
            datetime.fromtimestamp(int(period_end), tz=UTC) if period_end else None
        ),
        cancel_at_period_end=bool(_get(sub, "cancel_at_period_end", False)),
        workspace_id=workspace_id,
    )


class StripeGateway:
    def __init__(
        self,
        *,
        secret_key: str,
        webhook_secret: str,
        timeout_seconds: float = 20.0,
        client: stripe.StripeClient | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._client = client or stripe.StripeClient(
            secret_key,
            http_client=stripe.HTTPXClient(timeout=timeout_seconds),
            max_network_retries=2,
        )

    async def create_customer(self, *, email: str, name: str, workspace_id: uuid.UUID) -> str:
        try:
            customer = await self._client.v1.customers.create_async(
                {
                    "email": email,
                    "name": name[:250],
                    "metadata": {"workspace_id": str(workspace_id)},
                }
            )
        except stripe.StripeError as exc:
            raise _error(exc) from exc
        return str(customer.id)

    async def checkout_url(
        self,
        *,
        customer_id: str,
        price_id: str,
        seats: int,
        workspace_id: uuid.UUID,
        success_url: str,
        cancel_url: str,
    ) -> str:
        meta = {"workspace_id": str(workspace_id)}
        try:
            session = await self._client.v1.checkout.sessions.create_async(
                {
                    "mode": "subscription",
                    "customer": customer_id,
                    "client_reference_id": str(workspace_id),
                    "line_items": [{"price": price_id, "quantity": seats}],
                    "subscription_data": {"metadata": meta},
                    "metadata": meta,
                    "allow_promotion_codes": True,
                    "billing_address_collection": "auto",
                    "tax_id_collection": {"enabled": True},
                    "customer_update": {"name": "auto", "address": "auto"},
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                }
            )
        except stripe.StripeError as exc:
            raise _error(exc) from exc
        if not session.url:  # pragma: no cover - hosted mode always returns a URL
            raise BillingGatewayError("Stripe did not return a checkout URL")
        return str(session.url)

    async def portal_url(self, *, customer_id: str, return_url: str) -> str:
        try:
            session = await self._client.v1.billing_portal.sessions.create_async(
                {"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as exc:
            raise _error(exc) from exc
        return str(session.url)

    async def subscription(self, subscription_id: str) -> SubscriptionState:
        try:
            sub = await self._client.v1.subscriptions.retrieve_async(subscription_id)
        except stripe.StripeError as exc:
            raise _error(exc) from exc
        return subscription_state(sub)

    async def update_subscription(
        self, subscription_id: str, *, price_id: str | None = None, seats: int | None = None
    ) -> SubscriptionState:
        try:
            sub = await self._client.v1.subscriptions.retrieve_async(subscription_id)
            item = sub["items"]["data"][0]
            change: dict[str, Any] = {"id": str(item["id"])}
            if price_id is not None:
                change["price"] = price_id
            change["quantity"] = seats if seats is not None else int(item["quantity"] or 1)
            params: Any = {"items": [change], "proration_behavior": "create_prorations"}
            updated = await self._client.v1.subscriptions.update_async(subscription_id, params)
        except stripe.StripeError as exc:
            raise _error(exc) from exc
        return subscription_state(updated)

    def verify_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise InvalidWebhookError(str(exc)) from exc
        event: dict[str, Any] = json.loads(payload)
        return event


def _error(exc: stripe.StripeError) -> BillingGatewayError:
    # Never log request bodies: they can contain customer details.
    logger.warning(
        "stripe request failed",
        extra={"stripe_error": type(exc).__name__, "http_status": exc.http_status},
    )
    return BillingGatewayError(type(exc).__name__)
