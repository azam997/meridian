import { AbilityIcon } from '../components/AbilityIcon';
import { fmtNum } from '../format';
import type { AnalysisResult } from '../sidecar/contract';

type Props = { analysis: AnalysisResult };

type Row = { ability: string; you: number; ref: number };

const parseRows = (rows: (string | number | boolean | null)[][]): Row[] =>
  rows.map((r) => ({
    ability: String(r[0] ?? ''),
    you: Number(r[1] ?? 0),
    ref: Number(r[2] ?? 0),
  }));

export const CountsView = ({ analysis }: Props) => {
  const abilities = analysis.comparisons.Abilities;
  const rows = abilities ? parseRows(abilities.yourDetailRows) : [];
  // Healer runs suppress the ref-median comparison (top-parsing healers force
  // DPS into heal windows, so their cast pattern isn't the honest target) —
  // the table then shows your own counts only. Prog (wipe) pulls too: a
  // truncated wipe's cast counts against full-kill medians is meaningless.
  const showRefs = !analysis.headline.rankSuppressed
    && !analysis.headline.isProgPull;
  // The Abilities detail rows carry only the ability name, so resolve each
  // icon path by name from the metadata map (names are unique in FFXIV).
  const pathByName: Record<string, string> = {};
  for (const m of Object.values(analysis.abilityMeta)) pathByName[m.name] = m.iconPath;

  return (
    <div className="content">
      <div className="hero">
        <h1>Cast counts</h1>
        <p>
          {showRefs
            ? 'Your casts vs the median of your selected references. Useful for spotting missed cooldowns and rotation pattern drift.'
            : 'Your casts per ability. The reference comparison is hidden for healers — top parses sacrifice planned healing for score, so their cast pattern is not the honest target.'}
        </p>
      </div>
      <div className="card" style={{ maxWidth: 760 }}>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>Ability</th>
                <th className="r">Your casts</th>
                {showRefs && <th className="r">Ref median</th>}
                {showRefs && <th className="r">Δ</th>}
                <th>Distribution</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const delta = r.you - r.ref;
                const max = Math.max(r.you, showRefs ? r.ref : 0) || 1;
                return (
                  <tr key={i}>
                    <td>
                      <span className="row" style={{ gap: 8 }}>
                        <AbilityIcon
                          kind="gcd1"
                          glyph={r.ability.slice(0, 1)}
                          name={r.ability}
                          iconPath={pathByName[r.ability]}
                          size={20}
                        />
                        {r.ability}
                      </span>
                    </td>
                    <td className="r num">{fmtNum(r.you)}</td>
                    {showRefs && <td className="r num mut">{fmtNum(r.ref)}</td>}
                    {showRefs && (
                      <td className="r num">
                        {delta > 0 && <span className="delta-pos">+{fmtNum(delta)}</span>}
                        {delta < 0 && <span className="delta-neg">−{fmtNum(Math.abs(delta))}</span>}
                        {delta === 0 && <span className="mut">·</span>}
                      </td>
                    )}
                    <td style={{ width: 200 }}>
                      <div style={{ position: 'relative', height: 14 }}>
                        {showRefs && (
                          <div className="bar" style={{ position: 'absolute', inset: '4px 0', height: 6 }}>
                            <div
                              className="fill"
                              style={{ width: (r.ref / max) * 100 + '%', background: 'var(--info-soft)' }}
                            />
                          </div>
                        )}
                        <div
                          className="bar"
                          style={{ position: 'absolute', inset: '4px 0', height: 6, background: 'transparent' }}
                        >
                          <div
                            className="fill"
                            style={{ width: (r.you / max) * 100 + '%', background: 'var(--accent)' }}
                          />
                        </div>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
