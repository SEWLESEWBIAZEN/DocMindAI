import './landing.css'

import { Benefits } from './sections/Benefits'
import { CapabilityStrip } from './sections/CapabilityStrip'
import { FinalCta } from './sections/FinalCta'
import { Hero } from './sections/Hero'
import { HowItWorks } from './sections/HowItWorks'
import { Security } from './sections/Security'
import { SiteFooter } from './sections/SiteFooter'
import { SiteHeader } from './sections/SiteHeader'
import { UseCases } from './sections/UseCases'

export function LandingPage() {
  return (
    <div className="lp">
      <a href="#main" className="lp-skip">
        Skip to content
      </a>
      <SiteHeader />
      <main id="main">
        <Hero />
        <CapabilityStrip />
        <Benefits />
        <UseCases />
        <HowItWorks />
        <Security />
        <FinalCta />
      </main>
      <SiteFooter />
    </div>
  )
}
