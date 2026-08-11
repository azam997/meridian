type Props = {
  yourValue: number;
  refs: number[];
  /** Optional formatter for the median/you labels. Default = "k"-suffix
   *  potency (e.g. 38500 → "38.5k"). Pass a percent formatter when the
   *  values are efficiency percentages. */
  formatLabel?: (v: number) => string;
};

const defaultFormat = (v: number) => `${(v / 1000).toFixed(1)}k`;

/** Horizontal distribution strip: reference dots on a number line with the
 *  median marked and "you" highlighted. Positions are CSS percentages so the
 *  strip fills its container at any width while the markers stay perfectly
 *  round (an SVG with preserveAspectRatio="none" stretched them into ovals on
 *  wide screens). */
export const DistroChart = ({ yourValue, refs, formatLabel = defaultFormat }: Props) => {
  const min = Math.min(...refs, yourValue);
  const max = Math.max(...refs, yourValue);
  const span = max - min || 1;
  // Clamp into a small inset so edge markers + their labels don't clip.
  const pos = (v: number) => 2 + ((v - min) / span) * 96;
  const sorted = [...refs].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];

  return (
    <div className="distro">
      <div className="distro-track">
        <div className="distro-axis" />
        {refs.map((v, i) => (
          <span key={i} className="distro-dot ref" style={{ left: `${pos(v)}%` }}>
            <span className="distro-tip">{formatLabel(v)}</span>
          </span>
        ))}
        <div className="distro-median" style={{ left: `${pos(median)}%` }}>
          <span className="distro-label distro-label-top">median {formatLabel(median)}</span>
        </div>
        <span className="distro-dot you" style={{ left: `${pos(yourValue)}%` }}>
          <span className="distro-label distro-label-bottom">you {formatLabel(yourValue)}</span>
        </span>
      </div>
    </div>
  );
};
