import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'

export function FinalCta() {
  return (
    <section className="lp-section" aria-labelledby="cta-title">
      <div className="lp-container">
        <Reveal className="lp-cta">
          <h2 id="cta-title">Turn your documents into answers you can trust</h2>
          <p>Start in a personal workspace, then bring your team in when you’re ready.</p>
          <div className="lp-hero__actions">
            <a href="/register" className="lp-btn lp-btn--primary">
              Create your workspace
              <Icon name="arrowRight" size={16} />
            </a>
            <a href="/login" className="lp-btn lp-btn--ghost">
              Sign in
            </a>
          </div>
        </Reveal>
      </div>
    </section>
  )
}
