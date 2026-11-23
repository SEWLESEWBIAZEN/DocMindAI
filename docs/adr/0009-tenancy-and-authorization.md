# ADR 0009: Tenancy model and authorization policy

- Status: Accepted
- Date: 2026-11-23

## Context

Users need personal and organization workspaces, membership in several
workspaces, roles (Owner, Admin, Member, Viewer) and explicit permission
policies. Every data access must enforce tenant isolation and prevent IDOR.

## Decision

**The workspace is the tenant boundary.** Each user owns exactly one *personal*
workspace, created at registration. Each *organization* owns exactly one
workspace. Database CHECK and UNIQUE constraints enforce both rules. All future
tenant data (documents, conversations, usage) will carry a `workspace_id`.
`workspace_members(workspace_id, user_id, role_id)` is the only source of access.

**Permissions, not role names.**

- `domain/identity/permissions.py` defines every permission and which roles
  grant it. Migrations seed the `roles`, `permissions` and `role_permissions`
  tables with frozen literal data, and a test asserts the database matches the
  code.
- At runtime a member's permissions are loaded from the database into a
  `WorkspaceContext`. Services call `ctx.require(Permission.X)`; nothing
  compares role names.
- Role *rank* (Owner 40 > Admin 30 > Member 20 > Viewer 10) is used only for
  the hierarchy rules below.

| | Owner | Admin | Member | Viewer |
|---|:-:|:-:|:-:|:-:|
| workspace.read, members.read, documents.read, conversations.use | ✓ | ✓ | ✓ | ✓ |
| documents.write, documents.delete | ✓ | ✓ | ✓ | |
| workspace.update, members.invite, members.manage, audit.read, usage.read | ✓ | ✓ | | |
| billing.manage | ✓ | | | |

**Hierarchy and ownership rules** (pure functions in `domain/identity/policies.py`):

- Owners can assign any role. Everyone else can only assign roles below their own.
- Changing or removing a member requires outranking them; owners are the
  exception. Nobody manages their own membership; to step away you *leave*.
- An organization always keeps at least one owner. The owner rows are locked
  with `SELECT … FOR UPDATE` so two concurrent demotions can't both succeed.
- Personal workspaces can't be shared, left or have their membership changed.

**Isolation and IDOR.** Every workspace route has the form
`/workspaces/{workspace_id}/…`.

- A dependency resolves the caller's membership first. Non-members get **404**,
  never 403, so a workspace's existence isn't revealed.
- Repository methods take `workspace_id` as a required argument and filter in
  SQL. A member or invitation ID from another workspace is simply not found.
- **Workspace switching** stores `users.default_workspace_id`, after a
  membership check.

**Invitations.** Invitations go to an email address with a role, and the
inviter may only grant roles they're allowed to assign. Tokens are hashed,
single-use and valid for 7 days. Re-inviting replaces the pending invitation.
Acceptance requires the signed-in user's *verified* email to equal the invited
address, which defeats forwarded or leaked links.

**Audit.** An append-only `audit_logs` row is written in the same transaction
as each security-relevant change: registration, verification, logins, logouts,
refresh-token reuse, password events, organization creation, renames,
invitations, role changes, removals and departures. Workspace and actor IDs
have no foreign keys, so the trail outlives deletions.

## Alternatives

- *PostgreSQL row-level security:* a strong second layer, but it requires a
  per-request session variable and connection discipline. It's worth adding
  once document data exists (Phase 3/9) as defence in depth. The application
  filter remains the primary control.
- *Organization as a separate tenant from its workspace:* adds a second
  boundary with no current use case. Organization 1:1 workspace keeps queries
  simple, and multiple workspaces per organization can be added later without
  changing the workspace-scoped data model.
- *Hard-coded role checks:* scatter policy across code. The catalog makes it
  reviewable in one file.

## Consequences

- Organization deletion and ownership-transfer UX are not implemented yet.
  Transfer works by promoting another owner and then leaving.
- Member emails are visible to all members of a workspace (normal for team tools).
