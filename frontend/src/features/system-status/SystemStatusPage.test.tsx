import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../lib/api/errors'
import { renderWithQuery } from '../../test/render'
import { fetchHealth } from './api'
import { SystemStatusPage } from './SystemStatusPage'
import type { HealthResponse } from './types'

vi.mock('./api', () => ({ fetchHealth: vi.fn() }))
const fetchHealthMock = vi.mocked(fetchHealth)

function report(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    status: 'ok',
    timestamp: '2026-11-23T00:00:00Z',
    app: { name: 'DocuMind AI', version: '0.1.0', environment: 'development' },
    components: {
      database: { status: 'ok', latency_ms: 2.5, details: { pgvector_version: '0.8.7' }, error: null },
      redis: { status: 'ok', latency_ms: 1, details: {}, error: null },
    },
    ...overrides,
  }
}

describe('SystemStatusPage', () => {
  beforeEach(() => {
    fetchHealthMock.mockReset()
  })

  it('shows a loading state, then healthy components', async () => {
    fetchHealthMock.mockResolvedValue(report())
    renderWithQuery(<SystemStatusPage />)

    expect(screen.getByLabelText('Loading status')).toBeInTheDocument()
    expect(await screen.findByText('All systems operational')).toBeInTheDocument()
    expect(screen.getByText('PostgreSQL + pgvector')).toBeInTheDocument()
    expect(screen.getByText('0.8.7')).toBeInTheDocument()
    expect(screen.getByText(/DocuMind AI v0\.1\.0/)).toBeInTheDocument()
  })

  it('shows degraded state and the failing component error', async () => {
    fetchHealthMock.mockResolvedValue(
      report({
        status: 'degraded',
        components: {
          object_storage: { status: 'error', latency_ms: 3000, details: {}, error: 'timed out after 3s' },
        },
      }),
    )
    renderWithQuery(<SystemStatusPage />)

    expect(await screen.findByText('Degraded')).toBeInTheDocument()
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(screen.getByText('timed out after 3s')).toBeInTheDocument()
  })

  it('shows an error when the API is unreachable', async () => {
    fetchHealthMock.mockRejectedValue(new ApiError('Unable to reach the server.', null, null))
    renderWithQuery(<SystemStatusPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the API.')
  })

  it('refreshes on demand', async () => {
    fetchHealthMock.mockResolvedValue(report())
    renderWithQuery(<SystemStatusPage />)
    await screen.findByText('All systems operational')

    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(fetchHealthMock).toHaveBeenCalledTimes(2)
  })
})
