type Props = {
  /** 0–100, drives the ring fill. */
  pct: number;
  /** Center text (defaults to `pct`). */
  value?: string | number;
  /** Optional small kicker above the label. */
  eyebrow?: string;
  label: string;
  subtext?: string;
  /** Ring + center-value color. Falls back to a tri-color keyed off `pct`. */
  color?: string;
  /** Ring diameter in px (geometry + center font scale with it). */
  size?: number;
};

/** A labelled progress ring. Generic — used for the headline efficiency hero
 *  (coloured by parse tier) and the percentile fallback for sim-less jobs. */
export const StatRing = ({ pct, value, eyebrow, label, subtext, color, size = 120 }: Props) => {
  const sw = size * 0.066;             // stroke width (~8 at 120)
  const r = (size - sw) / 2 - 1;       // radius that fits the stroke inside the box
  const cx = size / 2;
  const c = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(100, pct)) / 100;
  const off = c * (1 - frac);
  const stroke =
    color ?? (pct < 33 ? 'var(--bad)' : pct < 66 ? 'var(--warn)' : 'var(--good)');
  // End-of-arc knob position (arc starts at 12 o'clock, sweeps clockwise).
  const endAngle = (-90 + frac * 360) * (Math.PI / 180);
  const endX = cx + r * Math.cos(endAngle);
  const endY = cx + r * Math.sin(endAngle);

  return (
    <div className="percentile">
      <div className="ring" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <circle cx={cx} cy={cx} r={r} stroke="var(--bg-2)" strokeWidth={sw} fill="none" />
          <circle
            cx={cx}
            cy={cx}
            r={r}
            stroke={stroke}
            strokeWidth={sw}
            fill="none"
            strokeLinecap="butt"
            strokeDasharray={c}
            strokeDashoffset={off}
            transform={`rotate(-90 ${cx} ${cx})`}
            style={{ transition: 'stroke-dashoffset 0.6s ease' }}
          />
          {/* Start tick at 12 o'clock — spans exactly the stroke band so it
              neither pokes outside the ring nor past the (now flat) arc start. */}
          <line
            x1={cx} y1={cx - r - sw / 2}
            x2={cx} y2={cx - r + sw / 2}
            stroke="var(--text-2)" strokeWidth={2}
          />
          {/* End knob — a little white circle riding the arc's leading edge. */}
          <circle
            cx={endX} cy={endY} r={sw * 0.55}
            fill="#fff" stroke="oklch(0 0 0 / 0.35)" strokeWidth={1}
          />
        </svg>
        <div className="v" style={{ color, fontSize: Math.round(size * 0.2) }}>
          {value ?? pct}
          <span className="u">%</span>
        </div>
      </div>
      <div className="percentile-meta">
        {eyebrow && <div className="percentile-eyebrow">{eyebrow}</div>}
        <div className="percentile-headline">{label}</div>
        {subtext && (
          <div className="mut" style={{ fontSize: 12 }}>
            {subtext}
          </div>
        )}
      </div>
    </div>
  );
};
