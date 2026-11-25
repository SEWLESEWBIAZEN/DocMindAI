import { useId, useState } from 'react'

import { COMPARISON, highlights, planAction, priceLine, usePlans, type Interval, type PlanCatalog } from '../pricing/catalog'
import { Icon } from '../components/Icon'
import { Reveal } from '../components/Reveal'
import { SectionHeading } from '../components/SectionHeading'

export function IntervalToggle({
  value,
  onChange,
  months,
}: {
  value: Interval
  onChange: (value: Interval) => void
  months: number
}) {
  const name = useId()
  return (
    <fieldset className="lp-interval">
      <legend className="sr-only">Billing period</legend>
      {(['month', 'year'] as const).map((option) => (
        <label key={option} className="lp-interval__option" data-checked={value === option || undefined}>
          <input type="radio" name={name} value={option} checked={value === option} onChange={() => onChange(option)} />
          {option === 'month' ? 'Monthly' : 'Yearly'}
          {option === 'year' && <span className="lp-tag lp-tag--accent">{12 - months} months free</span>}
        </label>
      ))}
    </fieldset>
  )
}

function PlanCards({ catalog, interval }: { catalog: PlanCatalog; interval: Interval }) {
  return (
    <ul className="lp-plans">
      {catalog.plans.map((plan) => {
        const price = priceLine(plan, interval)
        const action = planAction(plan, catalog, interval)
        const featured = plan.key === 'business'
        return (
          <li key={plan.key}>
            <div className={`lp-plan${featured ? ' lp-plan--featured' : ''}`}>
              <div className="lp-plan__head">
                <h3 id={`plan-${plan.key}`}>{plan.name}</h3>
                {featured && <span className="lp-tag lp-tag--accent">Recommended for teams</span>}
              </div>
              <p className="lp-plan__desc">{plan.description}</p>
              <p className="lp-plan__price">
                <strong>{price.amount}</strong> <span>{price.unit}</span>
              </p>
              <p className="lp-plan__note">
                {plan.trial_days > 0 ? `${plan.trial_days} days free, no card needed` : (price.note ?? '\u00a0')}
              </p>
              <a
                href={action.href}
                className={`lp-btn ${featured ? 'lp-btn--primary' : 'lp-btn--ghost'}`}
                aria-describedby={`plan-${plan.key}`}
              >
                {action.label}
              </a>
              <ul className="lp-plan__list" aria-label={`${plan.name} includes`}>
                {highlights(plan).map((item) => (
                  <li key={item}>
                    <Icon name="check" size={16} />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

function Comparison({ catalog }: { catalog: PlanCatalog }) {
  return (
    <div className="lp-compare">
      <table>
        <caption className="sr-only">Compare plans</caption>
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Feature</span>
            </th>
            {catalog.plans.map((p) => (
              <th key={p.key} scope="col">
                {p.name}
              </th>
            ))}
          </tr>
        </thead>
        {COMPARISON.map((group) => (
          <tbody key={group.group}>
            <tr className="lp-compare__group">
              <th scope="colgroup" colSpan={catalog.plans.length + 1}>
                {group.group}
              </th>
            </tr>
            {group.rows.map((row) => (
              <tr key={row.label}>
                <th scope="row">{row.label}</th>
                {catalog.plans.map((p) => {
                  const value = row.value(p)
                  return (
                    <td key={p.key}>
                      {value === true ? (
                        <>
                          <Icon name="check" size={16} />
                          <span className="sr-only">Included</span>
                        </>
                      ) : value === false ? (
                        <span aria-label="Not included">—</span>
                      ) : (
                        value
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  )
}

const FAQ = [
  {
    q: 'How are members counted?',
    a: 'Paid plans are priced per member of the workspace. When people join or leave, the next invoice is adjusted automatically and prorated.',
  },
  {
    q: 'What do I save with yearly billing?',
    a: 'Yearly billing charges ten months for twelve: two months free on Pro and Business.',
  },
  {
    q: 'Can we keep documents in our own storage or database?',
    a: 'Yes. Pro lets you store files in your own S3, MinIO or Azure Blob storage. Business adds a dedicated database hosted by us, or a connection to your own PostgreSQL. Enterprise adds processing on your own servers.',
  },
  {
    q: 'What happens if we cancel or move to a smaller plan?',
    a: "Nothing is deleted. If your workspace uses storage or a database the smaller plan doesn't include, it keeps working and we ask you to move the data back or upgrade. Switching down online is only possible once nothing depends on it.",
  },
  {
    q: 'Do you store card details?',
    a: 'No. Payments are handled by Stripe; DocuMind never sees or stores card numbers.',
  },
  {
    q: 'Can I use my own AI key on any plan?',
    a: "Yes, including Free. Usage on your own key isn't counted against your plan's AI limits.",
  },
]

export function Pricing({ full = false }: { full?: boolean }) {
  const { data, error } = usePlans()
  const [interval, setInterval] = useState<Interval>('year')
  const Heading = full ? 'h1' : undefined
  return (
    <section className={`lp-section${full ? ' lp-section--page' : ' lp-section--tint'}`} id="pricing" aria-labelledby="pricing-title">
      <div className="lp-container">
        {Heading ? (
          <Reveal className="lp-heading lp-heading--center">
            <p className="lp-eyebrow">Pricing</p>
            <h1 id="pricing-title">Simple plans that grow with your team</h1>
            <p className="lp-lead">
              Start free. Pay per member when you need more room, your own storage or your own database.
            </p>
          </Reveal>
        ) : (
          <SectionHeading
            id="pricing-title"
            eyebrow="Pricing"
            title="Simple plans that grow with your team"
            lead="Start free. Pay per member when you need more room, your own storage or your own database."
          />
        )}
        {data ? (
          <>
            <div className="lp-pricing__controls">
              <IntervalToggle value={interval} onChange={setInterval} months={data.annual_months_charged} />
            </div>
            <PlanCards catalog={data} interval={interval} />
            <p className="lp-footnote lp-pricing__footnote">
              Prices in US dollars, excluding taxes. Your own AI key works on every plan.
              {!full && (
                <>
                  {' '}
                  <a href="/pricing">Compare every feature</a>
                </>
              )}
            </p>
            {full && (
              <>
                <h2 className="lp-pricing__subhead">Compare plans</h2>
                <Comparison catalog={data} />
                <h2 className="lp-pricing__subhead">Questions</h2>
                <dl className="lp-faq">
                  {FAQ.map((item) => (
                    <div key={item.q}>
                      <dt>{item.q}</dt>
                      <dd>{item.a}</dd>
                    </div>
                  ))}
                </dl>
              </>
            )}
          </>
        ) : error ? (
          <p className="lp-pricing__status" role="status">
            Prices couldn't be loaded right now. <a href="/register">Start free</a> and upgrade any time.
          </p>
        ) : (
          <p className="lp-pricing__status" role="status">
            Loading plans…
          </p>
        )}
      </div>
    </section>
  )
}
