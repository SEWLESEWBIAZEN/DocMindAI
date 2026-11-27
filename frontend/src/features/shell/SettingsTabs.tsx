import { NavLink, useLocation } from 'react-router'

import { useWorkspace } from '../../lib/workspace/WorkspaceContext'
import type { Permission } from '../../lib/workspace/permissions'

interface Tab {
  path: string
  label: string
  permission?: Permission
  organizationOnly?: boolean
}

/** Same pages and permissions as the sidebar's workspace section. */
const TABS: Tab[] = [
  { path: 'settings', label: 'General' },
  { path: 'members', label: 'Members', permission: 'members.read', organizationOnly: true },
  { path: 'billing', label: 'Billing', permission: 'billing.manage' },
  { path: 'usage', label: 'Usage', permission: 'usage.read' },
  { path: 'answer-quality', label: 'Answer quality', permission: 'usage.read' },
  { path: 'ai-providers', label: 'AI providers', permission: 'ai_providers.manage' },
  { path: 'integrations', label: 'Integrations', permission: 'conversations.use' },
  { path: 'connectors', label: 'Connectors', permission: 'workspace.update' },
  { path: 'infrastructure', label: 'Infrastructure', permission: 'infrastructure.manage', organizationOnly: true },
]

/**
 * Workspace settings as one place with tabs (design board, screen 6). Shown on each
 * settings page; only the tabs this person can open are listed.
 */
export function SettingsTabs() {
  const { base, can, workspace } = useWorkspace()
  const { pathname } = useLocation()
  const section = pathname.slice(base.length + 1).split('/')[0]
  if (!TABS.some((t) => t.path === section)) return null
  const tabs = TABS.filter(
    (t) => (!t.permission || can(t.permission)) && (!t.organizationOnly || workspace.kind === 'organization'),
  )
  if (tabs.length < 2) return null
  return (
    <nav className="settings-tabs" aria-label={workspace.kind === 'organization' ? 'Organization settings' : 'Workspace settings'}>
      <span className="settings-tabs__title">{workspace.kind === 'organization' ? 'Organization settings' : 'Workspace settings'}</span>
      <div className="tabs tabs--page">
        {tabs.map((t) => (
          <NavLink key={t.path} to={`${base}/${t.path}`} className="tabs__tab">
            {t.label}
          </NavLink>
        ))}
      </div>
    </nav>
  )
}
