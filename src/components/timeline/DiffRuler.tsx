import { useEffect, useRef, useState, type RefObject } from 'react';

export type RulerDiff = { lane: 'you' | 'ideal'; timeSec: number };

type Props = {
  /** Every diff in the pull, sorted by time. */
  diffs: RulerDiff[];
  /** Index of the locator's current diff (highlighted tick), −1 for none. */
  cursor: number;
  /** Fight extent in seconds (`scale.maxTime`) + the pre-pull zone width. */
  maxTime: number;
  prezoneSec: number;
  /** The `.timeline-scroll` container — the ruler reads its scroll metrics for
   *  the viewport frame and writes `scrollLeft` on click/drag. */
  scrollRef: RefObject<HTMLDivElement | null>;
};

/** Whole-pull overview band: a 2px tick per diff (accent = yours, sim = the
 *  simulated lane's), the locator's current diff emphasized, and the visible
 *  viewport drawn as a draggable frame. Lives OUTSIDE the scroll container (a
 *  9-minute fight scrolls; the overview must not), so all geometry is
 *  fractional: ticks at (t + prezone) / (maxTime + prezone), the frame from
 *  scrollLeft / scrollWidth. Ratios of client rects are zoom-invariant, so no
 *  root-zoom correction is needed here (unlike the shell's crosshair). */
export const DiffRuler = ({ diffs, cursor, maxTime, prezoneSec, scrollRef }: Props) => {
  const bandRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [frame, setFrame] = useState<{ left: number; width: number } | null>(null);

  // Track the scroll container's viewport as fractions of its content width.
  // rAF-throttled: scroll events fire per frame anyway, but ResizeObserver +
  // zoom changes can burst.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let raf = 0;
    const update = () => {
      raf = 0;
      const sw = el.scrollWidth || 1;
      const w = el.clientWidth / sw;
      setFrame(w >= 0.999 ? null : { left: el.scrollLeft / sw, width: w });
    };
    const req = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };
    update();
    el.addEventListener('scroll', req, { passive: true });
    const ro = new ResizeObserver(req);
    ro.observe(el);
    return () => {
      el.removeEventListener('scroll', req);
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
    // Re-attach if the scroll element identity changes (view remounts).
  }, [scrollRef]);

  const total = maxTime + prezoneSec || 1;
  const fx = (t: number) => ((t + prezoneSec) / total) * 100;

  const seek = (clientX: number) => {
    const band = bandRef.current;
    const el = scrollRef.current;
    if (!band || !el) return;
    const r = band.getBoundingClientRect();
    if (r.width <= 0) return;
    const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    el.scrollLeft = frac * el.scrollWidth - el.clientWidth / 2;
  };

  return (
    <div
      ref={bandRef}
      className="diff-ruler"
      title="Potential sequencing opportunity windows · click or drag to pan the timeline"
      onPointerDown={(e) => {
        dragging.current = true;
        (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
        seek(e.clientX);
      }}
      onPointerMove={(e) => {
        if (dragging.current) seek(e.clientX);
      }}
      onPointerUp={() => {
        dragging.current = false;
      }}
      onPointerCancel={() => {
        dragging.current = false;
      }}
    >
      <div className="diff-ruler-axis" />
      {diffs.map((d, i) => (
        <span
          key={i}
          className={`diff-ruler-tick ${d.lane}${i === cursor ? ' cur' : ''}`}
          style={{ left: `${fx(d.timeSec)}%` }}
        />
      ))}
      {frame && (
        <div
          className="diff-ruler-frame"
          style={{ left: `${frame.left * 100}%`, width: `${frame.width * 100}%` }}
        />
      )}
    </div>
  );
};
