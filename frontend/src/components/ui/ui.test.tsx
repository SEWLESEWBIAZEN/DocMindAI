import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmProvider, Field, Menu, TextInput, ToastProvider, useConfirm, useToast } from '.'

describe('Field', () => {
  it('labels the control and links hint and error', () => {
    render(
      <Field label="Email" hint="Work address" error="Required">
        <TextInput />
      </Field>,
    )
    const input = screen.getByLabelText('Email')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Work address Required')
  })
})

describe('ConfirmDialog', () => {
  function Harness({ onResult }: { onResult: (ok: boolean) => void }) {
    const confirm = useConfirm()
    return (
      <button type="button" onClick={async () => onResult(await confirm({ title: 'Delete it?', confirmLabel: 'Delete', danger: true }))}>
        open
      </button>
    )
  }

  it('resolves true on confirm and false on cancel', async () => {
    const user = userEvent.setup()
    const results: boolean[] = []
    render(
      <ConfirmProvider>
        <Harness onResult={(ok) => results.push(ok)} />
      </ConfirmProvider>,
    )
    await user.click(screen.getByText('open'))
    expect(screen.getByRole('heading', { name: 'Delete it?' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByText('open'))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(results).toEqual([true, false])
  })
})

describe('Menu', () => {
  it('opens with the keyboard, moves with arrows and closes with Escape', async () => {
    const user = userEvent.setup()
    const onA = vi.fn()
    render(
      <Menu
        label="Actions"
        trigger="…"
        items={[
          { label: 'First', onSelect: onA },
          { label: 'Second', onSelect: vi.fn() },
        ]}
      />,
    )
    const trigger = screen.getByRole('button', { name: 'Actions' })
    trigger.focus()
    await user.keyboard('{ArrowDown}')
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await act(() => new Promise((resolve) => requestAnimationFrame(resolve)))
    expect(screen.getByRole('menuitem', { name: 'First' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Second' })).toHaveFocus()
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onA).toHaveBeenCalledOnce()
    expect(trigger).toHaveFocus()
    await user.click(trigger)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})

describe('Toast', () => {
  function Harness() {
    const toast = useToast()
    const [, force] = useState(0)
    return (
      <>
        <button type="button" onClick={() => { toast.success('Saved.'); force(1) }}>ok</button>
        <button type="button" onClick={() => toast.error('Failed.')}>bad</button>
      </>
    )
  }

  it('announces successes politely and errors assertively', async () => {
    const user = userEvent.setup()
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    )
    await user.click(screen.getByText('ok'))
    await user.click(screen.getByText('bad'))
    expect(screen.getByRole('status')).toHaveTextContent('Saved.')
    expect(screen.getByRole('alert')).toHaveTextContent('Failed.')
    await user.click(screen.getAllByRole('button', { name: 'Dismiss' })[0] as HTMLElement)
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })
})
