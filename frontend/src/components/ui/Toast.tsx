import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

import { Icon } from './Icon'

export type ToastTone = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  tone: ToastTone
  message: string
}

interface ToastApi {
  show: (message: string, tone?: ToastTone) => void
  success: (message: string) => void
  error: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)
const DURATION_MS = 5000

/**
 * Toasts are announced through live regions: errors assertively, the rest
 * politely. They pause while hovered or focused and can be dismissed.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const next = useRef(1)
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>())

  const dismiss = useCallback((id: number) => {
    clearTimeout(timers.current.get(id))
    timers.current.delete(id)
    setItems((current) => current.filter((item) => item.id !== id))
  }, [])

  const schedule = useCallback(
    (id: number) => {
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), DURATION_MS),
      )
    },
    [dismiss],
  )

  const show = useCallback(
    (message: string, tone: ToastTone = 'info') => {
      const id = next.current++
      setItems((current) => [...current.slice(-3), { id, tone, message }])
      schedule(id)
    },
    [schedule],
  )

  const api = useMemo<ToastApi>(
    () => ({ show, success: (m) => show(m, 'success'), error: (m) => show(m, 'error') }),
    [show],
  )

  const region = (tone: 'polite' | 'assertive') => (
    <div className="toasts" aria-live={tone} aria-relevant="additions" role={tone === 'assertive' ? 'alert' : 'status'}>
      {items
        .filter((item) => (item.tone === 'error') === (tone === 'assertive'))
        .map((item) => (
          <div
            key={item.id}
            className={`toast toast--${item.tone}`}
            onMouseEnter={() => clearTimeout(timers.current.get(item.id))}
            onMouseLeave={() => schedule(item.id)}
            onFocus={() => clearTimeout(timers.current.get(item.id))}
            onBlur={() => schedule(item.id)}
          >
            <Icon name={item.tone === 'success' ? 'check' : item.tone === 'error' ? 'alert' : 'info'} />
            <span className="toast__message">{item.message}</span>
            <button type="button" className="toast__close" aria-label="Dismiss" onClick={() => dismiss(item.id)}>
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
    </div>
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack">
        {region('polite')}
        {region('assertive')}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext)
  if (!api) throw new Error('useToast must be used inside <ToastProvider>')
  return api
}
