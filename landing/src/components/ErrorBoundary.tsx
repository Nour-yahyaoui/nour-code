import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode; label: string };
type State = { error: Error | null };

/**
 * The last line of defence from the UX skill: wrap each section, not just the
 * app root, so one broken widget cannot blank the whole page.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // No tracker on a local-first page: the console is the report.
    console.error(`[${this.props.label}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="boundary" role="alert">
        <p className="boundary__title">This panel failed to render.</p>
        <p className="boundary__body">
          The rest of the page is unaffected. {this.props.label} can be retried without a reload.
        </p>
        <button type="button" className="btn btn--ghost" onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </div>
    );
  }
}
