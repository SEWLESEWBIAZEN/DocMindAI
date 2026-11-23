import { useRef, type CSSProperties, type ReactNode } from 'react'

import { useInView } from '../hooks'

interface RevealProps {
  children: ReactNode
  className?: string
  /** Stagger offset in ms (keep steps between 30 and 80 ms). */
  delay?: number
}

/**
 * Fades content up once when it first scrolls into view. Content is fully
 * visible without IntersectionObserver, and reduced motion keeps only the fade.
 */
export function Reveal({ children, className, delay = 0 }: RevealProps) {
  const ref = useRef<HTMLDivElement>(null)
  const shown = useInView(ref, { once: true, rootMargin: '0px 0px -8% 0px' })
  const style: CSSProperties | undefined = delay ? { transitionDelay: `${delay}ms` } : undefined
  return (
    <div ref={ref} className={['lp-reveal', className].filter(Boolean).join(' ')} data-shown={shown} style={style}>
      {children}
    </div>
  )
}
