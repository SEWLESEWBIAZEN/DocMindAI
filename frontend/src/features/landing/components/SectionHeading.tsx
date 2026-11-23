import type { ReactNode } from 'react'

import { Reveal } from './Reveal'

interface SectionHeadingProps {
  id: string
  eyebrow: string
  title: ReactNode
  lead?: string
  align?: 'center' | 'start'
}

export function SectionHeading({ id, eyebrow, title, lead, align = 'center' }: SectionHeadingProps) {
  return (
    <Reveal className={`lp-heading lp-heading--${align}`}>
      <p className="lp-eyebrow">{eyebrow}</p>
      <h2 id={id}>{title}</h2>
      {lead && <p className="lp-lead">{lead}</p>}
    </Reveal>
  )
}
