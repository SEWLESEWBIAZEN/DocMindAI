import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** Changing this resets the boundary (e.g. the current path). */
  resetKey?: string
  fallback?: (reset: () => void) => ReactNode
}

interface State {
  error: Error | null
  resetKey?: string
}

/** Catches render errors so one broken view never blanks the whole app. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, resetKey: this.props.resetKey }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.resetKey) return { resetKey: props.resetKey, error: null }
    return null
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Unhandled UI error', error, info.componentStack)
  }

  reset = () => this.setState({ error: null })

  render() {
    if (!this.state.error) return this.props.children
    if (this.props.fallback) return this.props.fallback(this.reset)
    return (
      <div className="error-state error-state--page" role="alert">
        <div>
          <h2 className="error-state__title">Something went wrong</h2>
          <p className="error-state__message">This view hit an unexpected error. Your data is safe.</p>
        </div>
        <div className="row">
          <button type="button" className="btn btn--secondary btn--md" onClick={this.reset}>
            Try again
          </button>
          <a className="btn btn--ghost btn--md" href="/app">
            Go to dashboard
          </a>
        </div>
      </div>
    )
  }
}
