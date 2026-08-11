// Reaper-specific dashboard panels. The burst cast-count section is now a
// declarative CastCountPanel spec in the job registry (src/jobs/reaper.ts); what
// remains here is Death's Design, which has richer columns than a count table.
// Like the MCH panels it gates on the presence of its backing aspect state, so
// it stays silent if the analyzer skipped it.

import { Skull } from 'lucide-react';
import { fmtClock } from '../../format';
import type { DeathsDesignState } from '../../sidecar/contract';
import { aspectState, type JobPanelProps } from './types';

export const DeathsDesignPanel = ({ analysis }: JobPanelProps) => {
  const dd = aspectState<DeathsDesignState>(analysis, 'DeathsDesign');
  if (!dd || typeof dd.coveragePct !== 'number') return null;
  const cov = dd.coveragePct;
  const tone = cov >= 99 ? 'var(--good)' : cov >= 95 ? 'var(--warn)' : 'var(--bad)';
  const dropped = dd.uncoveredWindows ?? [];
  return (
    <>
      <div className="section-title">Death&apos;s Design</div>
      <div className="card">
        <div className="card-head">
          <Skull size={14} />
          <h2>Death&apos;s Design uptime</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <tbody>
              <tr>
                <td>Uptime</td>
                <td className="r num" style={{ color: tone, fontWeight: 600 }}>
                  {cov.toFixed(1)}%
                </td>
              </tr>
              <tr>
                <td>Potency lost to the 10% amp</td>
                <td className="r num">{Math.round(dd.lostPotency).toLocaleString()}</td>
              </tr>
              {dropped.length > 0 && (
                <tr>
                  <td>Dropped at</td>
                  <td className="r num">
                    {dropped.slice(0, 6).map(([s]) => fmtClock(s)).join(', ')}
                    {dropped.length > 6 ? ' …' : ''}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};
