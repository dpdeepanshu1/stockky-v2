import { Component, type ErrorInfo, type ReactNode } from "react";

// group 443: a React component that throws while rendering unmounts the WHOLE app (blank page). Wrapped around a tab, a crash shows this card
// (message + Try again) and the rest of the app, navigation included, keeps working.
export default class ErrorBoundary extends Component<{ children: ReactNode; label?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`[${this.props.label || "tab"}] render crash`, error, info.componentStack); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="rounded-2xl border border-signal-sell bg-graphite p-4 text-[12px]" role="alert">
        <div className="font-bold text-signal-sell">{this.props.label || "This page"} hit an error and was stopped</div>
        <p className="mt-1 text-mist">{String(this.state.error.message || this.state.error)}</p>
        <button type="button" className="btn-terminal mt-2" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
