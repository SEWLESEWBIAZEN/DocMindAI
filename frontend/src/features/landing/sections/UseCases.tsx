import { useRef, useState, type KeyboardEvent } from 'react'

import { SCENARIOS } from '../content'
import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'
import { SectionHeading } from '../components/SectionHeading'

/** Accessible tabs (WAI-ARIA tabs pattern with automatic activation). */
export function UseCases() {
  const [active, setActive] = useState(0)
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const scenario = SCENARIOS[active]

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const last = SCENARIOS.length - 1
    const moves: Record<string, number> = {
      ArrowRight: active === last ? 0 : active + 1,
      ArrowLeft: active === 0 ? last : active - 1,
      Home: 0,
      End: last,
    }
    if (!(event.key in moves)) return
    event.preventDefault()
    const next = moves[event.key]
    setActive(next)
    tabRefs.current[next]?.focus()
  }

  return (
    <section className="lp-section lp-section--tint" id="use-cases" aria-labelledby="use-cases-title">
      <div className="lp-container">
        <SectionHeading
          id="use-cases-title"
          eyebrow="Use cases"
          title="Built for every team that runs on documents"
          lead="The same workspace helps legal, finance, research, people and support teams answer different questions, each with sources attached."
        />

        <Reveal className="lp-usecases">
          <div className="lp-tabs" role="tablist" aria-label="Teams" onKeyDown={onKeyDown}>
            {SCENARIOS.map((s, i) => (
              <button
                key={s.id}
                ref={(el) => {
                  tabRefs.current[i] = el
                }}
                type="button"
                role="tab"
                id={`tab-${s.id}`}
                aria-selected={i === active}
                aria-controls={`panel-${s.id}`}
                tabIndex={i === active ? 0 : -1}
                className="lp-tab"
                onClick={() => setActive(i)}
              >
                <Icon name={s.icon} size={16} />
                {s.team}
              </button>
            ))}
          </div>

          <div
            key={scenario.id}
            className="lp-usecase"
            role="tabpanel"
            id={`panel-${scenario.id}`}
            aria-labelledby={`tab-${scenario.id}`}
            tabIndex={0}
          >
            <div className="lp-usecase__copy">
              <h3>{scenario.headline}</h3>
              <p className="lp-usecase__before">
                <span className="lp-tag">Before</span>
                {scenario.before}
              </p>
              <p className="lp-tag lp-tag--accent">With DocuMind</p>
              <ul className="lp-checklist">
                {scenario.outcomes.map((outcome) => (
                  <li key={outcome}>
                    <Icon name="check" size={16} />
                    {outcome}
                  </li>
                ))}
              </ul>
            </div>

            <figure className="lp-usecase__example">
              <figcaption className="lp-muted">
                <Icon name="layers" size={14} /> {scenario.workspace}
              </figcaption>
              <p className="lp-msg lp-msg--user">{scenario.question}</p>
              <div className="lp-msg lp-msg--ai">
                <span className="lp-msg__avatar" aria-hidden="true">
                  <Icon name="sparkles" size={14} />
                </span>
                <p className="lp-msg__text">
                  {scenario.answer.map((part, i) =>
                    part.kind === 'text' ? (
                      <span key={i}>{part.text}</span>
                    ) : (
                      <span key={i} className="lp-cite lp-cite--static" aria-label={`source ${part.id}`}>
                        {part.id}
                      </span>
                    ),
                  )}
                </p>
              </div>
              <ol className="lp-sources">
                {scenario.citations.map((c) => (
                  <li key={c.id}>
                    <span className="lp-cite lp-cite--static">{c.id}</span>
                    <span className="lp-sources__doc">{c.document}</span>
                    <span className="lp-muted">p. {c.page}</span>
                  </li>
                ))}
              </ol>
            </figure>
          </div>
        </Reveal>
        <p className="lp-footnote">Example documents and answers are illustrative.</p>
      </div>
    </section>
  )
}
