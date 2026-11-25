"""Required-2FA policy (ADR 0026)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from documind.domain.errors import PermissionDeniedError
from documind.domain.identity.permissions import RoleKey
from documind.domain.identity.policies import (
    WorkspaceContext,
    WorkspaceKind,
    ensure_mfa_policy_met,
    mfa_enrollment_deadline,
    mfa_required_by,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("plan", "org", "expected"),
    [
        (None, False, None),
        ("free", False, None),
        ("free", True, "organization"),
        ("pro", False, "plan"),
        ("business", True, "plan"),
        ("enterprise", False, "plan"),
    ],
)
def test_who_requires_2fa(plan: str | None, org: bool, expected: str | None) -> None:
    assert mfa_required_by(plan, org) == expected


def test_grace_counts_from_when_it_became_required() -> None:
    assert mfa_enrollment_deadline(NOW, NOW + timedelta(days=3), 14) == NOW + timedelta(days=14)
    assert mfa_enrollment_deadline(None, NOW, 14) == NOW + timedelta(days=14)


def _ctx(**overrides: object) -> WorkspaceContext:
    base = WorkspaceContext(
        workspace_id=uuid.uuid4(),
        workspace_kind=WorkspaceKind.ORGANIZATION,
        user_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role=RoleKey.MEMBER,
        permissions=frozenset(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_members_are_refused_only_after_the_deadline() -> None:
    deadline = NOW + timedelta(days=1)
    ensure_mfa_policy_met(_ctx(), NOW)  # not required
    ensure_mfa_policy_met(_ctx(mfa_required=True, mfa_enrollment_deadline=deadline), NOW)
    ensure_mfa_policy_met(
        _ctx(mfa_required=True, mfa_enabled=True, mfa_enrollment_deadline=None), NOW
    )
    with pytest.raises(PermissionDeniedError) as exc:
        ensure_mfa_policy_met(_ctx(mfa_required=True, mfa_enrollment_deadline=deadline), deadline)
    assert exc.value.code == "workspace_requires_mfa"
