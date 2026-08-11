// Red Mage dashboard panels: Verfire/Verstone proc utilization and White/Black
// mana balance. Both gate on the presence of their backing aspect state, so they
// stay silent if the analyzer skipped them. Registered for RDM in
// src/jobs/redmage.ts.

import { Droplets, Sparkles } from 'lucide-react';
import type { OvercapAspectState, ProcsState } from '../../sidecar/contract';
import { aspectState, type JobPanelProps } from './types';

const tone = (pct: number) =>
  pct >= 95 ? 'var(--good)' : pct >= 85 ? 'var(--warn)' : 'var(--bad)';

export const ProcUtilizationPanel = ({ analysis }: JobPanelProps) => {
  const p = aspectState<ProcsState>(analysis, 'Procs');
  if (!p || typeof p.totalGrants !== 'number') return null;
  // Nothing to show if no procs were ever granted (e.g. a tiny pull).
  if (p.totalGrants === 0) return null;
  const util = p.utilizationPct;
  return (
    <>
      <div className="section-title">Procs</div>
      <div className="card">
        <div className="card-head">
          <Sparkles size={14} />
          <h2>Verfire / Verstone utilization</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <tbody>
              <tr>
                <td>Utilization</td>
                <td className="r num" style={{ color: tone(util), fontWeight: 600 }}>
                  {util.toFixed(0)}%
                </td>
              </tr>
              <tr>
                <td>Procs used / granted</td>
                <td className="r num">{p.totalUsed} / {p.totalGrants}</td>
              </tr>
              <tr>
                <td>Verfire (used / wasted)</td>
                <td className="r num">{p.verfireUsed} / {p.verfireWasted}</td>
              </tr>
              <tr>
                <td>Verstone (used / wasted)</td>
                <td className="r num">{p.verstoneUsed} / {p.verstoneWasted}</td>
              </tr>
              {p.overwrites > 0 && (
                <tr>
                  <td>Overwritten (re-procced unspent)</td>
                  <td className="r num">{p.overwrites}</td>
                </tr>
              )}
              {p.lostPotency > 0 && (
                <tr>
                  <td>Potency lost to wasted procs</td>
                  <td className="r num">{Math.round(p.lostPotency).toLocaleString()}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};

type ManaRow = { count: number; wasted: number; lost: number };

const sumGauge = (state: OvercapAspectState, gauge: string): ManaRow =>
  (state.findings ?? [])
    .filter((f) => f.gauge === gauge)
    .reduce(
      (acc, f) => ({
        count: acc.count + 1,
        wasted: acc.wasted + (f.wasted ?? 0),
        lost: acc.lost + (f.lostPotency ?? 0),
      }),
      { count: 0, wasted: 0, lost: 0 },
    );

export const ManaBalancePanel = ({ analysis }: JobPanelProps) => {
  const oc = aspectState<OvercapAspectState>(analysis, 'Overcap');
  if (!oc) return null;
  const white = sumGauge(oc, 'white_mana');
  const black = sumGauge(oc, 'black_mana');
  const totalLost = white.lost + black.lost;
  const clean = white.wasted === 0 && black.wasted === 0;
  return (
    <>
      <div className="section-title">Mana</div>
      <div className="card">
        <div className="card-head">
          <Droplets size={14} />
          <h2>White / Black mana overcap</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <tbody>
              <tr>
                <td>White mana overcapped</td>
                <td className="r num" style={{ color: white.wasted ? 'var(--warn)' : 'var(--good)' }}>
                  {white.wasted} {white.count ? `(×${white.count})` : ''}
                </td>
              </tr>
              <tr>
                <td>Black mana overcapped</td>
                <td className="r num" style={{ color: black.wasted ? 'var(--warn)' : 'var(--good)' }}>
                  {black.wasted} {black.count ? `(×${black.count})` : ''}
                </td>
              </tr>
              <tr>
                <td>Potency lost to overcap</td>
                <td className="r num">
                  {clean ? '—' : Math.round(totalLost).toLocaleString()}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};
