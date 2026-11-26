import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, NavLink, Navigate, Outlet, useLocation, useNavigate, useParams } from 'react-router'

import { ErrorBoundary } from '../../app/ErrorBoundary'
import { FullPageLoading } from '../../app/FullPageLoading'
import { NotFoundPage } from '../../app/NotFoundPage'
import { Button, ErrorState, Icon, Menu, type IconName } from '../../components/ui'
import { apiClient } from '../../lib/api/client'
import { useWorkspace, useWorkspaces, WorkspaceProvider } from '../../lib/workspace/WorkspaceContext'
import { setTheme, useTheme } from '../../lib/theme'
import type { Permission } from '../../lib/workspace/permissions'
import { ROLE_LABELS } from '../../lib/workspace/permissions'
import { useAuth, useUser } from '../auth/AuthProvider'
import { conversationKeys, listConversations } from '../chat/api'
import { ACCEPT } from '../documents/api'
import { UploadProvider, useUploads } from '../documents/UploadManager'
import { UploadTray } from '../documents/UploadTray'
import { Logo } from '../landing/components/Logo'
import { VoiceProvider } from '../voice/VoiceContext'
import { MfaGate } from './MfaGate'

/** `/app`: send the user to their default workspace. */
export function AppHome() {
  const { data, error, refetch } = useWorkspaces()
  const user = useUser()
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!data) return <FullPageLoading />
  const target = data.find((w) => w.id === user.default_workspace_id) ?? data[0]
  if (!target) return <NotFoundPage />
  return <Navigate to={`/w/${target.id}/dashboard`} replace />
}

/** `/app/billing?plan=…`: the default workspace's billing page (links from /pricing). */
export function AppBilling() {
  const { data, error, refetch } = useWorkspaces()
  const user = useUser()
  const location = useLocation()
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!data) return <FullPageLoading />
  const target = data.find((w) => w.id === user.default_workspace_id) ?? data[0]
  if (!target) return <NotFoundPage />
  return <Navigate to={`/w/${target.id}/billing${location.search}#plans`} replace />
}

/** Loads the workspace in the URL; unknown or inaccessible ids get a 404 page. */
export function WorkspaceLayout() {
  const { workspaceId } = useParams()
  const { data, error, refetch } = useWorkspaces()
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!data) return <FullPageLoading />
  const workspace = data.find((w) => w.id === workspaceId)
  if (!workspace) return <NotFoundPage />
  return (
    <WorkspaceProvider workspace={workspace} workspaces={data}>
      <UploadProvider key={workspace.id} workspaceId={workspace.id}>
        <VoiceProvider workspaceId={workspace.id}>
          <Shell />
        </VoiceProvider>
      </UploadProvider>
    </WorkspaceProvider>
  )
}

interface NavItem {
  to: string
  label: string
  icon: IconName
  permission?: Permission
  end?: boolean
}

const COLLAPSED_KEY = 'documind.sidebar.collapsed'

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

function Shell() {
  const [navOpen, setNavOpen] = useState(false)
  // Desktop only: the sidebar shrinks to an icon rail. The mobile drawer always shows labels.
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const toggleCollapsed = () => {
    setCollapsed((was) => {
      try {
        window.localStorage.setItem(COLLAPSED_KEY, was ? '0' : '1')
      } catch {
        // Private mode: the choice lasts until reload.
      }
      return !was
    })
  }
  const location = useLocation()
  const main = useRef<HTMLElement>(null)
  const [path, setPath] = useState(location.pathname)
  if (path !== location.pathname) {
    // Navigating closes the mobile drawer.
    setPath(location.pathname)
    setNavOpen(false)
  }

  // Move focus to the page on navigation so screen readers announce the new view.
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    const heading = main.current?.querySelector('h1')
    if (heading) {
      heading.setAttribute('tabindex', '-1')
      heading.focus({ preventScroll: false })
    }
  }, [location.pathname])

  return (
    <div className={`shell${navOpen ? ' shell--nav-open' : ''}${collapsed ? ' shell--collapsed' : ''}`}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <Sidebar onClose={() => setNavOpen(false)} collapsed={collapsed} onToggleCollapsed={toggleCollapsed} />
      {navOpen && <div className="shell__scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <div className="shell__body">
        <Topbar onOpenNav={() => setNavOpen(true)} navOpen={navOpen} />
        <main id="main" className="shell__main" ref={main}>
          <ErrorBoundary resetKey={location.pathname}>
            <MfaGate>
              <Outlet />
            </MfaGate>
          </ErrorBoundary>
        </main>
      </div>
      <UploadTray />
    </div>
  )
}

function Sidebar({
  onClose,
  collapsed,
  onToggleCollapsed,
}: {
  onClose: () => void
  collapsed: boolean
  onToggleCollapsed: () => void
}) {
  const { base, can, workspace } = useWorkspace()
  const navigate = useNavigate()
  const primary: NavItem[] = [
    { to: `${base}/dashboard`, label: 'Dashboard', icon: 'home' },
    { to: `${base}/chat`, label: 'Ask', icon: 'message', permission: 'conversations.use', end: true },
    { to: `${base}/conversations`, label: 'Conversations', icon: 'history', permission: 'conversations.use' },
    { to: `${base}/documents`, label: 'Documents', icon: 'file', permission: 'documents.read' },
    { to: `${base}/collections`, label: 'Collections', icon: 'layers', permission: 'documents.read' },
    { to: `${base}/upload`, label: 'Upload', icon: 'upload', permission: 'documents.write' },
    { to: `${base}/extract`, label: 'Extract', icon: 'flask', permission: 'documents.read' },
    { to: `${base}/deadlines`, label: 'Deadlines', icon: 'clock', permission: 'documents.read' },
  ]
  const admin: NavItem[] = [
    { to: `${base}/members`, label: 'Members', icon: 'users', permission: 'members.read' },
    { to: `${base}/organization`, label: 'Organizations', icon: 'building' },
    { to: `${base}/usage`, label: 'Usage', icon: 'chart', permission: 'usage.read' },
    { to: `${base}/billing`, label: 'Billing', icon: 'scale', permission: 'billing.manage' },
    { to: `${base}/ai-providers`, label: 'AI providers', icon: 'key', permission: 'ai_providers.manage' },
    { to: `${base}/connectors`, label: 'Connectors', icon: 'refresh', permission: 'workspace.update' },
    { to: `${base}/integrations`, label: 'Integrations', icon: 'sparkles', permission: 'conversations.use' },
    ...(workspace.kind === 'organization'
      ? [{ to: `${base}/infrastructure`, label: 'Infrastructure', icon: 'cpu' as const, permission: 'infrastructure.manage' as const }]
      : []),
    { to: `${base}/settings`, label: 'Settings', icon: 'settings' },
  ]
  const visible = (items: NavItem[]) => items.filter((item) => !item.permission || can(item.permission))
  const showMembers = workspace.kind === 'organization'
  // With labels hidden, a hover tooltip names each icon.
  const tip = (label: string) => (collapsed ? label : undefined)
  const link = (item: NavItem) => (
    <li key={item.to}>
      <NavLink to={item.to} end={item.end} className="nav-link" title={tip(item.label)}>
        <Icon name={item.icon} size={18} />
        <span className="sidebar__label">{item.label}</span>
      </NavLink>
    </li>
  )

  return (
    <aside className="sidebar" id="sidebar" aria-label="Sidebar">
      <div className="sidebar__head">
        <div className="sidebar__top">
          <Link to={`${base}/dashboard`} className="sidebar__brand" aria-label="DocuMind dashboard" title={tip('DocuMind')}>
            <Logo size={26} />
            <span className="sidebar__label">DocuMind</span>
          </Link>
          <Button className="sidebar__close" variant="ghost" size="sm" icon="x" label="Close navigation" onClick={onClose} />
        </div>
        <WorkspaceSwitcher />
        {can('conversations.use') && (
          <Button
            variant="primary"
            icon="plus"
            className="sidebar__new"
            title={tip('New conversation')}
            onClick={() => navigate(`${base}/chat`, { state: { fresh: Date.now() } })}
          >
            <span className="sidebar__label">New conversation</span>
          </Button>
        )}
      </div>
      <div className="sidebar__scroll">
        <nav aria-label="Main" className="sidebar__nav">
          <ul>{visible(primary).map(link)}</ul>
          <p className="sidebar__section">
            <span className="sidebar__label">Workspace</span>
          </p>
          <ul>{visible(admin).filter((item) => showMembers || !item.to.endsWith('/members')).map(link)}</ul>
        </nav>
        {can('conversations.use') && <RecentConversations />}
      </div>
      <div className="sidebar__foot">
        <button
          type="button"
          className="nav-link sidebar__collapse"
          aria-controls="sidebar"
          aria-expanded={!collapsed}
          title={collapsed ? 'Expand sidebar' : undefined}
          onClick={onToggleCollapsed}
        >
          <Icon name="panelLeft" size={18} />
          <span className="sidebar__label">{collapsed ? 'Expand sidebar' : 'Collapse sidebar'}</span>
        </button>
      </div>
    </aside>
  )
}

function RecentConversations() {
  const { workspace, base } = useWorkspace()
  const { data } = useQuery({
    queryKey: conversationKeys.list(workspace.id, '', 0, 8),
    queryFn: () => listConversations(workspace.id, { limit: 8 }),
  })
  if (!data?.items.length) return null
  return (
    <section className="sidebar__recent" aria-labelledby="recent-heading">
      <p className="sidebar__section" id="recent-heading">
        Recent
      </p>
      <ul>
        {data.items.map((c) => (
          <li key={c.id}>
            <NavLink to={`${base}/chat/${c.id}`} className="nav-link nav-link--sub" title={c.title}>
              <span className="truncate">{c.title}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </section>
  )
}

function WorkspaceSwitcher() {
  const { workspace, workspaces } = useWorkspace()
  const navigate = useNavigate()
  const switchTo = (id: string) => {
    void apiClient.put('/me/active-workspace', { workspace_id: id }).catch(() => undefined)
    navigate(`/w/${id}/dashboard`)
  }
  return (
    <Menu
      label={`Workspace: ${workspace.name}. Switch workspace`}
      className="ws-switcher"
      triggerClassName="ws-switcher__trigger"
      align="start"
      trigger={
        <>
          <span className="ws-switcher__avatar" aria-hidden="true">
            {workspace.name.slice(0, 1).toUpperCase()}
          </span>
          <span className="ws-switcher__text">
            <span className="ws-switcher__name truncate">{workspace.name}</span>
            <span className="ws-switcher__meta">
              {workspace.kind === 'personal' ? 'Personal' : 'Organization'} · {ROLE_LABELS[workspace.role]}
            </span>
          </span>
          <Icon name="chevronDown" size={16} />
        </>
      }
      items={[
        ...workspaces.map((w) => ({
          label: `${w.id === workspace.id ? '✓ ' : ''}${w.name}${w.kind === 'personal' ? ' (personal)' : ''}`,
          icon: (w.kind === 'personal' ? 'user' : 'building') as IconName,
          onSelect: () => switchTo(w.id),
        })),
        'separator' as const,
        {
          label: 'Create organization',
          icon: 'plus' as IconName,
          onSelect: () => navigate(`/w/${workspace.id}/organization?create=1`),
        },
      ]}
    />
  )
}

function Topbar({ onOpenNav, navOpen }: { onOpenNav: () => void; navOpen: boolean }) {
  const { base, can } = useWorkspace()
  const navigate = useNavigate()
  const location = useLocation()
  const [query, setQuery] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const { enqueue } = useUploads()
  const urlQuery = location.pathname.endsWith('/documents') ? (new URLSearchParams(location.search).get('q') ?? '') : null
  const [syncedQuery, setSyncedQuery] = useState(urlQuery)
  if (urlQuery !== syncedQuery) {
    // Keep the box in sync with the library's ?q= (e.g. back/forward).
    setSyncedQuery(urlQuery)
    if (urlQuery !== null) setQuery(urlQuery)
  }

  const onSearch = (event: FormEvent) => {
    event.preventDefault()
    const q = query.trim()
    navigate(`${base}/documents${q ? `?q=${encodeURIComponent(q)}` : ''}`)
  }

  return (
    <header className="topbar">
      <Button
        className="topbar__menu"
        variant="ghost"
        icon="menu"
        label="Open navigation"
        aria-expanded={navOpen}
        onClick={onOpenNav}
      />
      {can('documents.read') && (
        <form role="search" className="topbar__search" onSubmit={onSearch}>
          <Icon name="search" size={16} />
          <label htmlFor="global-search" className="sr-only">
            Search documents
          </label>
          <input
            id="global-search"
            type="search"
            placeholder="Search documents…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            autoComplete="off"
          />
        </form>
      )}
      <div className="topbar__actions">
        {can('documents.write') && (
          <>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPT}
              hidden
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                if (files.length) enqueue(files)
                event.target.value = ''
              }}
            />
            <Button icon="upload" onClick={() => fileInput.current?.click()}>
              <span className="hide-sm">Upload</span>
            </Button>
          </>
        )}
        <UserMenu />
      </div>
    </header>
  )
}

function UserMenu() {
  const user = useUser()
  const { signOut } = useAuth()
  const { base } = useWorkspace()
  const navigate = useNavigate()
  const theme = useTheme()
  const initials = user.display_name
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
  return (
    <Menu
      label={`Account menu for ${user.display_name}`}
      triggerClassName="avatar-button"
      trigger={<span className="avatar" aria-hidden="true">{initials || '?'}</span>}
      header={
        <>
          <strong className="truncate">{user.display_name}</strong>
          <span className="muted truncate">{user.email}</span>
        </>
      }
      items={[
        { label: 'Profile', icon: 'user', onSelect: () => navigate(`${base}/profile`) },
        { label: 'Security & 2FA', icon: 'shield', onSelect: () => navigate(`${base}/profile#security`) },
        { label: 'Workspace settings', icon: 'settings', onSelect: () => navigate(`${base}/settings`) },
        'separator',
        { group: 'Theme' },
        { label: 'Light theme', icon: 'sun', checked: theme === 'light', onSelect: () => setTheme('light') },
        { label: 'Dark theme', icon: 'moon', checked: theme === 'dark', onSelect: () => setTheme('dark') },
        { label: 'System theme', icon: 'monitor', checked: theme === 'system', onSelect: () => setTheme('system') },
        'separator',
        {
          label: 'Sign out',
          icon: 'logout',
          onSelect: () => {
            void signOut().then(() => navigate('/login', { replace: true }))
          },
        },
      ]}
    />
  )
}
