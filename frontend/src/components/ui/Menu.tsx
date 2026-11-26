import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { Icon, type IconName } from './Icon'

export interface MenuItem {
  label: string
  icon?: IconName
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
  /** Set for one-of-several choices: the item becomes a menuitemradio. */
  checked?: boolean
}

interface MenuProps {
  /** Accessible label for the trigger. */
  label: string
  trigger: ReactNode
  /** `{ group }` is a small visual heading; give the items themselves self-explanatory labels. */
  items: (MenuItem | 'separator' | { group: string })[]
  align?: 'start' | 'end'
  header?: ReactNode
  className?: string
  triggerClassName?: string
}

/** Menu button (WAI-ARIA pattern): arrows, Home/End, Escape, type-ahead free. */
export function Menu({ label, trigger, items, align = 'end', header, className, triggerClassName }: MenuProps) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const actionable = items.filter((item): item is MenuItem => typeof item === 'object' && 'onSelect' in item && !item.disabled)

  const focusItem = (index: number) => {
    const nodes = root.current?.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]:not([disabled])')
    if (!nodes?.length) return
    nodes[(index + nodes.length) % nodes.length]?.focus()
  }

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    requestAnimationFrame(() => focusItem(0))
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const close = (refocus = true) => {
    setOpen(false)
    if (refocus) button.current?.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    const nodes = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]:not([disabled])') ?? [])
    const current = nodes.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      focusItem(current + 1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      focusItem(current - 1)
    } else if (event.key === 'Home') {
      event.preventDefault()
      focusItem(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      focusItem(nodes.length - 1)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      close()
    } else if (event.key === 'Tab') {
      close(false)
    }
  }

  return (
    <div className={`menu ${className ?? ''}`} ref={root} onKeyDown={open ? onKeyDown : undefined}>
      <button
        ref={button}
        type="button"
        className={triggerClassName ?? 'btn btn--ghost btn--md'}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={label}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' && !open) {
            event.preventDefault()
            setOpen(true)
          }
        }}
      >
        {trigger}
      </button>
      {open && (
        <div className={`menu__popover menu__popover--${align}`} id={id} role="menu" aria-label={label}>
          {header && <div className="menu__header">{header}</div>}
          {items.map((item, index) =>
            item === 'separator' ? (
              <div key={`sep-${index}`} className="menu__separator" role="separator" />
            ) : typeof item === 'object' && 'group' in item ? (
              <div key={`group-${item.group}`} className="menu__group" aria-hidden="true">
                {item.group}
              </div>
            ) : (
              <button
                key={item.label}
                type="button"
                role={item.checked === undefined ? 'menuitem' : 'menuitemradio'}
                aria-checked={item.checked}
                className={`menu__item${item.danger ? ' menu__item--danger' : ''}`}
                disabled={item.disabled}
                tabIndex={-1}
                onClick={() => {
                  close()
                  item.onSelect()
                }}
              >
                {item.icon && <Icon name={item.icon} size={16} />}
                {item.label}
                {item.checked && <Icon name="check" size={16} className="menu__check" />}
              </button>
            ),
          )}
          {actionable.length === 0 && <div className="menu__empty">No actions</div>}
        </div>
      )}
    </div>
  )
}
