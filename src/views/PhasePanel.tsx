import { Fragment, useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type {
  AnalysisResult,
  Improvement,
  PhaseAnalysisJson,
  PhaseInfoJson,
} from '../sidecar/contract';
import { fmtNum } from '../format';
import { ImprovementRow, type JumpToTime } from './ImprovementRow';

/** m:ss for a duration in seconds. */
const fmtDur = (s: number): string => {
  const n = Math.max(0, Math.round(s));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
};

/** Non-carryover deviation severity is assigned here (frontend) — these
 *  callouts carry no lostPotency, so the improvements-panel `severityFor`
 *  doesn't apply. */
const devSeverity = (kind: string): 'warn' | 'info' =>
  kind === 'pot_phase' || kind === 'potency_low' ? 'warn' : 'info';

/** A "you N / refs ~M" comparison cell with a tiny proportional bar. */
const CompareCell = ({ you, refMed, unit = '' }: { you: number; refMed: number; unit?: string }) => {
  const max = Math.max(you, refMed, 1);
  return (
    <div className="phase-compare">
      <div className="phase-bars">
        <span className="bar you" style={{ width: `${(you / max) * 100}%` }} />
        <span className="bar ref" style={{ width: `${(refMed / max) * 100}%` }} />
      </div>
      <span className="phase-nums">
        <b>{fmtNum(Math.round(you))}{unit}</b> <span className="mut">/ {fmtNum(Math.round(refMed))}{unit}</span>
      </span>
    </div>
  );
};

/** Flatten improvements to their located leaves (a card's children are the
 *  located instances; a childless card is itself a leaf), keeping only
 *  phase-locatable ones (timeSec > 0, plus the opener at 0:00). Unlocated
 *  aggregates (drift/residual) stay in the full-fight panel only. */
const locatedLeaves = (imps: Improvement[]): Improvement[] => {
  const out: Improvement[] = [];
  for (const im of imps) {
    const leaves = im.children && im.children.length ? im.children : [im];
    for (const l of leaves) {
      if (l.timeSec > 0 || l.kind === 'opener') out.push(l);
    }
  }
  return out;
};

/**
 * Per-phase (phasic) analysis panel for ultimates. Each phase row expands to
 * reveal that phase's Potential Improvements (the full-fight cards, regrouped
 * by the phase their time falls in), the resource/pacing deviations vs the top
 * clears, and a carryover warning when a resource issue leaks into the next
 * phase. Encounter-driven (gated on `analysis.phaseAnalysis`); rendered ABOVE
 * the full-fight improvements panel.
 */
export const PhasePanel = ({
  analysis,
  improvements,
  onJumpToTime,
}: {
  analysis: AnalysisResult;
  /** The RE-PRICED / denial-filtered improvements from DashboardView — they
   *  change with the multi-target crediting mode and per-window denials, so the
   *  per-phase cards stay in sync with the full-fight panel. */
  improvements: Improvement[];
  onJumpToTime: JumpToTime;
}) => {
  const pa: PhaseAnalysisJson | undefined = analysis.phaseAnalysis;
  const phases = useMemo<PhaseInfoJson[]>(() => analysis.phases ?? [], [analysis.phases]);
  const [openPhases, setOpenPhases] = useState<Set<number>>(new Set());

  // Improvement leaves + lost-potency subtotal, bucketed by the phase their
  // time falls in. Trailing casts past the last boundary fall into the last
  // phase; the opener (0:00) into the first.
  const { impsByPhase, lostByPhase } = useMemo(() => {
    const byPhase = new Map<number, Improvement[]>();
    const lost = new Map<number, number>();
    if (phases.length) {
      for (const l of locatedLeaves(improvements)) {
        const t = Math.max(0, l.timeSec);
        const ph = phases.find((p) => t >= p.startSec && t < p.endSec) ?? phases[phases.length - 1];
        const arr = byPhase.get(ph.id);
        if (arr) arr.push(l);
        else byPhase.set(ph.id, [l]);
        lost.set(ph.id, (lost.get(ph.id) ?? 0) + Math.max(0, l.lostPotency));
      }
      for (const arr of byPhase.values()) arr.sort((a, b) => b.lostPotency - a.lostPotency);
    }
    return { impsByPhase: byPhase, lostByPhase: lost };
  }, [improvements, phases]);

  if (!pa || pa.user.length === 0) return null;

  const refByPhase = new Map(pa.refs.map((r) => [r.phaseId, r]));
  const infoByPhase = new Map(phases.map((p) => [p.id, p]));
  const nextPhase = (id: number): PhaseInfoJson | undefined => {
    const i = phases.findIndex((p) => p.id === id);
    return i >= 0 ? phases[i + 1] : undefined;
  };
  const devsByPhase = new Map<number, PhaseAnalysisJson['deviations']>();
  for (const d of pa.deviations) {
    const arr = devsByPhase.get(d.phaseId);
    if (arr) arr.push(d);
    else devsByPhase.set(d.phaseId, [d]);
  }

  const toggle = (id: number) =>
    setOpenPhases((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="phase-panel">
      <div className="section-title">Phase-by-phase (vs top clears)</div>
      <div className="card phase-card">
        <table className="phase-table">
          <thead>
            <tr>
              <th>Phase</th>
              <th>Window</th>
              <th>GCDs</th>
              <th>Damage</th>
              <th>Resource at phase end</th>
              <th>Recoverable</th>
            </tr>
          </thead>
          <tbody>
            {pa.user.map((u) => {
              const r = refByPhase.get(u.phaseId);
              const info = infoByPhase.get(u.phaseId);
              const open = openPhases.has(u.phaseId);
              const reachTag = info && !info.reached
                ? 'not reached'
                : u.partial
                  ? 'partial'
                  : null;
              const lost = lostByPhase.get(u.phaseId) ?? 0;
              const devs = devsByPhase.get(u.phaseId) ?? [];
              const imps = impsByPhase.get(u.phaseId) ?? [];
              const carryover = devs.filter((d) => d.kind === 'gauge_exit');
              const otherDevs = devs.filter((d) => d.kind !== 'gauge_exit');
              return (
                <Fragment key={u.phaseId}>
                  <tr
                    className={`phase-row${open ? ' open' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => toggle(u.phaseId)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        toggle(u.phaseId);
                      }
                    }}
                  >
                    <td>
                      <ChevronRight size={13} className={`chev${open ? ' open' : ''}`} />
                      <span className="phase-name">{info?.name ?? `P${u.phaseId}`}</span>
                      {reachTag && <span className={`chip ${reachTag === 'partial' ? 'warn' : 'mut'}`}>{reachTag}</span>}
                      {info?.isIntermission && <span className="chip mut">break</span>}
                    </td>
                    <td className="mut num">
                      {info ? `${fmtDur(info.startSec)}–${fmtDur(info.endSec)}` : '—'}
                      <div className="phase-sub">{fmtDur(u.activeSec)} active</div>
                    </td>
                    <td>
                      {r ? <CompareCell you={u.gcdCasts} refMed={r.gcdCasts.median} /> : <b>{u.gcdCasts}</b>}
                    </td>
                    <td>
                      {r && r.deliveredPotency.median > 0
                        ? <CompareCell you={u.deliveredPotency} refMed={r.deliveredPotency.median} unit="p" />
                        : <b>{fmtNum(Math.round(u.deliveredPotency))}p</b>}
                    </td>
                    <td>
                      {u.gauges.length === 0 ? (
                        <span className="mut">—</span>
                      ) : (
                        u.gauges.map((g) => {
                          const rg = r?.gauges.find((x) => x.name === g.name);
                          return (
                            <div key={g.name} className="phase-gauge">
                              <span className="phase-gauge-name">{g.name}</span>
                              {rg ? <CompareCell you={g.exit} refMed={rg.exit.median} /> : <b>{g.exit}</b>}
                            </div>
                          );
                        })
                      )}
                    </td>
                    <td className="num">
                      {lost > 0 ? <span className="phase-lost">−{fmtNum(Math.round(lost))}p</span> : <span className="mut">—</span>}
                      {(devs.length > 0 || imps.length > 0) && (
                        <div className="phase-sub">{open ? 'hide' : 'details'}</div>
                      )}
                    </td>
                  </tr>
                  {open && (
                    <tr className="phase-detail-row">
                      <td colSpan={6}>
                        <div className="phase-detail">
                          {carryover.map((d, i) => (
                            <div key={`c${i}`} className="finding warn static phase-carryover">
                              <div className="sev">⇄</div>
                              <div>
                                <div className="title">
                                  Carries into {nextPhase(u.phaseId)?.name ?? 'the next phase'}
                                </div>
                                <div className="desc">
                                  {d.text}{' '}
                                  <span className="mut">
                                    The long untargetable transition into it recovers some of this — treat it as a nudge, not a hard loss.
                                  </span>
                                </div>
                              </div>
                            </div>
                          ))}

                          {otherDevs.map((d, i) => {
                            const sev = devSeverity(d.kind);
                            return (
                              <div key={`d${i}`} className={`finding ${sev} static`}>
                                <div className="sev">{sev === 'warn' ? '▲' : '◔'}</div>
                                <div>
                                  <div className="desc">{d.text}</div>
                                </div>
                              </div>
                            );
                          })}

                          {imps.length > 0 ? (
                            <div className="phase-imps">
                              <div className="phase-imps-head">
                                Potential improvements in this phase
                                <span className="delta">−{fmtNum(Math.round(lost))}p</span>
                              </div>
                              <div className="findings">
                                {imps.map((im, i) => (
                                  <ImprovementRow key={i} im={im} meta={analysis.abilityMeta} onJump={onJumpToTime} />
                                ))}
                              </div>
                            </div>
                          ) : (
                            devs.length === 0 && (
                              <div className="mut" style={{ fontSize: 12, padding: '4px 2px' }}>
                                Clean phase — nothing located here.
                              </div>
                            )
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {pa.refs[0] && (
          <div className="phase-legend mut">
            <span className="swatch you" /> you
            <span className="swatch ref" /> top-clear median · click a phase for its breakdown, deviations and carryover
          </div>
        )}
      </div>
    </div>
  );
};
