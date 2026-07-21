// Ability icon — renders the real FFXIV icon if we know it, with the
// procedural colored tile as a load-time / fallback layer underneath.

import { useState } from 'react';
import { iconUrlFromPath, localIconUrl } from './abilityIcons';

type Kind = 'gcd1' | 'gcd2' | 'gcd3' | 'ogcd1' | 'ogcd2' | 'ogcd3' | 'buff' | 'proc' | 'big';

const ABILITY_COLORS: Record<Kind, [string, string]> = {
  gcd1:  ['#3b82f6', '#1d4ed8'],
  gcd2:  ['#06b6d4', '#0e7490'],
  gcd3:  ['#a855f7', '#6d28d9'],
  ogcd1: ['#f59e0b', '#b45309'],
  ogcd2: ['#ef4444', '#991b1b'],
  ogcd3: ['#10b981', '#047857'],
  buff:  ['#fbbf24', '#a16207'],
  proc:  ['#ec4899', '#9d174d'],
  big:   ['#f97316', '#9a3412'],
};

export type AbilityIconProps = {
  kind: string;
  glyph: string;
  /** Full ability name — used for the tooltip / alt text. */
  name?: string;
  /** Explicit full icon URL — overrides everything (rare). */
  iconUrl?: string;
  /** XIVAPI-relative path the backend resolved (AbilityMetaJson.iconPath /
   *  CastEvent.iconPath). The preferred source — see abilityIcons.ts. */
  iconPath?: string;
  size?: number;
  dim?: boolean;
  title?: string;
};

export const AbilityIcon = ({
  kind, glyph, name, iconUrl, iconPath, size = 30, dim = false, title = '',
}: AbilityIconProps) => {
  const [a, b] = ABILITY_COLORS[(kind as Kind)] ?? ABILITY_COLORS.gcd1;
  // Ordered icon sources: explicit override → local disk cache (Tauri) → xivapi.
  // We advance through them on load error, then fall through to the glyph tile.
  const sources = [iconUrl, localIconUrl(iconPath), iconUrlFromPath(iconPath)]
    .filter((s): s is string => !!s);
  const key = sources.join('|');
  const [srcIdx, setSrcIdx] = useState(0);
  // Reset to the first source when the source list changes (component reused for
  // a different ability). The render-time "adjust state on prop change" pattern —
  // no effect, no extra commit. See react.dev "You Might Not Need an Effect".
  const [prevKey, setPrevKey] = useState(key);
  if (key !== prevKey) {
    setPrevKey(key);
    setSrcIdx(0);
  }
  const url = sources[srcIdx];
  const showImg = url !== undefined;

  return (
    <div
      title={title || name || ''}
      style={{
        width: size,
        height: size,
        borderRadius: 6,
        background: `linear-gradient(135deg, ${a}, ${b})`,
        display: 'grid',
        placeItems: 'center',
        color: 'white',
        fontFamily: 'var(--font-mono)',
        fontSize: Math.round(size * 0.45),
        fontWeight: 700,
        border: '1px solid rgba(0,0,0,0.35)',
        boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.25)',
        opacity: dim ? 0.45 : 1,
        textShadow: '0 1px 1px rgba(0,0,0,0.5)',
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      {showImg ? (
        <img
          key={url}
          src={url}
          alt={name ?? ''}
          width={size}
          height={size}
          draggable={false}
          onError={() => setSrcIdx((i) => i + 1)}
          style={{
            position: 'absolute',
            inset: 0,
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            pointerEvents: 'none',
          }}
        />
      ) : (
        glyph
      )}
    </div>
  );
};
