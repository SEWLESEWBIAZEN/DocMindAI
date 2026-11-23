"""Workspaces, organizations, membership, invitations and tenant isolation."""

from __future__ import annotations

import uuid

import pytest

from tests.integration.conftest import Api

pytestmark = pytest.mark.integration

WS = "/api/v1/workspaces"


def _workspaces(api: Api, headers: dict[str, str]) -> list[dict[str, object]]:
    response = api.client.get("/api/v1/me/workspaces", headers=headers)
    assert response.status_code == 200
    result: list[dict[str, object]] = response.json()
    return result


# --- organizations + switching ---


def test_create_organization_and_switch_workspaces(api: Api) -> None:
    ada = api.signup("ada@example.com")
    personal = _workspaces(api, ada)
    assert [(w["kind"], w["role"], w["is_default"]) for w in personal] == [
        ("personal", "owner", True)
    ]

    org = api.create_org(ada, "  Acme   Legal ")
    assert org["name"] == "Acme Legal"
    assert org["kind"] == "organization"
    assert org["role"] == "owner"
    assert str(org["organization_slug"]).startswith("acme-legal-")

    # A user can belong to several workspaces and choose the active one.
    assert {w["kind"] for w in _workspaces(api, ada)} == {"personal", "organization"}
    assert (
        api.client.put(
            "/api/v1/me/active-workspace", json={"workspace_id": org["id"]}, headers=ada
        ).status_code
        == 204
    )
    assert api.me(ada)["default_workspace_id"] == org["id"]
    defaults = {w["id"]: w["is_default"] for w in _workspaces(api, ada)}
    assert defaults[org["id"]] is True


def test_cannot_switch_to_a_workspace_you_do_not_belong_to(api: Api) -> None:
    ada = api.signup("ada@example.com")
    bob = api.signup("bob@example.com")
    org = api.create_org(ada)
    for target in (org["id"], str(uuid.uuid4())):
        response = api.client.put(
            "/api/v1/me/active-workspace", json={"workspace_id": target}, headers=bob
        )
        assert response.status_code == 404


def test_rename_workspace_requires_permission(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    member = api.add_member(org["id"], ada, "mia@example.com", "member")
    assert (
        api.client.patch(f"{WS}/{org['id']}", json={"name": "Acme Inc"}, headers=member).status_code
        == 403
    )
    renamed = api.client.patch(f"{WS}/{org['id']}", json={"name": "Acme Inc"}, headers=ada)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Acme Inc"


# --- tenant isolation / IDOR ---


def test_cross_workspace_access_is_rejected_as_not_found(api: Api) -> None:
    ada = api.signup("ada@example.com")
    eve = api.signup("eve@example.com")
    org = api.create_org(ada)
    ada_personal = next(w for w in _workspaces(api, ada) if w["kind"] == "personal")

    for workspace_id in (org["id"], ada_personal["id"], str(uuid.uuid4())):
        for method, path in [
            ("GET", ""),
            ("PATCH", ""),
            ("GET", "/members"),
            ("GET", "/invitations"),
            ("POST", "/invitations"),
            ("GET", "/audit-logs"),
            ("POST", "/leave"),
        ]:
            body = (
                {"name": "pwned"}
                if method == "PATCH"
                else {"email": "eve@example.com", "role": "owner"}
            )
            response = api.client.request(
                method, f"{WS}/{workspace_id}{path}", json=body, headers=eve
            )
            # 404, not 403: outsiders can't even confirm the workspace exists.
            assert response.status_code == 404, (method, path, response.text)
    # Nothing changed.
    assert api.client.get(f"{WS}/{org['id']}", headers=ada).json()["name"] == "Acme"


def test_member_ids_from_another_workspace_are_not_found(api: Api) -> None:
    """An admin of org A can't touch members of org B by reusing B's member IDs (IDOR)."""
    ada = api.signup("ada@example.com")
    org_a = api.create_org(ada, "Org A")
    eve = api.signup("eve@example.com")
    org_b = api.create_org(eve, "Org B")
    api.add_member(org_b["id"], eve, "bob@example.com", "member")
    victim = api.member_id(org_b["id"], eve, "bob@example.com")

    assert (
        api.client.patch(
            f"{WS}/{org_a['id']}/members/{victim}", json={"role": "viewer"}, headers=ada
        ).status_code
        == 404
    )
    assert api.client.delete(f"{WS}/{org_a['id']}/members/{victim}", headers=ada).status_code == 404
    roles = {
        m["email"]: m["role"]
        for m in api.client.get(f"{WS}/{org_b['id']}/members", headers=eve).json()
    }
    assert roles["bob@example.com"] == "member"


def test_invitation_ids_from_another_workspace_are_not_found(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org_a = api.create_org(ada, "Org A")
    eve = api.signup("eve@example.com")
    org_b = api.create_org(eve, "Org B")
    invitation = api.client.post(
        f"{WS}/{org_b['id']}/invitations",
        json={"email": "x@example.com", "role": "member"},
        headers=eve,
    ).json()
    assert (
        api.client.delete(
            f"{WS}/{org_a['id']}/invitations/{invitation['id']}", headers=ada
        ).status_code
        == 404
    )
    # Someone else's invitation can't be accepted or declined by ID either.
    assert (
        api.client.post(
            f"/api/v1/me/invitations/{invitation['id']}/accept", headers=ada
        ).status_code
        == 404
    )


# --- invitations ---


def test_invitation_flow(api: Api) -> None:
    ada = api.signup("ada@example.com", name="Ada")
    org = api.create_org(ada, "Acme")
    bob = api.signup("bob@example.com")

    created = api.client.post(
        f"{WS}/{org['id']}/invitations",
        json={"email": "BOB@example.com", "role": "member"},
        headers=ada,
    )
    assert created.status_code == 201
    assert created.json()["email"] == "bob@example.com"
    email = api.mail.to("bob@example.com")[-1]
    assert email.subject == "Ada invited you to Acme on DocuMind"
    assert "https://app.test.example/invitations/accept?token=" in email.text

    pending = api.client.get("/api/v1/me/invitations", headers=bob).json()
    assert [(i["workspace_name"], i["role"]) for i in pending] == [("Acme", "member")]
    accepted = api.client.post(f"/api/v1/me/invitations/{pending[0]['id']}/accept", headers=bob)
    assert accepted.status_code == 200
    assert accepted.json()["workspace_id"] == org["id"]

    assert api.client.get(f"{WS}/{org['id']}", headers=bob).json()["role"] == "member"
    assert api.client.get("/api/v1/me/invitations", headers=bob).json() == []
    # The emailed token can't be used a second time.
    token = api.mail.last_token("bob@example.com", "/invitations/accept")
    assert (
        api.client.post(
            "/api/v1/invitations/accept", json={"token": token}, headers=bob
        ).status_code
        == 400
    )


def test_invitation_is_bound_to_the_invited_email(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    api.client.post(
        f"{WS}/{org['id']}/invitations",
        json={"email": "bob@example.com", "role": "admin"},
        headers=ada,
    )
    token = api.mail.last_token("bob@example.com", "/invitations/accept")
    eve = api.signup("eve@example.com")
    stolen = api.client.post("/api/v1/invitations/accept", json={"token": token}, headers=eve)
    assert stolen.status_code == 403
    assert stolen.json()["code"] == "invitation_email_mismatch"
    assert api.client.get(f"{WS}/{org['id']}", headers=eve).status_code == 404


def test_decline_revoke_and_reinvite(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    bob = api.signup("bob@example.com")
    first = api.client.post(
        f"{WS}/{org['id']}/invitations", json={"email": "bob@example.com"}, headers=ada
    ).json()
    second = api.client.post(
        f"{WS}/{org['id']}/invitations", json={"email": "bob@example.com"}, headers=ada
    ).json()
    assert first["id"] != second["id"]
    listed = api.client.get(f"{WS}/{org['id']}/invitations", headers=ada).json()
    assert [i["id"] for i in listed] == [second["id"]]  # re-invite replaced the first

    assert (
        api.client.post(f"/api/v1/me/invitations/{second['id']}/decline", headers=bob).status_code
        == 204
    )
    third = api.client.post(
        f"{WS}/{org['id']}/invitations", json={"email": "bob@example.com"}, headers=ada
    ).json()
    assert (
        api.client.delete(f"{WS}/{org['id']}/invitations/{third['id']}", headers=ada).status_code
        == 204
    )
    assert (
        api.client.post(f"/api/v1/me/invitations/{third['id']}/accept", headers=bob).status_code
        == 400
    )


def test_invitation_rules(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    admin = api.add_member(org["id"], ada, "alan@example.com", "admin")
    viewer = api.add_member(org["id"], ada, "vic@example.com", "viewer")
    url = f"{WS}/{org['id']}/invitations"

    assert api.client.post(url, json={"email": "x@example.com"}, headers=viewer).status_code == 403
    assert (
        api.client.post(
            url, json={"email": "x@example.com", "role": "admin"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        api.client.post(
            url, json={"email": "x@example.com", "role": "member"}, headers=admin
        ).status_code
        == 201
    )
    already = api.client.post(url, json={"email": "vic@example.com"}, headers=ada)
    assert already.status_code == 409
    assert already.json()["code"] == "already_member"
    personal = next(w for w in _workspaces(api, ada) if w["kind"] == "personal")
    shared = api.client.post(
        f"{WS}/{personal['id']}/invitations", json={"email": "x@example.com"}, headers=ada
    )
    assert shared.status_code == 409
    assert shared.json()["code"] == "personal_workspace"


def test_unverified_users_cannot_accept(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    api.client.post(f"{WS}/{org['id']}/invitations", json={"email": "bob@example.com"}, headers=ada)
    # Bob registers but never verifies: he can't sign in, so he can't accept either.
    api.register("bob@example.com")
    assert api.login("bob@example.com").status_code == 403


# --- roles and ownership ---


def test_role_permission_matrix_over_http(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    admin = api.add_member(org["id"], ada, "alan@example.com", "admin")
    member = api.add_member(org["id"], ada, "mia@example.com", "member")
    viewer = api.add_member(org["id"], ada, "vic@example.com", "viewer")
    base = f"{WS}/{org['id']}"

    for headers in (ada, admin, member, viewer):
        assert api.client.get(base, headers=headers).status_code == 200
        assert api.client.get(f"{base}/members", headers=headers).status_code == 200
    for headers, allowed in ((ada, 200), (admin, 200), (member, 403), (viewer, 403)):
        assert api.client.get(f"{base}/audit-logs", headers=headers).status_code == allowed
        assert api.client.get(f"{base}/invitations", headers=headers).status_code == allowed


def test_role_changes_follow_hierarchy(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    admin = api.add_member(org["id"], ada, "alan@example.com", "admin")
    api.add_member(org["id"], ada, "mia@example.com", "member")
    api.add_member(org["id"], ada, "ann@example.com", "admin")
    base = f"{WS}/{org['id']}/members"
    ids = {
        e: api.member_id(org["id"], ada, e)
        for e in ("ada@example.com", "alan@example.com", "mia@example.com", "ann@example.com")
    }

    assert (
        api.client.patch(
            f"{base}/{ids['mia@example.com']}", json={"role": "viewer"}, headers=admin
        ).status_code
        == 204
    )
    assert (
        api.client.patch(
            f"{base}/{ids['mia@example.com']}", json={"role": "admin"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        api.client.patch(
            f"{base}/{ids['ann@example.com']}", json={"role": "member"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        api.client.patch(
            f"{base}/{ids['ada@example.com']}", json={"role": "member"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        api.client.patch(
            f"{base}/{ids['alan@example.com']}", json={"role": "owner"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        api.client.patch(
            f"{base}/{ids['mia@example.com']}", json={"role": "owner"}, headers=ada
        ).status_code
        == 204
    )
    roles = {m["email"]: m["role"] for m in api.client.get(base, headers=ada).json()}
    assert roles == {
        "ada@example.com": "owner",
        "mia@example.com": "owner",
        "alan@example.com": "admin",
        "ann@example.com": "admin",
    }


def test_last_owner_rules(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    base = f"{WS}/{org['id']}"
    last = api.client.post(f"{base}/leave", headers=ada)
    assert last.status_code == 409
    assert last.json()["code"] == "last_owner"

    bob = api.add_member(org["id"], ada, "bob@example.com", "member")
    bob_id = api.member_id(org["id"], ada, "bob@example.com")
    assert (
        api.client.patch(
            f"{base}/members/{bob_id}", json={"role": "owner"}, headers=ada
        ).status_code
        == 204
    )
    assert (
        api.client.post(f"{base}/leave", headers=ada).status_code == 204
    )  # another owner exists now
    assert api.client.get(base, headers=ada).status_code == 404
    ada_id_gone = [m["email"] for m in api.client.get(f"{base}/members", headers=bob).json()]
    assert ada_id_gone == ["bob@example.com"]
    assert api.client.post(f"{base}/leave", headers=bob).status_code == 409


def test_owners_can_demote_each_other_but_not_the_last_one(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    bob = api.add_member(org["id"], ada, "bob@example.com", "owner")
    base = f"{WS}/{org['id']}/members"
    ada_id = api.member_id(org["id"], ada, "ada@example.com")
    assert (
        api.client.patch(f"{base}/{ada_id}", json={"role": "admin"}, headers=bob).status_code == 204
    )
    # Bob is now the only owner; nobody can remove that.
    bob_id = api.member_id(org["id"], ada, "bob@example.com")
    assert (
        api.client.patch(f"{base}/{bob_id}", json={"role": "admin"}, headers=ada).status_code == 403
    )
    assert (
        api.client.patch(f"{base}/{bob_id}", json={"role": "admin"}, headers=bob).status_code == 403
    )  # can't manage self


def test_remove_member_and_leave(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    admin = api.add_member(org["id"], ada, "alan@example.com", "admin")
    mia = api.add_member(org["id"], ada, "mia@example.com", "member")
    api.client.put("/api/v1/me/active-workspace", json={"workspace_id": org["id"]}, headers=mia)

    mia_id = api.member_id(org["id"], ada, "mia@example.com")
    assert api.client.delete(f"{WS}/{org['id']}/members/{mia_id}", headers=admin).status_code == 204
    assert api.client.get(f"{WS}/{org['id']}", headers=mia).status_code == 404
    # Mia's active workspace falls back to her personal one.
    personal = next(w for w in _workspaces(api, mia) if w["kind"] == "personal")
    assert api.me(mia)["default_workspace_id"] == personal["id"]

    assert api.client.post(f"{WS}/{org['id']}/leave", headers=admin).status_code == 204
    assert api.client.post(f"{WS}/{personal['id']}/leave", headers=mia).status_code == 409


def test_viewer_and_member_cannot_manage_members(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    member = api.add_member(org["id"], ada, "mia@example.com", "member")
    api.add_member(org["id"], ada, "vic@example.com", "viewer")
    vic_id = api.member_id(org["id"], ada, "vic@example.com")
    assert (
        api.client.patch(
            f"{WS}/{org['id']}/members/{vic_id}", json={"role": "member"}, headers=member
        ).status_code
        == 403
    )
    assert (
        api.client.delete(f"{WS}/{org['id']}/members/{vic_id}", headers=member).status_code == 403
    )


# --- audit ---


def test_audit_log_records_membership_events(api: Api) -> None:
    ada = api.signup("ada@example.com")
    org = api.create_org(ada)
    api.add_member(org["id"], ada, "mia@example.com", "member")
    mia_id = api.member_id(org["id"], ada, "mia@example.com")
    api.client.patch(f"{WS}/{org['id']}/members/{mia_id}", json={"role": "viewer"}, headers=ada)

    entries = api.client.get(f"{WS}/{org['id']}/audit-logs", headers=ada).json()
    actions = [e["action"] for e in entries]
    assert actions == [
        "member.role_changed",
        "invitation.accepted",
        "invitation.created",
        "organization.created",
    ]
    assert entries[0]["details"] == {
        "user_id": entries[0]["details"]["user_id"],
        "from": "member",
        "to": "viewer",
    }

    page = api.client.get(f"{WS}/{org['id']}/audit-logs", params={"limit": 2}, headers=ada).json()
    older = api.client.get(
        f"{WS}/{org['id']}/audit-logs",
        params={"limit": 2, "before_id": page[-1]["id"]},
        headers=ada,
    ).json()
    assert [e["action"] for e in page + older] == actions
