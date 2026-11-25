"""Port for the payment provider (Stripe in production, a fake in tests)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class BillingGatewayError(Exception):
    """The payment provider failed or rejected a request."""


class InvalidWebhookError(Exception):
    """A webhook payload whose signature doesn't verify."""


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    """The provider's current view of one subscription."""

    id: str
    customer_id: str
    status: str
    price_id: str | None
    seats: int
    current_period_end: datetime | None
    cancel_at_period_end: bool
    workspace_id: uuid.UUID | None
    trial_end: datetime | None = None


class BillingGateway(Protocol):
    async def create_customer(self, *, email: str, name: str, workspace_id: uuid.UUID) -> str: ...

    async def checkout_url(
        self,
        *,
        customer_id: str,
        price_id: str,
        seats: int,
        workspace_id: uuid.UUID,
        success_url: str,
        cancel_url: str,
        trial_days: int | None = None,
    ) -> str: ...

    async def portal_url(self, *, customer_id: str, return_url: str) -> str: ...

    async def subscription(self, subscription_id: str) -> SubscriptionState: ...

    async def update_subscription(
        self, subscription_id: str, *, price_id: str | None = None, seats: int | None = None
    ) -> SubscriptionState: ...

    def verify_event(self, payload: bytes, signature: str) -> dict[str, Any]: ...
