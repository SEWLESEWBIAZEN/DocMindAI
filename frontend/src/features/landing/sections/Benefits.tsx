import type { ReactNode } from 'react'

import { Icon, type IconName } from '../components/Icon'
import { Reveal } from '../components/Reveal'
import { SectionHeading } from '../components/SectionHeading'

interface BenefitProps {
  icon: IconName
  title: string
  body: string
  wide?: boolean
  delay: number
  children: ReactNode
}

function Benefit({ icon, title, body, wide, delay, children }: BenefitProps) {
  return (
    <Reveal delay={delay} className={wide ? 'lp-bento__wide' : undefined}>
      <article className="lp-benefit">
        <div className="lp-benefit__visual" aria-hidden="true">
          {children}
        </div>
        <div className="lp-benefit__copy">
          <h3>
            <Icon name={icon} size={18} />
            {title}
          </h3>
          <p>{body}</p>
        </div>
      </article>
    </Reveal>
  )
}

export function Benefits() {
  return (
    <section className="lp-section" id="benefits" aria-labelledby="benefits-title">
      <div className="lp-container">
        <SectionHeading
          id="benefits-title"
          eyebrow="How DocuMind helps"
          title="Less searching. More knowing."
          lead="Teams use DocuMind to get answers out of their documents faster, and to trust those answers enough to act on them."
        />

        <div className="lp-bento">
          <Benefit
            wide
            delay={0}
            icon="quote"
            title="Every answer shows its source"
            body="Citations name the document, page and passage behind each claim, so anyone can verify an answer in one click before acting on it."
          >
            <div className="lp-v-cite">
              <p>
                Renews for one-year terms unless notice is given 90 days before expiry
                <span className="lp-cite lp-cite--static" data-active>
                  1
                </span>
                , and Legal must review renewals above $50k
                <span className="lp-cite lp-cite--static">3</span>.
              </p>
              <div className="lp-v-cite__source">
                <span className="lp-filetype lp-filetype--pdf">PDF</span>
                <span>Halcyon Logistics MSA</span>
                <span className="lp-muted">p. 14</span>
              </div>
              <div className="lp-v-cite__excerpt">“…unless either party provides written notice at least ninety (90) days prior…”</div>
            </div>
          </Benefit>

          <Benefit
            delay={60}
            icon="search"
            title="Answers in seconds, not searches"
            body="Ask in plain language. DocuMind finds passages by meaning, not just matching keywords."
          >
            <div className="lp-v-search">
              <div className="lp-v-search__box">
                <Icon name="search" size={14} />
                notice period for renewals
              </div>
              {[
                ['Halcyon MSA · p. 14', 94],
                ['Brightline Supply · p. 6', 81],
                ['Vendor Policy · p. 3', 62],
              ].map(([label, score]) => (
                <div key={label} className="lp-v-search__row">
                  <span>{label}</span>
                  <span className="lp-v-search__bar">
                    <i style={{ transform: `scaleX(${Number(score) / 100})` }} />
                  </span>
                </div>
              ))}
            </div>
          </Benefit>

          <Benefit
            delay={0}
            icon="layers"
            title="Connect the dots across documents"
            body="Compare reports, contracts and studies in one question. Conflicting sources are called out, not blended."
          >
            <div className="lp-v-merge">
              <span className="lp-v-merge__doc">Q2 report</span>
              <span className="lp-v-merge__doc">Q3 report</span>
              <span className="lp-v-merge__doc">Budget memo</span>
              <span className="lp-v-merge__answer">
                <Icon name="sparkles" size={14} /> One cited answer
              </span>
            </div>
          </Benefit>

          <Benefit
            delay={60}
            icon="message"
            title="Honest when evidence is thin"
            body="If your documents don’t support an answer, DocuMind says so instead of guessing."
          >
            <div className="lp-v-honest">
              <span className="lp-v-honest__icon">?</span>
              <p>
                These documents don’t mention an exception for contractors. <strong>Confirm with HR.</strong>
              </p>
            </div>
          </Benefit>

          <Benefit
            delay={120}
            icon="history"
            title="Pick up where you left off"
            body="Conversations are saved with their sources, so the reasoning is still there next quarter."
          >
            <ul className="lp-v-history">
              <li data-active>Renewal notice periods</li>
              <li>Q3 opex variance</li>
              <li>Sample sizes across studies</li>
            </ul>
          </Benefit>

          <Benefit
            wide
            delay={0}
            icon="users"
            title="Shared knowledge, private by default"
            body="Keep personal documents in your own workspace, and share team knowledge in organization workspaces with Owner, Admin, Member and Viewer roles."
          >
            <div className="lp-v-workspaces">
              {[
                ['Personal', 'Only you', 'P'],
                ['Acme · Legal', 'Admin', 'L'],
                ['Acme · Finance', 'Viewer', 'F'],
              ].map(([name, role, initial], i) => (
                <div key={name} className="lp-v-workspaces__row" data-active={i === 1 || undefined}>
                  <span className="lp-v-workspaces__avatar">{initial}</span>
                  <span>{name}</span>
                  <span className="lp-role">{role}</span>
                </div>
              ))}
            </div>
          </Benefit>

          <Benefit
            delay={60}
            icon="chart"
            title="Visibility for admins"
            body="See storage, documents and AI usage per workspace, with audit logs for every change."
          >
            <div className="lp-v-usage">
              {[38, 54, 46, 72, 60, 84, 68].map((h, i) => (
                <i key={i} style={{ transform: `scaleY(${h / 100})` }} />
              ))}
            </div>
          </Benefit>
        </div>
      </div>
    </section>
  )
}
