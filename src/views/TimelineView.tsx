import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from 'react';
import { Crosshair, Pin, Skull } from 'lucide-react';
import { AbilityIcon } from '../components/AbilityIcon';
import { TimelineShell, type FilterState } from '../components/timeline/TimelineShell';
import { TimelineCast } from '../components/timeline/TimelineCast';
import { DiffRuler } from '../components/timeline/DiffRuler';
import {
  GCD_TOP,
  ICON_SIZE,
  LANE_H,
  OGCD_TOP,
  clampBubbleLeft,
  useTimelineScale,
} from '../components/timeline/scale';
import { fmtClock, fmtDuration } from '../format';
import { currentZoom } from '../state/zoom';
import {
  isWindowDenied,
  multiTargetWindowId,
  MT_MODE_DEFAULT,
  refEffAdjusted,
  type MtMode,
} from '../state/multiTargetModes';
import { kindLabel } from './findings';
import { isDeniedReviewableCast, isReviewableCast } from './reviewableWindows';
import { getJobProfile, nonRotationalNames } from '../jobs';
import type {
  AbilityMetaJson,
  AnalysisResult,
  CastEvent,
  ClippingAspectState,
  Improvement,
  ReviewableWindowGroup,
  ScoringState,
} from '../sidecar/contract';

/** A jump request from a dashboard Potential Improvement. `nonce` changes on
 *  every click (even the same row) so re-clicking re-triggers the scroll.
 *  `kind`/`abilityId` (when the jump came from an improvement card) pick the
 *  right highlight target: ideal-lane cast for missed casts, the death flag for
 *  deaths, the matching-ability cast for delivered-side kinds. Absent (e.g. a
 *  WindowReview jump) → nearest-any cast, the historic behavior. */
export type TimelineFocus = {
  timeSec: number;
  nonce: number;
  kind?: string;
  abilityId?: number;
};

/** Improvement kinds whose located time points at the IDEALIZED lane (the cast
 *  exists only in the sim) — pulsing a nearby delivered cast would finger an
 *  innocent one, so these highlight the sim lane instead. */
const IDEAL_KINDS = new Set(['missed_cast', 'missed_enabler', 'flamethrower']);

/** Nearest cast in `track` to `t`; prefers an `abilityId` match within `tol`
 *  when one is asked for. No ability match → nearest-any when `fallbackAny`,
 *  else no target at all (the focus marker line alone locates the jump). */
function nearestCast(
  track: CastEvent[],
  t: number,
  abilityId: number | undefined,
  tol: number,
  fallbackAny: boolean,
): number | null {
  let best: number | null = null;
  let bestDist = Infinity;
  let bestAb: number | null = null;
  let bestAbDist = Infinity;
  track.forEach((e, i) => {
    const d = Math.abs(e.startSec - t);
    if (d < bestDist) { bestDist = d; best = i; }
    if (abilityId != null && abilityId > 0 && e.abilityId === abilityId && d < bestAbDist) {
      bestAbDist = d;
      bestAb = i;
    }
  });
  if (bestAb != null && bestAbDist <= tol) return bestAb;
  return fallbackAny ? best : null;
}

type Props = {
  analysis: AnalysisResult;
  /** Active job — selects the non-rotational ability/name sets from the job
   *  registry (src/jobs) so the lane filter + cast-diff are job-aware. */
  job: string;
  /** When set, scroll to this time and pulse the nearest cast. */
  focus?: TimelineFocus | null;
  /** Reviewable windows the user marked "not possible" on the dashboard — those
   *  squeezes are dropped from the idealized lane (and the diff) here. For
   *  multi-target windows the entries are overrides relative to `mtMode`. */
  deniedWindows?: Set<string>;
  /** Multi-target crediting mode (see state/multiTargetModes.ts). 'off' denies
   *  every window by default; the cap modes only change pricing, so the lanes
   *  render exactly like 'maximal'. */
  mtMode?: MtMode;
};

// Timeline clock labels round to the nearest second (axis ticks, hover chips);
// the implementations live in src/format.ts. fmtDur is the shared duration form.
const fmtAxisTick = (s: number): string => fmtClock(s, true);
const fmtDur = fmtDuration;

// Which casts belong on the DPS timeline is data-driven: the backend tags every
// ability with `isDefensive` (shared role actions ∪ the job's defensive oGCDs),
// so a defensive/utility cast carries no DPS value and the sim never fires it —
// diffing it would just be "extra cast" noise. The frontend reads that one flag;
// `nonRotNames` is a small shared fallback for casts that arrive without a
// resolved id/meta (so the backend flag couldn't apply).

/** Resolve a cast's display name: prefer the metadata map (real abilities),
 *  else parse the leading token of the tooltip ("Sprint  (oGCD)  @  0:42"),
 *  which is how name-only casts (no bundled id) arrive. */
function castDisplayName(c: CastEvent, abilityMeta: Record<number, AbilityMetaJson>): string {
  if (c.abilityId != null) {
    const n = abilityMeta[c.abilityId]?.name;
    if (n) return n;
  }
  return c.tooltip?.split('  ')[0]?.trim() ?? '';
}

/** A cast belongs on the DPS timeline unless it's a non-combat utility — by the
 *  backend `isDefensive` flag (the primary, data-driven path), or by name for
 *  casts whose metadata didn't resolve (the shared fallback). */
const isRotational = (
  c: CastEvent,
  abilityMeta: Record<number, AbilityMetaJson>,
  nonRotNames: Set<string>,
): boolean => {
  if (c.abilityId != null) {
    const m = abilityMeta[c.abilityId];
    if (m?.isDefensive) return false;
    if (m) return true; // resolved + not flagged defensive → rotational
  }
  return !nonRotNames.has(castDisplayName(c, abilityMeta));
};

/** The exact complement of `isRotational` — a defensive/utility cast (backend
 *  `isDefensive` flag, or a shared non-rotational name for unresolved casts). Used
 *  to populate the tank Defensives lane; these never enter the DPS diff/scoring. */
const isDefensiveCast = (
  c: CastEvent,
  abilityMeta: Record<number, AbilityMetaJson>,
  nonRotNames: Set<string>,
): boolean => {
  if (c.abilityId != null) {
    const m = abilityMeta[c.abilityId];
    if (m) return !!m.isDefensive;
  }
  return nonRotNames.has(castDisplayName(c, abilityMeta));
};

// Compact Defensives-lane icon geometry (a single band, shorter than the
// full GCD/oGCD lanes since these casts don't split into two rows).
const DEF_ICON = 34;
const DEF_TOP = 10;
// Mirrors `.tl-row.def .strip` height — the tank Defensives lane sits between
// the pinned You/Sim rows, so the Sim-lane bubble anchor accounts for it.
const DEF_LANE_H = 56;
// Mirrors `.tl-axis` height — the overlay layers (bubble coordinates) start below it.
const AXIS_H = 26;
// Mirrors the shell's flag band (this page always mounts it) — overlays start
// below axis + band, so rect-based bubble anchors subtract the full head.
const FLAG_BAND_H = 20;
const HEAD_H = AXIS_H + FLAG_BAND_H;

/** Filter an idealized track to the casts that render on the lane: rotational
 *  only, minus any reviewable squeeze the user denied on the dashboard. */
function filterIdealTrack(
  track: CastEvent[],
  abilityMeta: Record<number, AbilityMetaJson>,
  nonRotNames: Set<string>,
  reviewable: ReviewableWindowGroup[] | undefined,
  denied?: Set<string>,
): CastEvent[] {
  return track.filter(
    (c) =>
      isRotational(c, abilityMeta, nonRotNames) &&
      !(denied != null && isDeniedReviewableCast(reviewable, denied, c.abilityId, c.startSec)),
  );
}

/** Align the player's casts against the idealized rotation by greedy nearest-
 *  time pairing PER ABILITY, then read off the count surpluses:
 *   - a you-cast with no idealized partner → an extra / suboptimal cast (`youDiff`)
 *   - an idealized cast with no you partner → one you missed (`idealDiff`)
 *  Pairing per ability (not globally by time) means pure timing drift — a Drill
 *  cast 3s late but still present — never registers as a diff; only a genuine
 *  count mismatch of a given ability does. That keeps this in step with the
 *  Potential Improvements panel (missed-cast semantics), leaving drift to its
 *  own category. */
function computeCastDiff(
  you: CastEvent[],
  ideal: CastEvent[],
  abilityMeta: Record<number, AbilityMetaJson>,
): { youDiff: Set<number>; idealDiff: Set<number> } {
  const youDiff = new Set<number>();
  const idealDiff = new Set<number>();
  if (ideal.length === 0) return { youDiff, idealDiff };

  const byAbility = new Map<number, { you: number[]; ideal: number[] }>();
  const bucket = (ab: number) => {
    let b = byAbility.get(ab);
    if (!b) {
      b = { you: [], ideal: [] };
      byAbility.set(ab, b);
    }
    return b;
  };
  // Skip casts we can't identify (no abilityId — e.g. role actions) or that are
  // purely defensive (backend isDefensive flag): the sim doesn't model them, so
  // they'd only ever surface as one-sided "extra" diffs.
  // Pre-pull casts (t < 0 — precast channels / openers) render on the lanes but
  // are excluded from the diff: the comparison is about in-fight execution, and
  // begincast vs cast-completion anchoring differs across lanes pre-pull.
  const diffable = (c: CastEvent): c is CastEvent & { abilityId: number } =>
    c.abilityId != null && c.startSec >= 0 && !abilityMeta[c.abilityId]?.isDefensive;
  you.forEach((c, i) => { if (diffable(c)) bucket(c.abilityId).you.push(i); });
  ideal.forEach((c, i) => { if (diffable(c)) bucket(c.abilityId).ideal.push(i); });

  for (const { you: ys, ideal: is } of byAbility.values()) {
    // Every candidate pair, nearest in time first; greedily consume 1:1.
    const pairs: { yi: number; ii: number; d: number }[] = [];
    for (const yi of ys) {
      for (const ii of is) {
        pairs.push({ yi, ii, d: Math.abs(you[yi].startSec - ideal[ii].startSec) });
      }
    }
    pairs.sort((a, b) => a.d - b.d);
    const usedY = new Set<number>();
    const usedI = new Set<number>();
    for (const p of pairs) {
      if (usedY.has(p.yi) || usedI.has(p.ii)) continue;
      usedY.add(p.yi);
      usedI.add(p.ii);
    }
    for (const yi of ys) if (!usedY.has(yi)) youDiff.add(yi);
    for (const ii of is) if (!usedI.has(ii)) idealDiff.add(ii);
  }
  return { youDiff, idealDiff };
}

/** Flatten the (possibly grouped) Potential Improvements into the located leaf
 *  suggestions — those tied to a real ability at a real time. Aggregate cards
 *  (idle/clip totals, "×N" groups, the residual) expose their per-cast children;
 *  we want those children, not the rollup. */
function flattenLocated(imps: Improvement[]): Improvement[] {
  const out: Improvement[] = [];
  const walk = (im: Improvement) => {
    if (im.timeSec > 0 && im.abilityId > 0) out.push(im);
    (im.children ?? []).forEach(walk);
  };
  imps.forEach(walk);
  return out;
}

/** Greedily attach the closest located Improvement to each diffed cast, matching
 *  on ability id and time (within `tol` seconds), 1:1. Returns castIndex →
 *  Improvement. `usedImp` is shared across calls so the missed-cast lane claims a
 *  suggestion before the extra-cast lane can reuse it. */
function matchImprovements(
  casts: CastEvent[],
  diff: Set<number>,
  located: Improvement[],
  tol: number,
  usedImp: Set<number>,
): Map<number, Improvement> {
  const out = new Map<number, Improvement>();
  const pairs: { ci: number; ii: number; d: number }[] = [];
  for (const ci of diff) {
    const ab = casts[ci].abilityId;
    if (ab == null) continue;
    located.forEach((im, ii) => {
      if (im.abilityId !== ab) return;
      const d = Math.abs(im.timeSec - casts[ci].startSec);
      if (d <= tol) pairs.push({ ci, ii, d });
    });
  }
  pairs.sort((a, b) => a.d - b.d);
  const usedCast = new Set<number>();
  for (const p of pairs) {
    if (usedCast.has(p.ci) || usedImp.has(p.ii)) continue;
    usedCast.add(p.ci);
    usedImp.add(p.ii);
    out.set(p.ci, located[p.ii]);
  }
  return out;
}

/** Scroll offset of the timeline scrollport, read from the hovered element at
 *  event time (DOM-derived, deliberately no React refs). The pinned You/Sim
 *  lanes sit at natural + scrollTop in grid coordinates once stuck, so bubbles
 *  anchored to them (or parked near the top of the plot) add this. */
const scrollTopOf = (e: ReactMouseEvent): number =>
  e.currentTarget.closest('.timeline-scroll')?.scrollTop ?? 0;

// Pointer focus on a diff. Gaps (missed casts) can be hovered from either the
// ghost on your lane (`gap-ghost`) or its partner on the idealized lane
// (`gap-ideal`) — both key off the idealized index. `you` is an extra/suboptimal
// or jumped-to cast on your lane.
type Hover =
  | { kind: 'gap-ghost' | 'gap-ideal'; idx: number }
  | { kind: 'you'; idx: number }
  | { kind: 'idle' | 'clip'; idx: number }
  | { kind: 'downA' | 'downB' | 'downBHigh' | 'death' | 'mt' | 'phase'; idx: number }
  // Multi-target cast hover carries its data inline (it's per-cast on every
  // lane, so there's no single index to look up). `topPx` is the bubble's
  // overlay-coordinate top, measured from the hovered icon's real on-screen
  // row at enter time — exact for any lane, pinned or scrolled.
  | { kind: 'mthit'; hit: number; max: number; lost: number; leftSec: number; topPx: number };

export const TimelineView = ({
  analysis, job, focus, deniedWindows, mtMode = MT_MODE_DEFAULT,
}: Props) => {
  const [zoom, setZoom] = useState(1);
  const [highlightDiff, setHL] = useState(true);
  // Which idealized lane to show: the sim's throughput-OPTIMAL rotation, or the
  // CANONICAL "hold burst for the 2-min window" line. Toggled in the toolbar.
  const [idealMode, setIdealMode] = useState<'optimal' | 'canonical'>('optimal');
  const [filter, setFilter] = useState<FilterState>({ gcd: true, ogcd: true, refs: true });
  // Per-lane pin toggles (in each lane's label): a pinned lane parks under the
  // axis while the reference lanes scroll. Default unpinned — two pinned lanes
  // leave the ref lanes barely visible at the default window size.
  const [pinYou, setPinYou] = useState(false);
  const [pinIdeal, setPinIdeal] = useState(false);
  // Hover state may carry the scrollport offset captured at enter time (see
  // scrollTopOf): bubbles anchored to the pinned You/Sim lanes — or parked
  // near the top of the plot — add it, since the bubble overlay scrolls with
  // the grid while those anchors visually don't.
  const [hover, setHover] = useState<(Hover & { scrollTop?: number }) | null>(null);

  // Scroll container ref — owned here (not by the shell) for the dashboard
  // "jump to cast" scroll-to-time effect below; forwarded onto `.timeline-scroll`.
  const scrollRef = useRef<HTMLDivElement>(null);

  // Non-rotational casts (Sprint, role actions, job-specific defensive oGCDs)
  // are dropped from every lane up front so they never render, diff, or shift
  // indices downstream. Defensive-ness is the backend `isDefensive` flag on
  // abilityMeta; `nonRotNames` is the shared name fallback for unresolved casts.
  const meta = analysis.abilityMeta;
  const nonRotNames = useMemo(() => nonRotationalNames(job), [job]);
  // Tanks get a dedicated Defensives lane (Rampart / Sentinel / Holy Sheltron …);
  // every other job leaves the flag off and these casts stay filtered everywhere.
  const showDefensives = getJobProfile(job).defensiveTimeline ?? false;
  const events: CastEvent[] = useMemo(
    () =>
      (analysis.you.abilitiesTrack ?? []).filter((c) =>
        isRotational(c, meta, nonRotNames),
      ),
    [analysis.you.abilitiesTrack, meta, nonRotNames],
  );
  // Your defensive casts (the complement of `events`). Empty unless the job opts
  // into the Defensives lane.
  const defensiveEvents: CastEvent[] = useMemo(
    () =>
      showDefensives
        ? (analysis.you.abilitiesTrack ?? []).filter((c) =>
            isDefensiveCast(c, meta, nonRotNames),
          )
        : [],
    [showDefensives, analysis.you.abilitiesTrack, meta, nonRotNames],
  );
  // Idealized perfect-sim rotation — the theoretical-best comparison lane.
  // A reviewable squeeze the user denied on the dashboard is dropped here too,
  // so the idealized lane (and the cast-diff that reads off it) matches the
  // adjusted ceiling. `reviewable` also drives the in-downtime z-lift below.
  const denied = deniedWindows;
  const reviewable = analysis.reviewableWindows;
  // Confirmed multi-target windows. A window that's effectively "not possible"
  // (per-window toggle XOR the crediting mode — mode 'off' denies everything by
  // default) is dropped from the timeline: its zone/label disappear, its
  // per-cast splash dots are suppressed (see `inDeniedMt`), AND the idealized
  // lane is spliced back to the cached single-target track inside it (see
  // `idealized` below) — so the whole timeline matches the recomputed
  // efficiency. The cap modes only reprice, so they render like 'maximal'.
  const mtDenied = (startSec: number): boolean =>
    isWindowDenied(multiTargetWindowId(startSec), deniedWindows ?? new Set(), mtMode);
  const allMultiTarget = analysis.headline.multiTargetWindows ?? [];
  const multiTarget = allMultiTarget.filter((w) => !mtDenied(w.startSec));
  const deniedMtRanges = allMultiTarget
    .filter((w) => mtDenied(w.startSec))
    .map((w) => [w.startSec, w.endSec] as const);
  const inDeniedMt = (t: number): boolean =>
    deniedMtRanges.some(([s, e]) => s <= t && t < e);
  // The canonical "hold burst for the 2-min window" lane (empty when no party
  // buffs were present); the player toggles between it and the optimal lane.
  const canonicalIdeal: CastEvent[] = useMemo(
    () => filterIdealTrack(analysis.idealizedTrackCanonical ?? [], meta, nonRotNames, reviewable, denied),
    [analysis.idealizedTrackCanonical, meta, nonRotNames, reviewable, denied],
  );
  const hasCanonical = canonicalIdeal.length > 0;
  const idealized: CastEvent[] = useMemo(
    () => {
      if (idealMode === 'canonical' && hasCanonical) return canonicalIdeal;
      const aoe = filterIdealTrack(analysis.idealizedTrack ?? [], meta, nonRotNames, reviewable, denied);
      // Splice the cached SINGLE-TARGET lane into any window the user marked "not
      // possible" so the idealized rotation visibly reverts to single-target there
      // (no re-sim — both lanes are precomputed). Self-contained range computation
      // to keep the memo's deps clean. [] strict track => not a credited pull.
      const strict = analysis.idealizedTrackStrict ?? [];
      const ranges = (analysis.headline.multiTargetWindows ?? [])
        .filter((w) =>
          isWindowDenied(multiTargetWindowId(w.startSec), denied ?? new Set(), mtMode))
        .map((w) => [w.startSec, w.endSec] as const);
      if (ranges.length === 0 || strict.length === 0) return aoe;
      const inDenied = (t: number): boolean => ranges.some(([s, e]) => s <= t && t < e);
      const strictF = filterIdealTrack(strict, meta, nonRotNames, reviewable, denied);
      return [
        ...aoe.filter((c) => !inDenied(c.startSec)),
        ...strictF.filter((c) => inDenied(c.startSec)),
      ].sort((a, b) => a.startSec - b.startSec);
    },
    [analysis.idealizedTrack, analysis.idealizedTrackStrict, analysis.headline.multiTargetWindows,
     canonicalIdeal, hasCanonical, idealMode, meta, nonRotNames, reviewable, denied, mtMode],
  );
  // Reference rotations — one comparison lane per fetched reference log,
  // SORTED by efficiency (desc) and re-numbered by sorted position. The
  // efficiency runs through the same mt-crediting adjustment the Dashboard's
  // distro chart uses (refEffAdjusted), so the two views agree. The backend
  // bakes "#i Name" into `label` — strip the rank prefix; the full name
  // survives as a tooltip only.
  const refLanes = useMemo(
    () =>
      analysis.refs
        .map((r, i) => ({
          name: (r.label ?? '').replace(/^#\d+\s*/, ''),
          eff: refEffAdjusted(
            analysis.refs, i,
            analysis.headline.multiTargetWindows ?? [],
            deniedWindows ?? new Set(), mtMode,
          ),
          track: (r.abilitiesTrack ?? []).filter((c) =>
            isRotational(c, meta, nonRotNames),
          ),
          pots: r.tinctureWindows ?? [],
          // Per-ref defensives (empty unless the job opts into the lane).
          def: showDefensives
            ? (r.abilitiesTrack ?? []).filter((c) =>
                isDefensiveCast(c, meta, nonRotNames),
              )
            : [],
        }))
        // Keep the backend's ranking order (top 10 by rDPS) — the lane rank
        // matches the FFLogs ranking, not an efficiency re-sort.
        .filter((r) => r.track.length > 0),
    [analysis.refs, analysis.headline.multiTargetWindows, deniedWindows, mtMode,
     meta, nonRotNames, showDefensives],
  );

  // Cast-diff vs the idealized rotation. Only meaningful when there IS an
  // idealized lane (sim-backed jobs); for sim-less jobs the toggle is hidden.
  const canDiff = idealized.length > 0;
  const { youDiff, idealDiff } = useMemo(
    () => computeCastDiff(events, idealized, meta),
    [events, idealized, meta],
  );
  // The Highlight toggle drives whether the diff markers render.
  const diffMode = canDiff && highlightDiff;

  // Every diff in the pull, sorted by time — the locator steps through this
  // list and the overview ruler draws a tick per entry. Indices are into
  // `events` / the (mt-spliced) `idealized` memo, matching the marker classes.
  const diffs = useMemo(() => {
    const out: { lane: 'you' | 'ideal'; idx: number; timeSec: number }[] = [];
    youDiff.forEach((i) => {
      const c = events[i];
      if (c) out.push({ lane: 'you', idx: i, timeSec: c.startSec });
    });
    idealDiff.forEach((i) => {
      const c = idealized[i];
      if (c) out.push({ lane: 'ideal', idx: i, timeSec: c.startSec });
    });
    out.sort((a, b) => a.timeSec - b.timeSec);
    return out;
  }, [youDiff, idealDiff, events, idealized]);
  // View-local locator cursor + the step-triggered pulse target. The nonce
  // remounts the pulsed cast so re-stepping onto it restarts the animation
  // (the same trick the dashboard-jump focus uses). stepDiff itself is defined
  // after the timeline scale below (it needs xOf).
  const [diffCursor, setDiffCursor] = useState(-1);
  const [stepFocus, setStepFocus] =
    useState<{ lane: 'you' | 'ideal'; idx: number; nonce: number } | null>(null);

  // Located leaf suggestions, shared across all the matching below.
  const located = useMemo(
    () => flattenLocated(analysis.improvements ?? []),
    [analysis.improvements],
  );
  // Map each diffed cast to its Potential Improvement so the bubble can show the
  // real suggestion from the dashboard. Missed casts (idealized lane) claim a
  // suggestion first; extra casts (your lane) match leftover clip/overcap cards.
  const { gapImp, extraImp } = useMemo(() => {
    const tol = Math.max(3, (analysis.headline.effectiveGcdSec || 2.5) * 1.5);
    const used = new Set<number>();
    const gapImp = matchImprovements(idealized, idealDiff, located, tol, used);
    const extraImp = matchImprovements(events, youDiff, located, tol, used);
    return { gapImp, extraImp };
  }, [located, analysis.headline.effectiveGcdSec, idealized, events, idealDiff, youDiff]);

  const downtimeA = analysis.headline.downtimeTierA ?? [];
  const downtimeB = analysis.headline.downtimeTierB ?? [];
  // High-confidence (near-unanimous) sub-cores of Tier B: the genuinely-forced
  // stretches the idealized lane skips. Rendered firmly over the lighter
  // suspected hatch so the confidence gradient reads at a glance.
  const downtimeBHigh = analysis.headline.downtimeTierBHigh ?? [];
  // Boss phase segments (phased fights only — ultimates). Empty on Savage, so
  // the phase track simply doesn't render there.
  const phases = analysis.phases ?? [];
  // (multiTarget / deniedMtRanges / inDeniedMt are defined above, before the
  // idealized useMemo that splices the single-target lane into denied windows.)
  // Memoized: focusDeathIdx below depends on it, and `?? []` would otherwise
  // mint a fresh array every render when the field is absent.
  const deaths = useMemo(
    () => analysis.headline.deaths ?? [],
    [analysis.headline.deaths],
  );
  // Tincture (Medicated) windows are PER-ACTOR — each lane draws its own (you,
  // sim, each ref), color-coded, behind the casts (pointer-events off so casts
  // stay hoverable). Not a fight-wide band like downtime/multi-target.
  const youPots = analysis.you.tinctureWindows ?? [];
  const idealPots = analysis.idealizedTinctureWindows ?? [];
  // Priced death cards, to surface each death's cost in its hover bubble.
  const deathImps = useMemo(
    () => (analysis.improvements ?? []).filter((im) => im.kind === 'death'),
    [analysis.improvements],
  );

  // OBSERVED party-buff windows, merged into union bands (overlapping buffs
  // fuse into one zone; the labels join). Drift/diffs only mean something
  // relative to when the party's buffs were actually up, so these render as
  // background bands on every lane. Empty for buffless pulls.
  const buffZones = useMemo(() => {
    const wins = ((analysis.aspectStates.Scoring as ScoringState | undefined)
      ?.observedBuffWindows ?? [])
      .map(([s, e, mult, label]) => ({ s, e, mult, label }))
      .sort((a, b) => a.s - b.s);
    const out: { s: number; e: number; labels: string[] }[] = [];
    for (const w of wins) {
      const tag = `${w.label} ×${w.mult.toFixed(2)}`;
      const last = out[out.length - 1];
      if (last && w.s <= last.e + 0.001) {
        last.e = Math.max(last.e, w.e);
        last.labels.push(tag);
      } else {
        out.push({ s: w.s, e: w.e, labels: [tag] });
      }
    }
    return out;
  }, [analysis.aspectStates.Scoring]);

  // Pacing anomalies (always shown — factual, not diff-dependent). The Clipping
  // aspect splits GCD-gap excess into idle stretches and true over-weave clips,
  // each with the worst occurrences' (time, …) so we can mark them on the lane.
  const clipping = (analysis.aspectStates.Clipping as ClippingAspectState | undefined)?.clipping;
  const worstIdle = clipping?.worstIdle ?? [];
  const worstClips = clipping?.worstClips ?? [];
  const avgGcdP = clipping?.avgGcdPotency ?? 0;
  const effGcd = clipping?.effectiveGcdSec || 2.5;

  // Shared timeline scale (time→x, the pre-pull zone, ticks, gridlines). The
  // lane set drives the strip width + earliest precast; memoized so the scale
  // recomputes only on a real lane change, not every render.
  const laneCasts = useMemo(
    () => [
      events,
      idealized,
      canonicalIdeal,
      ...refLanes.map((r) => r.track),
      defensiveEvents,
      ...refLanes.map((r) => r.def),
    ],
    [events, idealized, canonicalIdeal, refLanes, defensiveEvents],
  );
  const scale = useTimelineScale(zoom, laneCasts);
  const { xOf, pxPerSec, stripWidth, stripStyle } = scale;

  // Step the locator cursor through the sorted diff list: centered scroll +
  // a focus pulse on the target cast (declared here — needs xOf above).
  const stepDiff = (dir: 1 | -1) => {
    if (diffs.length === 0) return;
    const next =
      diffCursor < 0 || diffCursor >= diffs.length
        ? dir === 1 ? 0 : diffs.length - 1
        : (diffCursor + dir + diffs.length) % diffs.length;
    setDiffCursor(next);
    const d = diffs[next];
    setStepFocus({ lane: d.lane, idx: d.idx, nonce: Date.now() });
    const el = scrollRef.current;
    if (el) {
      el.scrollTo({
        left: Math.max(0, xOf(d.timeSec) - el.clientWidth / 2),
        behavior: 'smooth',
      });
    }
  };
  // Bare-letter diff stepping (n / ] next, N / [ previous). First bare-letter
  // global binding in the app, so it guards typing targets explicitly and
  // detaches on unmount (the view unmounts when leaving the tab). Deliberately
  // no dep array: re-subscribing keeps the closure's cursor fresh.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (
        t instanceof HTMLInputElement ||
        t instanceof HTMLTextAreaElement ||
        t instanceof HTMLSelectElement ||
        (t && t.isContentEditable)
      )
        return;
      if (e.key === 'n' || e.key === ']') {
        e.preventDefault();
        stepDiff(1);
      } else if (e.key === 'N' || e.key === '[') {
        e.preventDefault();
        stepDiff(-1);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  // Highlight-target tolerance for jump matching — the same form the
  // improvement-bubble matching uses, so a card and its highlight agree.
  const focusTol = Math.max(3, (analysis.headline.effectiveGcdSec || 2.5) * 1.5);

  // Index of the cast on YOUR lane nearest the dashboard jump target — derived
  // (not state) so the highlight follows `focus` without a setState-in-effect.
  // Suppressed for ideal-side kinds (the cast the card is about isn't on your
  // lane) and deaths (the death flag is the target): pulsing the nearest
  // delivered cast there would finger an innocent one.
  const focusIdx = useMemo<number | null>(() => {
    if (!focus || events.length === 0) return null;
    if (focus.kind != null && (IDEAL_KINDS.has(focus.kind) || focus.kind === 'death')) {
      return null;
    }
    return nearestCast(events, focus.timeSec, focus.abilityId, focusTol, true);
  }, [focus, events, focusTol]);

  // For ideal-side kinds: the sim-lane cast of that ability nearest the target.
  // The cards diff against the strict timeline while the lane shows the display
  // (buff-aware) one, so a match may be slightly off or absent — no match within
  // tolerance means no pulse (the focus marker line still lands exactly).
  const focusIdealIdx = useMemo<number | null>(() => {
    if (!focus || !focus.kind || !IDEAL_KINDS.has(focus.kind)) return null;
    if (idealized.length === 0) return null;
    return nearestCast(idealized, focus.timeSec, focus.abilityId, focusTol, false);
  }, [focus, idealized, focusTol]);

  // For death-card jumps: the rendered death flag nearest the target.
  const focusDeathIdx = useMemo<number | null>(() => {
    if (!focus || focus.kind !== 'death' || deaths.length === 0) return null;
    let best: number | null = null;
    let bd = Infinity;
    deaths.forEach((d, i) => {
      const dd = Math.abs(d.timeSec - focus.timeSec);
      if (dd < bd) { bd = dd; best = i; }
    });
    return bd <= 3 ? best : null;
  }, [focus, deaths]);

  // The dashboard suggestion the jump came from — matched by time so hovering
  // the jumped-to cast can re-show "why you're here".
  const focusImp = useMemo<Improvement | undefined>(() => {
    if (!focus) return undefined;
    let best: Improvement | undefined;
    let bd = Infinity;
    for (const im of located) {
      const d = Math.abs(im.timeSec - focus.timeSec);
      if (d < bd) { bd = d; best = im; }
    }
    return bd <= 2.5 ? best : undefined;
  }, [focus, located]);

  // Scroll the focused cast into the center of the viewport. DOM-only side
  // effect; re-runs on each new jump (nonce changes even for the same row).
  useEffect(() => {
    if (!focus) return;
    const el = scrollRef.current;
    if (el) {
      const x = xOf(focus.timeSec);
      el.scrollTo({ left: Math.max(0, x - el.clientWidth / 2), behavior: 'smooth' });
    }
    // pxPerSec is read at trigger time; only re-scroll on a new jump request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce]);

  // oGCDs ride an upper band, GCDs a lower band (OGCD_TOP / GCD_TOP / ICON_SIZE
  // are the shared band geometry from components/timeline/scale).
  const bandVisible = (isOgcd: boolean) => (isOgcd ? filter.ogcd : filter.gcd);

  const gapActive = hover && hover.kind !== 'you' && hover.kind !== 'mthit' ? hover.idx : null;
  const youActive = hover && hover.kind === 'you' ? hover.idx : null;

  const renderCast = (
    c: CastEvent,
    i: number,
    opts: {
      focused?: boolean;
      lane: 'you' | 'ideal' | 'ref';
      isDiff?: boolean;
      paired?: boolean;
      /** Folded into the element key so a re-step onto the same cast remounts
       *  it and restarts the focus pulse (the dashboard-jump nonce trick). */
      keySuffix?: string | number;
      onEnter?: (e: ReactMouseEvent<HTMLDivElement>) => void;
      onLeave?: () => void;
    },
  ) => {
    const { focused = false, lane, isDiff = false, paired = false, keySuffix, onEnter, onLeave } = opts;
    const isOgcd = c.yOffset < 0;
    if (!bandVisible(isOgcd)) return null;
    const meta = c.abilityId !== undefined ? analysis.abilityMeta[c.abilityId] : undefined;
    const name = meta?.name ?? c.tooltip;
    // Class precedence: a focus pulse always wins; otherwise in diff mode a
    // deviating cast gets a gutter marker bar (accent on your lane, sim-blue
    // on idealized) + a soft outline. Matched casts stay at FULL visibility —
    // the surrounding rotation is the context that makes a diff interpretable;
    // the marker's location does the work, not contrast.
    let cls = 'cast';
    if (focused) cls += ' focus';
    else if (diffMode && isDiff) cls += lane === 'you' ? ' you-diff' : ' diff';
    if (paired) cls += ' paired';
    // A reviewable squeeze (e.g. Flamethrower) sits inside a downtime window,
    // whose band (front overlay) is hoverable and would otherwise capture the
    // pointer — lift it above so its own tooltip/bubble wins. Ref lanes skip
    // the lift: their strips scroll under the pinned You/Sim rows, and a
    // z-lifted cast would ride over them; the band's bubble covers hover there.
    if (lane !== 'ref' && isReviewableCast(reviewable, c.abilityId, c.startSec)) cls += ' in-downtime';
    const baseTitle = `${name} @ ${c.startSec.toFixed(1)}s${lane === 'ideal' ? ' (simulated)' : ''}`;
    const title = diffMode && isDiff
      ? `Potential sequencing opportunity window · ${baseTitle} · ${
          lane === 'you' ? 'extra vs simulated' : 'missed in simulated'
        }`
      : baseTitle;
    // Multi-target badge: present on splash casts inside a confirmed window
    // (every lane). Hovering the whole icon surfaces the X/Y bubble — easier to
    // inspect than aiming at the dot. Yields to an existing diff/focus hover.
    const mt = (c.mtMax != null && !inDeniedMt(c.startSec))
      ? { hit: c.mtHit ?? 0, max: c.mtMax, lost: c.mtLost ?? 0, full: (c.mtHit ?? 0) >= c.mtMax }
      : null;
    const mtEnter = mt
      ? (e: ReactMouseEvent<HTMLDivElement>) => {
          // Anchor the bubble to the hovered icon's REAL row (mt casts exist on
          // every lane): rect math against the grid absorbs sticky + scroll
          // offsets exactly, at any UI zoom.
          const el = e.currentTarget;
          const grid = el.closest('.timeline');
          const topPx = grid
            ? (el.getBoundingClientRect().top - grid.getBoundingClientRect().top) / currentZoom()
              - HEAD_H + ICON_SIZE + 12
            : bubbleTop(scrollTopOf(e), isOgcd);
          setHover({ kind: 'mthit', hit: mt.hit, max: mt.max, lost: mt.lost, leftSec: c.startSec, topPx });
        }
      : undefined;
    const enter = onEnter ?? mtEnter;
    const leave = onLeave ?? (mt ? () => setHover(null) : undefined);
    return (
      <TimelineCast
        key={keySuffix != null ? `${i}-${keySuffix}` : i}
        cast={c}
        meta={meta}
        scale={scale}
        className={cls}
        title={title}
        onMouseEnter={enter}
        onMouseLeave={leave}
      />
    );
  };

  // Resolve the hovered anomaly into a display-ready bubble: positioned at the
  // actual hovered icon (correct lane + band) so the tail points right at it,
  // with the matched dashboard suggestion when we have one, else an honest
  // explanation of what the anomaly is.
  type BubbleView = {
    left: number; top: number;
    head: string; name?: string; body: string;
    cost: number; approx?: boolean; iconId?: number;
  };
  const clampLeft = (x: number) => clampBubbleLeft(x, stripWidth);
  const bubbleTop = (laneTop: number, isOgcd: boolean) =>
    laneTop + (isOgcd ? OGCD_TOP : GCD_TOP) + ICON_SIZE + 12;
  // The bubble overlay scrolls WITH the grid while the You/Sim lanes stay
  // stuck under the axis — so a bubble anchored to a pinned lane adds the
  // scroll offset captured into the hover state at enter time.
  const hoverScrollTop = hover?.scrollTop ?? 0;
  const defLaneH = showDefensives && defensiveEvents.length > 0 ? DEF_LANE_H : 0;
  // An unpinned lane sits at its natural row position; a pinned one rides the
  // scroll offset captured at hover time.
  const youLaneTop = pinYou ? hoverScrollTop : 0;
  // The Sim row parks right under You (when You is pinned) once the unpinned
  // Defensives lane between them (tanks only) has scrolled away; until then it
  // rides at its natural row position.
  const idealLaneTop = pinIdeal
    ? Math.max(LANE_H + defLaneH, (pinYou ? LANE_H : 0) + hoverScrollTop)
    : LANE_H + defLaneH;

  const bubble: BubbleView | null = (() => {
    if (!hover) return null;
    if (hover.kind === 'gap-ghost' || hover.kind === 'gap-ideal') {
      const c = idealized[hover.idx];
      if (!c) return null;
      // Diff hovers resolve via gapImp; a focus-only hover (the jumped-to
      // ideal cast, hoverable even outside diff mode) falls back to the
      // originating suggestion so "why you're here" survives the jump. A
      // focused cast isn't necessarily a diff, so only diffed casts get the
      // "you didn't cast it" claim.
      const isGap = idealDiff.has(hover.idx);
      const im = gapImp.get(hover.idx)
        ?? (hover.idx === focusIdealIdx ? focusImp : undefined);
      const name = analysis.abilityMeta[c.abilityId ?? -1]?.name ?? 'this cast';
      return {
        left: clampLeft(xOf(c.startSec)),
        top: bubbleTop(hover.kind === 'gap-ideal' ? idealLaneTop : youLaneTop, c.yOffset < 0),
        head: im
          ? kindLabel(im.kind, 'Missed cast')
          : isGap ? 'Potential sequencing opportunity window' : 'Jumped from suggestion',
        name, iconId: c.abilityId,
        body: im
          ? im.summary
          : isGap
            ? `${name} belongs here in the simulated rotation — you didn't cast it in this slot.`
            : `You jumped here from a Potential Improvement — ${name} is the nearest matching cast on the simulated lane.`,
        cost: im && im.lostPotency > 0 ? Math.round(im.lostPotency) : 0,
      };
    }
    if (hover.kind === 'you') {
      const c = events[hover.idx];
      if (!c) return null;
      const isExtra = youDiff.has(hover.idx);
      const im = isExtra ? extraImp.get(hover.idx) : focusImp;
      const name = analysis.abilityMeta[c.abilityId ?? -1]?.name ?? 'this cast';
      return {
        left: clampLeft(xOf(c.startSec)),
        top: bubbleTop(youLaneTop, c.yOffset < 0),
        head: isExtra ? 'Potential sequencing opportunity window' : 'Jumped from suggestion',
        name, iconId: c.abilityId,
        body: im
          ? im.summary
          : isExtra
            ? `${name} isn't in the simulated rotation here — likely an off-plan or drifted GCD. No specific potency was pinned to it; check your pacing around this point.`
            : multiTarget.some((w) => Math.abs(w.startSec - c.startSec) < 2.5)
              ? 'Multi-target zone begins here.'
              : 'You jumped here from a Potential Improvement on the dashboard.',
        cost: im && im.lostPotency > 0 ? Math.round(im.lostPotency) : 0,
      };
    }
    if (hover.kind === 'downA') {
      const w = downtimeA[hover.idx];
      if (!w) return null;
      return {
        left: clampLeft(xOf((w.startSec + w.endSec) / 2)),
        top: 30 + hoverScrollTop,
        head: 'Boss untargetable',
        body: `No enemy was targetable from ${fmtAxisTick(w.startSec)} to ${fmtAxisTick(w.endSec)} (${fmtDur(w.endSec - w.startSec)}). Confirmed from targetability events — this stretch is excluded from your rotation, so gaps here aren't counted against you.`,
        cost: 0,
      };
    }
    if (hover.kind === 'downB') {
      const w = downtimeB[hover.idx];
      if (!w) return null;
      return {
        left: clampLeft(xOf((w.startSec + w.endSec) / 2)),
        top: 30 + hoverScrollTop,
        head: 'Suspected downtime (consensus)',
        body: `${fmtDur(w.endSec - w.startSec)} (${fmtAxisTick(w.startSec)}–${fmtAxisTick(w.endSec)}) where at least ${w.nIdle}/${w.nTotal} reference players were also idle. Ambiguous — the ideal still casts here (and improvements may nudge), since it's not near-unanimous. Excluded under lenient scoring; strict still counts it as your time.`,
        cost: 0,
      };
    }
    if (hover.kind === 'downBHigh') {
      const w = downtimeBHigh[hover.idx];
      if (!w) return null;
      return {
        left: clampLeft(xOf((w.startSec + w.endSec) / 2)),
        top: 30 + hoverScrollTop,
        head: 'Forced downtime (high confidence)',
        body: `${fmtDur(w.endSec - w.startSec)} (${fmtAxisTick(w.startSec)}–${fmtAxisTick(w.endSec)}) where ${w.nIdle}/${w.nTotal} of the top parses were idle — near-unanimous, so this is treated as genuinely forced: the ideal skips it and it isn't scored against you. The window is trimmed to where the pool agrees, so edge squeezes stay yours.`,
        cost: 0,
      };
    }
    if (hover.kind === 'phase') {
      const ph = phases[hover.idx];
      if (!ph) return null;
      const dur = ph.endSec - ph.startSec;
      const reachTag = !ph.reached
        ? ' — not reached this pull'
        : !ph.completed
          ? ' — reached, ended the pull here'
          : '';
      return {
        left: clampLeft(xOf((ph.startSec + ph.endSec) / 2)),
        top: 30 + hoverScrollTop,
        head: ph.isIntermission ? `${ph.name} (intermission)` : ph.name,
        body: `${fmtAxisTick(ph.startSec)}–${fmtAxisTick(ph.endSec)} (${fmtDur(dur)})`
          + (ph.downtimeSec > 0.5 ? `, incl. ${fmtDur(ph.downtimeSec)} boss-untargetable` : '')
          + reachTag + '.',
        cost: 0,
      };
    }
    if (hover.kind === 'mt') {
      const w = multiTarget[hover.idx];
      if (!w) return null;
      return {
        left: clampLeft(xOf((w.startSec + w.endSec) / 2)),
        top: 30 + hoverScrollTop,
        head: `Multi-target window ×${w.targetCount}`,
        body: `${fmtDur(w.endSec - w.startSec)} (${fmtAxisTick(w.startSec)}–${fmtAxisTick(w.endSec)}) where ≥${w.targetCount} enemies were targetable and the top references cleaved too. Splash is credited here on both your output (+${Math.round(w.deliveredSplash).toLocaleString()}p) and the simulated ceiling (+${Math.round(w.ceilingSplash).toLocaleString()}p). Mark it "not possible" in the dashboard panel to drop it.`,
        cost: 0,
      };
    }
    if (hover.kind === 'mthit') {
      const full = hover.hit >= hover.max;
      return {
        left: clampLeft(xOf(hover.leftSec)),
        top: hover.topPx,
        head: full ? 'Multi-target hit' : 'Multi-target — under target',
        body: `Multi-target hit ${hover.hit}/${hover.max} targets${hover.lost > 0 ? ` — lost ${Math.round(hover.lost).toLocaleString()} potency` : ''}`,
        cost: hover.lost > 0 ? Math.round(hover.lost) : 0,
      };
    }
    if (hover.kind === 'death') {
      const d = deaths[hover.idx];
      if (!d) return null;
      let im: Improvement | undefined;
      let bd = Infinity;
      for (const x of deathImps) {
        const dd = Math.abs(x.timeSec - d.timeSec);
        if (dd < bd) { bd = dd; im = x; }
      }
      if (bd > 1) im = undefined;
      return {
        left: clampLeft(xOf(d.timeSec)),
        top: 30 + hoverScrollTop,
        head: 'Death',
        body: im
          ? im.summary
          : `Died at ${fmtAxisTick(d.timeSec)} — down for ${fmtDur(d.durationSec)}. The simulated rotation kept dealing damage through this; that lost uptime is priced separately.`,
        cost: im && im.lostPotency > 0 ? Math.round(im.lostPotency) : 0,
      };
    }
    if (hover.kind === 'idle') {
      const w = worstIdle[hover.idx];
      if (!w) return null;
      const [t, dur] = w;
      const lostGcds = dur / effGcd;
      const est = Math.round(lostGcds * avgGcdP);
      return {
        left: clampLeft(xOf(t + dur / 2)),
        top: bubbleTop(youLaneTop, false),
        head: 'Idle time',
        body: `${dur.toFixed(1)}s with no GCD — about ${lostGcds.toFixed(1)} dropped GCD${lostGcds >= 1.5 ? 's' : ''}. Keep your filler rolling through this gap.`,
        cost: est > 0 ? est : 0,
        approx: true,
      };
    }
    // clip
    const w = worstClips[hover.idx];
    if (!w) return null;
    const [t, clipSec, nOg] = w;
    const est = Math.round((clipSec / effGcd) * avgGcdP);
    return {
      left: clampLeft(xOf(t)),
      top: bubbleTop(youLaneTop, false),
      head: 'GCD clip',
      body: `Weaving ${nOg} oGCD${nOg === 1 ? '' : 's'} here pushed your next GCD ${clipSec.toFixed(2)}s late. Trim a weave or move one to a slower window.`,
      cost: est > 0 ? est : 0,
      approx: true,
    };
  })();

  const renderBubble = () => {
    if (!bubble) return null;
    const meta = bubble.iconId != null ? analysis.abilityMeta[bubble.iconId] : undefined;
    return (
      <div className="diff-bubble" style={{ left: bubble.left, top: bubble.top }}>
        <div className="bub-head">
          {meta && (
            <AbilityIcon kind="gcd1" glyph={(meta.name ?? '').slice(0, 3)} name={meta.name} iconPath={meta.iconPath} size={22} />
          )}
          <div>
            <div className="bub-kind">{bubble.head}</div>
            {bubble.name && <div className="bub-name">{bubble.name}</div>}
          </div>
          {bubble.cost > 0 && (
            <div className="bub-cost">
              {bubble.approx ? '≈ ' : ''}−{bubble.cost.toLocaleString()}p
            </div>
          )}
        </div>
        <div className="bub-body">{bubble.body}</div>
      </div>
    );
  };

  // --- Slots handed to the shared shell ------------------------------------
  // The Timeline page's only non-generic toolbar controls: the diff Highlight
  // toggle and the optimal/canonical Burst-usage toggle.
  const toolbarExtra = (
    <>
      {canDiff && (
        <div className="row" style={{ gap: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>Highlight</span>
          <div className="segctrl">
            <button className={highlightDiff ? 'on' : ''} onClick={() => setHL(true)}>Diffs only</button>
            <button className={!highlightDiff ? 'on' : ''} onClick={() => setHL(false)}>All casts</button>
          </div>
        </div>
      )}
      {canDiff && diffs.length > 0 && (
        <div className="tl-diff-locator">
          <span className="count">{diffs.length} diff{diffs.length === 1 ? '' : 's'}</span>
          <button className="step" onClick={() => stepDiff(-1)} title="Previous diff (N or [)">
            ‹
          </button>
          <span className="pos">
            {diffCursor >= 0 && diffCursor < diffs.length
              ? `${diffCursor + 1} / ${diffs.length} · ${fmtClock(Math.max(0, Math.round(diffs[diffCursor].timeSec)))}`
              : `– / ${diffs.length}`}
          </span>
          <button className="step" onClick={() => stepDiff(1)} title="Next diff (n or ])">
            ›
          </button>
        </div>
      )}
      {hasCanonical && (
        <div className="row" style={{ gap: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>Burst Usage</span>
          <div className="segctrl">
            <button
              className={idealMode === 'optimal' ? 'on' : ''}
              onClick={() => setIdealMode('optimal')}
              title="The simulator tends to estimate immediate burst usage generates more potency than during buff use — especially in a weak party buff scenario."
            >
              Simulated
            </button>
            <button
              className={idealMode === 'canonical' ? 'on' : ''}
              onClick={() => setIdealMode('canonical')}
              title="Force the simulator to use standard opener burst timing."
            >
              Canonical
            </button>
          </div>
        </div>
      )}
    </>
  );

  // Multi-target zones + observed-buff bands — behind the casts so they stay
  // visible + hoverable (the mt hover chip lives in the front overlay).
  const backOverlay = (
    <>
      {buffZones.map((z, i) => (
        <div
          key={`bz${i}`}
          className="tl-buff-zone"
          style={{ left: xOf(z.s), width: (z.e - z.s) * pxPerSec }}
        />
      ))}
      {multiTarget.map((w, i) => (
        <div
          key={`mt${i}`}
          className={`tl-mt-zone${hover?.kind === 'mt' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
        />
      ))}
    </>
  );

  // Invisible per-strip hover surfaces for the multi-target windows: the
  // visible zone fill is a back-overlay layer no pointer can reach (the strips
  // sit on top of it), so each strip carries a transparent strip-height twin
  // wired to the mt bubble. Casts render after it and stay on top for their
  // own hovers — this only catches the window's empty background.
  const mtHoverStrips = multiTarget.map((w, i) => (
    <div
      key={`mth${i}`}
      className="tl-mt-hover"
      style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
      onMouseEnter={(e) => setHover({ kind: 'mt', idx: i, scrollTop: scrollTopOf(e) })}
      onMouseLeave={() => setHover(null)}
    />
  ));

  // The pinned You/Sim strips are opaque (they park over scrolling refs), so
  // the back overlay can't show through them — each pinned strip re-renders
  // the context bands inside itself: pre-pull shade, observed-buff zones,
  // multi-target zones. pointer-events:none keeps the casts hoverable.
  // Rendered per lane only while THAT lane is pinned: an unpinned strip is
  // transparent again and the real overlay bands show through — the tints
  // would double-paint.
  const stripTints = (
    <>
      {scale.prezoneSec > 0 && (
        <div className="tl-strip-tint prezone" style={{ left: 0, width: xOf(0) }} />
      )}
      {buffZones.map((z, i) => (
        <div
          key={`bz${i}`}
          className="tl-strip-tint buff"
          title={z.labels.join(' · ')}
          style={{ left: xOf(z.s), width: (z.e - z.s) * pxPerSec }}
        />
      ))}
      {multiTarget.map((w, i) => (
        <div
          key={`mtt${i}`}
          className={`tl-strip-tint mt${hover?.kind === 'mt' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
        />
      ))}
      {/* Downtime bands re-tinted for the pinned strips: the real hoverable
          bands (front overlay) sit UNDER the opaque pinned rows, so these
          carry the same hover wiring — the downtime bubble keeps working over
          the pinned lanes, and `.on` lights band + tints together. Casts
          render after and stay on top for their own hovers. */}
      {downtimeA.map((w, i) => (
        <div
          key={`dta${i}`}
          className={`tl-strip-tint down down-a${hover?.kind === 'downA' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downA', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {downtimeB.map((w, i) => (
        <div
          key={`dtb${i}`}
          className={`tl-strip-tint down down-b${hover?.kind === 'downB' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downB', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {downtimeBHigh.map((w, i) => (
        <div
          key={`dtbh${i}`}
          className={`tl-strip-tint down down-bh${hover?.kind === 'downBHigh' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downBHigh', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
    </>
  );

  // Compact Defensives-lane renderer (You + each ref): a single band of icons,
  // native-title tooltip, no diff/scoring participation.
  const renderDefensiveStrip = (track: CastEvent[]) =>
    track.map((c, i) => {
      const m = c.abilityId != null ? analysis.abilityMeta[c.abilityId] : undefined;
      const name = m?.name ?? castDisplayName(c, analysis.abilityMeta);
      return (
        <TimelineCast
          key={i}
          cast={c}
          meta={m}
          scale={scale}
          className="cast def"
          title={`${name} @ ${c.startSec.toFixed(1)}s`}
          size={DEF_ICON}
          top={DEF_TOP}
        />
      );
    });

  const lanes = (
    <>
      <div className={`tl-row you${pinYou ? ' pinned' : ''}`}>
        <div className="label">
          <div className="lbl-lines">
            <div className="lbl-top">
              {/* Research loads carry the ranked player's name; normal runs say "You". */}
              <span className="lbl-name">{analysis.you.label || 'You'}</span>
              <span className="badge">You</span>
            </div>
            <button
              className={`tl-lane-pin${pinYou ? ' on' : ''}`}
              aria-pressed={pinYou}
              title="Keep this lane parked under the axis while the reference lanes scroll"
              onClick={() => setPinYou((v) => !v)}
            >
              <Pin size={9} />
              {pinYou ? 'Pinned' : 'Pin'}
            </button>
          </div>
          <span className="band-lbl ogcd">oGCD</span>
          <span className="band-lbl gcd">GCD</span>
        </div>
        <div className="strip" style={stripStyle}>
          {pinYou && stripTints}
          {mtHoverStrips}
          {youPots.map((w, i) => (
            <div
              key={`pot${i}`}
              className="tl-pot you"
              style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
            >
              <span className="lbl">pot</span>
            </div>
          ))}
          {events.map((c, i) => {
            const stepped = stepFocus?.lane === 'you' && stepFocus.idx === i;
            const hoverable = (diffMode && youDiff.has(i)) || i === focusIdx;
            return renderCast(c, i, {
              focused: i === focusIdx || stepped,
              lane: 'you',
              isDiff: youDiff.has(i),
              paired: youActive === i,
              keySuffix: stepped ? stepFocus.nonce : undefined,
              onEnter: hoverable ? (e) => setHover({ kind: 'you', idx: i, scrollTop: scrollTopOf(e) }) : undefined,
              onLeave: hoverable ? () => setHover(null) : undefined,
            });
          })}
          {/* Gap ghosts: where a missed (idealized) cast belonged, drawn on
              YOUR lane so the diff reads against your own rotation. */}
          {diffMode &&
            [...idealDiff].map((ii) => {
              const c = idealized[ii];
              const isOgcd = c.yOffset < 0;
              if (!bandVisible(isOgcd)) return null;
              const meta = c.abilityId != null ? analysis.abilityMeta[c.abilityId] : undefined;
              return (
                <div
                  key={`ghost${ii}`}
                  className={`tl-ghost${gapActive === ii ? ' paired' : ''}${isReviewableCast(reviewable, c.abilityId, c.startSec) ? ' in-downtime' : ''}`}
                  style={{ left: xOf(c.startSec), top: isOgcd ? OGCD_TOP : GCD_TOP }}
                  onMouseEnter={(e) => setHover({ kind: 'gap-ghost', idx: ii, scrollTop: scrollTopOf(e) })}
                  onMouseLeave={() => setHover(null)}
                >
                  <span className="ghost-ico">
                    <AbilityIcon kind="gcd1" glyph={c.label} name={meta?.name} iconPath={c.iconPath ?? meta?.iconPath} size={ICON_SIZE} />
                  </span>
                </div>
              );
            })}
          {/* Pacing anomalies on your lane — idle gaps (band) + GCD clips
              (tick). Always shown; hover for the explanation. */}
          {worstIdle.map(([t, dur], k) => (
            <div
              key={`idle${k}`}
              className={`tl-idle${hover?.kind === 'idle' && hover.idx === k ? ' on' : ''}`}
              style={{
                left: xOf(t),
                width: Math.max(6, dur * pxPerSec),
                top: GCD_TOP - 4,
                height: ICON_SIZE + 8,
              }}
              onMouseEnter={(e) => setHover({ kind: 'idle', idx: k, scrollTop: scrollTopOf(e) })}
              onMouseLeave={() => setHover(null)}
            />
          ))}
          {worstClips.map(([t], k) => (
            <div
              key={`clip${k}`}
              className={`tl-clip${hover?.kind === 'clip' && hover.idx === k ? ' on' : ''}`}
              style={{ left: xOf(t), top: GCD_TOP - 4, height: ICON_SIZE + 8 }}
              onMouseEnter={(e) => setHover({ kind: 'clip', idx: k, scrollTop: scrollTopOf(e) })}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        </div>
      </div>

      {showDefensives && defensiveEvents.length > 0 && (
        <div className="tl-row def">
          <div className="label">
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              Defensives
            </span>
            <span className="badge">Def</span>
          </div>
          <div className="strip def" style={stripStyle}>
            {renderDefensiveStrip(defensiveEvents)}
          </div>
        </div>
      )}

      {idealized.length > 0 && (
        <div
          className={`tl-row ideal${pinIdeal ? ' pinned' : ''}`}
          // With the You lane unpinned there's nothing to park under — the Sim
          // lane pins directly beneath the axis instead of leaving a gap.
          style={pinYou ? undefined : ({ '--tl-ideal-top': 'var(--tl-head-h)' } as CSSProperties)}
        >
          <div className="label">
            <div className="lbl-lines">
              <div className="lbl-top">
                <span className="lbl-name">
                  {idealMode === 'canonical' && hasCanonical ? 'Canonical' : 'Simulated'}
                </span>
                <span className="badge">Sim</span>
              </div>
              <button
                className={`tl-lane-pin${pinIdeal ? ' on' : ''}`}
                aria-pressed={pinIdeal}
                title="Keep this lane parked under the axis while the reference lanes scroll"
                onClick={() => setPinIdeal((v) => !v)}
              >
                <Pin size={9} />
                {pinIdeal ? 'Pinned' : 'Pin'}
              </button>
            </div>
            <span className="band-lbl ogcd">oGCD</span>
            <span className="band-lbl gcd">GCD</span>
          </div>
          <div className="strip" style={stripStyle}>
            {pinIdeal && stripTints}
            {mtHoverStrips}
            {idealPots.map((w, i) => (
              <div
                key={`pot${i}`}
                className="tl-pot ideal"
                style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
              >
                <span className="lbl">pot</span>
              </div>
            ))}
            {idealized.map((c, i) => {
              const stepped = stepFocus?.lane === 'ideal' && stepFocus.idx === i;
              const hoverable = (diffMode && idealDiff.has(i)) || i === focusIdealIdx;
              return renderCast(c, i, {
                focused: i === focusIdealIdx || stepped,
                lane: 'ideal',
                isDiff: idealDiff.has(i),
                paired: gapActive === i,
                keySuffix: stepped ? stepFocus.nonce : undefined,
                onEnter: hoverable ? (e) => setHover({ kind: 'gap-ideal', idx: i, scrollTop: scrollTopOf(e) }) : undefined,
                onLeave: hoverable ? () => setHover(null) : undefined,
              });
            })}
          </div>
        </div>
      )}

      {filter.refs && refLanes.length > 0 && (
        <div className="tl-row ref-divider">
          <div className="label">
            {refLanes.length} reference{refLanes.length === 1 ? '' : 's'}
          </div>
          <div className="strip" style={{ width: stripWidth }}>
            <span className="sort-note">top 10 by rDPS ↓</span>
          </div>
        </div>
      )}
      {filter.refs &&
        refLanes.map((r, ri) => (
          <Fragment key={ri}>
            <div className="tl-row ref">
              {/* Efficiency in the gutter (the one useful fact about a ref);
                  the character name survives as the tooltip. Numbered by the
                  sorted position, so #1 is always the best. */}
              <div className="label" title={r.name}>
                <span className="rank">#{ri + 1}</span>
                <span className="ref-eff">
                  {r.eff > 0 ? `${r.eff.toFixed(1)}%` : '—'}
                </span>
                <span style={{ flex: 1 }} />
                <span className="badge">Ref</span>
                <span className="band-lbl ogcd">oGCD</span>
                <span className="band-lbl gcd">GCD</span>
              </div>
              <div className="strip" style={stripStyle}>
                {mtHoverStrips}
                {r.pots.map((w, i) => (
                  <div
                    key={`pot${i}`}
                    className="tl-pot ref"
                    style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
                  >
                    <span className="lbl">pot</span>
                  </div>
                ))}
                {r.track.map((c, i) => renderCast(c, i, { lane: 'ref' }))}
              </div>
            </div>
            {showDefensives && r.def.length > 0 && (
              <div className="tl-row def ref">
                <div className="label" title={r.name}>
                  <span className="rank">#{ri + 1}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {r.name}
                  </span>
                  <span className="badge">Def</span>
                </div>
                <div className="strip def" style={stripStyle}>
                  {renderDefensiveStrip(r.def)}
                </div>
              </div>
            )}
          </Fragment>
        ))}
    </>
  );

  // Above-casts overlay: phase track (top), downtime bands (hoverable),
  // multi-target flags, deaths.
  const frontOverlay = (
    <>
      {/* Phase track: a slim labeled band per boss phase pinned to the top of
          the plot, with a separator at each boundary. Phased fights only. */}
      {phases.map((ph, i) => (
        <div
          key={`ph${ph.id}-${i}`}
          className={
            'tl-phase-band'
            + (ph.isIntermission ? ' intermission' : '')
            + (!ph.reached ? ' unreached' : '')
            + (hover?.kind === 'phase' && hover.idx === i ? ' on' : '')
          }
          style={{ left: xOf(ph.startSec), width: (ph.endSec - ph.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'phase', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        >
          <span className="lbl">{ph.name}</span>
        </div>
      ))}
      {downtimeA.map((w, i) => (
        <div
          key={`a${i}`}
          className={`tl-band tier-a${hover?.kind === 'downA' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downA', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {downtimeB.map((w, i) => (
        <div
          key={`b${i}`}
          className={`tl-band tier-b${hover?.kind === 'downB' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downB', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {/* High-confidence cores overlaid on the suspected hatch (a subset of it).
          On top so a hover over the core reads it; the lighter edges fall through
          to the tier-b band beneath. */}
      {downtimeBHigh.map((w, i) => (
        <div
          key={`bh${i}`}
          className={`tl-band tier-b-high${hover?.kind === 'downBHigh' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'downBHigh', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {/* Death markers: the vertical line spans the lanes; the skull chip lives
          in the flag band above the axis (see flagBand). */}
      {deaths.map((d, i) => (
        <div key={`d${i}`} className="tl-death" style={{ left: xOf(d.timeSec) }}>
          <div className="tl-death-line" />
        </div>
      ))}
      {/* Exact-time marker for every dashboard jump: a transient vertical line
          at the jump target, so the location reads precisely even when no cast
          matched (or the target sits inside a gap). Blue — navigation, never a
          counted mistake. Remounts per nonce to restart the fade. */}
      {focus && (
        <div
          key={`fl${focus.nonce}`}
          className="tl-focus-line"
          style={{ left: xOf(focus.timeSec) }}
        />
      )}
    </>
  );

  // Flag band content — the chips that used to float over the You lane's top
  // strip now get their own sticky row above the axis, so they never cover
  // casts and never collide with tick labels.
  const flagBand = (
    <>
      {multiTarget.map((w, i) => (
        <div
          key={`mt${i}`}
          className={`tl-flag mt${hover?.kind === 'mt' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec) }}
          onMouseEnter={(e) => setHover({ kind: 'mt', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        >
          <Crosshair size={10} />
          <span>×{w.targetCount}</span>
        </div>
      ))}
      {deaths.map((d, i) => {
        // The nonce in the key remounts a focused flag on re-click, restarting
        // the pulse animation for the same death.
        const isFocus = i === focusDeathIdx;
        return (
          <div
            key={isFocus ? `d${i}f${focus?.nonce}` : `d${i}`}
            className={`tl-flag death${isFocus ? ' focus' : ''}`}
            style={{ left: xOf(d.timeSec) }}
            onMouseEnter={(e) => setHover({ kind: 'death', idx: i, scrollTop: scrollTopOf(e) })}
            onMouseLeave={() => setHover(null)}
          >
            <Skull size={11} />
          </div>
        );
      })}
    </>
  );

  return (
    <div className="content timeline-content" style={{ paddingTop: 16 }}>
      <TimelineShell
        scale={scale}
        zoom={zoom}
        setZoom={setZoom}
        filter={filter}
        setFilter={setFilter}
        hasRefs={refLanes.length > 0}
        toolbarExtra={toolbarExtra}
        belowToolbar={
          canDiff && diffs.length > 0 ? (
            <div className="diff-ruler-row">
              <DiffRuler
                diffs={diffs}
                cursor={diffCursor}
                maxTime={scale.maxTime}
                prezoneSec={scale.prezoneSec}
                scrollRef={scrollRef}
              />
            </div>
          ) : undefined
        }
        helpText={
          'Hover anything for details — casts, gaps, downtime bands, multi-target zones, deaths, idle/clip markers.\n' +
          'oGCDs ride the upper band, GCDs the lower.\n' +
          'n / ] next diff, N / [ previous — or use the locator buttons.\n' +
          'Click empty track to pin a time; click again to clear.\n' +
          'Gridlines mark the axis ticks.'
        }
        scrollRef={scrollRef}
        pinnedLanes={pinYou || (idealized.length > 0 && pinIdeal)}
        flags={flagBand}
        backOverlay={backOverlay}
        lanes={lanes}
        frontOverlay={frontOverlay}
        bubble={renderBubble()}
      />
    </div>
  );
};
