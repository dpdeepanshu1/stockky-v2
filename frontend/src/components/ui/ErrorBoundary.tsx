import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "../kit/Button";
import { isChunkLoadError, reloadOnceForNewVersion } from "../../chunkReload";

// group 443: a React component that throws while rendering unmounts the WHOLE app (blank page). Wrapped around a tab, a crash shows this card
// (message + Try again) and the rest of the app, navigation included, keeps working.
export default class ErrorBoundary extends Component<{ children: ReactNode; label?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.label || "tab"}] render crash`, error, info.componentStack);
    if (isChunkLoadError(error)) reloadOnceForNewVersion(); // E564: a page file from an older build is gone: load the new build once, automatically
  }
  render() {
    if (!this.state.error) return this.props.children;
    if (isChunkLoadError(this.state.error)) {
      // E564: "Try again" cannot help here (React.lazy keeps the failed import), only a reload fetches the new index.html
      return (
        <div className="rounded-2xl border border-signal-sell bg-graphite p-4 text-[12px]" role="alert">
          <div className="font-bold text-signal-sell">Stockky was updated</div>
          <p className="mt-1 text-mist">This tab is still running the previous version. Reload to get the new one.</p>
          <div className="mt-2"><Button variant="ghost" onClick={() => window.location.reload()}>Reload</Button></div>
        </div>
      );
    }
    return (
      <div className="rounded-2xl border border-signal-sell bg-graphite p-4 text-[12px]" role="alert">
        <div className="font-bold text-signal-sell">{this.props.label || "This page"} hit an error and was stopped</div>
        <p className="mt-1 text-mist">{String(this.state.error.message || this.state.error)}</p>
        <div className="mt-2"><Button variant="ghost" onClick={() => this.setState({ error: null })}>Try again</Button></div>
      </div>
    );
  }
}
