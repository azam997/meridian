// Blocking popup shown while a *priority* reference warm is in flight — the
// player's saved job on launch, or a job they just confirmed on the setup
// screen. Silent background warming does NOT show this (blocking.active=false).
//
// Reuses the LoadingView visual vocabulary (`loader`, `bar`, `tasks`, …) so
// the per-reference task bars look identical to the analysis loader; only the
// overlay wrapper is new (see .refs-modal-* in main.css).

import type { WarmBlocking } from '../state/refsPrefetch';

const STATE_LABEL: Record<string, string> = {
  pending: 'queued',
  in_flight: 'downloading',
  done: 'done',
  failed: 'failed',
};

export const RefsLoadingModal = ({ blocking }: { blocking: WarmBlocking }) => {
  if (!blocking.active) return null;

  const width = Math.min(95, blocking.pct || 0);
  const tasks = blocking.tasks;
  const hasTasks = !!tasks && tasks.length > 0;
  const inFlight = tasks?.filter((t) => t.state === 'in_flight').length ?? 0;

  return (
    <div className="refs-modal-overlay" role="dialog" aria-modal="true" aria-busy="true">
      <div className={`refs-modal loader${hasTasks ? ' wide' : ''}`}>
        <img className="pulse" src="/meridian.svg" alt="" draggable={false} />
        <div className="label">{blocking.stage || `Loading ${blocking.job ?? ''} references…`}</div>
        <span className="net-tag">
          <span className="dot" />
          {hasTasks
            ? `FFLogs · ${inFlight} concurrent request${inFlight === 1 ? '' : 's'}`
            : 'FFLogs · network'}
        </span>
        <div className="bar main-bar">
          <div className="fill" style={{ width: `${width}%` }} />
        </div>
        {hasTasks && (
          <div className="tasks">
            {tasks!.map((t, i) => (
              <div key={i} className={`task ${t.state}`}>
                <span className="glyph" />
                <span className="name" title={t.label}>{t.label}</span>
                <span className={`state ${t.state}`}>{STATE_LABEL[t.state] ?? t.state}</span>
                <div className="mini-bar">
                  <div className="mini-fill" />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
