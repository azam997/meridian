// Reviewable-window registry — the per-kind prose / icon for the situational
// ceiling squeezes the backend emits as `analysis.reviewableWindows`, plus the
// helpers Dashboard + Timeline use to render them generically. This replaces the
// hardcoded Flamethrower knowledge that used to live in flamethrower.ts and leak
// into both shared views: the backend now ships the windows as data, and adding
// a second job's situational window is a registry entry here + a backend
// producer, not an edit to DashboardView / TimelineView.

import type { ReactNode } from 'react';
import { Flame } from 'lucide-react';
import type { ReviewableWindow, ReviewableWindowGroup } from '../sidecar/contract';

/** Per-group-kind UI: icon, heading, the singular noun for the KPI hint, and a
 *  blurb (given the per-window potency, which the backend supplies as data). */
type GroupMeta = {
  icon: ReactNode;
  heading: string;
  /** Singular noun for the "Excludes N <noun>s you marked impossible" hint. */
  noun: string;
  blurb: (potency: number) => ReactNode;
};

const GROUP_META: Record<string, GroupMeta> = {
  flamethrower: {
    icon: <Flame size={14} />,
    heading: 'Flamethrower windows',
    noun: 'Flamethrower squeeze',
    blurb: (p) => (
      <>
        At each boss-untargetable edge the sim judges whether there's a large
        enough window to cast a Flamethrower ({p}p) before another action needs
        to be taken. However, the sim can't judge movement or other
        incapacitation in these windows. If movement or other factors made a
        Flamethrower window impossible, mark it as such.
      </>
    ),
  },
};

const DEFAULT_META: GroupMeta = {
  icon: <Flame size={14} />,
  heading: 'Situational windows',
  noun: 'window',
  blurb: () => (
    <>
      Situational opportunities the sim assumed. If one wasn't actually possible,
      mark it so its potency is dropped from the ceiling.
    </>
  ),
};

export const groupMeta = (kind: string): GroupMeta => GROUP_META[kind] ?? DEFAULT_META;

/** The ceiling-only groups — the ones whose denial adjusts just the idealized
 *  ceiling (the multi-target windows, side 'both', ride their own path). */
export const ceilingGroups = (
  groups: ReviewableWindowGroup[] | undefined,
): ReviewableWindowGroup[] => (groups ?? []).filter((g) => g.side === 'ceiling');

const ceilingWindows = (groups: ReviewableWindowGroup[] | undefined): ReviewableWindow[] =>
  ceilingGroups(groups).flatMap((g) => g.windows);

/** Total ceiling potency the user denied across all ceiling groups. */
export const deniedCeilingPotency = (
  groups: ReviewableWindowGroup[] | undefined,
  denied: Set<string>,
): number =>
  ceilingWindows(groups).reduce((acc, w) => acc + (denied.has(w.id) ? w.potency : 0), 0);

/** Count of denied ceiling windows (for the KPI hint pluralization). */
export const deniedCeilingCount = (
  groups: ReviewableWindowGroup[] | undefined,
  denied: Set<string>,
): number => ceilingWindows(groups).filter((w) => denied.has(w.id)).length;

/** The noun to describe denied ceiling windows in the hint. Uses the single
 *  ceiling group's noun when there's exactly one kind, else a neutral fallback. */
export const ceilingNoun = (groups: ReviewableWindowGroup[] | undefined): string => {
  const cg = ceilingGroups(groups);
  return cg.length === 1 ? groupMeta(cg[0].kind).noun : 'situational window';
};

/** Is this cast one of the reviewable windows (matched by ability + exact time —
 *  the window's timeSec is the same backend float as the idealized-lane cast's
 *  startSec, so they compare equal across the JSON round-trip)? Drives the
 *  in-downtime z-lift on the timeline. */
export const isReviewableCast = (
  groups: ReviewableWindowGroup[] | undefined,
  abilityId: number | undefined,
  timeSec: number,
): boolean =>
  abilityId != null &&
  (groups ?? []).some((g) =>
    g.windows.some((w) => w.abilityId === abilityId && w.timeSec === timeSec),
  );

/** Is this cast a reviewable window the user DENIED (so it must drop off the
 *  idealized lane + cast-diff)? */
export const isDeniedReviewableCast = (
  groups: ReviewableWindowGroup[] | undefined,
  denied: Set<string>,
  abilityId: number | undefined,
  timeSec: number,
): boolean =>
  abilityId != null &&
  (groups ?? []).some((g) =>
    g.windows.some(
      (w) => w.abilityId === abilityId && w.timeSec === timeSec && denied.has(w.id),
    ),
  );

/** Does a denied reviewable window correspond to this improvement (same ability,
 *  ~same time)? Lets the dashboard hide a "missed Flamethrower" card once the
 *  user says that squeeze wasn't possible. Numeric match (no id round-trip) with
 *  a small tolerance, since the improvement's time comes from the strict
 *  timeline while the window's comes from the display one (identical for the
 *  downtime-edge squeeze, but tolerant just in case). */
export const isImprovementDenied = (
  groups: ReviewableWindowGroup[] | undefined,
  denied: Set<string>,
  abilityId: number,
  timeSec: number,
): boolean =>
  (groups ?? []).some((g) =>
    g.windows.some(
      (w) =>
        w.abilityId === abilityId &&
        denied.has(w.id) &&
        Math.abs(w.timeSec - timeSec) < 0.05,
    ),
  );
