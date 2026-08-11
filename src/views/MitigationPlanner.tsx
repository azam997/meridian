import { useEffect, useMemo, useState } from 'react';
import {
  ClipboardList, Clock, HeartPulse, ListChecks, Loader2, Play, Shield,
  ShieldCheck, Sparkles, Swords, Target, Users,
} from 'lucide-react';
import { jobColor, jobIcon } from '../components/jobs';
import { TimelineShell, type FilterState } from '../components/timeline/TimelineShell';
import { TimelineCast } from '../components/timeline/TimelineCast';
import { clampBubbleLeft, useTimelineScale } from '../components/timeline/scale';
import { fmtClock, fmtDuration } from '../format';
import { sidecar } from '../sidecar';
import { MitPlanBoard } from './MitPlanBoard';
import { KIND_LABEL, SCHOOL_LABEL, fmtK } from './mitPlanShared';
import type {
  Catalog, MitCompSelection, MitDamageMarker, MitPlanResult,
} from '../sidecar/contract';

type Props = {
  /** Optional default from the app's current selection — the page works
   *  without it (no character/analysis required). */
  defaultEncounterId?: number;
  /** Healer flow: the pull Setup routed here with. When it carries a report
   *  code the plan auto-runs with the pull's comp (resolved backend-side from
   *  its actors, the analyzed job kept in its own slot). */
  pullContext?: { job: string; encounterId: number; reportCode?: string; fightId?: number };
  /** Healer flow (analyzable healers only): run the locked-GCD analysis of
   *  the routed pull. `compAdjusted` = the user changed the comp away from
   *  the pull's — the adjusted comp then rides the analysis request so the
   *  locked ceiling matches the plan on screen. `usePfPlan` locks the premade
   *  ("PF") plan instead of the auto one (ultimates that ship one). */
  onAnalyze?: (comp: MitCompSelection, compAdjusted: boolean,
               usePfPlan: boolean) => void;
};

const SHIELD_HEALERS = ['Sage', 'Scholar'] as const;
const REGEN_HEALERS = ['White Mage', 'Astrologian'] as const;
const TANK_JOBS = ['Paladin', 'Warrior', 'Dark Knight', 'Gunbreaker'] as const;
const DPS_JOBS = [
  'Monk', 'Dragoon', 'Ninja', 'Samurai', 'Reaper', 'Viper',
  'Bard', 'Machinist', 'Dancer',
  'Black Mage', 'Summoner', 'Red Mage', 'Pictomancer',
] as const;

const MP_HELP =
  'Each lane is one party slot; icons are planned casts, the bar under an icon ' +
  'is that cooldown’s coverage window.\n' +
  'Vertical markers are forced damage — color = plan status (green covered, ' +
  'amber tight, red uncovered).\n' +
  'Hover a marker for the mechanic; click it to jump to its row below.\n' +
  'Dim icons are suggested tank personals.';

// --- The plan timeline -------------------------------------------------------

const MitPlanTimeline = ({ result }: { result: MitPlanResult }) => {
  const [zoom, setZoom] = useState(1);
  const [filter, setFilter] = useState<FilterState>({ gcd: true, ogcd: true, refs: true });
  const [hover, setHover] = useState<number | null>(null);

  const laneCasts = useMemo(() => result.lanes.map((l) => l.casts), [result]);
  const scale = useTimelineScale(zoom, laneCasts, result.modelKillSec);
  const { xOf, pxPerSec, stripWidth, stripStyle } = scale;

  const markers = result.damageMarkers;
  const jumpTo = (m: MitDamageMarker) =>
    document.getElementById(`mp-mech-${m.mechanicId}`)?.scrollIntoView({
      behavior: 'smooth', block: 'center',
    });

  const backOverlay = result.downtimeWindows.map((w, i) => (
    <div
      key={`dt${i}`}
      className="tl-band tier-a"
      title={`No enemy targetable ${fmtClock(w.startSec)}–${fmtClock(w.endSec)}`}
      style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
    />
  ));

  const lanes = result.lanes.map((lane) => {
    const icon = jobIcon(lane.job);
    return (
      <div className="tl-row def mp" key={lane.slot}>
        <div className="label" title={lane.job}>
          {icon ? (
            <img src={icon} alt="" width={18} height={18} draggable={false} />
          ) : (
            <span className="mp-lane-dot" style={{ background: jobColor(lane.job) }} />
          )}
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {lane.job}
          </span>
          <span className="badge">{lane.slot}</span>
        </div>
        <div className="strip" style={stripStyle}>
          {lane.casts.map((c, i) => {
            if (!(c.yOffset < 0 ? filter.ogcd : filter.gcd)) return null;
            const meta = c.abilityId != null ? result.abilityMeta[c.abilityId] : undefined;
            return (
              <span key={i}>
                <span
                  className="mp-span"
                  style={{
                    left: xOf(c.startSec),
                    width: Math.max(2, (c.endSec - c.startSec) * pxPerSec),
                    background: c.color,
                  }}
                />
                <TimelineCast
                  cast={c}
                  meta={meta}
                  scale={scale}
                  className={`cast def${c.color === '#565f89' ? ' mp-suggest' : ''}`}
                  size={30}
                  top={6}
                  title={c.tooltip}
                />
              </span>
            );
          })}
        </div>
      </div>
    );
  });

  const frontOverlay = (
    <>
      {markers.map((m, i) => (
        <div
          key={m.mechanicId}
          className={`mp-marker ${m.status}${hover === i ? ' on' : ''}`}
          style={{
            left: xOf(m.timeSec),
            width: Math.max(3, (m.endSec - m.timeSec) * pxPerSec),
          }}
          onMouseEnter={() => setHover(i)}
          onMouseLeave={() => setHover(null)}
          onClick={() => jumpTo(m)}
        />
      ))}
    </>
  );

  const bubble = (() => {
    if (hover == null) return null;
    const m = markers[hover];
    if (!m) return null;
    return (
      <div
        className="diff-bubble"
        style={{ left: clampBubbleLeft(xOf(m.timeSec), stripWidth), top: 26 }}
      >
        <div className="bub-head">
          <div>
            <div className="bub-kind">{m.name}</div>
          </div>
        </div>
        <div className="bub-body">
          {fmtClock(m.timeSec)} · {KIND_LABEL[m.kind]} · {SCHOOL_LABEL[m.school]} ·{' '}
          {fmtK(m.unmitTotal)} unmitigated party-wide — {m.status}. Click to jump
          to the card.
        </div>
      </div>
    );
  })();

  return (
    <TimelineShell
      scale={scale}
      zoom={zoom}
      setZoom={setZoom}
      filter={filter}
      setFilter={setFilter}
      hasRefs={false}
      helpText={MP_HELP}
      axisMarks={[{ sec: result.modelKillSec, label: fmtClock(result.modelKillSec), className: 'target' }]}
      backOverlay={backOverlay}
      lanes={<>{lanes}</>}
      frontOverlay={frontOverlay}
      bubble={bubble}
      embedded
    />
  );
};

// --- The view ----------------------------------------------------------------

export const MitigationPlanner = ({ defaultEncounterId, pullContext, onAnalyze }: Props) => {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [encounterId, setEncounterId] = useState<number>(
    pullContext?.encounterId || defaultEncounterId || 0);
  const [shieldHealer, setShieldHealer] = useState<string>('Sage');
  const [regenHealer, setRegenHealer] = useState<string>('White Mage');
  const [tanks, setTanks] = useState<string[]>(['Paladin', 'Dark Knight']);
  const [dps, setDps] = useState<string[]>(['Samurai', 'Dragoon', 'Bard', 'Pictomancer']);
  const [result, setResult] = useState<MitPlanResult | null>(null);
  const [lastRunKey, setLastRunKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState<{ pct: number; stage: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The comp key the routed pull resolved to (null when no pull) — the
  // "compAdjusted" baseline — plus where the comp came from + any
  // substitution warnings, for the comp-source line.
  const [pullCompKey, setPullCompKey] = useState<string | null>(null);
  const [compSource, setCompSource] = useState<MitPlanResult['compSource']>();
  const [compWarnings, setCompWarnings] = useState<string[]>([]);
  // Ultimate-only: lock the hand-authored premade ("PF") plan rather than the
  // sim-derived one. Defaults ON — where an ultimate ships a PF plan it's the
  // one groups actually run, so it's the better starting point; the `usePf`
  // gate below no-ops it on encounters without a premade. Toggling marks the
  // plan dirty (like a comp change), so the Re-plan button re-runs with it.
  const [usePfPlan, setUsePfPlan] = useState(true);
  // Healer flow only: a routed pull's comp is resolved backend-side, so until
  // its plan returns the selectors would show the stale defaults (Sage / White
  // Mage) — misleading on, say, a Scholar/Astrologian log. Gate the whole
  // config panel behind a loading screen until the plan seeds the real comp, so
  // the user never sees a duo that isn't the one they ran.
  const isPullRoute = !!(pullContext?.reportCode && pullContext?.fightId
    && pullContext?.encounterId);
  const [pullSeeding, setPullSeeding] = useState(isPullRoute);

  useEffect(() => {
    let alive = true;
    sidecar
      .getCatalog()
      .then((c) => {
        if (!alive) return;
        setCatalog(c);
        setEncounterId((e) =>
          e && c.encounters.some((x) => x.id === e) ? e : c.encounters[0]?.id ?? 0,
        );
      })
      .catch(() => setError('Could not load the encounter catalog.'));
    return () => {
      alive = false;
    };
  }, []);

  const encounters = useMemo(() => catalog?.encounters ?? [], [catalog]);

  // A hand-authored premade plan is available only for ultimates that ship one.
  const activeEnc = useMemo(
    () => encounters.find((e) => e.id === encounterId), [encounters, encounterId]);
  const pfAvailable = activeEnc?.category === 'ultimate' && !!activeEnc?.hasPfPlan;
  const usePf = usePfPlan && pfAvailable;

  const compKey = `${shieldHealer}|${regenHealer}|${tanks.join(',')}|${dps.join(',')}`;
  const runKey = `${encounterId}|${compKey}|${usePf ? 'pf' : 'auto'}`;
  const dirty = lastRunKey === null || lastRunKey !== runKey;
  const canRun = !!encounterId && !loading;

  const run = async () => {
    if (!canRun) return;
    setLoading(true);
    setError(null);
    setProgress({ pct: 0, stage: 'Starting…' });
    try {
      const res = await sidecar.planMitigation(
        { encounterId, shieldHealer, regenHealer, tanks, dps, usePfMitPlan: usePf },
        (pct, stage) => setProgress({ pct, stage }),
      );
      setResult(res);
      setLastRunKey(runKey);
      setCompSource(res.compSource ?? 'request');
      setCompWarnings(res.compWarnings ?? []);
    } catch (e) {
      setError(`Plan failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
      setProgress(null);
    }
  };

  // Healer flow: a routed pull auto-runs the plan with its comp resolved
  // backend-side from the pull's actors, then seeds the selectors from the
  // response's slot order (T1,T2,H1,H2,D1..D4) so the user can adjust from
  // what they actually ran with.
  useEffect(() => {
    const pc = pullContext;
    if (!pc?.reportCode || !pc.fightId || !pc.encounterId) return;
    let alive = true;
    // The initializer covers the mount; this covers a re-route to another
    // pull while the view stays mounted (same pattern as SetupView's reset).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setEncounterId(pc.encounterId);
    setUsePfPlan(true);    // default to the PF plan where the ultimate ships one
    setPullSeeding(true);  // hide the config panel until the real comp seeds
    setLoading(true);
    setError(null);
    setProgress({ pct: 0, stage: 'Reading your pull’s party…' });
    sidecar
      .planMitigation(
        // usePfMitPlan: true — the backend gates it to ultimates that ship a
        // premade (no-op otherwise), so the initial auto-run already previews
        // the PF plan on Dancing Mad without a Re-plan round-trip.
        { encounterId: pc.encounterId, reportCode: pc.reportCode,
          fightId: pc.fightId, spec: pc.job, usePfMitPlan: true },
        (pct, stage) => { if (alive) setProgress({ pct, stage }); },
      )
      .then((res) => {
        if (!alive) return;
        const pj = res.partyJobs;
        const seedTanks = pj.slice(0, 2);
        const seedDps = pj.slice(4, 8);
        setTanks(seedTanks);
        setDps(seedDps);
        setShieldHealer(pj[2]);
        setRegenHealer(pj[3]);
        setResult(res);
        const seededComp = `${pj[2]}|${pj[3]}|${seedTanks.join(',')}|${seedDps.join(',')}`;
        // Match the run-key suffix to what actually applied so the plan isn't
        // marked dirty on arrival (PF where it took, sim/auto where it didn't).
        setLastRunKey(`${pc.encounterId}|${seededComp}|${res.pfPlanApplied ? 'pf' : 'auto'}`);
        setPullCompKey(seededComp);
        setCompSource(res.compSource ?? 'pull');
        setCompWarnings(res.compWarnings ?? []);
      })
      .catch((e) => {
        if (!alive) return;
        setError(`Plan failed: ${e instanceof Error ? e.message : String(e)}`);
      })
      .finally(() => {
        if (!alive) return;
        setLoading(false);
        setPullSeeding(false);   // comp is seeded (or errored) — reveal the panel
        setProgress(null);
      });
    return () => {
      alive = false;
    };
  }, [pullContext?.reportCode, pullContext?.fightId, pullContext?.encounterId]); // eslint-disable-line react-hooks/exhaustive-deps

  const compAdjusted = pullCompKey !== null && compKey !== pullCompKey;
  const canAnalyze = !!onAnalyze && !!result && !!pullContext
    && encounterId === pullContext.encounterId;

  const duoTile = (j: string, on: boolean, set: (j: string) => void) => {
    const icon = jobIcon(j);
    return (
      <button
        key={j}
        className={'btn job-tile ' + (on ? 'primary' : '')}
        onClick={() => set(j)}
      >
        {icon ? (
          <img src={icon} alt="" width={22} height={22} draggable={false} className="job-tile-icon" />
        ) : (
          <span className="job-tile-icon" style={{ background: jobColor(j) }} />
        )}
        <span className="job-tile-label">{j}</span>
      </button>
    );
  };

  const jobSelect = (value: string, options: readonly string[], onChange: (j: string) => void, key: string) => (
    <select key={key} className="select mp-job-select" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((j) => (
        <option key={j} value={j}>{j}</option>
      ))}
    </select>
  );

  // PF plan only: role-generic mits (Feint/Addle/Reprisal) resolve to comp jobs
  // in slot order, so swapping two same-row slots just reassigns who casts them
  // — the ability placement (which mechanic, when) is unchanged.
  const swapDps = (i: number) =>
    setDps((d) => { const n = [...d]; [n[i], n[i + 1]] = [n[i + 1], n[i]]; return n; });
  const swapBtnStyle = { padding: '3px 9px', fontSize: 11.5, minWidth: 0 } as const;

  const s = result?.summary;

  return (
    <div className="content">
      <div className="card ktt-card">
        <div className="card-head">
          <HeartPulse size={14} />
          <h2>Healing / Mitigation</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            A shareable mitigation plan for your healer duo
          </span>
        </div>
        <div className="card-body">
          {pullSeeding ? (
            <div className="mp-seeding">
              <Loader2 size={22} className="mp-seeding-spin" />
              <div className="mp-seeding-lbl">
                {progress?.stage ?? 'Reading your pull’s party…'}
              </div>
              <div className="mp-seeding-track">
                <div
                  className="ktt-progress-bar"
                  style={{ width: `${progress?.pct ?? 6}%` }}
                />
              </div>
              <span className="ktt-hint mp-seeding-hint">
                Reading the party you ran with so the plan matches your pull.
              </span>
            </div>
          ) : (<>
          <p className="mut" style={{ fontSize: 12.5, margin: '0 0 14px' }}>
            Pick an encounter and your healer duo — the planner measures every
            forced hit (raidwides, busters, bleeds) across the encounter’s top
            kill logs, then schedules party mitigation, healer cooldowns, and
            shields so the damage is handled with as few healing GCDs as
            possible. No character or prior analysis required.
          </p>

          <div className="ktt-form">
            <div className="ktt-field-block">
              <span className="field-label">
                <Target size={12} /> Encounter
              </span>
              <select
                className="select"
                value={encounterId}
                onChange={(e) => setEncounterId(Number(e.target.value))}
              >
                {encounters.length === 0 && <option value={0}>Loading…</option>}
                {encounters.map((e) => (
                  <option key={e.id} value={e.id}>{e.name}</option>
                ))}
              </select>
            </div>

            {pfAvailable && (
              <div className="ktt-field-block">
                <span className="field-label">
                  <ClipboardList size={12} /> Mit plan source
                </span>
                <div className="job-grid mp-duo">
                  <button
                    className={'btn job-tile ' + (!usePfPlan ? 'primary' : '')}
                    onClick={() => setUsePfPlan(false)}
                  >
                    <span className="job-tile-label">Sim Plan (BETA)</span>
                  </button>
                  <button
                    className={'btn job-tile ' + (usePfPlan ? 'primary' : '')}
                    onClick={() => setUsePfPlan(true)}
                  >
                    <span className="job-tile-label">Use PF mit plan</span>
                  </button>
                </div>
                <span className="ktt-hint">
                  The premade party-finder plan for this ultimate pins which mit
                  covers each mechanic; the sim still schedules the timing.
                </span>
              </div>
            )}

            <div className="ktt-field-block">
              <span className="field-label">
                <Shield size={12} /> Shield healer
              </span>
              <div className="job-grid mp-duo">
                {SHIELD_HEALERS.map((j) => duoTile(j, shieldHealer === j, setShieldHealer))}
              </div>
            </div>

            <div className="ktt-field-block">
              <span className="field-label">
                <Sparkles size={12} /> Regen healer
              </span>
              <div className="job-grid mp-duo">
                {REGEN_HEALERS.map((j) => duoTile(j, regenHealer === j, setRegenHealer))}
              </div>
            </div>

            <div className="ktt-field-block">
              <span className="field-label">
                <Swords size={12} /> Rest of the party
              </span>
              <div className="mp-comp">
                {jobSelect(tanks[0], TANK_JOBS, (j) => setTanks([j, tanks[1]]), 't1')}
                {jobSelect(tanks[1], TANK_JOBS, (j) => setTanks([tanks[0], j]), 't2')}
                {dps.map((d, i) =>
                  jobSelect(d, DPS_JOBS, (j) => setDps(dps.map((x, k) => (k === i ? j : x))), `d${i}`),
                )}
              </div>
              <span className="ktt-hint">2 tanks + 4 DPS — their Reprisal, Feint, Addle and 90s mits are scheduled into the plan</span>
              {usePf && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginTop: 8 }}>
                  <span className="ktt-hint" style={{ margin: 0 }}>Swap who casts:</span>
                  <button type="button" className="btn" style={swapBtnStyle}
                    onClick={() => setTanks([tanks[1], tanks[0]])}>
                    {tanks[0]} ⇄ {tanks[1]}
                  </button>
                  {[0, 1, 2].map((i) => (
                    <button key={i} type="button" className="btn" style={swapBtnStyle}
                      onClick={() => swapDps(i)}>
                      {dps[i]} ⇄ {dps[i + 1]}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {(dirty || loading) && (
              <div className="ktt-run-row">
                <button className="btn primary ktt-run" disabled={!canRun} onClick={() => void run()}>
                  <Play size={13} />
                  {loading ? 'Planning…' : lastRunKey ? 'Re-plan' : 'Build plan'}
                </button>
              </div>
            )}

            {canAnalyze && (
              <div className="ktt-run-row">
                <button
                  className="btn primary ktt-run"
                  disabled={loading || dirty}
                  title={dirty ? 'Re-plan first so the plan matches these selections' : undefined}
                  onClick={() => onAnalyze!(
                    { shieldHealer, regenHealer, tanks, dps }, compAdjusted, usePf)}
                >
                  <Sparkles size={13} />
                  Analyze my pull
                </button>
                <span className="ktt-hint">
                  Runs the standard analysis with these planned heals locked
                  into your damage ceiling — the honest maximum for a healer.
                </span>
              </div>
            )}
          </div>
          </>)}

          {loading && progress && !pullSeeding && (
            <div className="ktt-progress">
              <div className="ktt-progress-track">
                <div className="ktt-progress-bar" style={{ width: `${progress.pct}%` }} />
              </div>
              <span className="ktt-progress-lbl mut">{progress.stage}</span>
            </div>
          )}

          {error && (
            <div className="ktt-error" role="alert">
              {error}
            </div>
          )}

          {result && s && (
            <div className="ktt-result">
              <div className="mp-summary">
                <div className="mp-summary-main">
                  <ShieldCheck size={16} />
                  <div>
                    <div className="mp-summary-title">
                      {result.encounterName} · {s.mechanicCount} forced mechanics
                    </div>
                    <div className="mut mp-summary-sub">
                      {s.raidwideCount} raidwides · {s.tankbusterCount} busters ·{' '}
                      {s.bleedCount + s.multiHitCount} bleeds/trains — from{' '}
                      {result.refCount} top kills (median {fmtClock(result.modelKillSec)})
                      {result.avoidableCount > 0 && `, ${result.avoidableCount} avoidable instances excluded`}
                    </div>
                  </div>
                </div>
                <div className="mp-pills">
                  {result.pfPlanApplied && (
                    <span
                      className="mp-pill covered"
                      title="Locked to the premade party-finder plan for this ultimate — healer mits are the plan's; the sim scheduled the timing"
                    >
                      <ClipboardList size={11} /> PF plan
                    </span>
                  )}
                  <span className="mp-pill covered">{s.coveredCount} covered</span>
                  {s.tightCount > 0 && <span className="mp-pill tight">{s.tightCount} tight</span>}
                  {s.uncoveredCount > 0 && <span className="mp-pill uncovered">{s.uncoveredCount} uncovered</span>}
                  <span
                    className="mp-pill mut"
                    title={`${s.gcdHealCount} healing GCDs (~${fmtDuration(s.gcdHealTimeSec)} of cast time, ~${Math.round(s.gcdHealPotencyLost)} potency) — everything else is oGCD, so the duo's damage rotation is untouched`}
                  >
                    <Users size={11} /> {s.gcdHealCount} GCD heals
                  </span>
                </div>
              </div>
              {(compSource === 'pull' || compWarnings.length > 0) && (
                <div className="mp-warnings mut">
                  {compSource === 'pull' && !compAdjusted && 'Party comp read from your pull. '}
                  {compSource === 'pull' && compAdjusted && 'Comp adjusted away from your pull’s — the analysis will use the adjusted plan. '}
                  {compWarnings.join(' ')}
                </div>
              )}
              {result.warnings.length > 0 && (
                <div className="mp-warnings mut">{result.warnings.join(' ')}</div>
              )}
              <div className="mp-section">
                <div className="mp-section-head">
                  <ListChecks size={14} />
                  <h3>Mitigation Plan</h3>
                  <span className="sub mut">
                    who covers what, top to bottom — click a row for details
                  </span>
                </div>
                <MitPlanBoard result={result} />
              </div>
              <div className="mp-section mp-section-timeline">
                <div className="mp-section-head">
                  <Clock size={14} />
                  <h3>Mitigation Timeline</h3>
                  <span className="sub mut">
                    the same plan on the fight’s clock
                  </span>
                </div>
                <MitPlanTimeline result={result} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
