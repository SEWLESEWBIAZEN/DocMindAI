"""Required 2FA (ADR 0026): paid plans, or any organization that turns it on.

Members without 2FA keep access during a grace period, then get 403
``workspace_requires_mfa`` until they set it up. Reading the workspace and
leaving it always work, so nobody is trapped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration.conftest import Api
from tests.integration.test_processing_pipeline import sql
from tests.integration.test_usage_and_quotas import assign

pytestmark = pytest.mark.integration


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _summary(api: Api, headers: dict[str, str], workspace: str) -> dict[str, Any]:
    listed = api.client.get("/api/v1/me/workspaces", headers=headers).json()
    return next(w for w in listed if w["id"] == workspace)


def _documents(api: Api, headers: dict[str, str], workspace: str) -> Any:
    return api.client.get(f"/api/v1/workspaces/{workspace}/documents", headers=headers)


def _end_grace(api: Api, workspace: str) -> None:
    sql(
        api,
        "UPDATE workspaces SET mfa_enforced_since = now() - interval '15 days' WHERE id = :w",
        w=uuid.UUID(workspace),
    )


def test_free_workspaces_leave_2fa_optional(api: Api) -> None:
    headers = api.signup(_email("free"))
    personal = str(api.me(headers)["default_workspace_id"])
    summary = _summary(api, headers, personal)
    assert summary["mfa_required_by"] is None
    assert summary["mfa_enrollment_deadline"] is None
    assert _documents(api, headers, personal).status_code == 200


def test_paid_plans_require_2fa_after_a_grace_period(api: Api) -> None:
    owner = api.signup(_email("owner"))
    org = str(api.create_org(owner, "Paid Org")["id"])
    member_email = _email("member")
    member = api.add_member(org, owner, member_email, "member")
    assign(api, org, "pro")

    # Grace period: access continues, and the deadline is announced.
    assert _documents(api, member, org).status_code == 200
    summary = _summary(api, member, org)
    assert summary["mfa_required_by"] == "plan"
    deadline = datetime.fromisoformat(summary["mfa_enrollment_deadline"])
    assert timedelta(days=13) < deadline - datetime.now(UTC) <= timedelta(days=14)

    # After the grace period: locked out of the workspace's content...
    _end_grace(api, org)
    blocked = _documents(api, member, org)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "workspace_requires_mfa"
    assert (
        api.client.get(f"/api/v1/workspaces/{org}/members", headers=member).json()["code"]
        == "workspace_requires_mfa"
    )
    # ...but the workspace can still be read (for the UI), and personal workspaces are unaffected.
    assert api.client.get(f"/api/v1/workspaces/{org}", headers=member).status_code == 200
    personal = str(api.me(member)["default_workspace_id"])
    assert _documents(api, member, personal).status_code == 200

    # Setting up 2FA restores access immediately; the same token keeps working.
    api.enable_mfa(member)
    assert _documents(api, member, org).status_code == 200
    assert _summary(api, member, org)["mfa_enrollment_deadline"] is None


def test_locked_out_members_can_still_leave(api: Api) -> None:
    owner = api.signup(_email("owner"))
    org = str(api.create_org(owner, "Leavers")["id"])
    member = api.add_member(org, owner, _email("leaver"), "member")
    assign(api, org, "business")
    _documents(api, member, org)  # first use while required starts the grace period
    _end_grace(api, org)
    assert _documents(api, member, org).status_code == 403
    left = api.client.post(f"/api/v1/workspaces/{org}/leave", headers=member)
    assert left.status_code == 204, left.text


def test_organizations_can_require_2fa_on_the_free_plan(api: Api) -> None:
    owner = api.signup(_email("owner"))
    org = str(api.create_org(owner, "Careful Org")["id"])
    member_email = _email("member")
    member = api.add_member(org, owner, member_email, "member")
    url = f"/api/v1/workspaces/{org}/mfa-requirement"

    # Changing a security setting needs 2FA and a fresh code; members can't.
    refused = api.client.put(url, json={"required": True}, headers=owner)
    assert refused.status_code == 403
    assert refused.json()["code"] == "mfa_enrollment_required"
    assert api.client.put(url, json={"required": True}, headers=member).status_code == 403
    secret, _ = api.enable_mfa(owner)
    turned_on = api.client.put(url, json={"required": True}, headers=api.step_up(owner, secret))
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json()["organization_requires_mfa"] is True
    assert turned_on.json()["mfa_required_by"] == "organization"

    # Admins see who still has to set it up; plain members don't see others' status.
    as_owner = {
        m["email"]: m["mfa_enabled"]
        for m in api.client.get(f"/api/v1/workspaces/{org}/members", headers=owner).json()
    }
    assert as_owner[member_email] is False
    assert all(
        m["mfa_enabled"] is None
        for m in api.client.get(f"/api/v1/workspaces/{org}/members", headers=member).json()
    )

    assert _summary(api, member, org)["mfa_enrollment_deadline"] is not None
    _end_grace(api, org)
    assert _documents(api, member, org).json()["code"] == "workspace_requires_mfa"

    # Turning it off restores access and resets the grace period for next time.
    turned_off = api.client.put(url, json={"required": False}, headers=api.step_up(owner, secret))
    assert turned_off.status_code == 200
    assert _documents(api, member, org).status_code == 200
    since = sql(api, "SELECT mfa_enforced_since FROM workspaces WHERE id = :w", w=uuid.UUID(org))
    assert since[0].mfa_enforced_since is None
    actions = [
        r.action
        for r in sql(
            api,
            "SELECT action FROM audit_logs WHERE workspace_id = :w ORDER BY created_at",
            w=uuid.UUID(org),
        )
    ]
    assert actions.count("workspace.mfa_requirement_changed") == 2


def test_personal_workspaces_require_2fa_on_paid_plans(api: Api) -> None:
    headers = api.signup(_email("solo"))
    personal = str(api.me(headers)["default_workspace_id"])
    assign(api, personal, "pro")
    assert _summary(api, headers, personal)["mfa_required_by"] == "plan"
    _documents(api, headers, personal)
    _end_grace(api, personal)
    assert _documents(api, headers, personal).json()["code"] == "workspace_requires_mfa"
    # Personal workspaces have no organization setting to change.
    assert (
        api.client.put(
            f"/api/v1/workspaces/{personal}/mfa-requirement",
            json={"required": False},
            headers=headers,
        ).status_code
        == 403
    )
    # A downgrade lifts the requirement, and the grace period starts again on the next upgrade.
    assign(api, personal, "free")
    assert _documents(api, headers, personal).status_code == 200
    assign(api, personal, "pro")
    assert _documents(api, headers, personal).status_code == 200
