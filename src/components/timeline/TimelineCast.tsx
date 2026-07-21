import { AbilityIcon } from '../AbilityIcon';
import { GCD_TOP, ICON_SIZE, OGCD_TOP, type TimelineScale } from './scale';
import type { AbilityMetaJson, CastEvent } from '../../sidecar/contract';

type Props = {
  cast: CastEvent;
  meta?: AbilityMetaJson;
  scale: TimelineScale;
  /** Full className — the caller includes the base `cast` (TimelineView layers
   *  on diff/focus/faded/paired/in-downtime). Defaults to plain `cast`. */
  className?: string;
  title?: string;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
  size?: number;
  /** Override the band top (px). Defaults to the oGCD/GCD band from `yOffset`;
   *  the compact tank Defensives lane passes its own single-band position. */
  top?: number;
};

/** One cast icon, positioned on its band (oGCD upper / GCD lower) at its time.
 *  Shared by the Timeline page and the Kill Time Theorizer so both render casts
 *  identically; the per-cast multi-target badge rides along when present. */
export const TimelineCast = ({
  cast,
  meta,
  scale,
  className = 'cast',
  title,
  onMouseEnter,
  onMouseLeave,
  size = ICON_SIZE,
  top,
}: Props) => {
  const isOgcd = cast.yOffset < 0;
  const mt = cast.mtMax != null ? { full: (cast.mtHit ?? 0) >= cast.mtMax } : null;
  return (
    <div
      className={className}
      style={{ left: scale.xOf(cast.startSec), top: top ?? (isOgcd ? OGCD_TOP : GCD_TOP) }}
      title={title}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <AbilityIcon
        kind={isOgcd ? 'ogcd1' : 'gcd1'}
        glyph={cast.label}
        name={meta?.name}
        iconPath={cast.iconPath ?? meta?.iconPath}
        size={size}
      />
      {mt && <span className={`mt-dot ${mt.full ? 'full' : 'missed'}`} />}
    </div>
  );
};
