// Last-resort UI crash catcher. Logs the crash to the sidecar event log
// (ui_crash) and shows a reload card instead of React's blank white screen.
// Render errors only — async/global errors are caught in main.tsx.

import { Component, type ErrorInfo, type ReactNode } from 'react';
import { logEvent } from '../log';

type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    logEvent('error', 'ui_crash', error.message, {
      stack: error.stack ?? '',
      componentStack: info.componentStack ?? '',
    });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{
        height: '100vh', display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: 24,
      }}>
        <div className="card" style={{ maxWidth: 460, textAlign: 'center', padding: 24 }}>
          <h2>Something went wrong</h2>
          <p className="mut" style={{ margin: '12px 0 20px' }}>
            The view crashed unexpectedly. The error was recorded in the app's
            event log — if it keeps happening, please send it our way via
            Submit Feedback after reloading.
          </p>
          <button className="btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
