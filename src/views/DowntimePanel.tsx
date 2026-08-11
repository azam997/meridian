import { Eye, EyeOff } from 'lucide-react';
import type { HeadlineKPIs } from '../sidecar/contract';

type Props = {
  headline: HeadlineKPIs;
};

const fmtMmSs = (sec: number): string => {
  const n = Math.max(0, Math.round(sec));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
};

const fmtDur = (sec: number): string =>
  sec >= 60 ? `${(sec / 60).toFixed(1)}m` : `${sec.toFixed(1)}s`;

/**
 * Per-window downtime breakdown.
 *
 * Renders nothing when both Tier A and Tier B are empty — the dashboard
 * stays clean for fights where the boss was always targetable AND refs
 * showed no consensus stops. When either tier has data, surfaces it as a
 * compact two-section card next to the headline so the user can see
 * exactly what the efficiency number is computed against.
 *
 * Confidence on Tier B rows is the worst-case `nIdle / nTotal` ratio
 * across the window — matches what the aggregator records.
 */
export const DowntimePanel = ({ headline: h }: Props) => {
  const tierA = h.downtimeTierA;
  const tierB = h.downtimeTierB;
  const ranged = h.rangedWindows ?? [];
  const md = h.meleeDowntime;

  if (
    tierA.length === 0 &&
    tierB.length === 0 &&
    ranged.length === 0 &&
    !(md && md.potency > 0)
  ) {
    return null;
  }

  const tierATotal = tierA.reduce((acc, w) => acc + (w.endSec - w.startSec), 0);
  const tierBTotal = tierB.reduce((acc, w) => acc + (w.endSec - w.startSec), 0);
  const rangedTotal = ranged.reduce((acc, w) => acc + (w.endSec - w.startSec), 0);

  return (
    <div className="card">
      <div className="card-head">
        <Eye size={14} />
        <h2>Downtime detection</h2>
      </div>
      <div className="card-body tbl-body" style={{ paddingTop: 12 }}>
        {tierA.length > 0 && (
          <>
            <div style={{ fontSize: 12, marginBottom: 6 }}>
              <strong>Confirmed boss untargetable</strong>
              <span className="mut" style={{ marginLeft: 8 }}>
                {tierA.length} {tierA.length === 1 ? 'window' : 'windows'} ·{' '}
                {fmtDur(tierATotal)} total
              </span>
            </div>
            <table
              className="tbl"
              style={{ marginBottom: tierB.length > 0 || ranged.length > 0 ? 16 : 0 }}
            >
              <thead>
                <tr>
                  <th>Start</th>
                  <th>End</th>
                  <th className="r">Duration</th>
                </tr>
              </thead>
              <tbody>
                {tierA.map((w, i) => (
                  <tr key={i}>
                    <td className="num">{fmtMmSs(w.startSec)}</td>
                    <td className="num">{fmtMmSs(w.endSec)}</td>
                    <td className="r num">{fmtDur(w.endSec - w.startSec)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {tierB.length > 0 && (
          <>
            <div style={{ fontSize: 12, marginBottom: 6 }}>
              <strong>Consensus forced downtime</strong>
              <span className="mut" style={{ marginLeft: 8 }}>
                {tierB.length} {tierB.length === 1 ? 'window' : 'windows'} ·{' '}
                {fmtDur(tierBTotal)} total · ref agreement shown per window
              </span>
            </div>
            <table className="tbl">
              <thead>
                <tr>
                  <th>Start</th>
                  <th>End</th>
                  <th className="r">Duration</th>
                  <th className="r">Agreement</th>
                </tr>
              </thead>
              <tbody>
                {tierB.map((w, i) => (
                  <tr key={i}>
                    <td className="num">{fmtMmSs(w.startSec)}</td>
                    <td className="num">{fmtMmSs(w.endSec)}</td>
                    <td className="r num">{fmtDur(w.endSec - w.startSec)}</td>
                    <td className="r num">
                      {w.nIdle}/{w.nTotal} refs idle
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div
              className="mut"
              style={{
                fontSize: 11,
                marginTop: 8,
                marginBottom: ranged.length > 0 ? 16 : 0,
              }}
            >
              <EyeOff size={11} style={{ verticalAlign: '-2px', marginRight: 4 }} />
              Lenient efficiency assumes these windows are forced; strict treats
              them as the player&apos;s time.
            </div>
          </>
        )}

        {ranged.length > 0 && (
          <>
            <div style={{ fontSize: 12, marginBottom: 6 }}>
              <strong>Consensus ranged uptime</strong>
              <span className="mut" style={{ marginLeft: 8 }}>
                {ranged.length} {ranged.length === 1 ? 'window' : 'windows'} ·{' '}
                {fmtDur(rangedTotal)} total · forced melee disconnects
              </span>
            </div>
            <table className="tbl">
              <thead>
                <tr>
                  <th>Start</th>
                  <th>End</th>
                  <th className="r">Duration</th>
                  <th className="r">Agreement</th>
                </tr>
              </thead>
              <tbody>
                {ranged.map((w, i) => (
                  <tr key={i}>
                    <td className="num">{fmtMmSs(w.startSec)}</td>
                    <td className="num">{fmtMmSs(w.endSec)}</td>
                    <td className="r num">{fmtDur(w.endSec - w.startSec)}</td>
                    <td className="r num">
                      {w.nCasting}/{w.nTotal} refs at range
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mut" style={{ fontSize: 11, marginTop: 8 }}>
              <EyeOff size={11} style={{ verticalAlign: '-2px', marginRight: 4 }} />
              Top players bridged these stretches with their ranged filler
              instead of melee GCDs. The lenient ceiling credits the full
              consensus stretch; the strict (rank) ceiling credits the portion
              you were personally forced out (below).
            </div>
          </>
        )}

        {md && md.potency > 0 && (
          <div style={{ marginTop: ranged.length > 0 ? 14 : 0 }}>
            <div style={{ fontSize: 12, marginBottom: 4 }}>
              <strong>Forced melee downtime — credited to rank</strong>
            </div>
            <div className="mut" style={{ fontSize: 11 }}>
              <EyeOff size={11} style={{ verticalAlign: '-2px', marginRight: 4 }} />
              −{Math.round(md.potency).toLocaleString()}p ({md.pct.toFixed(1)}% of
              the ideal ceiling) was removed from your strict efficiency: the
              stretches you yourself were forced out of melee into your ranged
              filler, where no melee GCD was possible. Self-limited to your own
              disconnects, so it never credits time you spent in melee.
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
