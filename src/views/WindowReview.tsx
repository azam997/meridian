import type { ReactNode } from 'react';
import { fmtClock } from '../format';

/** One auto-detected, situational window the user confirms or denies. `potency`
 *  is what the caller removes from the idealized ceiling when the window is
 *  denied (marked "not possible"). `badge` is an optional evidence chip on the
 *  row (the multi-target geometry verdict). */
export type ReviewWindow = {
  id: string;
  timeSec: number;
  potency: number;
  title: string;
  detail?: string;
  badge?: { label: string; tone: 'good' | 'bad'; title?: string };
};

type Props = {
  icon?: ReactNode;
  heading: string;
  blurb?: ReactNode;
  windows: ReviewWindow[];
  /** ids the user has toggled away from the default (see `defaultDenied`). */
  denied: Set<string>;
  onToggle: (id: string) => void;
  onJump?: (timeSec: number) => void;
  /** Noun for the denied-potency summary ("ceiling" for one-sided ceiling
   *  adjustments like Flamethrower; "splash" for the two-sided multi-target
   *  credit). Defaults to "ceiling". */
  potencyNoun?: string;
  /** Optional control rendered at the top of the card body (the multi-target
   *  crediting-mode selector). */
  modeSelector?: ReactNode;
  /** When true, windows default to "not possible" and a `denied` entry means
   *  "restored to possible" (the crediting mode's Not-possible default). The
   *  effective per-row state is `denied.has(id) XOR defaultDenied`. */
  defaultDenied?: boolean;
};

/**
 * Reusable per-window confirmation panel.
 *
 * The analyzer detects windows it can't fully reason about (e.g. a Flamethrower
 * downtime-edge squeeze: computable timing gates pass, but whether there's an
 * add to hit instead, or fight-specific context, is uninferable). Each window
 * defaults to *counted* (the sim assumed it); the user marks the ones that
 * weren't actually possible, and the caller subtracts their potency from the
 * ceiling. Deliberately generic so the same control can drive other situational
 * reviews later (e.g. confirming consensus forced-downtime windows).
 */
export const WindowReview = ({
  icon, heading, blurb, windows, denied, onToggle, onJump, potencyNoun = 'ceiling',
  modeSelector, defaultDenied = false,
}: Props) => {
  if (windows.length === 0) return null;
  const isRowDenied = (id: string) => denied.has(id) !== defaultDenied;
  const deniedP = windows.reduce((a, w) => a + (isRowDenied(w.id) ? w.potency : 0), 0);
  const counted = windows.filter((w) => !isRowDenied(w.id)).length;
  return (
    <div className="card window-review">
      <div className="card-head">
        {icon}
        <h2>{heading}</h2>
        <span className="sub" style={{ marginLeft: 'auto' }}>
          {counted}/{windows.length} counted
          {deniedP > 0 ? ` · ${potencyNoun} −${deniedP.toLocaleString()}p` : ''}
        </span>
      </div>
      <div className="card-body">
        {modeSelector}
        {blurb && (
          <p className="mut" style={{ fontSize: 12.5, margin: '0 0 10px' }}>{blurb}</p>
        )}
        <div className="wr-list">
          {windows.map((w) => {
            const isDenied = isRowDenied(w.id);
            return (
              <div className={`wr-row${isDenied ? ' denied' : ''}`} key={w.id}>
                <button
                  className="wr-loc"
                  onClick={() => onJump?.(w.timeSec)}
                  title={onJump ? `Jump to ${fmtClock(w.timeSec)} on the timeline` : undefined}
                >
                  <span className="wr-time mono">{fmtClock(w.timeSec)}</span>
                  <span className="wr-title">{w.title}</span>
                  {w.badge && (
                    <span className={`wr-badge ${w.badge.tone}`} title={w.badge.title}>
                      {w.badge.label}
                    </span>
                  )}
                  {w.detail && <span className="wr-detail mut">{w.detail}</span>}
                </button>
                <div className="wr-toggle">
                  <button
                    className={!isDenied ? 'on' : ''}
                    onClick={() => { if (isDenied) onToggle(w.id); }}
                  >
                    Possible
                  </button>
                  <button
                    className={isDenied ? 'on bad' : ''}
                    onClick={() => { if (!isDenied) onToggle(w.id); }}
                  >
                    Not possible
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
