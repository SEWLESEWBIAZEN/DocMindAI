import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AnswerMarkdown } from './AnswerMarkdown'

describe('AnswerMarkdown', () => {
  it('renders citation markers as buttons', async () => {
    const onCitation = vi.fn()
    render(<AnswerMarkdown text="Notice is **90 days** [1][2]." citationNumbers={new Set([1, 2])} onCitation={onCitation} />)
    expect(screen.getByText('90 days').tagName).toBe('STRONG')
    await userEvent.click(screen.getByRole('button', { name: 'Source 2' }))
    expect(onCitation).toHaveBeenCalledWith(2)
  })

  it('never renders raw HTML, scripts or unsafe links from model output', () => {
    const { container } = render(
      <AnswerMarkdown
        text={[
          '<script>alert(1)</script>',
          'Inline <img src=x onerror=alert(1)> html.',
          '[click](javascript:alert(1)) and ![t](https://tracker.test/p.png)',
          '[ok](https://example.com)',
        ].join('\n\n')}
        citationNumbers={new Set()}
      />,
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('a[href^="javascript"]')).toBeNull()
    expect(container).toHaveTextContent('click')
    const ok = screen.getByRole('link', { name: 'ok' })
    expect(ok).toHaveAttribute('rel', 'noopener noreferrer nofollow')
    expect(ok).toHaveAttribute('target', '_blank')
  })
})
