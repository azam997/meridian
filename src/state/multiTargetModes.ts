// Multi-target crediting modes — the pure math + shared helpers behind the
// dashboard's mode selector and the per-window Possible/Not-possible overrides.
//
// The mode picks the GRADING BASIS for the confirmed multi-target windows and
// everything recomputes client-side from the per-window deltas the backend
// ships on each `MultiTargetWindow` (deliveredSplash/ceilingSplash for you,
// refDeliveredSplash/refCeilingSplash per reference, refAvgDeliveredSplash).
// No sidecar round trip: the backend's own numbers — and therefore the
// `ceiling_anomaly` watchdog and its event log — never see a capped ceiling,
// so the cap modes' intentional >100% displays produce zero log noise.
//
//   off     — every window defaults to "not possible": splash removed from
//             both sides (delivered and ceiling), grading single-target.
//   maximal — the default and today's behavior verbatim: full credit at the
//             model's maximally-possible N.
//   top10   — each window's ceiling contribution is capped at the references'
//             AVERAGE credited splash there. Outliers (you or a ref) who
//             cleaved more than the average register over 100%.
//   player  — each window's ceiling contribution is capped at YOUR credited
//             splash there (your gap from that window goes to zero). Top-10
//             references who out-cleaved you register over 100%.
//
// The `deniedWindows` set holds per-window OVERRIDES relative to the mode's
// default: under every mode but 'off' an entry means "not possible"; under
// 'off' an entry means "restored to possible" (at full maximal credit).

import type { AnalysisResult, Improvement, MultiTargetWindow } from '../sidecar/contract';

export type MtMode = 'off' | 'maximal' | 'top10' | 'player';

export const MT_MODE_DEFAULT: MtMode = 'maximal';

/** Stable id for a multi-target window in the shared `deniedWindows` set
 *  (namespaced apart from the ceiling squeezes' `ft@` ids). */
export const multiTargetWindowId = (startSec: number): string =>
  `mt@${startSec.toFixed(1)}`;

/** Effective per-window denial: the set XOR the mode's default. */
export const isWindowDenied = (
  id: string,
  denied: Set<string>,
  mode: MtMode,
): boolean => denied.has(id) !== (mode === 'off');

/** Window ids the geometry advisory marked unreachable — the auto-deny
 *  defaults seeded into `deniedWindows` when an analysis lands (the user can
 *  toggle any of them back on). */
export const unreachableWindowIds = (analysis: AnalysisResult): string[] =>
  (analysis.headline.multiTargetWindows ?? [])
    .filter((w) => w.cleaveGeometry?.verdict === 'unreachable')
    .map((w) => multiTargetWindowId(w.startSec));

/** This run's own delivered-splash delta for a window (you when `refIdx` is
 *  undefined, else that reference's). Missing ref arrays (older responses,
 *  sparse mocks) degrade to 0 — "no adjustment", never NaN. */
const windowDelivered = (w: MultiTargetWindow, refIdx?: number): number =>
  refIdx == null ? w.deliveredSplash : (w.refDeliveredSplash?.[refIdx] ?? 0);

const windowCeiling = (w: MultiTargetWindow, refIdx?: number): number =>
  refIdx == null ? w.ceilingSplash : (w.refCeilingSplash?.[refIdx] ?? 0);

/** The capped ceiling contribution a counted window makes under a cap mode.
 *  Both caps are SHARED by every run — that is what lets a run's own delivered
 *  splash exceed it (the warned-about >100%). */
const windowCap = (mode: MtMode, w: MultiTargetWindow): number =>
  mode === 'player'
    ? w.deliveredSplash
    : (w.refAvgDeliveredSplash ?? w.ceilingSplash);

export type RunBasis = {
  delivered: number;
  idealized: number;
  /** `RunSummary.multiTargetCredited` — false means the pair is single-target
   *  and the splash-inclusive pair must be reconstructed first. */
  credited: boolean;
};

/** Recompute one run's (delivered, idealized, efficiency) under a mode + the
 *  per-window overrides. Under 'maximal' with an empty override set this is
 *  arithmetically the identity (subtracts nothing). */
export const adjustRun = (
  base: RunBasis,
  windows: MultiTargetWindow[],
  denied: Set<string>,
  mode: MtMode,
  refIdx?: number,
): { delivered: number; idealized: number; eff: number } => {
  let d = base.delivered;
  let c = base.idealized;
  // An uncredited run's pair is single-target — rebuild the splash-inclusive
  // pair so every mode grades all runs on one basis. (This is exactly the run
  // the >100% guard refused to credit, so the rebuilt ratio may exceed 100% —
  // accepted and warned under the cap modes.)
  if (!base.credited) {
    for (const w of windows) {
      d += windowDelivered(w, refIdx);
      c += windowCeiling(w, refIdx);
    }
  }
  for (const w of windows) {
    if (isWindowDenied(multiTargetWindowId(w.startSec), denied, mode)) {
      d -= windowDelivered(w, refIdx);
      c -= windowCeiling(w, refIdx);
    } else if (mode === 'top10' || mode === 'player') {
      c -= windowCeiling(w, refIdx) - windowCap(mode, w);
    }
  }
  d = Math.max(0, d);
  c = Math.max(0, c);
  return { delivered: d, idealized: c, eff: c > 0 ? (d / c) * 100 : 0 };
};

/** Rank/percentile of `youEff` against the (recomputed) ref efficiencies —
 *  mirrors sidecar/main.py::_headline so the "Nth percentile · rank …" line
 *  stays consistent when a cap mode reshuffles the field. */
export const rankAgainst = (
  youEff: number,
  refEffs: number[],
): { percentile: number; rank: number; total: number; beat: number } => {
  const pop = [...refEffs, youEff].sort((a, b) => b - a);
  const rank = pop.indexOf(youEff) + 1;
  const total = pop.length;
  const beat = refEffs.filter((e) => youEff > e).length;
  const percentile =
    total > 1 ? Math.round(100 * (1 - (rank - 1) / (total - 1))) : 100;
  return { percentile, rank, total, beat };
};

/** Swap the "~Np" figure inside a backend improvement summary for the repriced
 *  one (the prose framing stays valid — only the magnitude changed). */
const repriceSummary = (summary: string, lost: number): string =>
  summary.replace(/~[\d,]+p/, `~${Math.round(lost)}p`);

/** Reprice the grouped "multitarget" improvement card to match the mode:
 *  maximal → untouched (byte-identical); top10 → each counted window priced at
 *  max(0, refAvg − yourDelivered); player → your gap is zero, card dropped;
 *  off → dropped except windows the user restored, priced at maximal. Denied
 *  windows always drop their child. Other cards pass through untouched. */
export const repriceImprovements = (
  improvements: Improvement[],
  windows: MultiTargetWindow[],
  denied: Set<string>,
  mode: MtMode,
): Improvement[] => {
  if (mode === 'maximal') return improvements;
  return improvements.flatMap((im) => {
    if (im.kind !== 'multitarget') return [im];
    const children = (im.children ?? []).flatMap((ch) => {
      const w = windows.find((x) => Math.abs(x.startSec - ch.timeSec) < 0.05);
      if (!w) return [ch];
      if (isWindowDenied(multiTargetWindowId(w.startSec), denied, mode)) return [];
      const lost =
        mode === 'player'
          ? 0
          : mode === 'top10'
            ? Math.max(
                0,
                (w.refAvgDeliveredSplash ?? w.ceilingSplash) - w.deliveredSplash,
              )
            : w.ceilingSplash - w.deliveredSplash; // 'off' + restored → maximal
      if (lost <= 0) return [];
      return [{ ...ch, lostPotency: lost, summary: repriceSummary(ch.summary, lost) }];
    });
    const total = children.reduce((a, ch) => a + ch.lostPotency, 0);
    if (children.length === 0 || total <= 0) return [];
    return [
      {
        ...im,
        lostPotency: total,
        children,
        summary: im.summary.replace(
          /across \d+ windows?/,
          `across ${children.length} window${children.length === 1 ? '' : 's'}`,
        ),
      },
    ];
  });
};
