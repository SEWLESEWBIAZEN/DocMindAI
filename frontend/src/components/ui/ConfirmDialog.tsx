import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'

import { announce } from './announce'
import { Button } from './Button'
import { Dialog } from './Dialog'

export interface ConfirmOptions {
  title: string
  description?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

type Confirm = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<Confirm | null>(null)

/** `const ok = await confirm({...})` from anywhere below the provider. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [options, setOptions] = useState<ConfirmOptions | null>(null)
  const resolver = useRef<((value: boolean) => void) | null>(null)

  const confirm = useCallback<Confirm>((next) => {
    resolver.current?.(false)
    setOptions(next)
    const description = typeof next.description === 'string' ? ` ${next.description}` : ''
    announce(`${next.title}${description} Press ${next.confirmLabel ?? 'Confirm'} to continue, or Escape to cancel.`)
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve
    })
  }, [])

  const settle = (value: boolean) => {
    resolver.current?.(value)
    resolver.current = null
    setOptions(null)
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog
        open={options !== null}
        title={options?.title ?? ''}
        description={options?.description}
        onClose={() => settle(false)}
        size="sm"
        footer={
          <>
            <Button onClick={() => settle(false)}>{options?.cancelLabel ?? 'Cancel'}</Button>
            <Button variant={options?.danger ? 'danger' : 'primary'} onClick={() => settle(true)} autoFocus>
              {options?.confirmLabel ?? 'Confirm'}
            </Button>
          </>
        }
      />
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): Confirm {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm must be used inside <ConfirmProvider>')
  return confirm
}
