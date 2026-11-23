/** Temporary destination for account links until authentication ships (Phase 2/7). */
export function ComingSoonPage({ action }: { action: 'sign-in' | 'sign-up' }) {
  return (
    <main className="page">
      <section className="card" aria-labelledby="coming-soon-title">
        <h1 id="coming-soon-title">{action === 'sign-in' ? 'Sign in' : 'Create your workspace'} is coming soon</h1>
        <p>
          Accounts and workspaces are being built right now. In the meantime you can explore the product tour or check
          the platform status.
        </p>
        <p style={{ display: 'flex', gap: 12, marginTop: 20 }}>
          <a className="button" href="/">
            Back to home
          </a>
          <a className="button" href="/status">
            System status
          </a>
        </p>
      </section>
    </main>
  )
}
