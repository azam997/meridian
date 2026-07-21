// MCH-specific dashboard panels. Each renders a section card below the
// shared headline / findings; both gate on the presence of their backing
// aspect state so they stay silent if the analyzer skipped them.

import { Crown, Target } from 'lucide-react';
import { fmtClock } from '../../format';
import type { QueenState, WildfireState } from '../../sidecar/contract';
import { aspectState, type JobPanelProps } from './types';

export const QueenPanel = ({ analysis }: JobPanelProps) => {
  const queen = aspectState<QueenState>(analysis, 'Queen');
  if (!queen || queen.queens.length === 0) return null;
  return (
    <>
      <div className="section-title">Queen</div>
      <div className="card">
        <div className="card-head">
          <Crown size={14} />
          <h2>Queen casts</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th className="r">Battery</th>
                <th className="r">Duration</th>
                <th className="c">Finish</th>
                <th className="r">Pet damage</th>
              </tr>
            </thead>
            <tbody>
              {queen.queens.map((q, i) => (
                <tr key={i}>
                  <td className="num">{i + 1}</td>
                  <td className="num">{fmtClock(q.timeSec)}</td>
                  <td className="r num">{q.battery}</td>
                  <td className="r num">{q.durationSec.toFixed(1)}s</td>
                  <td className="c">{q.finished ? '✓' : '✗'}</td>
                  <td className="r num">{q.petDamage.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};

export const WildfirePanel = ({ analysis }: JobPanelProps) => {
  const wildfire = aspectState<WildfireState>(analysis, 'Wildfire');
  if (!wildfire || wildfire.windows.length === 0) return null;
  return (
    <>
      <div className="section-title">Wildfire</div>
      <div className="card">
        <div className="card-head">
          <Target size={14} />
          <h2>Wildfire windows</h2>
        </div>
        <div className="card-body tbl-body" style={{ padding: 0 }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th className="r">Hits</th>
                <th>Bucket</th>
              </tr>
            </thead>
            <tbody>
              {wildfire.windows.map((w, i) => (
                <tr key={i}>
                  <td className="num">{i + 1}</td>
                  <td className="num">{fmtClock(w.castTimeSec)}</td>
                  <td className="r num">{w.hits} / 6</td>
                  <td>{w.bucket}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};
