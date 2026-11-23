import { useEffect, useState, type RefObject } from 'react'

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

/** Tracks the user's reduced-motion preference (false where matchMedia is unavailable). */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(REDUCED_MOTION_QUERY).matches
      : false,
  )
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(REDUCED_MOTION_QUERY)
    const onChange = () => setReduced(query.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])
  return reduced
}

/**
 * Whether the element is in the viewport. Defaults to `true` where
 * IntersectionObserver is unavailable so content is never hidden.
 */
export function useInView(ref: RefObject<Element | null>, options?: { once?: boolean; rootMargin?: string }): boolean {
  const supported = typeof IntersectionObserver !== 'undefined'
  const [inView, setInView] = useState(!supported)
  const once = options?.once ?? false
  const rootMargin = options?.rootMargin ?? '0px'

  useEffect(() => {
    const element = ref.current
    if (!element || !supported) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        setInView(entry.isIntersecting)
        if (entry.isIntersecting && once) observer.disconnect()
      },
      { rootMargin, threshold: 0.12 },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref, once, rootMargin, supported])

  return inView
}
