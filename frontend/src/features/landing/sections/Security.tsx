import { SECURITY_POINTS } from '../content'
import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'
import { SectionHeading } from '../components/SectionHeading'

export function Security() {
  return (
    <section className="lp-section lp-section--tint" id="security" aria-labelledby="security-title">
      <div className="lp-container lp-security">
        <div className="lp-security__intro">
          <SectionHeading
            id="security-title"
            align="start"
            eyebrow="Security & trust"
            title="Your documents stay yours"
            lead="DocuMind is multi-tenant from the ground up. Isolation is enforced where the data lives, not just in the interface."
          />
          <Reveal className="lp-security__badge" delay={60}>
            <Icon name="lock" size={22} />
            <span>
              <strong>Private by default</strong>
              <span className="lp-muted">Nothing is shared outside a workspace unless you share it.</span>
            </span>
          </Reveal>
        </div>
        <ul className="lp-security__grid">
          {SECURITY_POINTS.map((point, i) => (
            <li key={point.title}>
              <Reveal delay={(i % 2) * 60} className="lp-security__item">
                <Icon name="shield" size={18} />
                <div>
                  <h3>{point.title}</h3>
                  <p>{point.body}</p>
                </div>
              </Reveal>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
