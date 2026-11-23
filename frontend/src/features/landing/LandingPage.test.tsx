import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SCENARIOS } from './content'
import { demoDuration } from './demo/timeline'
import { LandingPage } from './LandingPage'

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: reduce && query.includes('prefers-reduced-motion'),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('LandingPage', () => {
  it('renders the hero and every section', () => {
    mockReducedMotion(true)
    render(<LandingPage />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Ask your documents anything.')
    for (const name of [
      'Less searching. More knowing.',
      'Built for every team that runs on documents',
      'From upload to cited answer',
      'Your documents stay yours',
    ]) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.getAllByRole('link', { name: /get started/i })[0]).toHaveAttribute('href', '/register')
  })

  it('use-case tabs follow the ARIA tabs keyboard pattern', async () => {
    mockReducedMotion(true)
    render(<LandingPage />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(SCENARIOS.length)
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')

    await userEvent.click(tabs[0])
    await userEvent.keyboard('{ArrowRight}')
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true')
    expect(tabs[1]).toHaveFocus()
    expect(screen.getByRole('tabpanel')).toHaveTextContent(SCENARIOS[1].headline)

    await userEvent.keyboard('{ArrowLeft}{ArrowLeft}')
    expect(tabs[SCENARIOS.length - 1]).toHaveAttribute('aria-selected', 'true')
  })

  describe('product demo', () => {
    it('shows the complete answer immediately with reduced motion', () => {
      mockReducedMotion(true)
      render(<LandingPage />)
      const demo = screen.getByRole('region', { name: 'DocuMind product demo' })
      expect(within(demo).getByText(SCENARIOS[0].question)).toBeInTheDocument()
      const viewer = within(demo).getByRole('complementary', { name: 'Source viewer' })
      expect(viewer).toHaveTextContent(SCENARIOS[0].citations[0].excerpt)
      expect(within(demo).queryByRole('button', { name: 'Replay' })).not.toBeInTheDocument()
    })

    it('clicking a citation shows that source and pauses autoplay', async () => {
      mockReducedMotion(true)
      render(<LandingPage />)
      const demo = screen.getByRole('region', { name: 'DocuMind product demo' })
      await userEvent.click(within(demo).getByRole('button', { name: 'Show source 2' }))
      const viewer = within(demo).getByRole('complementary', { name: 'Source viewer' })
      expect(viewer).toHaveTextContent(SCENARIOS[0].citations[1].excerpt)
      expect(viewer).toHaveTextContent(`p. ${SCENARIOS[0].citations[1].page}`)
    })

    it('switches example when a team is chosen', async () => {
      mockReducedMotion(true)
      render(<LandingPage />)
      const demo = screen.getByRole('region', { name: 'DocuMind product demo' })
      await userEvent.click(within(demo).getByRole('button', { name: SCENARIOS[2].team }))
      expect(within(demo).getByText(SCENARIOS[2].question)).toBeInTheDocument()
    })

    it('animates the question being typed when motion is allowed', () => {
      vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance', 'Date'] })
      mockReducedMotion(false)
      render(<LandingPage />)
      const demo = screen.getByRole('region', { name: 'DocuMind product demo' })
      expect(within(demo).getByText('Live demo')).toBeInTheDocument()
      // Nothing sent yet: the answer has not appeared.
      expect(within(demo).queryByRole('button', { name: 'Show source 1' })).not.toBeInTheDocument()

      // Just before the first example ends: question sent, answer fully streamed.
      act(() => {
        vi.advanceTimersByTime(demoDuration(SCENARIOS[0]) - 200)
      })
      expect(within(demo).getByText(SCENARIOS[0].question)).toBeInTheDocument()
      expect(within(demo).getByRole('button', { name: 'Show source 1' })).toBeInTheDocument()

      // After it ends, autoplay moves on to the next team's example.
      act(() => {
        vi.advanceTimersByTime(400)
      })
      expect(within(demo).getByRole('button', { name: SCENARIOS[1].team })).toHaveAttribute('aria-pressed', 'true')
    })
  })
})
