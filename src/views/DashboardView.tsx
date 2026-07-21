import {
  Activity,
  Clock,
  Crosshair,
  Layers,
  Lightbulb,
  MessageSquare,
  RefreshCw,
} from 'lucide-react';
import { AbilityIcon } from '../components/AbilityIcon';
import { DistroChart } from '../components/DistroChart';
import { KPI } from '../components/KPI';
import { StatRing } from '../components/StatRing';
import { fmtClock, fmtNum } from '../format';
import { getJobProfile } from '../jobs';
import type {
  AnalysisResult,
  BuffDriftAspectState,
  ClippingAspectState,
  DriftAspectState,
} from '../sidecar/contract';
import type { View } from '../state/appState';
import {
  adjustRun,
  multiTargetWindowId,
  rankAgainst,
  repriceImprovements,
  type MtMode,
} from '../state/multiTargetModes';
import { DowntimePanel } from './DowntimePanel';
import { categorizeImprovements, tagBadge } from './findings';
import { ImprovementRow } from './ImprovementRow';
import { JobPanels } from './jobPanels';
import { PhasePanel } from './PhasePanel';
import { WindowReview, type ReviewWindow } from './WindowReview';
import {
  ceilingGroups,
  ceilingNoun,
  deniedCeilingCount,
  deniedCeilingPotency,
  groupMeta,
  isImprovementDenied,
} from './reviewableWindows';

/** Parse-style colour tiers for the efficiency hero, high → low. Scaled to the
 *  compressed range efficiency actually lives in (delivered ÷ sim is rarely
 *  below ~90 even on a rough pull) rather than raw 0–100, so the colours
 *  separate real performance. First bin the value clears wins. Below 80 trips
 *  the red "trolling" easter egg. */
const EFFICIENCY_TIERS: ReadonlyArray<readonly [number, string]> = [
  [99.75, '#e5cc80'], // gold   — ~perfect
  [99, '#e268a8'],    // pink   — 99–99.74
  [98, '#ff8000'],    // orange — 98–98.99
  [96.5, '#a335ee'],  // purple — 96.5–97.99
  [94, '#3d9bfd'],    // blue   — 94–96.49
  [92, '#1eff00'],    // green  — 92–93.99
  [80, '#9aa0a6'],    // gray   — 80–91.99 (a ~5-parse 90% lands here)
  [0, '#ff3b3b'],     // red    — sub-80 easter egg
];
const EFFICIENCY_FLOOR = 80;   // below this is the "trolling" easter egg
const efficiencyColor = (pct: number): string =>
  (EFFICIENCY_TIERS.find(([min]) => pct >= min) ?? EFFICIENCY_TIERS[EFFICIENCY_TIERS.length - 1])[1];

/** Cosmetic ring-fill remap: spread efficiency's compressed range across the
 *  gauge and always leave a visible gap (cap < 100), so 90 vs 98 read distinctly
 *  and even a near-perfect run never fully closes the ring. The displayed
 *  number stays the true efficiency — this only drives the arc length. */
const RING_FLOOR_FILL = 8;     // % fill at the efficiency floor
const RING_MAX_FILL = 95;      // % fill cap at 100 (still leaves a gap; a ~99 pink pull reads nearly full)
const efficiencyRingFill = (pct: number): number => {
  if (pct <= EFFICIENCY_FLOOR) return Math.max(0, (pct / EFFICIENCY_FLOOR) * RING_FLOOR_FILL);
  const t = Math.min(1, (pct - EFFICIENCY_FLOOR) / (100 - EFFICIENCY_FLOOR));
  return RING_FLOOR_FILL + t * (RING_MAX_FILL - RING_FLOOR_FILL);
};

type Props = {
  analysis: AnalysisResult;
  setView: (v: View) => void;
  job: string;
  /** Re-run the current analysis (relocated from the old shared topbar). */
  onRerun: () => void;
  /** Jump to a time in the Timeline (click target of a Potential Improvement).
   *  `opts` carries the originating card's kind/ability so the Timeline can
   *  highlight the right target (ideal-lane cast, death flag, matching cast). */
  onJumpToTime: (timeSec: number, opts?: { kind?: string; abilityId?: number }) => void;
  /** Per-window overrides relative to the crediting mode's default (see
   *  state/multiTargetModes.ts; also carries the ceiling squeezes' plain
   *  denials). */
  deniedWindows: Set<string>;
  onToggleWindow: (id: string) => void;
  /** Multi-target crediting mode (global, per-analysis). */
  mtMode: MtMode;
  onSetMtMode: (m: MtMode) => void;
  /** Open Submit Feedback prefilled with the over-ceiling anomaly (the
   *  headline `ceilingAnomaly` nudge's button). */
  onReportAnomaly: () => void;
};

const fmtPotency = (p: number) => fmtNum(p);

export const DashboardView = ({
  analysis: a, setView, job, onRerun, onJumpToTime, deniedWindows, onToggleWindow,
  mtMode, onSetMtMode, onReportAnomaly,
}: Props) => {
  const h = a.headline;
  // For a prog wipe on a phased fight (ultimates), name the phase the player
  // wiped in from the phase segments — preferring the phase that contains the
  // terminal death, else the one matching lastPhase by id. Null when there are
  // no phase segments (Savage); the compact fallback stays "P{lastPhase}".
  const wipePhaseName: string | null = (() => {
    if (!h.isProgPull) return null;
    const phases = a.phases ?? [];
    const t = h.terminalDeathSec;
    const byTime = t != null
      ? phases.find((p) => t >= p.startSec && t < p.endSec)
      : undefined;
    const byId = h.lastPhase ? phases.find((p) => p.id === h.lastPhase) : undefined;
    return (byTime ?? byId)?.name ?? (h.lastPhase ? `P${h.lastPhase}` : null);
  })();
  const clip = a.aspectStates.Clipping as ClippingAspectState | undefined;
  const driftState = a.aspectStates.Drift as DriftAspectState | undefined;
  const buffDrift = a.aspectStates.BuffDrift as
    | BuffDriftAspectState
    | undefined;
  const buffDriftFindings = buffDrift?.findings ?? [];

  const drift = driftState?.findings ?? [];
  // Unified Potential Improvements — a located, ranked decomposition of the
  // recoverable gap, already reconciled to sum to it on the backend.
  const improvements = a.improvements ?? [];

  // Phase 1 multi-target disclaimer. When the backend flags this pull as
  // multi-target (>= 2 enemies targetable for a sustained span), the
  // single-target model understates output: badge the efficiency KPI and
  // replace the (backend-suppressed) improvements panel with a disclaimer.
  const multiTargetDisclaimed = h.multiTargetDisclaimed ?? false;

  // Situational ceiling-only squeezes the sim credited (e.g. MCH's Flamethrower
  // downtime-edge tick) — shipped from the backend as data. They're confirmed/
  // denied in the generic WindowReview panels below; a denied one is removed
  // from the idealized ceiling, which lifts the efficiency it's the denominator
  // of. `deniedCeiling` is that removed potency, summed across all groups.
  const reviewable = a.reviewableWindows;
  const deniedCeiling = deniedCeilingPotency(reviewable, deniedWindows);

  // Confirmed multi-target windows (when the backend credited splash). The
  // crediting mode + per-window overrides pick the grading basis: a denied
  // window's splash comes off BOTH sides (delivered and ceiling), a cap mode
  // swaps each counted window's ceiling contribution for the cap (top-10
  // average / your credited splash). `h.yourPotency` / `h.yourIdealizedPotency`
  // already carry the credited totals on a credited pull; the recompute is
  // entirely client-side (state/multiTargetModes.ts).
  const multiTargetCredited = h.multiTargetCredited ?? false;
  const mtWindows = h.multiTargetWindows ?? [];
  const mtReview: ReviewWindow[] = mtWindows.map((w) => ({
    id: multiTargetWindowId(w.startSec),
    timeSec: w.startSec,
    potency: Math.round(w.deliveredSplash),
    title: `Multi-target window ×${w.targetCount}`,
    detail: `${Math.round(w.endSec - w.startSec)}s · +${Math.round(w.deliveredSplash).toLocaleString()}p splash credited`,
    // Advisory geometry verdict from this pull's sampled enemy positions;
    // 'unknown' renders no chip. 'unreachable' windows arrive pre-denied
    // (App.tsx seeds them into deniedWindows) — the toggle stays live.
    badge:
      w.cleaveGeometry?.verdict === 'unreachable'
        ? { label: '⛔ out of cleave range', tone: 'bad' as const, title: w.cleaveGeometry.detail }
        : w.cleaveGeometry?.verdict === 'reachable'
          ? { label: '✓ targets in range', tone: 'good' as const, title: w.cleaveGeometry.detail }
          : undefined,
  }));

  // Idealized ceiling + delivered + efficiency after the mode + overrides
  // (Flamethrower squeezes adjust the ceiling only; multi-target windows adjust
  // per the mode). Under 'maximal' with no overrides this is byte-identical to
  // the backend headline. The "Potential improvements" decomposition stays on
  // the raw sim gap under 'maximal'; cap modes reprice it below.
  const youMtAdj = adjustRun(
    { delivered: h.yourPotency, idealized: h.yourIdealizedPotency, credited: true },
    mtWindows, deniedWindows, mtMode,
  );
  const deliveredAdj = youMtAdj.delivered;
  const idealizedAdj = Math.max(0, youMtAdj.idealized - deniedCeiling);
  const adjEff = idealizedAdj > 0
    ? (deliveredAdj / idealizedAdj) * 100
    : h.efficiencyPct;
  // Ref efficiencies under the mode. 'maximal' keeps the backend numbers
  // verbatim (today's behavior, even with manual denials); the other modes
  // regrade every reference on the same basis as you — which is what makes
  // "top-10s over 100%" literally visible under the player cap.
  const refsAdjusted = mtMode !== 'maximal' && mtWindows.length > 0;
  const refEffAdj = (i: number): number =>
    adjustRun(
      {
        delivered: a.refs[i].deliveredPotency,
        idealized: a.refs[i].idealizedPotency,
        credited: a.refs[i].multiTargetCredited ?? true,
      },
      mtWindows, deniedWindows, mtMode, i,
    ).eff;
  const simBacked = h.yourIdealizedPotency > 0;
  // Sub-floor efficiency trips the easter egg (red + a ribbing message) — except
  // for jobs that supply a `lowEfficiencyNote` (healers, whose damage efficiency
  // legitimately drops when the fight forces healing GCDs), which surface that
  // constructive note instead of the ribbing.
  const belowFloor = simBacked && adjEff < EFFICIENCY_FLOOR;
  const lowEfficiencyNote = getJobProfile(job).lowEfficiencyNote;
  const trolling = belowFloor && !lowEfficiencyNote;
  // A missed-squeeze card whose window the user denied isn't a missed
  // opportunity (the squeeze wasn't possible), so hide it — the ceiling already
  // dropped above to match. The grouped multi-target card is then repriced to
  // the crediting mode ('maximal' passes through untouched).
  const shownImprovements = repriceImprovements(
    improvements.filter(
      (im) => !isImprovementDenied(reviewable, deniedWindows, im.abilityId, im.timeSec),
    ),
    mtWindows, deniedWindows, mtMode,
  );
  // Category sections for the panel — computed AFTER repricing + denial
  // filtering so the per-category subtotals agree with the cards. With fewer
  // than two non-empty categories the panel renders flat (headers on a
  // one-bucket list are noise; also keeps the clean-run path unchanged).
  const improvementCats = categorizeImprovements(
    shownImprovements,
    getJobProfile(job).improvementCategories,
  );
  const showImpCategories = improvementCats.length >= 2;

  // The measured loss budget the cards attribute: idealized_strict − delivered.
  // Anchors the panel so the itemized cards read as a decomposition, not a pile
  // of independent estimates. Only meaningful for sim-backed jobs (idealized>0).
  // Under a non-maximal crediting mode the budget follows the adjusted pair, so
  // the line agrees with the repriced cards (may read 0 under the player cap).
  const recoverable =
    mtMode !== 'maximal' && mtWindows.length > 0
      ? (idealizedAdj > 0 ? Math.max(0, idealizedAdj - deliveredAdj) : 0)
      : h.yourIdealizedPotency > 0
        ? Math.max(0, h.yourIdealizedPotency - h.yourPotency)
        : 0;
  const recoverableBase =
    mtMode !== 'maximal' && mtWindows.length > 0 ? idealizedAdj : h.yourIdealizedPotency;
  const recoverablePct = recoverableBase > 0 ? (recoverable / recoverableBase) * 100 : 0;

  const yourPotencyDelta = h.yourPotency - h.refAvgPotency;
  const killTimeDelta = h.killTimeSec - h.refKillTimeSec;

  // Rank/percentile/vs-refs comparisons are hidden for locked-healer runs
  // (rankSuppressed — heals forced into the ceiling) AND for prog (wipe)
  // pulls (isProgPull — a truncated wipe against kill references is never a
  // fair comparison). The two flags stay separate on the wire because each
  // implies its own framing card.
  const vsRefsHidden = !!h.rankSuppressed || !!h.isProgPull;

  // Effective-GCD pacing delta: time spent idle (the bulk) and true GCD
  // clipping from over-weaving, reported apart. "clean pacing" when neither
  // is material.
  const pacingDelta = (() => {
    const c = clip?.clipping;
    const idle = c?.totalIdleSec ?? 0;
    const clipped = c?.totalClipSec ?? 0;
    if (!c) return { dir: 'up' as const, text: 'no pacing data' };
    const parts: string[] = [];
    if (idle >= 0.5) parts.push(`${idle.toFixed(1)}s idle`);
    if (clipped >= 0.3) parts.push(`${clipped.toFixed(1)}s clip`);
    return parts.length
      ? { dir: 'down' as const, text: parts.join(' · ') }
      : { dir: 'up' as const, text: 'clean pacing' };
  })();

  // Lenient efficiency — shown as a secondary hint when the delta is
  // material (>= 0.5pp). Tier B windows widen the gap by carving forced
  // downtime out of the idealized ceiling; consensus ranged-filler windows
  // (forced melee disconnects bridged with e.g. Harpe) widen it by swapping
  // the ceiling's melee GCDs for the filler. When refs disagree, neither
  // signal fires and the two efficiencies match. Carries the same
  // denied-squeeze adjustment so it's comparable to the (adjusted) headline.
  const idealizedLenientAdj = Math.max(0, h.yourIdealizedPotencyLenient - deniedCeiling);
  const adjLenientEff = idealizedLenientAdj > 0
    ? (h.yourPotency / idealizedLenientAdj) * 100
    : h.efficiencyPctLenient;
  const lenientDelta = adjLenientEff - adjEff;
  const tierBSeconds = h.downtimeTierB.reduce(
    (acc, w) => acc + (w.endSec - w.startSec),
    0,
  );
  const rangedSeconds = (h.rangedWindows ?? []).reduce(
    (acc, w) => acc + (w.endSec - w.startSec),
    0,
  );
  const showLenientHint =
    lenientDelta >= 0.5 && (tierBSeconds >= 1 || rangedSeconds >= 1);

  // Raid-buff lens — two efficiency numbers scored under the buffs that
  // actually landed:
  //   observed = delivered / idealized-under-observed-buffs (fair, on you)
  //   master   = delivered / idealized-under-perfect-party-buffs
  // master <= observed when the PARTY's buffs were late/short — context,
  // not the analyzed player's fault. Hidden when there's no buff data.
  const showBuffLens = h.idealizedObserved > 0;
  const buffLensDelta = h.efficiencyPctObserved - h.efficiencyPctMaster;
  const showBuffLensGap = buffLensDelta >= 0.5;

  // Distro chart compares EFFICIENCY (delivered / idealized), not raw potency
  // — fights with different durations have different absolute potency ceilings,
  // so raw potency comparisons mislead. Matches how the legacy Execution
  // comparison ranks runs. Under a non-maximal crediting mode each ref is
  // regraded on the mode's basis (may exceed 100% — warned on the card).
  const refEfficiencies = a.refs
    .map((r, i) => (refsAdjusted ? refEffAdj(i) : r.efficiencyPct))
    .filter((e) => e > 0);
  const refAvgEff = refsAdjusted && refEfficiencies.length > 0
    ? refEfficiencies.reduce((x, y) => x + y, 0) / refEfficiencies.length
    : h.refEfficiencyPct;
  const effDelta = adjEff - refAvgEff;
  // Rank / percentile / beat mirror the backend's efficiency ranking
  // (main.py::_headline); recomputed locally only when a mode reshuffles the
  // field, else the backend stamp is shown verbatim.
  const standing = refsAdjusted
    ? rankAgainst(adjEff, refEfficiencies)
    : { percentile: h.percentile, rank: h.rank.you, total: h.rank.total, beat: h.beat.count };

  return (
    <div className="content">
      <div className="row" style={{ justifyContent: 'flex-end', marginBottom: 4 }}>
        <button className="btn ghost sm" onClick={onRerun}>
          <RefreshCw size={13} />
          Re-run analysis
        </button>
      </div>
      {/* High-level overview: percentile rank, efficiency spread, and downtime
          breakdown free-flowing in one fluid grid (column count follows the
          window width). */}
      <div className="overview-grid">
        {simBacked ? (
          <StatRing
            pct={efficiencyRingFill(adjEff)}
            value={adjEff.toFixed(2)}
            label={
              trolling
                ? 'Trolling detected — sub-80% efficiency. Just how many times did you die?'
                : belowFloor && lowEfficiencyNote
                  ? lowEfficiencyNote
                  : h.healLocksApplied
                    ? 'Efficiency vs the honest ceiling — mit-plan heals locked in'
                    : 'Efficiency compared to the Sim'
            }
            color={efficiencyColor(adjEff)}
            size={168}
          />
        ) : h.isProgPull ? (
          <StatRing
            pct={Math.max(0, 100 - (h.fightPercentage ?? 100))}
            value={(100 - (h.fightPercentage ?? 100)).toFixed(0)}
            eyebrow="Fight progress"
            label={h.lastPhase ? `wiped in P${h.lastPhase}` : 'at the wipe'}
            size={168}
          />
        ) : (
          <StatRing
            pct={h.percentile}
            value={h.percentile}
            eyebrow="Percentile rank"
            label="vs top 10 refs"
            subtext={`Rank ${h.rank.you} of ${h.rank.total} — beat ${h.beat.count} of ${h.beat.of} references`}
            size={168}
          />
        )}
        {/* Hidden for healers (rankSuppressed): top-parsing healers force DPS
            into heal windows for score, so ranking against their unlocked
            efficiencies would punish honest healing. Same for prog (wipe)
            pulls — kill refs aren't a fair yardstick for a truncated wipe. */}
        {refEfficiencies.length > 0 && !vsRefsHidden && (
          <div className="card">
            <div className="card-head">
              <Activity size={14} />
              <h2>Efficiency vs references</h2>
            </div>
            <div className="card-body">
              {simBacked && (
                <div className="mut" style={{ fontSize: 12, marginBottom: 10 }}>
                  {standing.percentile}th percentile · rank {standing.rank} of {standing.total}{' '}
                  · beat {standing.beat} of {h.beat.of} references
                </div>
              )}
              <DistroChart
                yourValue={adjEff}
                refs={refEfficiencies}
                formatLabel={(v) => `${v.toFixed(1)}%`}
              />
              <div className="legend" style={{ marginTop: 10 }}>
                <div className="item">
                  <span className="swatch" style={{ background: 'var(--accent)' }} />
                  You
                </div>
                <div className="item">
                  <span className="swatch" style={{ background: 'var(--info-soft)' }} />
                  References
                </div>
                <div className="item">
                  <span className="swatch" style={{ background: 'var(--text-2)' }} />
                  Median
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Ceiling-invariant watchdog: someone in this analysis scored over 100%
          efficiency, which the sim's ceiling construction says can't happen —
          i.e. a modeling bug we want the data for. Gated ONLY on the backend
          stamp (never adjEff > 100: user-denied windows can legitimately push
          the adjusted ratio over 100). */}
      {h.ceilingAnomaly && (
        <div className="findings" style={{ marginTop: 14 }}>
          <div className="finding info static">
            <div className="sev">!</div>
            <div>
              <div className="title">
                Result exceeded the theoretical ceiling
                {' '}(max {h.ceilingAnomaly.maxEffPct.toFixed(2)}%
                {h.ceilingAnomaly.entries[0]?.who === 'ref' ? ', a reference log' : ''})
              </div>
              <div className="desc">
                This result exceeded the theoretical ceiling — please help us
                out by submitting the data for us to improve the sim with.
              </div>
              <div style={{ marginTop: 8 }}>
                <button className="btn ghost sm" onClick={onReportAnomaly}>
                  <MessageSquare size={12} /> Submit feedback
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Healer mit-plan framing: the ceiling already pays for the healing this
          pull demanded — reconciled to the healer's ACTUAL casts (mit-plan floor,
          topped up to what they really cast, capped so over-healing still cards),
          the honest maximum. Over 100% here is EXPECTED (planned healing traded
          for damage, the top-parser pattern) so it gets this explainer instead of
          the ceiling-anomaly nudge; a wipe credits every heal, so it can't. */}
      {h.healLocksApplied && (
        <div className="findings" style={{ marginTop: 14 }}>
          <div className="finding info static">
            <div className="sev">{adjEff > 100 ? '↑' : '✚'}</div>
            <div>
              <div className="title">
                {adjEff > 100
                  ? `Above the honest ceiling (${adjEff.toFixed(2)}%)`
                  : h.isProgPull
                    ? 'Your healing is credited into the ceiling'
                    : 'Mitigation plan locked into the ceiling'}
              </div>
              <div className="desc">
                The ceiling already spends the{' '}
                {h.healLockCount ?? 0} healing GCD{(h.healLockCount ?? 0) === 1 ? '' : 's'}
                {' '}this pull demanded (~{Math.round(h.healLockPotency ?? 0)}p of damage
                {h.mitPlanCompSource === 'pull' ? '; comp read from this pull'
                  : h.mitPlanCompSource === 'override' ? '; comp adjusted in the planner'
                  : ''}).
                {h.isProgPull
                  ? ' Every heal GCD you cast on this wipe is credited — the ceiling never asks a progging healer to skip healing to win the parse.'
                  : adjEff > 100
                    ? ' Exceeding it means planned healing was sacrificed for damage — the top-parser pattern, not the honest target.'
                    : ' Reference comparisons are hidden: top-parsing healers skip planned heals for score.'}
                {h.mitPlanWarnings && h.mitPlanWarnings.length > 0
                  ? ` ${h.mitPlanWarnings.join(' ')}` : ''}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Prog (wipe) framing: the scored window ends at the terminal death,
          rank/vs-refs comparisons are hidden, and the projected kill time is
          the party-output extrapolation — set expectations before the KPIs. */}
      {h.isProgPull && (
        <div className="findings" style={{ marginTop: 14 }}>
          <div className="finding info static">
            <div className="sev">◔</div>
            <div>
              <div className="title">
                {wipePhaseName ? `In-progress pull — wiped in ${wipePhaseName}` : 'In-progress pull (wipe)'}
              </div>
              <div className="desc">
                {h.terminalDeathSec != null
                  ? `Scored up to your death at ${fmtClock(Math.round(h.terminalDeathSec))} (wipe at ${fmtClock(Math.round(h.pullDurationSec ?? h.killTimeSec))}) — time after it isn't held against you. `
                  : `Scored to the wipe at ${fmtClock(Math.round(h.pullDurationSec ?? h.killTimeSec))}. `}
                {h.projectedKillTimeSec != null
                  ? `The projected kill (${fmtClock(Math.round(h.projectedKillTimeSec))}) extends the party's active burn rate over the remaining ${h.fightPercentage != null ? `${h.fightPercentage.toFixed(0)}%` : 'fight'} and adds the downtime still ahead. `
                  : ''}
                Rank and reference comparisons are hidden — kill references
                aren't a fair yardstick for a wipe.
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="section-title">Headline</div>
      <div className="kpis card-grid">
        <KPI
          label="Your potency"
          value={fmtPotency(h.yourPotency)}
          unit="p"
          tone="accent"
          delta={
            h.yourIdealizedPotency > 0
              ? {
                  dir: 'down',
                  text: `of ${fmtPotency(idealizedAdj)}p simulated @${fmtClock(h.killTimeSec)}`,
                }
              : {
                  dir: yourPotencyDelta >= 0 ? 'up' : 'down',
                  text: `${yourPotencyDelta >= 0 ? '+' : '−'}${fmtNum(Math.abs(yourPotencyDelta))}p vs refs avg`,
                }
          }
          hint={`Refs avg: ${fmtPotency(h.refAvgPotency)}p (simulated ${fmtPotency(h.refAvgIdealizedPotency)}p)`}
        />
        <KPI
          label="Rotation efficiency"
          value={adjEff.toFixed(1)}
          unit="%"
          tone="good"
          delta={vsRefsHidden ? undefined : {
            dir: effDelta >= 0 ? 'up' : 'down',
            text: `${effDelta >= 0 ? '+' : '−'}${Math.abs(effDelta).toFixed(1)}% vs refs (${refAvgEff.toFixed(1)}%)`,
          }}
          hint={
            h.healLocksApplied
              ? `Ceiling already spends the ${h.healLockCount ?? 0} healing GCDs this pull demanded (~${Math.round(h.healLockPotency ?? 0)}p) — the honest maximum`
              : multiTargetDisclaimed
              ? 'Single-target model — multi-target output understated; treat this as a lower bound'
              : multiTargetCredited && mtMode === 'top10'
                ? `Multi-target ceiling capped at the top-10 average splash across ${mtWindows.length} window${mtWindows.length === 1 ? '' : 's'} — outliers can exceed 100%`
                : multiTargetCredited && mtMode === 'player'
                  ? `Multi-target ceiling capped at your credited splash across ${mtWindows.length} window${mtWindows.length === 1 ? '' : 's'} — top-10 references can exceed 100%`
                  : multiTargetCredited && mtMode === 'off'
                    ? `Multi-target windows excluded — graded as single-target`
                    : multiTargetCredited
                      ? `Includes splash credited over ${mtWindows.length} multi-target window${mtWindows.length === 1 ? '' : 's'} — edit them below`
                      : h.meleeDowntime && h.meleeDowntime.pct >= 1
                  ? `Includes ${Math.round(h.meleeDowntime.potency).toLocaleString()}p (${h.meleeDowntime.pct.toFixed(1)}%) credited as forced melee downtime — see the downtime panel`
                  : deniedCeiling > 0
                    ? `Excludes ${deniedCeilingCount(reviewable, deniedWindows)} ${ceilingNoun(reviewable)}${deniedCeilingCount(reviewable, deniedWindows) === 1 ? '' : 's'} you marked impossible (−${deniedCeiling}p ceiling)`
                    : showLenientHint
                      ? `${adjLenientEff.toFixed(1)}% with all ref-consensus forced stops excluded`
                      : undefined
          }
        />
        <KPI
          label="Effective GCD"
          value={h.effectiveGcdSec.toFixed(2)}
          unit="s"
          delta={pacingDelta}
          hint="Avg GCD potency this run"
        />
        {h.isProgPull ? (
          <>
            <KPI
              label="Pull duration"
              value={fmtClock(Math.round(h.pullDurationSec ?? h.killTimeSec))}
              tone="warn"
              delta={
                h.terminalDeathSec != null
                  ? {
                      dir: 'down',
                      text: `scored to your death at ${fmtClock(Math.round(h.terminalDeathSec))}`,
                    }
                  : undefined
              }
              hint={
                h.fightPercentage != null
                  ? `Wiped with ${h.fightPercentage.toFixed(0)}% of the fight left${wipePhaseName ? ` (${wipePhaseName})` : ''}`
                  : undefined
              }
            />
            {h.projectedKillTimeSec != null && (
              <KPI
                label="Projected kill"
                value={fmtClock(Math.round(h.projectedKillTimeSec))}
                tone="accent"
                delta={
                  h.refKillTimeSec > 0
                    ? {
                        dir: h.projectedKillTimeSec <= h.refKillTimeSec ? 'up' : 'down',
                        text: `refs avg ${fmtClock(h.refKillTimeSec)}`,
                      }
                    : undefined
                }
                hint={
                  h.projectionMeta
                    ? `Party burned ${h.projectionMeta.burnedPct.toFixed(0)}% of the fight in ${fmtClock(Math.round(h.projectionMeta.activeSec))} active; projection extends that rate and adds ${Math.round(h.projectionMeta.downtimeBeyondSec)}s of downtime ahead (from the closest reference kill, ${fmtClock(Math.round(h.projectionMeta.refKillSec))})`
                    : undefined
                }
              />
            )}
          </>
        ) : (
          <KPI
            label="Kill time"
            value={fmtClock(h.killTimeSec)}
            tone="warn"
            delta={{
              dir: killTimeDelta <= 0 ? 'up' : 'down',
              text: `${killTimeDelta <= 0 ? '−' : '+'}${fmtNum(Math.abs(killTimeDelta))}s vs refs avg (${fmtClock(h.refKillTimeSec)})`,
            }}
          />
        )}
        {h.deaths.length > 0 && (
          <KPI
            label="Deaths"
            value={String(h.deaths.length)}
            tone="warn"
            // Only show the potency delta when a death actually cost rotation
            // potency. A death during downtime (boss untargetable) overlaps a
            // window the idealized rotation has no casts in, so its cost is a
            // genuine 0 — showing "−0p lost to deaths" there reads as a bug.
            delta={
              h.deathsLostPotency > 0
                ? {
                    dir: 'down',
                    text: `−${fmtNum(Math.round(h.deathsLostPotency))}p lost to deaths`,
                  }
                : undefined
            }
            hint={`Died at ${h.deaths
              .map((d) => fmtClock(Math.round(d.timeSec)))
              .join(', ')}. Dead time is no longer counted as idle — see the Death cards in Potential improvements.`}
          />
        )}
        {showBuffLens && (
          <>
            <KPI
              label="Raid-buff alignment Observed"
              value={h.efficiencyPctObserved.toFixed(1)}
              unit="%"
              tone="good"
              hint="given the buffs you got — the fair, player-accountable number"
            />
            <KPI
              label="Raid-buff alignment vs party-perfect buffs"
              value={h.efficiencyPctMaster.toFixed(1)}
              unit="%"
              delta={
                showBuffLensGap
                  ? {
                      dir: 'down',
                      text: `−${buffLensDelta.toFixed(1)}% from party buff timing`,
                    }
                  : { dir: 'up', text: 'party buffs landed on cadence' }
              }
              hint={`Master ceiling assumes party buffs on a perfect 2-minute cadence (simulated ${fmtPotency(h.idealizedMaster)}p). The gap is the party's buff timing — context, not your fault.`}
            />
          </>
        )}
      </div>

      {showBuffLens && buffDriftFindings.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="sub" style={{ fontWeight: 600, marginBottom: 6 }}>
            Party buff timing (context)
          </div>
          <div className="findings">
            {buffDriftFindings.map((f, i) => (
              <div className="finding info" key={i}>
                <div className="sev">{tagBadge('model')}</div>
                <div>
                  <div className="title">{f.provider}</div>
                  <div className="desc">{f.summary}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="section-title">Where the potency went</div>
      <div className="stack">
        {/* Situational ceiling squeezes the sim assumed (Flamethrower today),
            shipped from the backend as data. The user confirms which were
            actually possible; denials drop off the ceiling above. One generic
            panel per group; renders nothing when the sim found no windows. */}
        {ceilingGroups(reviewable).map((g) => {
          const meta = groupMeta(g.kind);
          return (
            <WindowReview
              key={g.kind}
              icon={meta.icon}
              heading={meta.heading}
              blurb={meta.blurb(g.windows[0]?.potency ?? 0)}
              windows={g.windows}
              denied={deniedWindows}
              onToggle={onToggleWindow}
              onJump={onJumpToTime}
            />
          );
        })}
        {/* Confirmed multi-target windows the efficiency above credits splash
            over (both delivered and the ceiling). Detection is targetability ∩
            ref-consensus, but a window could still be off (an add others
            ignored, a phase the targetability feed missed) — the crediting mode
            picks the grading basis, and the per-window toggles override it.
            Renders only when the backend credited splash. */}
        <WindowReview
          icon={<Crosshair size={14} />}
          heading="Multi-target windows"
          modeSelector={
            <div className="wr-mode-row">
              <span className="wr-mode-label mut">Crediting</span>
              <div className="wr-toggle wr-mode">
                {(
                  [
                    ['off', 'Not possible', 'Grade single-target: every window’s splash removed from both sides'],
                    ['maximal', 'Maximally possible', 'Full credit at the model’s maximally-possible target count (default)'],
                    ['top10', 'Cap at top-10 avg', 'Cap each window’s ceiling at the references’ average credited splash'],
                    ['player', 'Cap at player credited', 'Cap each window’s ceiling at your own credited splash'],
                  ] as const
                ).map(([value, label, title]) => (
                  <button
                    key={value}
                    className={mtMode === value ? (value === 'off' ? 'on bad' : 'on') : ''}
                    title={title}
                    onClick={() => { if (mtMode !== value) onSetMtMode(value); }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          }
          blurb={
            mtMode === 'top10' ? (
              <>
                ⚠ Ceiling capped at the top-10 average credited splash per
                window. Outliers who cleaved more than the average — you or a
                reference — will register over 100%.
              </>
            ) : mtMode === 'player' ? (
              <>
                ⚠ Ceiling capped at your credited splash per window — your
                multi-target gap reads as zero, and top-10 references who
                out-cleaved you will register over 100%.
              </>
            ) : mtMode === 'off' ? (
              <>
                All windows graded as not possible — splash removed from both
                delivered and the ceiling. Toggle a window back on to restore
                its full credit.
              </>
            ) : (
              <>
                Simulator detected the following multi-target windows. Toggle
                them off if it is not possible to cleave.
              </>
            )
          }
          windows={mtReview}
          denied={deniedWindows}
          onToggle={onToggleWindow}
          onJump={onJumpToTime}
          potencyNoun="splash"
          defaultDenied={mtMode === 'off'}
        />
        {/* Downtime detection sits right above the improvements: it frames what
            the efficiency ceiling was measured against before listing the
            recoverable losses. Renders nothing when there are no windows. */}
        <DowntimePanel headline={h} />
        <PhasePanel analysis={a} improvements={shownImprovements} onJumpToTime={onJumpToTime} />
        <div className="card">
          <div className="card-head">
            <Lightbulb size={14} />
            <h2>Potential improvements</h2>
            <span className="sub" style={{ marginLeft: 'auto' }}>
              Click to view on the timeline
            </span>
          </div>
          <div className="card-body">
            {multiTargetDisclaimed ? (
              <p className="mut" style={{ fontSize: 13, margin: 0 }}>
                Suppressed on this pull — multiple enemies were targetable, so
                the single-target model would mis-attribute losses (suggesting
                a single-target skill over the AoE/splash that was actually
                optimal). Multi-target scoring is on the way; until then, treat
                the efficiency above as a lower bound.
              </p>
            ) : (
              <>
                {recoverable > 0 && (
                  <p className="mut" style={{ fontSize: 12.5, margin: '0 0 10px' }}>
                    ≈{fmtNum(Math.round(recoverable))}p ({recoverablePct.toFixed(1)}%)
                    {h.isProgPull
                      ? h.terminalDeathSec != null
                        ? <> recoverable vs simulated — wipe scored to your death at {fmtClock(h.killTimeSec)}.</>
                        : <> recoverable vs simulated across the {fmtClock(h.killTimeSec)} wipe.</>
                      : <> recoverable vs simulated @{fmtClock(h.killTimeSec)}.</>}
                  </p>
                )}
                {shownImprovements.length === 0 ? (
                  <p className="mut" style={{ fontSize: 13, margin: 0 }}>
                    No notable findings — clean run.
                  </p>
                ) : showImpCategories ? (
                  improvementCats.map((cat) => {
                    const CatIcon = cat.def.icon;
                    return (
                      <div key={cat.def.id} className="imp-cat">
                        <div className="imp-cat-head">
                          <CatIcon size={13} />
                          <span>{cat.def.label}</span>
                          {cat.subtotal > 0 && (
                            <span className="sum">
                              −{fmtNum(Math.round(cat.subtotal))}p
                            </span>
                          )}
                        </div>
                        <div className="findings">
                          {cat.cards.map((im, i) => (
                            <ImprovementRow
                              key={i}
                              im={im}
                              meta={a.abilityMeta}
                              onJump={onJumpToTime}
                            />
                          ))}
                        </div>
                      </div>
                    );
                  })
                ) : (
                  <div className="findings">
                    {shownImprovements.map((im, i) => (
                      <ImprovementRow
                        key={i}
                        im={im}
                        meta={a.abilityMeta}
                        onJump={onJumpToTime}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {drift.length > 0 && (
          <div className="card">
            <div className="card-head">
              <Clock size={14} />
              <h2>Drift detail</h2>
              <button
                className="btn ghost sm"
                style={{ marginLeft: 'auto' }}
                onClick={() => setView('counts')}
              >
                <Layers size={12} />
                All abilities
              </button>
            </div>
            <div className="card-body tbl-body" style={{ padding: 0 }}>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Ability</th>
                    <th className="r">Casts</th>
                    <th className="r">Drift</th>
                    <th className="r">Lost (p)</th>
                  </tr>
                </thead>
                <tbody>
                  {drift.map((d, i) => {
                    const meta = a.abilityMeta[d.abilityId];
                    return (
                      <tr key={i}>
                        <td>
                          <span className="row" style={{ gap: 8 }}>
                            <AbilityIcon
                              kind="ogcd1"
                              glyph={d.abilityName.slice(0, 1)}
                              name={meta?.name ?? d.abilityName}
                              iconPath={meta?.iconPath}
                              size={20}
                            />
                            {d.abilityName}
                          </span>
                        </td>
                        <td className="r num">{d.casts}</td>
                        <td className="r num">{d.cappedSeconds.toFixed(1)}s</td>
                        <td className="r num delta-neg">−{fmtNum(d.lostPotency)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      <JobPanels job={job} analysis={a} />
    </div>
  );
};
