import { useEffect, useMemo, useRef, useState } from 'react'

import { SCENARIOS, type Scenario } from '../content'
import { demoDuration, finalFrame, getDemoFrame, tokenize, type DemoFrame } from '../demo/timeline'
import { useInView, usePrefersReducedMotion } from '../hooks'
import { Icon } from './Icon'

// Line widths for the illustrative "page" around the cited passage.
const LINES_BEFORE = [92, 100, 86, 97, 64]
const LINES_AFTER = [95, 88, 100, 71]

function sameFrame(a: DemoFrame, b: DemoFrame): boolean {
  return (
    a.phase === b.phase &&
    a.typedChars === b.typedChars &&
    a.visibleTokens === b.visibleTokens &&
    a.activeCitation === b.activeCitation &&
    a.finished === b.finished
  )
}

interface ProductDemoProps {
  scenarios?: Scenario[]
}

export function ProductDemo({ scenarios = SCENARIOS }: ProductDemoProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const reducedMotion = usePrefersReducedMotion()
  const inView = useInView(rootRef, { rootMargin: '0px' })

  const [index, setIndex] = useState(0)
  // `manualCitation` set means the user has taken control: autoplay stops.
  const [manualCitation, setManualCitation] = useState<number | null>(null)
  const [runId, setRunId] = useState(0)
  const scenario = scenarios[index]
  const paused = manualCitation !== null
  const playing = !reducedMotion && !paused && inView

  // Frames are tagged with the run they belong to, so a new run starts from
  // frame zero immediately instead of briefly showing the previous run's state.
  const runKey = `${scenario.id}:${runId}`
  const [animated, setAnimated] = useState<{ key: string; frame: DemoFrame }>(() => ({
    key: runKey,
    frame: getDemoFrame(scenario, 0),
  }))

  useEffect(() => {
    if (!playing) return
    let elapsed = 0
    let last = performance.now()
    let raf = 0
    const tick = (now: number) => {
      // Clamp the step so a backgrounded tab resumes instead of jumping ahead.
      elapsed += Math.min(now - last, 100)
      last = now
      const next = getDemoFrame(scenario, elapsed)
      setAnimated((prev) => (prev.key === runKey && sameFrame(prev.frame, next) ? prev : { key: runKey, frame: next }))
      if (next.finished) {
        setIndex((i) => (i + 1) % scenarios.length)
        setRunId((r) => r + 1)
        return
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, scenario, scenarios.length, runKey])

  const animatedFrame = animated.key === runKey ? animated.frame : getDemoFrame(scenario, 0)
  const frame = reducedMotion || paused ? finalFrame(scenario, manualCitation) : animatedFrame
  const tokens = useMemo(() => tokenize(scenario.answer), [scenario])
  const citation = scenario.citations.find((c) => c.id === frame.activeCitation) ?? null
  const streaming = frame.phase === 'streaming'

  const selectScenario = (next: number) => {
    setIndex(next)
    setManualCitation(null)
    setRunId((r) => r + 1)
  }
  const replay = () => {
    setManualCitation(null)
    setRunId((r) => r + 1)
  }

  return (
    <div className="lp-demo" ref={rootRef} role="region" aria-label="DocuMind product demo">
      <div className="lp-demo__chrome">
        <span className="lp-demo__dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="lp-demo__title">
          <Icon name="layers" size={14} /> {scenario.workspace}
        </span>
        <span className="lp-demo__live" data-playing={playing || undefined}>
          {playing ? 'Live demo' : 'Demo'}
        </span>
      </div>

      <div className="lp-demo__body" key={scenario.id}>
        <aside className="lp-demo__docs" aria-label="Selected documents">
          <p className="lp-demo__label">Selected documents</p>
          <ul>
            {scenario.documents.map((doc) => (
              <li key={doc.name} data-cited={citation?.document === doc.name || undefined}>
                <span className={`lp-filetype lp-filetype--${doc.type.toLowerCase()}`}>{doc.type}</span>
                <span className="lp-demo__docname">{doc.name}</span>
                <Icon name="check" size={14} className="lp-demo__indexed" />
              </li>
            ))}
          </ul>
        </aside>

        <section className="lp-demo__chat" aria-label="Conversation">
          <div className="lp-demo__thread">
            {frame.sent && <div className="lp-msg lp-msg--user">{scenario.question}</div>}

            {frame.phase === 'searching' && (
              <div className="lp-demo__searching" role="status">
                <span className="lp-pulse" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
                Searching {scenario.documents.length} documents…
              </div>
            )}

            {frame.visibleTokens > 0 && (
              <div className="lp-msg lp-msg--ai">
                <span className="lp-msg__avatar" aria-hidden="true">
                  <Icon name="sparkles" size={14} />
                </span>
                <p className="lp-msg__text">
                  {tokens.slice(0, frame.visibleTokens).map((token, i) =>
                    token.kind === 'word' ? (
                      <span key={i} className="lp-tok">
                        {token.text}
                      </span>
                    ) : (
                      <button
                        key={i}
                        type="button"
                        className="lp-cite lp-tok"
                        data-active={frame.activeCitation === token.id || undefined}
                        aria-pressed={frame.activeCitation === token.id}
                        aria-label={`Show source ${token.id}`}
                        onClick={() => setManualCitation(token.id)}
                      >
                        {token.id}
                      </button>
                    ),
                  )}
                  {streaming && <span className="lp-caret" aria-hidden="true" />}
                </p>
              </div>
            )}
          </div>

          <div className="lp-demo__composer" aria-hidden="true">
            <span className="lp-demo__input">
              {frame.phase === 'typing' && frame.typedChars > 0 ? (
                <>
                  {scenario.question.slice(0, frame.typedChars)}
                  <span className="lp-caret" />
                </>
              ) : (
                <span className="lp-demo__placeholder">Ask about your documents…</span>
              )}
            </span>
            <span className="lp-demo__send" data-ready={frame.typedChars > 0 || undefined}>
              <Icon name="send" size={16} />
            </span>
          </div>
        </section>

        <aside className="lp-demo__source" aria-label="Source viewer" aria-live="polite">
          {citation ? (
            <>
              <div className="lp-source__header">
                <span className="lp-cite lp-cite--static" data-active>
                  {citation.id}
                </span>
                <span className="lp-source__doc">{citation.document}</span>
                <span className="lp-source__page">p. {citation.page}</span>
              </div>
              <div className="lp-page" key={`${scenario.id}-${citation.id}`}>
                {LINES_BEFORE.map((w, i) => (
                  <span key={`b${i}`} className="lp-page__line" style={{ width: `${w}%` }} />
                ))}
                <p className="lp-page__excerpt">{citation.excerpt}</p>
                {LINES_AFTER.map((w, i) => (
                  <span key={`a${i}`} className="lp-page__line" style={{ width: `${w}%` }} />
                ))}
              </div>
            </>
          ) : (
            <div className="lp-source__empty">
              <Icon name="quote" size={20} />
              <span>Sources appear here as the answer cites them</span>
            </div>
          )}
        </aside>
      </div>

      <div className="lp-demo__controls">
        <div className="lp-demo__scenarios" role="group" aria-label="Choose an example">
          {scenarios.map((s, i) => (
            <button
              key={s.id}
              type="button"
              className="lp-demo__scenario"
              aria-pressed={i === index}
              onClick={() => selectScenario(i)}
            >
              <Icon name={s.icon} size={14} />
              {s.team}
              {i === index && playing && (
                <span
                  key={`${s.id}-${runId}`}
                  className="lp-demo__progress"
                  style={{ animationDuration: `${demoDuration(s)}ms` }}
                  aria-hidden="true"
                />
              )}
            </button>
          ))}
        </div>
        {paused && !reducedMotion && (
          <button type="button" className="lp-demo__replay" onClick={replay}>
            Replay
          </button>
        )}
      </div>
    </div>
  )
}
