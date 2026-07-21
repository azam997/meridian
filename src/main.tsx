import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

// Self-hosted Geist + Geist Mono — works offline once the app ships.
import '@fontsource/geist/400.css';
import '@fontsource/geist/500.css';
import '@fontsource/geist/600.css';
import '@fontsource/geist/700.css';
import '@fontsource/geist-mono/400.css';
import '@fontsource/geist-mono/500.css';
import '@fontsource/geist-mono/600.css';

import './styles/main.css';
import App from './App';
import { ErrorBoundary } from './components/ErrorBoundary';
import { logEvent } from './log';

// Global crash hooks → the sidecar event log (render crashes are caught by
// ErrorBoundary; these get everything else: async handlers, listeners…).
window.onerror = (message, source, lineno, colno, error) => {
  logEvent('error', 'window', String(message), {
    source: source ?? '', line: lineno ?? 0, col: colno ?? 0,
    stack: error?.stack ?? '',
  });
};
window.addEventListener('unhandledrejection', (ev) => {
  const reason = ev.reason as { message?: string; stack?: string } | undefined;
  logEvent('error', 'unhandled_rejection',
           String(reason?.message ?? ev.reason ?? 'unknown'),
           { stack: reason?.stack ?? '' });
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
