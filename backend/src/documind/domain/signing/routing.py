"""Signing order (design §5.3).

Every recipient has a ``group_order`` (1, 2, …). Everyone in a group may act at the same
time; a group starts only when every recipient in all earlier groups has finished.
Sequential routing = one recipient per group; parallel = everyone in group 1.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from documind.domain.errors import ValidationFailedError
from documind.domain.signing.states import RECIPIENT_DONE, RecipientRole, RecipientStatus


class Routing(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    GROUPS = "groups"


@dataclass(frozen=True, slots=True)
class RoutedRecipient:
    id: object
    group_order: int
    status: RecipientStatus
    role: RecipientRole = RecipientRole.SIGNER


def normalise_orders(routing: Routing, orders: list[int]) -> list[int]:
    """The group numbers to store, given the sender's choice and the order they entered."""
    if routing is Routing.PARALLEL:
        return [1] * len(orders)
    if routing is Routing.SEQUENTIAL:
        return list(range(1, len(orders) + 1))
    if any(o < 1 for o in orders):
        raise ValidationFailedError("Signing groups start at 1.", code="invalid_signing_order")
    # Groups: renumber 1..n without gaps, keeping the order the sender chose.
    distinct = sorted(set(orders))
    rank = {o: i + 1 for i, o in enumerate(distinct)}
    return [rank[o] for o in orders]


def active_group(recipients: Iterable[RoutedRecipient]) -> int | None:
    """The lowest group with someone still to act, or None when everyone has finished."""
    waiting = [r.group_order for r in recipients if r.status not in RECIPIENT_DONE]
    return min(waiting) if waiting else None


def may_act(recipient: RoutedRecipient, everyone: Iterable[RoutedRecipient]) -> bool:
    group = active_group(everyone)
    return group is not None and recipient.group_order == group


def all_done(recipients: Iterable[RoutedRecipient]) -> bool:
    return all(r.status in RECIPIENT_DONE for r in recipients)
