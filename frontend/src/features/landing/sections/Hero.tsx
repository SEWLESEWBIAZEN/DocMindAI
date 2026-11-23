import { Icon } from '../components/Icon'
import { ProductDemo } from '../components/ProductDemo'

export function Hero() {
  return (
    <section className="lp-hero" aria-labelledby="hero-title">
      <div className="lp-hero__backdrop" aria-hidden="true" />
      <div className="lp-container">
        <div className="lp-hero__copy">
          <a href="#how-it-works" className="lp-pill lp-enter" style={{ animationDelay: '0ms' }}>
            <span className="lp-pill__tag">New</span>
            Answers cited to the exact page
            <Icon name="arrowRight" size={14} />
          </a>
          <h1 id="hero-title" className="lp-enter" style={{ animationDelay: '70ms' }}>
            Ask your documents anything.
            <br />
            <span className="lp-marker">Trust every answer.</span>
          </h1>
          <p className="lp-hero__lead lp-enter" style={{ animationDelay: '140ms' }}>
            DocuMind turns contracts, reports, manuals and research into a workspace you can talk to. Every
            answer is grounded in your documents and cites the exact page it came from.
          </p>
          <div className="lp-hero__actions lp-enter" style={{ animationDelay: '210ms' }}>
            <a href="/register" className="lp-btn lp-btn--primary">
              Get started free
              <Icon name="arrowRight" size={16} />
            </a>
            <a href="#use-cases" className="lp-btn lp-btn--ghost">
              See it for your team
            </a>
          </div>
          <p className="lp-hero__note lp-enter" style={{ animationDelay: '280ms' }}>
            Personal and organization workspaces · PDF, DOCX, TXT and Markdown
          </p>
        </div>
        <div className="lp-hero__demo lp-enter lp-enter--demo" style={{ animationDelay: '320ms' }}>
          <ProductDemo />
        </div>
      </div>
    </section>
  )
}
