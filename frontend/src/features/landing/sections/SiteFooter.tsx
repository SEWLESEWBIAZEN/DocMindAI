import { Logo } from '../components/Logo'

const YEAR = new Date().getFullYear()

export function SiteFooter() {
  return (
    <footer className="lp-footer">
      <div className="lp-container lp-footer__inner">
        <div className="lp-brand">
          <Logo size={22} />
          <span>DocuMind</span>
        </div>
        <nav aria-label="Footer" className="lp-footer__links">
          <a href="/#benefits">Benefits</a>
          <a href="/#use-cases">Use cases</a>
          <a href="/#security">Security</a>
          <a href="/pricing">Pricing</a>
          <a href="/status">System status</a>
        </nav>
        <p className="lp-muted">© {YEAR} DocuMind AI</p>
      </div>
    </footer>
  )
}
