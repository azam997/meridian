import { useMemo } from 'react';
import type { CSSProperties } from 'react';
import { fmtClock } from '../../format';
import type { CastEvent } from '../../sidecar/contract';

// Shared band geometry for every rotation timeline (the feature-rich Timeline
// page and the embedded Kill Time Theorizer lane). oGCDs ride the upper band,
// GCDs the lower; kept in sync with `.tl-row .strip` height in main.css.
export const OGCD_TOP = 14;
export const GCD_TOP = 92;
export const ICON_SIZE = 40;
export const LANE_H = 150; // .tl-row .strip height; used to place lane-relative bubbles.

// Pre-pull zone: render up to this many seconds of NEGATIVE time so precast
// channels (RDM Verthunder III, RPR Harpe) + pre-pull setup (MCH Reassemble)
// aren't clipped at the t=0 edge. A class that precasts earlier renders clamped.
const PREZONE_MAX_S = 10;

/** Axis / chip label for a time that may be negative (the pre-pull zone), e.g. -5s. */
export const fmtTick = (s: number): string => (s < 0 ? `-${fmtClock(-s, true)}` : fmtClock(s, true));

/** Everything a lane/overlay needs to place itself: the time→x scale, the strip
 *  geometry, the axis ticks, and the per-strip CSS vars that keep the gridlines
 *  aligned to the ticks at any zoom. Pure derivation of (zoom, lane casts). */
export type TimelineScale = {
  zoom: number;
  pxPerSec: number;
  prezoneSec: number;
  maxTime: number;
  stripWidth: number;
  xOf: (sec: number) => number;
  secOf: (x: number) => number;
  ticks: number[];
  tickInterval: number;
  /** Width + grid CSS vars for a `.strip` element. */
  stripStyle: CSSProperties;
};

const lastEnd = (arr: CastEvent[]) => (arr.length ? arr[arr.length - 1].endSec : 0);
const firstStart = (arr: CastEvent[]) => (arr.length ? arr[0].startSec : 0);

/** Compute the timeline scale for a set of lanes. `laneCasts` MUST be a stable
 *  (memoized) reference per render or the scale recomputes every frame.
 *  `minMaxTime` extends the strip to at least this time (the theorizer's target
 *  kill time, so the strip always reaches the marker even past the last cast). */
export function useTimelineScale(
  zoom: number,
  laneCasts: CastEvent[][],
  minMaxTime = 0,
): TimelineScale {
  return useMemo(() => {
    // Generous horizontal scale — these are close-examination views, so casts
    // are spread out and we rely on horizontal scroll rather than cramming.
    const pxPerSec = 24 * zoom;
    const maxTime = Math.max(
      60,
      Math.ceil(Math.max(minMaxTime, ...laneCasts.map(lastEnd))) + 5,
    );
    const earliestStart = Math.min(0, ...laneCasts.map(firstStart));
    const prezoneSec = earliestStart < -0.01
      ? Math.min(PREZONE_MAX_S, Math.ceil(-earliestStart) + 1)
      : 0;
    // Time→x and its inverse. The pre-pull zone occupies the first `prezoneSec`
    // worth of px, so t=0 sits at `prezoneSec*pxPerSec`; earlier casts clamp left.
    const xOf = (sec: number) => (Math.max(sec, -prezoneSec) + prezoneSec) * pxPerSec;
    const secOf = (x: number) => x / pxPerSec - prezoneSec;
    const stripWidth = Math.max(800, (maxTime + prezoneSec) * pxPerSec);
    const tickInterval = zoom >= 1.4 ? 5 : zoom >= 0.8 ? 10 : 20;
    // Gridlines track the axis ticks so "gridlines mark Ns" holds at any zoom.
    const gridPx = tickInterval * pxPerSec;
    // Shift the gridline pattern so a line lands on t=0 (and every tick) despite
    // the pre-pull offset.
    const gridOffsetPx = (((prezoneSec * pxPerSec) % gridPx) + gridPx) % gridPx;
    const ticks: number[] = [];
    for (
      let s = Math.ceil(-prezoneSec / tickInterval) * tickInterval;
      s <= maxTime;
      s += tickInterval
    )
      ticks.push(s);
    const stripStyle = {
      width: stripWidth,
      minWidth: '100%',
      '--grid-px': `${gridPx}px`,
      '--grid-offset': `${gridOffsetPx}px`,
    } as CSSProperties;
    return { zoom, pxPerSec, prezoneSec, maxTime, stripWidth, xOf, secOf, ticks, tickInterval, stripStyle };
  }, [zoom, laneCasts, minMaxTime]);
}

/** Keep a bubble inside the strip's horizontal bounds (shared by both views). */
export const clampBubbleLeft = (x: number, stripWidth: number): number =>
  Math.min(Math.max(x, 130), stripWidth - 130);
