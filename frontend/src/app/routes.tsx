import { lazy, Suspense, type ComponentType, type ReactNode } from 'react'
import { createBrowserRouter, Outlet, type RouteObject } from 'react-router'

import { RedirectIfAuthenticated, RequireAuth } from '../features/auth/routeGuards'
import { LandingPage } from '../features/landing/LandingPage'
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
      { path: '/login', element: <Public>{page(auth, 'LoginPage')}</Public> },
      { path: '/register', element: <Public>{page(auth, 'RegisterPage')}</Public> },
      { path: '/forgot-password', element: <Public>{page(auth, 'ForgotPasswordPage')}</Public> },
      { path: '/reset-password', element: page(auth, 'ResetPasswordPage') },
      { path: '/verify-email', element: page(auth, 'VerifyEmailPage') },
      {
        path: '/invitations/accept',
        element: <RequireAuth>{page(auth, 'AcceptInvitationPage')}</RequireAuth>,
      },
      { path: '/app', element: <RequireAuth>{page(shell, 'AppHome')}</RequireAuth> },
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
