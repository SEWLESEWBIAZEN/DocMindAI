import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from documind.domain.errors import ConflictError, PermissionDeniedError, ValidationFailedError
from documind.domain.identity.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    Permission,
    RoleKey,
)
from documind.domain.identity.policies import (
    WorkspaceContext,
    WorkspaceKind,
    ensure_can_archive,
    ensure_can_assign_role,
    ensure_can_manage_member,
    ensure_owner_remains,
    ensure_recent_mfa,
    ensure_shared_workspace,
)
from documind.domain.identity.values import (
    clean_display_name,
    normalize_email,
    validate_password,
)

OWN, ADM, MEM, VIE = RoleKey.OWNER, RoleKey.ADMIN, RoleKey.MEMBER, RoleKey.VIEWER


def ctx(role: RoleKey, kind: WorkspaceKind = WorkspaceKind.ORGANIZATION) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=uuid.uuid4(),
        workspace_kind=kind,
        user_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role=role,
        permissions=ROLE_PERMISSIONS[role],
    )


class TestCatalog:
    def test_every_permission_is_described(self) -> None:
        assert set(PERMISSION_DESCRIPTIONS) == set(Permission)

    def test_roles_are_strictly_nested(self) -> None:
        assert (
            ROLE_PERMISSIONS[VIE]
            < ROLE_PERMISSIONS[MEM]
            < ROLE_PERMISSIONS[ADM]
            < ROLE_PERMISSIONS[OWN]
        )
        assert ROLE_PERMISSIONS[OWN] == frozenset(Permission)

    def test_ranks_follow_permissions(self) -> None:
        assert ROLE_RANK[OWN] > ROLE_RANK[ADM] > ROLE_RANK[MEM] > ROLE_RANK[VIE]

    @pytest.mark.parametrize(
        ("role", "permission", "allowed"),
        [
            (VIE, Permission.DOCUMENTS_READ, True),
            (VIE, Permission.DOCUMENTS_WRITE, False),
            (VIE, Permission.MEMBERS_INVITE, False),
            (MEM, Permission.DOCUMENTS_WRITE, True),
            (MEM, Permission.MEMBERS_INVITE, False),
            (MEM, Permission.DOCUMENTS_DELETE, False),
            (MEM, Permission.DOCUMENTS_ARCHIVE_OWN, True),
            (MEM, Permission.DOCUMENTS_ARCHIVE_ANY, False),
            (VIE, Permission.DOCUMENTS_ARCHIVE_OWN, False),
            (ADM, Permission.DOCUMENTS_DELETE, True),
            (ADM, Permission.DOCUMENTS_ARCHIVE_ANY, True),
            (ADM, Permission.MEMBERS_MANAGE, True),
            (ADM, Permission.AUDIT_READ, True),
            (ADM, Permission.BILLING_MANAGE, False),
            (OWN, Permission.BILLING_MANAGE, True),
        ],
    )
    def test_role_matrix(self, role: RoleKey, permission: Permission, allowed: bool) -> None:
        assert ctx(role).can(permission) is allowed


class TestRoleAssignment:
    @pytest.mark.parametrize("role", list(RoleKey))
    def test_owner_can_assign_any_role(self, role: RoleKey) -> None:
        ensure_can_assign_role(OWN, role)

    @pytest.mark.parametrize(("actor", "role"), [(ADM, MEM), (ADM, VIE), (MEM, VIE)])
    def test_lower_roles_allowed(self, actor: RoleKey, role: RoleKey) -> None:
        ensure_can_assign_role(actor, role)

    @pytest.mark.parametrize(("actor", "role"), [(ADM, ADM), (ADM, OWN), (MEM, MEM), (VIE, VIE)])
    def test_equal_or_higher_roles_denied(self, actor: RoleKey, role: RoleKey) -> None:
        with pytest.raises(PermissionDeniedError):
            ensure_can_assign_role(actor, role)


class TestManageMember:
    def test_admin_can_manage_member(self) -> None:
        ensure_can_manage_member(ctx(ADM), MEM, uuid.uuid4())

    @pytest.mark.parametrize("target", [ADM, OWN])
    def test_admin_cannot_manage_peers_or_owners(self, target: RoleKey) -> None:
        with pytest.raises(PermissionDeniedError):
            ensure_can_manage_member(ctx(ADM), target, uuid.uuid4())

    def test_owner_can_manage_other_owner(self) -> None:
        ensure_can_manage_member(ctx(OWN), OWN, uuid.uuid4())

    def test_member_lacks_permission(self) -> None:
        with pytest.raises(PermissionDeniedError):
            ensure_can_manage_member(ctx(MEM), VIE, uuid.uuid4())

    def test_cannot_manage_self(self) -> None:
        c = ctx(OWN)
        with pytest.raises(PermissionDeniedError):
            ensure_can_manage_member(c, OWN, c.user_id)


class TestOwnership:
    def test_last_owner_cannot_leave_or_be_demoted(self) -> None:
        with pytest.raises(ConflictError, match="at least one owner"):
            ensure_owner_remains(OWN, None, owner_count=1)
        with pytest.raises(ConflictError):
            ensure_owner_remains(OWN, ADM, owner_count=1)

    def test_owner_can_leave_when_another_owner_exists(self) -> None:
        ensure_owner_remains(OWN, None, owner_count=2)

    def test_non_owner_changes_unaffected(self) -> None:
        ensure_owner_remains(MEM, None, owner_count=1)
        ensure_owner_remains(OWN, OWN, owner_count=1)

    def test_personal_workspace_cannot_be_shared(self) -> None:
        with pytest.raises(ConflictError) as exc:
            ensure_shared_workspace(WorkspaceKind.PERSONAL)
        assert exc.value.code == "personal_workspace"
        ensure_shared_workspace(WorkspaceKind.ORGANIZATION)


class TestValues:
    def test_normalize_email(self) -> None:
        assert normalize_email("  Ada@Example.COM ") == "ada@example.com"

    @pytest.mark.parametrize("password", ["short", " " * 12, "x" * 129, "ada@example.com"])
    def test_rejected_passwords(self, password: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_password(password, email="Ada@example.com")

    def test_accepted_password(self) -> None:
        validate_password("correct horse battery staple", email="ada@example.com")

    def test_display_name(self) -> None:
        assert clean_display_name("  Ada   Lovelace ") == "Ada Lovelace"
        with pytest.raises(ValidationFailedError):
            clean_display_name("   ")
        with pytest.raises(ValidationFailedError):
            clean_display_name("x" * 101)


class TestArchivePolicy:
    def test_members_archive_only_their_own_uploads(self) -> None:
        c = ctx(MEM)
        ensure_can_archive(c, c.user_id)
        with pytest.raises(PermissionDeniedError, match="you uploaded"):
            ensure_can_archive(c, uuid.uuid4())
        with pytest.raises(PermissionDeniedError):
            ensure_can_archive(c, None)  # uploader unknown (e.g. deleted account)

    @pytest.mark.parametrize("role", [ADM, OWN])
    def test_admins_and_owners_archive_anything(self, role: RoleKey) -> None:
        ensure_can_archive(ctx(role), uuid.uuid4())
        ensure_can_archive(ctx(role), None)

    def test_viewers_cannot_archive(self) -> None:
        c = ctx(VIE)
        with pytest.raises(PermissionDeniedError):
            ensure_can_archive(c, c.user_id)


class TestRecentMfa:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def mfa_ctx(self, *, enabled: bool = True, seconds_ago: float | None = 10) -> WorkspaceContext:
        verified = None if seconds_ago is None else self.now - timedelta(seconds=seconds_ago)
        return dataclasses.replace(ctx(ADM), mfa_enabled=enabled, mfa_verified_at=verified)

    def test_recent_verification_passes(self) -> None:
        ensure_recent_mfa(self.mfa_ctx(seconds_ago=299), self.now, 300)

    def test_requires_enrollment(self) -> None:
        with pytest.raises(PermissionDeniedError) as exc:
            ensure_recent_mfa(self.mfa_ctx(enabled=False), self.now, 300)
        assert exc.value.code == "mfa_enrollment_required"

    @pytest.mark.parametrize("seconds_ago", [None, 301, -120])
    def test_missing_stale_or_future_verification(self, seconds_ago: float | None) -> None:
        with pytest.raises(PermissionDeniedError) as exc:
            ensure_recent_mfa(self.mfa_ctx(seconds_ago=seconds_ago), self.now, 300)
        assert exc.value.code == "mfa_step_up_required"
