import { lazy, Suspense, type ComponentType, type ReactNode } from 'react'
import { createBrowserRouter, Outlet, type RouteObject } from 'react-router'

import { RedirectIfAuthenticated, RequireAuth } from '../features/auth/routeGuards'
import { LandingPage } from '../features/landing/LandingPage'
import { PricingPage } from '../features/landing/PricingPage'
import { SystemStatusPage } from '../features/system-status/SystemStatusPage'
import { ErrorBoundary } from './ErrorBoundary'
import { FullPageLoading } from './FullPageLoading'
import { NotFoundPage } from './NotFoundPage'

/** Code-split a named export: the landing page never loads the app. */
function page(load: () => Promise<Record<string, unknown>>, name: string) {
  const Component = lazy(async () => ({ default: (await load())[name] as ComponentType }))
  return (
    <Suspense fallback={<FullPageLoading label="Loading page" />}>
      <Component />
    </Suspense>
  )
}

const auth = () => import('../features/auth/pages')
const shell = () => import('../features/shell/AppShell')
const documents = () => import('../features/documents/pages')
const chat = () => import('../features/chat/pages')
const admin = () => import('../features/admin/pages')
const dashboard = () => import('../features/dashboard/DashboardPage')
const aiProviders = () => import('../features/saas/AIProvidersPage')
const infrastructure = () => import('../features/saas/InfrastructurePage')
const billing = () => import('../features/saas/BillingPage')
const extraction = () => import('../features/extraction/pages')
const deadlines = () => import('../features/extraction/DeadlinesPage')
const connectors = () => import('../features/connectors/ConnectorsPage')
const integrations = () => import('../features/integrations/IntegrationsPage')

function Public({ children }: { children: ReactNode }) {
  return <RedirectIfAuthenticated>{children}</RedirectIfAuthenticated>
}

export const routes: RouteObject[] = [
  {
    element: (
      <ErrorBoundary>
        <Outlet />
      </ErrorBoundary>
    ),
    children: [
      { path: '/', element: <LandingPage /> },
      { path: '/status', element: <SystemStatusPage /> },
      { path: '/pricing', element: <PricingPage /> },
      { path: '/login', element: <Public>{page(auth, 'LoginPage')}</Public> },
      { path: '/register', element: <Public>{page(auth, 'RegisterPage')}</Public> },
      { path: '/forgot-password', element: <Public>{page(auth, 'ForgotPasswordPage')}</Public> },
      { path: '/reset-password', element: page(auth, 'ResetPasswordPage') },
      { path: '/verify-email', element: page(auth, 'VerifyEmailPage') },
      { path: '/sso/complete', element: <RequireAuth>{page(auth, 'SsoCompletePage')}</RequireAuth> },
      {
        path: '/invitations/accept',
        element: <RequireAuth>{page(auth, 'AcceptInvitationPage')}</RequireAuth>,
      },
      { path: '/app', element: <RequireAuth>{page(shell, 'AppHome')}</RequireAuth> },
      // From the pricing page: the current workspace's billing, keeping ?plan=&interval=.
      { path: '/app/billing', element: <RequireAuth>{page(shell, 'AppBilling')}</RequireAuth> },
      {
        path: '/w/:workspaceId',
        element: <RequireAuth>{page(shell, 'WorkspaceLayout')}</RequireAuth>,
        children: [
          { index: true, element: page(dashboard, 'DashboardPage') },
          { path: 'dashboard', element: page(dashboard, 'DashboardPage') },
          { path: 'documents', element: page(documents, 'DocumentLibraryPage') },
          { path: 'documents/:documentId', element: page(documents, 'DocumentViewerPage') },
          { path: 'upload', element: page(documents, 'UploadPage') },
          { path: 'collections', element: page(documents, 'CollectionsPage') },
          { path: 'collections/:collectionId', element: page(documents, 'CollectionDetailPage') },
          // One optional-segment route: creating a conversation mid-stream must not remount the page.
          { path: 'chat/:conversationId?', element: page(chat, 'ChatPage') },
          { path: 'conversations', element: page(chat, 'ConversationsPage') },
          { path: 'members', element: page(admin, 'MembersPage') },
          { path: 'organization', element: page(admin, 'OrganizationPage') },
          { path: 'settings', element: page(admin, 'SettingsPage') },
          { path: 'profile', element: page(admin, 'ProfilePage') },
          { path: 'usage', element: page(admin, 'UsagePage') },
          { path: 'ai-providers', element: page(aiProviders, 'AIProvidersPage') },
          { path: 'infrastructure', element: page(infrastructure, 'InfrastructurePage') },
          { path: 'billing', element: page(billing, 'BillingPage') },
          { path: 'extract', element: page(extraction, 'ExtractionsPage') },
          { path: 'extract/:runId', element: page(extraction, 'ExtractionRunPage') },
          { path: 'deadlines', element: page(deadlines, 'DeadlinesPage') },
          { path: 'connectors', element: page(connectors, 'ConnectorsPage') },
          { path: 'integrations', element: page(integrations, 'IntegrationsPage') },
          { path: '*', element: <NotFoundPage inApp /> },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]

export function createRouter() {
  return createBrowserRouter(routes)
}
