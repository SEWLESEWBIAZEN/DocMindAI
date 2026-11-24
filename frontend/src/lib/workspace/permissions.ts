import type { Role } from '../api/types'

/**
 * Mirror of the backend's role → permission map, used only to shape the UI
 * (hide actions a role can't take). The API enforces every permission itself.
 */
export type Permission =
  | 'workspace.read'
  | 'workspace.update'
  | 'members.read'
  | 'members.invite'
  | 'members.manage'
  | 'audit.read'
  | 'usage.read'
  | 'documents.read'
  | 'documents.write'
  | 'documents.archive_own'
  | 'documents.archive_any'
  | 'documents.delete'
  | 'conversations.use'
  | 'billing.manage'

const VIEWER: Permission[] = ['workspace.read', 'members.read', 'documents.read', 'conversations.use']
const MEMBER: Permission[] = [...VIEWER, 'documents.write', 'documents.archive_own']
const ADMIN: Permission[] = [
  ...MEMBER,
  'workspace.update',
  'members.invite',
  'members.manage',
  'audit.read',
  'usage.read',
  'documents.archive_any',
  'documents.delete',
]
const OWNER: Permission[] = [...ADMIN, 'billing.manage']

export const ROLE_PERMISSIONS: Record<Role, ReadonlySet<Permission>> = {
  viewer: new Set(VIEWER),
  member: new Set(MEMBER),
  admin: new Set(ADMIN),
  owner: new Set(OWNER),
}

export const ROLE_LABELS: Record<Role, string> = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
  viewer: 'Viewer',
}

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  owner: 'Full control, including billing and ownership',
  admin: 'Manages members, settings and all content',
  member: 'Works with documents and conversations',
  viewer: 'Reads documents and asks questions',
}

export function can(role: Role, permission: Permission): boolean {
  return ROLE_PERMISSIONS[role].has(permission)
}
