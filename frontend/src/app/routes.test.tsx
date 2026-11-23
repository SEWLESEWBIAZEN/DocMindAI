import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { resolvePage } from './routes'

vi.mock('../features/system-status/api', () => ({ fetchHealth: vi.fn(() => new Promise(() => {})) }))

function renderPath(path: string) {
  const client = new QueryClient()
  return render(<QueryClientProvider client={client}>{resolvePage(path)}</QueryClientProvider>)
}

describe('resolvePage', () => {
  it('renders the landing page at the root and for unknown paths', () => {
    renderPath('/')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Ask your documents anything.')
  })

  it.each([
    ['/status', 'System status'],
    ['/status/', 'System status'],
    ['/login', 'Sign in is coming soon'],
    ['/register', 'Create your workspace is coming soon'],
  ])('%s renders %s', (path, heading) => {
    renderPath(path)
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
  })
})
