import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmProvider, ToastProvider } from '../components/ui'
import { AuthProvider } from '../features/auth/AuthProvider'
import { routes } from './routes'

vi.mock('../features/system-status/api', () => ({ fetchHealth: vi.fn(() => new Promise(() => {})) }))
// No refresh cookie in tests: the visitor is anonymous.
vi.mock('../lib/api/session', async (original) => {
  const actual = await original<typeof import('../lib/api/session')>()
  return { ...actual, refreshSession: vi.fn(() => Promise.reject(new Error('no session'))) }
})

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <ToastProvider>
          <ConfirmProvider>
            <RouterProvider router={router} />
          </ConfirmProvider>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return router
}

describe('routes', () => {
  it('renders the landing page at the root', async () => {
    renderAt('/')
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('Ask your documents anything.')
  })

  it('renders the status page', async () => {
    renderAt('/status')
    expect(await screen.findByRole('heading', { level: 1, name: 'System status' })).toBeInTheDocument()
  })

  it('sends anonymous visitors from the app to sign-in, remembering where they were going', async () => {
    const router = renderAt('/w/abc/documents?q=x')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/w/abc/documents?q=x')}`)
  })

  it.each([
    ['/register', 'Create your account'],
    ['/forgot-password', 'Reset your password'],
    ['/reset-password', 'Invalid link'],
  ])('%s renders "%s"', async (path, heading) => {
    renderAt(path)
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
  })

  it('shows a 404 page for unknown paths', async () => {
    renderAt('/nope')
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })
})
