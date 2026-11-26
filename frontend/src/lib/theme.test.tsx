import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import { Menu } from '../components/ui'
import { setTheme, useTheme } from './theme'

function ThemeMenu() {
  const theme = useTheme()
  return (
    <Menu
      label="Account"
      trigger="…"
      items={[
        { group: 'Theme' },
        { label: 'Light theme', checked: theme === 'light', onSelect: () => setTheme('light') },
        { label: 'Dark theme', checked: theme === 'dark', onSelect: () => setTheme('dark') },
        { label: 'System theme', checked: theme === 'system', onSelect: () => setTheme('system') },
      ]}
    />
  )
}

describe('theme', () => {
  afterEach(() => act(() => setTheme('system')))

  it('applies, remembers and clears the choice', () => {
    act(() => setTheme('dark'))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(window.localStorage.getItem('documind.theme')).toBe('dark')
    act(() => setTheme('system'))
    expect(document.documentElement).not.toHaveAttribute('data-theme')
    expect(window.localStorage.getItem('documind.theme')).toBeNull()
  })

  it('is picked from the menu as one of three radio items', async () => {
    const user = userEvent.setup()
    render(<ThemeMenu />)
    await user.click(screen.getByRole('button', { name: 'Account' }))
    expect(screen.getByRole('menuitemradio', { name: 'System theme' })).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('menuitemradio', { name: 'Dark theme' }))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    await user.click(screen.getByRole('button', { name: 'Account' }))
    expect(screen.getByRole('menuitemradio', { name: 'Dark theme' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('menuitemradio', { name: 'Light theme' })).toHaveAttribute('aria-checked', 'false')
  })
})
