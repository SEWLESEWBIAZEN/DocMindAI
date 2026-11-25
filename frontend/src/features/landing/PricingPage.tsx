import './landing.css'

import { useEffect } from 'react'

import { FinalCta } from './sections/FinalCta'
import { Pricing } from './sections/Pricing'
import { SiteFooter } from './sections/SiteFooter'
import { SiteHeader } from './sections/SiteHeader'

export function PricingPage() {
  useEffect(() => {
    document.title = 'Pricing · DocuMind AI'
  }, [])
  return (
    <div className="lp">
      <a href="#main" className="lp-skip">
        Skip to content
      </a>
      <SiteHeader />
      <main id="main">
        <Pricing full />
        <FinalCta />
      </main>
      <SiteFooter />
    </div>
  )
}
