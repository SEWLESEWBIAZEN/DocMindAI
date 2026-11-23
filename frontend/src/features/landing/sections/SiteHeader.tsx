import { useEffect, useState } from 'react'

import { Logo } from '../components/Logo'

const NAV = [
  { href: '#benefits', label: 'Benefits' },
  { href: '#use-cases', label: 'Use cases' },
  { href: '#how-it-works', label: 'How it works' },
  { href: '#security', label: 'Security' },
]

export function SiteHeader() {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <header className="lp-header" data-scrolled={scrolled || undefined}>
      <div className="lp-container lp-header__inner">
        <a href="/" className="lp-brand" aria-label="DocuMind AI home">
          <Logo />
          <span>DocuMind</span>
        </a>
        <nav className="lp-nav" aria-label="Primary">
          {NAV.map((item) => (
            <a key={item.href} href={item.href}>
              {item.label}
            </a>
          ))}
        </nav>
        <div className="lp-header__actions">
          <a href="/login" className="lp-link">
            Sign in
          </a>
          <a href="/register" className="lp-btn lp-btn--primary lp-btn--sm">
            Get started
          </a>
        </div>
      </div>
    </header>
  )
}
