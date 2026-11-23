import { STEPS } from '../content'
import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'
import { SectionHeading } from '../components/SectionHeading'

export function HowItWorks() {
  return (
    <section className="lp-section" id="how-it-works" aria-labelledby="how-title">
      <div className="lp-container">
        <SectionHeading
          id="how-title"
          eyebrow="How it works"
          title="From upload to cited answer"
          lead="A processing pipeline that keeps page numbers and structure intact, so answers can always point back to their source."
        />
        <ol className="lp-steps">
          <span className="lp-steps__rail" aria-hidden="true">
            <i />
          </span>
          {STEPS.map((step, i) => (
            <li key={step.title}>
              <Reveal delay={i * 70} className="lp-step">
                <span className="lp-step__icon">
                  <Icon name={step.icon} size={20} />
                  <span className="lp-step__num">{i + 1}</span>
                </span>
                <h3>{step.title}</h3>
                <p>{step.body}</p>
                <span className="lp-chip">{step.meta}</span>
              </Reveal>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
