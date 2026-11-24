import { Link } from 'react-router'

import { Logo } from '../features/landing/components/Logo'

export function NotFoundPage({ inApp = false }: { inApp?: boolean }) {
  const content = (
    <section className="not-found" aria-labelledby="not-found-title">
      <p className="not-found__code">404</p>
      <h1 id="not-found-title">Page not found</h1>
      <p className="muted">The page doesn't exist, or you don't have access to it.</p>
      <Link className="btn btn--primary btn--md" to="/app">
        Go to dashboard
      </Link>
    </section>
  )
  if (inApp) return content
  return (
    <main className="auth-page" id="main">
      <Link to="/" className="auth-page__brand" aria-label="DocuMind home">
        <Logo />
        <span>DocuMind</span>
      </Link>
      {content}
    </main>
  )
}
