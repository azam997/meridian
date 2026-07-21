// Generic "signature ability cast counts" section card. Driven by a
// CastCountPanelSpec from the job registry (src/jobs/{job}.ts) — counts come
// from the shared Abilities aspect state, names + icons from abilityMeta. This
// replaces per-job bespoke count panels (e.g. RPR's old BurstPanel with its
// hardcoded id→name map): adding such a section to a job is now a data entry in
// its profile, not a new component.

import { AbilityIcon } from '../../components/AbilityIcon';
import type { AbilitiesState } from '../../sidecar/contract';
import type { CastCountPanelSpec } from '../../jobs/types';
import { aspectState, type JobPanelProps } from './types';

export const CastCountPanel = ({
  analysis,
  spec,
}: JobPanelProps & { spec: CastCountPanelSpec }) => {
  const counts = aspectState<AbilitiesState>(analysis, 'Abilities')?.abilityCounts;
  if (!counts) return null;
  const rows = spec.abilityIds
    .filter((id) => (counts[id] ?? 0) > 0)
    .map((id) => ({
      id,
      name: analysis.abilityMeta[id]?.name ?? `#${id}`,
      iconPath: analysis.abilityMeta[id]?.iconPath,
      casts: counts[id],
    }));
  if (rows.length === 0) return null;
  const Icon = spec.icon;
  return (
    <>
      <div className="section-title">{spec.sectionTitle}</div>
      <div className="card">
        <div className="card-head">
          <Icon size={14} />
          <h2>{spec.heading}</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>Ability</th>
                <th className="r">Casts</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className="row" style={{ gap: 8 }}>
                      <AbilityIcon
                        kind="gcd1"
                        glyph={r.name.slice(0, 1)}
                        name={r.name}
                        iconPath={r.iconPath}
                        size={20}
                      />
                      {r.name}
                    </span>
                  </td>
                  <td className="r num">{r.casts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};
