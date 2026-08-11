import { useRef, useState, type CSSProperties, type ReactNode, type Ref } from 'react';
import { Filter, HelpCircle, Minus, Pin, ZoomIn } from 'lucide-react';
import { currentZoom } from '../../state/zoom';
import { fmtTick, type TimelineScale } from './scale';

/** GCD / oGCD band visibility + (when there are ref lanes) the refs toggle. */
export type FilterState = { gcd: boolean; ogcd: boolean; refs: boolean };

/** An extra labeled axis tick (e.g. the theorizer's target kill-time marker). */
export type AxisMark = { sec: number; label: string; className?: string };

type Props = {
  scale: TimelineScale;
  zoom: number;
  setZoom: (z: number) => void;
  filter: FilterState;
  setFilter: (update: (prev: FilterState) => FilterState) => void;
  /** Show the "Show reference lanes" filter checkbox. */
  hasRefs: boolean;
  /** Left-aligned extra toolbar controls (Highlight / Burst-usage toggles). */
  toolbarExtra?: ReactNode;
  helpText: string;
  /** Extra labeled axis ticks drawn on top of the regular grid ticks. */
  axisMarks?: AxisMark[];
  /** Behind-casts overlay content (multi-target / raid-buff zones). The shell
   *  adds the pre-pull shade + the pinned-time line around it. */
  backOverlay?: ReactNode;
  /** The lanes — each a `.tl-row` using `display:contents` to drop into the grid. */
  lanes: ReactNode;
  /** Above-casts overlay content (downtime bands, window flags, deaths). The
   *  shell adds the live crosshair line around it. */
  frontOverlay?: ReactNode;
  /** The hover bubble, in its own top layer above everything. */
  bubble?: ReactNode;
  /** Forwarded onto `.timeline-scroll` so the caller can scroll-to-time (the
   *  Timeline page's dashboard "jump to cast"). */
  scrollRef?: Ref<HTMLDivElement>;
  /** Embedded mode (the theorizer card) constrains height instead of filling
   *  the timeline tab. */
  embedded?: boolean;
};

/** The shared timeline chrome: toolbar (zoom / filter / help + an extra slot),
 *  the scrolling strip with the sticky axis, the pre-pull zone, the live
 *  crosshair, and click-to-pin. Everything generic to a rotation timeline lives
 *  here; the caller supplies the lanes, the overlay content, and the bubble. */
export const TimelineShell = ({
  scale,
  zoom,
  setZoom,
  filter,
  setFilter,
  hasRefs,
  toolbarExtra,
  helpText,
  axisMarks,
  backOverlay,
  lanes,
  frontOverlay,
  bubble,
  scrollRef,
  embedded,
}: Props) => {
  const [filterOpen, setFilterOpen] = useState(false);
  const [pinnedSec, setPinnedSec] = useState<number | null>(null);

  // Crosshair is updated imperatively on mousemove so hundreds of casts don't
  // re-render on every pointer tick.
  const axisRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<HTMLDivElement>(null);
  const cursorTimeRef = useRef<HTMLDivElement>(null);

  const { xOf, secOf, stripWidth, prezoneSec } = scale;
  // Overlay bands (downtime + death + cursor) live in absolutely-positioned
  // layers spanning every lane. `left`/`width` match the strip column.
  const overlayStyle: CSSProperties = { left: 140, width: stripWidth };

  // --- Crosshair (imperative) ----------------------------------------------
  const moveCursor = (clientX: number) => {
    const axis = axisRef.current;
    const line = cursorRef.current;
    const chip = cursorTimeRef.current;
    if (!axis || !line || !chip) return;
    // `clientX` and getBoundingClientRect() are post-zoom *visual* px; dividing
    // by the root CSS `zoom` converts the delta to the *layout* px that `left`
    // expects (else the zoom is applied twice — the crosshair drifts right of
    // the cursor by (zoom-1)·distance). See currentZoom() in state/zoom.ts.
    const x = (clientX - axis.getBoundingClientRect().left) / currentZoom();
    if (x < 0 || x > stripWidth) {
      line.style.display = 'none';
      chip.style.display = 'none';
      return;
    }
    line.style.display = 'block';
    line.style.left = `${x}px`;
    chip.style.display = 'block';
    chip.style.left = `${x}px`;
    chip.textContent = fmtTick(secOf(x));
  };
  const hideCursor = () => {
    if (cursorRef.current) cursorRef.current.style.display = 'none';
    if (cursorTimeRef.current) cursorTimeRef.current.style.display = 'none';
  };
  const onStripClick = (e: React.MouseEvent) => {
    // Clicking a cast / marker (or the bubble) shouldn't drop a pin.
    if (
      (e.target as HTMLElement).closest(
        '.cast, .diff-bubble, .tl-ghost, .tl-idle, .tl-clip, .tl-band, .tl-phase-band, .tl-death-flag, .tl-mt-flag, .tl-buff-flag',
      )
    )
      return;
    const axis = axisRef.current;
    if (!axis) return;
    // Visual px → layout px (see moveCursor); pin lands under the pointer at any zoom.
    const x = (e.clientX - axis.getBoundingClientRect().left) / currentZoom();
    if (x < 0 || x > stripWidth) return;
    const sec = secOf(x);
    setPinnedSec((p) => (p != null && Math.abs(p - sec) < 0.6 ? null : sec));
  };

  return (
    <div className={`timeline-shell${embedded ? ' embedded' : ''}`}>
      <div className="timeline-toolbar">
        {toolbarExtra}
        <div className="row" style={{ marginLeft: 'auto', gap: 6 }}>
          <button className="btn sm ghost" onClick={() => setZoom(Math.max(0.5, zoom - 0.2))}>
            <Minus size={12} />
          </button>
          <span className="mono mut" style={{ fontSize: 12, width: 38, textAlign: 'center' }}>
            {Math.round(zoom * 100)}%
          </span>
          <button className="btn sm ghost" onClick={() => setZoom(Math.min(2.5, zoom + 0.2))}>
            <ZoomIn size={12} />
          </button>
          <span style={{ width: 1, height: 18, background: 'var(--border)', margin: '0 4px' }} />
          <div className="tl-filter-wrap">
            <button
              className={`btn sm ghost${filterOpen ? ' on' : ''}`}
              onClick={() => setFilterOpen((o) => !o)}
            >
              <Filter size={12} />
              Filter
            </button>
            {filterOpen && (
              <div className="tl-filter-pop" onMouseLeave={() => setFilterOpen(false)}>
                <label>
                  <input
                    type="checkbox"
                    checked={filter.gcd}
                    onChange={(e) => setFilter((f) => ({ ...f, gcd: e.target.checked }))}
                  />
                  Show GCDs
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={filter.ogcd}
                    onChange={(e) => setFilter((f) => ({ ...f, ogcd: e.target.checked }))}
                  />
                  Show oGCDs
                </label>
                {hasRefs && (
                  <label>
                    <input
                      type="checkbox"
                      checked={filter.refs}
                      onChange={(e) => setFilter((f) => ({ ...f, refs: e.target.checked }))}
                    />
                    Show reference lanes
                  </label>
                )}
              </div>
            )}
          </div>
          <button className="btn sm ghost tl-help" title={helpText}>
            <HelpCircle size={13} />
          </button>
        </div>
      </div>
      <div
        className="timeline-scroll"
        ref={scrollRef}
        onMouseMove={(e) => moveCursor(e.clientX)}
        onMouseLeave={hideCursor}
        onClick={onStripClick}
      >
        <div className="timeline">
          {/* Behind-casts overlay: pre-pull shade, caller zones, pinned-time line.
              Downtime bands live in the FRONT overlay so they can be hovered (the
              strips would otherwise capture the pointer over the band). */}
          <div className="tl-overlay back" style={overlayStyle}>
            {prezoneSec > 0 && (
              <>
                <div className="tl-prezone" style={{ left: 0, width: xOf(0) }} />
                <div className="tl-pull-line" style={{ left: xOf(0) }} />
              </>
            )}
            {backOverlay}
            {pinnedSec != null && <div className="tl-pin-line" style={{ left: xOf(pinnedSec) }} />}
          </div>

          <div />
          <div
            className="tl-axis"
            ref={axisRef}
            style={{ width: stripWidth, minWidth: '100%', position: 'sticky' }}
          >
            {scale.ticks.map((s) => (
              <div
                key={s}
                className={`tick${s === 0 && prezoneSec > 0 ? ' pull' : ''}`}
                style={{ left: xOf(s) }}
              >
                {fmtTick(s)}
              </div>
            ))}
            {axisMarks?.map((m, i) => (
              <div key={`mark${i}`} className={`tick ${m.className ?? ''}`} style={{ left: xOf(m.sec) }}>
                {m.label}
              </div>
            ))}
            {pinnedSec != null && (
              <div className="tl-pin-chip" style={{ left: xOf(pinnedSec) }}>
                <Pin size={9} /> {fmtTick(pinnedSec)}
              </div>
            )}
            <div ref={cursorTimeRef} className="tl-cursor-time" style={{ display: 'none' }} />
          </div>

          {lanes}

          {/* Above-casts overlay: downtime bands (hoverable), flags, deaths, and
              the live crosshair. Bands sit here (not behind the casts) so the
              pointer can reach them — the strips would otherwise eat the hover. */}
          <div className="tl-overlay front" style={overlayStyle}>
            {frontOverlay}
            <div ref={cursorRef} className="tl-cursor" style={{ display: 'none' }} />
          </div>

          {/* Hover bubble in its own top layer so it's never occluded by a cast
              that was lifted above the front overlay; pointer-events:none keeps
              it from stealing hovers. */}
          <div className="tl-overlay bubble" style={overlayStyle}>
            {bubble}
          </div>
        </div>
      </div>
    </div>
  );
};
