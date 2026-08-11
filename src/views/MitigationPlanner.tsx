import { useEffect, useMemo, useState } from 'react';
import {
  ChevronDown, ClipboardList, Clock, ListChecks, Loader2, Play,
  ShieldCheck, Sparkles, TriangleAlert, Users,
} from 'lucide-react';
import {
  DPS_JOBS, REGEN_HEALERS, SHIELD_HEALERS, TANK_JOBS, jobColor, jobIcon,
} from '../components/jobs';
import { EncounterPicker } from '../components/EncounterPicker';
import { JobTile } from '../components/JobTile';
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
  // Party-strip UI state: which slot's job popover is open, and the two ends
  // of an in-flight drag-swap. All ephemeral.
  const [openSlot, setOpenSlot] = useState<string | null>(null);
  const [dragSlot, setDragSlot] = useState<string | null>(null);
  const [overSlot, setOverSlot] = useState<string | null>(null);
  // The collapsed notices row's disclosure.
  const [noticesOpen, setNoticesOpen] = useState(false);

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

  // "Swap who casts" is a PF-plan concept: role-generic mits (Feint/Addle/
  // Reprisal) resolve to comp jobs in slot order, so a swap reassigns who
  // casts them without moving the ability placement. Until a plan is on
  // screen with the PF source active there is nothing to reassign — the drag
  // affordance (and its hint) stays off the page.
  const swapLive = usePf && !!result;

  // The eight party slots, derived per render from the existing comp state.
  // Setters map straight onto it, so compKey/runKey/dirty work unchanged.
  type Slot = {
    id: string; label: string; job: string;
    legal: readonly string[]; set: (j: string) => void;
    healer?: boolean;
  };
  const slots: Slot[] = [
    { id: 'T1', label: 'T1', job: tanks[0], legal: TANK_JOBS,
      set: (j) => setTanks([j, tanks[1]]) },
    { id: 'T2', label: 'T2', job: tanks[1], legal: TANK_JOBS,
      set: (j) => setTanks([tanks[0], j]) },
    { id: 'H1', label: 'H1 · shield', job: shieldHealer, legal: SHIELD_HEALERS,
      set: setShieldHealer, healer: true },
    { id: 'H2', label: 'H2 · regen', job: regenHealer, legal: REGEN_HEALERS,
      set: setRegenHealer, healer: true },
    ...dps.map((d, i): Slot => ({
      id: `D${i + 1}`, label: `D${i + 1}`, job: d, legal: DPS_JOBS,
      set: (j) => setDps(dps.map((x, k) => (k === i ? j : x))),
    })),
  ];

  // Drag one card onto another to swap who casts — tank↔tank and dps↔dps only
  // (the healer slots have disjoint legal jobs, so a swap is never legal).
  // Role-generic mits (Feint/Addle/Reprisal) resolve to comp jobs in slot
  // order, so a swap just reassigns who casts them — the ability placement
  // (which mechanic, when) is unchanged.
  const swapSlots = (a: string, b: string) => {
    if (a === b || a[0] !== b[0]) return;
    if (a[0] === 'T') {
      setTanks((t) => [t[1], t[0]]);
    } else if (a[0] === 'D') {
      const i = Number(a[1]) - 1;
      const k = Number(b[1]) - 1;
      setDps((d) => { const n = [...d]; [n[i], n[k]] = [n[k], n[i]]; return n; });
    }
  };

  // The job popover closes on click-outside and Escape (not mouseleave — it
  // holds a list the user scans).
  useEffect(() => {
    if (openSlot === null) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element | null;
      if (!t?.closest('.slot-wrap')) setOpenSlot(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenSlot(null);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [openSlot]);

  const slotCard = (slot: Slot) => {
    const isOver = overSlot === slot.id && dragSlot !== null && dragSlot !== slot.id;
    return (
      <div className="slot-wrap" key={slot.id}>
        <button
          className={'slot-card' + (slot.healer ? ' healer' : '')
            + (isOver ? ' drag-over' : '')
            + (dragSlot === slot.id ? ' dragging' : '')}
          draggable={swapLive && !slot.healer}
          onClick={() => setOpenSlot((o) => (o === slot.id ? null : slot.id))}
          onDragStart={(e) => {
            e.dataTransfer.setData('text/plain', slot.id);
            e.dataTransfer.effectAllowed = 'move';
            setDragSlot(slot.id);
            setOpenSlot(null);
          }}
          onDragEnd={() => { setDragSlot(null); setOverSlot(null); }}
          onDragOver={(e) => {
            // preventDefault marks the card a valid drop target (Chromium
            // requires it) — same-role cards only, so a tank can never land
            // on a dps slot.
            if (!dragSlot || dragSlot === slot.id || dragSlot[0] !== slot.id[0]) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            setOverSlot(slot.id);
          }}
          onDragLeave={() => setOverSlot((o) => (o === slot.id ? null : o))}
          onDrop={(e) => {
            e.preventDefault();
            const from = e.dataTransfer.getData('text/plain') || dragSlot;
            if (from) swapSlots(from, slot.id);
            setDragSlot(null);
            setOverSlot(null);
          }}
        >
          <JobTile job={slot.job} size={24} iconInset={2} />
          <span className="slot-lines">
            <span className="slot-id mono">{slot.label}</span>
            <span className="slot-name">{slot.job}</span>
          </span>
        </button>
        {openSlot === slot.id && (
          <div className="slot-pop">
            {slot.legal.map((j) => (
              <button
                key={j}
                className={'slot-pop-opt' + (j === slot.job ? ' on' : '')}
                onClick={() => { slot.set(j); setOpenSlot(null); }}
              >
                <JobTile job={j} size={20} iconInset={2} />
                {j}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  const s = result?.summary;

  // One collapsed row for everything the planner wants to flag, counted by
  // category (backend strings print verbatim in the expanded details).
  const notices = result ? [...compWarnings, ...result.warnings] : [];
  const noticeSummary = (() => {
    let skipped = 0, unmatched = 0, pf = 0, other = 0;
    for (const w of result?.warnings ?? []) {
      if (w.startsWith('PF plan: no mechanic matched')) unmatched += 1;
      else if (w.startsWith('PF plan:')) pf += 1;
      else if (w.includes('skipped:')) skipped += 1;
      else other += 1;
    }
    const parts: string[] = [];
    if (skipped) parts.push(`${skipped} log${skipped === 1 ? '' : 's'} skipped`);
    if (unmatched) parts.push(`${unmatched} mechanic${unmatched === 1 ? '' : 's'} unmatched`);
    if (pf) parts.push(`${pf} PF plan notice${pf === 1 ? '' : 's'}`);
    if (compWarnings.length) {
      parts.push(`${compWarnings.length} party notice${compWarnings.length === 1 ? '' : 's'}`);
    }
    if (other && parts.length) parts.push(`${other} other`);
    const head = `${notices.length} notice${notices.length === 1 ? '' : 's'}`;
    // All-uncategorizable → just the count; a breakdown that restates it is noise.
    return parts.length ? `${head}: ${parts.join(', ')}` : head;
  })();

  // The encounter hint: where the selection sits within its category.
  const activeCat = activeEnc?.category ?? 'savage';
  const catEncounters = encounters.filter((e) => (e.category ?? 'savage') === activeCat);
  const catIdx = catEncounters.findIndex((e) => e.id === encounterId);
  const catNoun = activeCat === 'ultimate'
    ? (catEncounters.length === 1 ? 'ultimate' : 'ultimates')
    : (catEncounters.length === 1 ? 'savage encounter' : 'savage encounters');
  const encHint = catIdx >= 0
    ? `${catIdx + 1} of ${catEncounters.length} ${catNoun} in the catalog`
    : undefined;

  return (
    <div className="content wide">
      <div className="page-title-row">
        <div>
          <h1>Heal / Mit planner</h1>
          <p className="page-meta">
            A shareable mitigation plan for your healer duo, scheduled across
            the encounter's top kill logs.
          </p>
        </div>
      </div>

      {pullSeeding ? (
        <div className="setup-panel planner">
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
        </div>
      ) : (<>
        <div className="setup-panel planner">
          <div className="setup-row">
            <span className="setup-label">Encounter</span>
            <div className="setup-controls">
              <EncounterPicker
                encounters={encounters}
                encounterId={encounterId}
                onPick={setEncounterId}
                hint={encHint}
              />
            </div>
          </div>

          {pfAvailable && (
            <div className="setup-row">
              <span className="setup-label">Plan source</span>
              <div className="setup-controls">
                <div className="seg-tabs">
                  <button
                    className={'seg-tab' + (usePfPlan ? ' on' : '')}
                    onClick={() => setUsePfPlan(true)}
                  >
                    PF mit plan
                  </button>
                  <button
                    className={'seg-tab' + (!usePfPlan ? ' on' : '')}
                    onClick={() => setUsePfPlan(false)}
                  >
                    Sim plan <span className="seg-suffix">BETA</span>
                  </button>
                </div>
                <span className="setup-hint" style={{ maxWidth: 520 }}>
                  The premade party-finder plan pins which mit covers each
                  mechanic; the sim still schedules the timing.
                </span>
              </div>
            </div>
          )}

          <div className="setup-row">
            <span className="setup-label">Party</span>
            <div className="setup-controls party">
              {compSource === 'pull' && (
                <span className="slot-comp-note">
                  {compAdjusted
                    ? "Comp adjusted away from your pull's, the analysis will use the adjusted plan."
                    : 'Party comp read from your pull.'}
                </span>
              )}
              <div className="slot-strip">
                <div className="slot-group tanks">
                  <span className="slot-group-caption">Tanks</span>
                  <div className="slot-cards">{slots.slice(0, 2).map(slotCard)}</div>
                </div>
                <div className="slot-group healers">
                  <span className="slot-group-caption">Healers</span>
                  <div className="slot-cards">{slots.slice(2, 4).map(slotCard)}</div>
                </div>
                <div className="slot-group dps">
                  <span className="slot-group-caption">DPS</span>
                  <div className="slot-cards">{slots.slice(4).map(slotCard)}</div>
                </div>
              </div>
            </div>
          </div>

          <div className="setup-footer">
            <span className="setup-footer-hint">
              {swapLive
                ? 'Click a slot to change the job · drag a card onto another to swap who casts'
                : 'Click a slot to change the job'}
            </span>
            {(dirty || loading) && (
              <span className="setup-footer-run">
                {dirty && lastRunKey !== null && !loading && (
                  <span className="setup-dirty">Party changed. Re-plan to apply.</span>
                )}
                <button className="btn primary" disabled={!canRun} onClick={() => void run()}>
                  <Play size={13} />
                  {loading ? 'Planning…' : lastRunKey ? 'Re-plan' : 'Build plan'}
                </button>
              </span>
            )}
          </div>
        </div>

        {canAnalyze && (
          <div className="ktt-run-row" style={{ marginTop: 14 }}>
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
          {notices.length > 0 && (
            <div className="mp-notices">
              <button
                className="mp-notices-head"
                aria-expanded={noticesOpen}
                onClick={() => setNoticesOpen((o) => !o)}
              >
                <TriangleAlert size={14} />
                <span className="mp-notices-sum">{noticeSummary}</span>
                <span style={{ flex: 1 }} />
                <span className="mp-notices-toggle">
                  {noticesOpen ? 'Hide details' : 'Show details'}
                </span>
                <ChevronDown
                  size={12}
                  className={'mp-notices-chev' + (noticesOpen ? ' open' : '')}
                />
              </button>
              {noticesOpen && (
                <div className="mp-notices-body">
                  {notices.map((w, i) => (
                    <div key={i}>{w}</div>
                  ))}
                </div>
              )}
            </div>
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
  );
};
