// Numeric formatting helpers shared across views.
//
// Backend (Python sidecar) emits raw floats for potency, drift seconds,
// percentages, etc. — `maximumFractionDigits` caps display precision and
// also drops trailing zeros (87.0 → "87", 87.50 → "87.5").

export const fmtNum = (n: number, max = 2): string =>
  n.toLocaleString(undefined, { maximumFractionDigits: max });

/** m:ss clock. Minutes are floored; seconds are floored by default, or rounded
 *  to the nearest second when `round` is set (the timeline axis rounds, every
 *  other caller floors). Replaces the per-view fmtTime/fmtKillTime/fmtImpTime/
 *  fmtAxisTick copies. */
export const fmtClock = (sec: number, round = false): string =>
  `${Math.floor(sec / 60)}:${String((round ? Math.round(sec) : Math.floor(sec)) % 60).padStart(2, '0')}`;

/** Human duration: "1.5m" at/over a minute, else "12.3s". */
export const fmtDuration = (sec: number): string =>
  sec >= 60 ? `${(sec / 60).toFixed(1)}m` : `${sec.toFixed(1)}s`;
