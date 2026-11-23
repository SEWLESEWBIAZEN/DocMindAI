import { CAPABILITIES } from '../content'
import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'

export function CapabilityStrip() {
  return (
    <section className="lp-capabilities" aria-label="Key capabilities">
      <div className="lp-container lp-capabilities__grid">
        {CAPABILITIES.map((item, i) => (
          <Reveal key={item.title} delay={i * 60} className="lp-capability">
            <span className="lp-icon-tile">
              <Icon name={item.icon} size={18} />
            </span>
            <span>
              <strong>{item.title}</strong>
              <span className="lp-muted">{item.detail}</span>
            </span>
          </Reveal>
        ))}
      </div>
    </section>
  )
}
