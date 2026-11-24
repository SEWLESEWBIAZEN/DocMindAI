import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'

import { Icon, type IconName } from './Icon'
import { Spinner } from './Spinner'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: 'sm' | 'md'
  icon?: IconName
  loading?: boolean
  /** Icon-only buttons must provide an accessible label. */
  label?: string
  children?: ReactNode
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', icon, loading = false, label, children, className, disabled, type, ...rest },
  ref,
) {
  const classes = ['btn', `btn--${variant}`, `btn--${size}`, children ? '' : 'btn--icon', className ?? '']
    .filter(Boolean)
    .join(' ')
  return (
    <button
      ref={ref}
      type={type ?? 'button'}
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-label={children ? undefined : label}
      title={children ? undefined : label}
      {...rest}
    >
      {loading ? <Spinner size={size === 'sm' ? 14 : 16} /> : icon ? <Icon name={icon} size={size === 'sm' ? 15 : 17} /> : null}
      {children}
    </button>
  )
})
