import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import {
  ChevronDown, ClipboardList, Clock, Download, Eraser, ListChecks, Loader2,
  Play, ShieldCheck, Sparkles, TriangleAlert, Upload, Users, Wand2,
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
import { revealPath } from '../tauri/revealPath';
import { MitPlanBoard } from './MitPlanBoard';
import { MitPalette } from './MitPalette';
import {
  draftCount, draftToPlan, healCount, loadPlanDraft, planToDraft,
  savePlanDraft, seedFromResult, type MitDraft, type MitHealDraft,
} from './mitPlanDraft';
import { blockedRowsFor } from './mitPlanAvailability';
import { KIND_LABEL, SCHOOL_LABEL, fmtK, renderMitSheet } from './mitPlanShared';
import type {
  Catalog, MitCompSelection, MitDamageMarker, MitLibraryAction,
  MitLibraryResult, MitPlanResult, UserMitPlan,
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
   *  ("PF") plan instead of the auto one (ultimates that ship one);
   *  `userMitPlan` locks the user's custom plan (never both). */
  onAnalyze?: (comp: MitCompSelection, compAdjusted: boolean,
               usePfPlan: boolean, userMitPlan?: UserMitPlan) => void;
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
  // Where the plan comes from: the hand-authored premade ("PF") plan (defaults
  // ON — where an ultimate ships one it's the plan groups actually run; the
  // `usePf` gate below no-ops it elsewhere), the sim's auto plan, or the
  // user's own drag-and-drop CUSTOM plan. Toggling pf/sim marks the plan dirty
  // (like a comp change); custom auto-scores on every edit instead.
  const [planSource, setPlanSource] = useState<'pf' | 'sim' | 'custom'>('pf');
  // Custom-plan editor state: the draft (mechanicId → placed mits — the
  // editor's source of truth), the comp's ability palette, the chip currently
  // dragging, and the lightweight re-score spinner.
  const [draft, setDraft] = useState<MitDraft>({});
  // The authored per-gap GCD heals (the detail card's +/- incrementer) —
  // plan content beside the mits, never auto-inserted for a custom plan.
  const [healDraft, setHealDraft] = useState<MitHealDraft>({});
  const [library, setLibrary] = useState<MitLibraryResult | null>(null);
  const [dragAction, setDragAction] = useState<
    { slot: string; job: string; action: MitLibraryAction;
      /** Set when an already-placed bar is being MOVED (its mechanic id). */
      from?: string } | null>(null);
  const [scoring, setScoring] = useState(false);
  // Editor-local notices (import matching, export fallback) merged into the
  // notices row alongside the backend's warnings.
  const [localNotices, setLocalNotices] = useState<string[]>([]);
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const libCache = useRef<Record<string, MitLibraryResult>>({});
  // The encounter whose autosave has been checked (restore runs once per
  // encounter visit, and persisting is gated behind it so the on-switch draft
  // clear can never wipe the NEXT encounter's autosave).
  const restoredEnc = useRef<number | null>(null);
  // Monotonic score sequence — the sidecar runs each request on its own
  // thread, so a stale response must never clobber a newer draft's result.
  const scoreSeq = useRef(0);
  // Latest result without making the score effect depend on it (that would
  // re-fire the effect on its own response — an infinite loop).
  const resultRef = useRef<MitPlanResult | null>(null);
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
  const usePf = planSource === 'pf' && pfAvailable;
  // The effective source: a 'pf' selection quietly means 'sim' on encounters
  // without a premade (the PF tab isn't even rendered there).
  const source: 'pf' | 'sim' | 'custom' =
    planSource === 'custom' ? 'custom' : usePf ? 'pf' : 'sim';

  const compKey = `${shieldHealer}|${regenHealer}|${tanks.join(',')}|${dps.join(',')}`;
  const runKey = `${encounterId}|${compKey}|${source === 'pf' ? 'pf' : source === 'custom' ? 'custom' : 'auto'}`;
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
    setPlanSource('pf');   // default to the PF plan where the ultimate ships one
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

  useEffect(() => {
    resultRef.current = result;
  }, [result]);

  // A draft is per-encounter: its keys are that encounter's mechanic ids.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDraft({});
    setHealDraft({});
    setLocalNotices([]);
  }, [encounterId]);

  // Restore the encounter's autosaved draft once the board's mechanics and
  // the palette are both known (once per encounter visit; a user edit that
  // lands first wins).
  useEffect(() => {
    if (planSource !== 'custom' || !library || !result
        || result.encounterId !== encounterId) return;
    if (restoredEnc.current === encounterId) return;
    restoredEnc.current = encounterId;
    const saved = loadPlanDraft(encounterId);
    if (!saved || draftCount(draft) > 0 || healCount(healDraft) > 0) return;
    const { draft: restored, heals, notices } = planToDraft(
      saved, result.mechanics, library);
    if (draftCount(restored) > 0 || healCount(heals) > 0) {
      // Restoring persisted state IS this effect's job (it reads localStorage).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setDraft(restored);
      setHealDraft(heals);
      if (notices.length) setLocalNotices((n) => [...n, ...notices]);
    }
  }, [planSource, library, result, encounterId, draft, healDraft]);

  // Autosave: every draft change after the restore check persists (an empty
  // draft removes the slot — "Clear plan" means it).
  useEffect(() => {
    if (planSource !== 'custom' || restoredEnc.current !== encounterId) return;
    const mechs = resultRef.current?.encounterId === encounterId
      ? resultRef.current.mechanics : [];
    if (!mechs.length) return;
    savePlanDraft(encounterId, draftCount(draft) || healCount(healDraft)
      ? draftToPlan(draft, healDraft, mechs, encounterId, activeEnc?.name ?? '')
      : null);
    // resultRef/activeEnc read at save time on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planSource, encounterId, draft, healDraft]);

  // Custom mode needs the comp's ability palette (cached per comp — the
  // backend memoizes too, this just skips the round trip).
  useEffect(() => {
    if (planSource !== 'custom') return;
    const cached = libCache.current[compKey];
    if (cached) {
      setLibrary(cached);
      return;
    }
    let alive = true;
    setLibrary(null);
    sidecar.getMitLibrary({ shieldHealer, regenHealer, tanks, dps })
      .then((lib) => {
        if (!alive) return;
        libCache.current[compKey] = lib;
        setLibrary(lib);
      })
      .catch(() => {
        if (alive) setError('Could not load the ability palette.');
      });
    return () => {
      alive = false;
    };
  }, [planSource, compKey, shieldHealer, regenHealer, tanks, dps]);

  // Custom mode auto-scores: every draft/comp edit re-runs the backend with
  // the authored plan (the damage model is cached, so warm re-scores are
  // milliseconds — debounced + sequence-guarded against out-of-order
  // responses). The first run on a cold encounter shows the full progress
  // track while the model downloads.
  useEffect(() => {
    if (planSource !== 'custom' || !encounterId || pullSeeding) return;
    const seq = ++scoreSeq.current;
    const cold = !resultRef.current
      || resultRef.current.encounterId !== encounterId;
    const timer = setTimeout(() => {
      const mechs = resultRef.current?.encounterId === encounterId
        ? resultRef.current.mechanics : [];
      if (cold) {
        setLoading(true);
        setProgress({ pct: 0, stage: 'Starting…' });
      } else {
        setScoring(true);
      }
      setError(null);
      sidecar.planMitigation(
        {
          encounterId, shieldHealer, regenHealer, tanks, dps,
          userMitPlan: draftToPlan(draft, healDraft, mechs, encounterId,
                                   activeEnc?.name ?? ''),
        },
        cold ? (pct, stage) => setProgress({ pct, stage }) : undefined,
      )
        .then((res) => {
          if (seq !== scoreSeq.current) return;
          setResult(res);
          setLastRunKey(`${encounterId}|${compKey}|custom`);
          setCompSource(res.compSource ?? 'request');
          setCompWarnings(res.compWarnings ?? []);
          // Reconcile: entries the backend REJECTED (cooldown, invuln rules,
          // same-debuff dedupe, off-comp imports) would otherwise linger
          // invisibly in the draft — no bar to remove, yet still blocking
          // the availability preview. Prune them; the backend's warning has
          // already surfaced in the notices row. Safe against racing edits:
          // the seq guard above means this response IS the current draft's.
          const mechById = new Map(res.mechanics.map((m) => [m.id, m]));
          setDraft((d) => {
            let changed = false;
            const next: MitDraft = {};
            for (const [id, mits] of Object.entries(d)) {
              const m = mechById.get(id);
              const kept = mits.filter((x) => m?.assignments.some(
                (a) => !a.isCarryover && a.job === x.job
                  && a.actionId === x.actionId));
              if (kept.length) next[id] = kept;
              if (kept.length !== mits.length) changed = true;
            }
            return changed ? next : d;
          });
          setHealDraft((d) => {
            let changed = false;
            const next: MitHealDraft = {};
            for (const [id, hs] of Object.entries(d)) {
              const m = mechById.get(id);
              const kept: typeof hs = [];
              for (const x of hs) {
                const g = m?.gcdHeals.find(
                  (g2) => g2.job === x.job && g2.actionId === x.actionId);
                if (!g) {
                  changed = true;      // dropped entirely (off-comp, no pool)
                  continue;
                }
                if (g.count < x.count) {
                  // The backend trimmed the count (the lily bucket ran dry
                  // before this mechanic) — sync the incrementer down so the
                  // draft never claims heals the plan can't schedule.
                  changed = true;
                  kept.push({ ...x, count: g.count });
                } else {
                  kept.push(x);
                }
              }
              if (kept.length) next[id] = kept;
            }
            return changed ? next : d;
          });
        })
        .catch((e) => {
          if (seq !== scoreSeq.current) return;
          setError(`Plan failed: ${e instanceof Error ? e.message : String(e)}`);
        })
        .finally(() => {
          if (seq !== scoreSeq.current) return;
          setLoading(false);
          setScoring(false);
          setProgress(null);
        });
    }, cold ? 0 : 300);
    return () => clearTimeout(timer);
    // resultRef/activeEnc are read at fire time on purpose — depending on the
    // result would re-fire the effect on its own response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planSource, encounterId, compKey, draft, healDraft, pullSeeding]);

  // Editor edits — all paths just update the draft; the score effect owns
  // the round trip. A payload with `from` is a MOVE: the source mechanic's
  // copy is removed in the same update.
  const addToDraft = (mechanicId: string,
                      p: { job: string; actionId: number; from?: string }) => {
    setDraft((d) => {
      if (p.from === mechanicId) return d;     // dropped back home — no-op
      let next = d;
      if (p.from) {
        const src = (d[p.from] ?? [])
          .filter((x) => !(x.job === p.job && x.actionId === p.actionId));
        next = { ...d };
        if (src.length) next[p.from] = src;
        else delete next[p.from];
      }
      const mits = next[mechanicId] ?? [];
      if (mits.some((x) => x.job === p.job && x.actionId === p.actionId)) {
        return next === d ? d : next;
      }
      return { ...next,
               [mechanicId]: [...mits, { job: p.job, actionId: p.actionId }] };
    });
    setDragAction(null);
  };

  // A placed bar starts dragging: mirror it into dragAction (with `from`) so
  // the drop strips + availability preview treat it as a move.
  const onCastDragStart = (from: string, slot: string, job: string,
                           actionId: number) => {
    const action = library?.slots.find((s) => s.job === job)
      ?.actions.find((a) => a.actionId === actionId);
    if (action) setDragAction({ slot, job, action, from });
  };

  // Native HTML5 drag never scrolls overflow containers, so while a drag is
  // live, edge-scroll every scrollable ancestor under the pointer (the board's
  // .mpb-scroll and the page's .content both matter here).
  useEffect(() => {
    if (!dragAction) return;
    const ZONE = 64;
    // Arm only after real movement: grabbing a bar that happens to sit inside
    // an edge zone must not start scrolling on its own.
    let startY: number | null = null;
    let armed = false;
    const onDragOver = (e: DragEvent) => {
      if (startY == null) startY = e.clientY;
      if (!armed && Math.abs(e.clientY - startY) > 28) armed = true;
      if (!armed) return;
      let el: Element | null = e.target instanceof Element ? e.target : null;
      while (el) {
        if (el instanceof HTMLElement
            && el.scrollHeight > el.clientHeight + 4) {
          const cs = getComputedStyle(el);
          if (/(auto|scroll)/.test(cs.overflowY)) {
            const r = el.getBoundingClientRect();
            if (e.clientY < r.top + ZONE) {
              el.scrollTop -= Math.ceil((r.top + ZONE - e.clientY) / 3);
            } else if (e.clientY > r.bottom - ZONE) {
              el.scrollTop += Math.ceil((e.clientY - (r.bottom - ZONE)) / 3);
            }
          }
        }
        el = el.parentElement;
      }
    };
    document.addEventListener('dragover', onDragOver);
    return () => document.removeEventListener('dragover', onDragOver);
  }, [dragAction]);
  const removeFromDraft = (mechanicId: string, job: string, actionId: number) => {
    setDraft((d) => {
      const mits = (d[mechanicId] ?? [])
        .filter((x) => !(x.job === job && x.actionId === actionId));
      const next = { ...d };
      if (mits.length) next[mechanicId] = mits;
      else delete next[mechanicId];
      return next;
    });
  };

  // "Seed from sim plan": one plain (auto) plan round trip, converted into the
  // draft — non-suggestion, non-carryover assignments, plus the sim's heal
  // choices as authored heals (the user's to keep, trim, or grow).
  const seedFromSim = async () => {
    setScoring(true);
    setError(null);
    try {
      const res = await sidecar.planMitigation(
        { encounterId, shieldHealer, regenHealer, tanks, dps,
          usePfMitPlan: false });
      const seeded = seedFromResult(res);
      setDraft(seeded.mits);
      setHealDraft(seeded.heals);
    } catch (e) {
      setError(`Seed failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setScoring(false);
    }
  };

  // The detail card's heal incrementer: bump one healer AoE GCD heal for the
  // gap before `mechanicId` (count clamps 0..8; 0 removes the row).
  const onHealDelta = (mechanicId: string, job: string, actionId: number,
                       delta: number) => {
    setHealDraft((d) => {
      const rows = d[mechanicId] ?? [];
      const prior = rows.find((x) => x.job === job && x.actionId === actionId);
      const count = Math.max(0, Math.min(8, (prior?.count ?? 0) + delta));
      const kept = rows.filter((x) => !(x.job === job && x.actionId === actionId));
      if (count > 0) kept.push({ job, actionId, count });
      const next = { ...d };
      if (kept.length) next[mechanicId] = kept;
      else delete next[mechanicId];
      return next;
    });
  };

  // Export: the sidecar writes two files into the config dir's mit_plans/
  // folder — the plan JSON (the wire format verbatim, re-importable) plus a
  // same-stem .txt mit sheet (the human copy for Discord/PF) — and Explorer
  // reveals them, the feedback-zip pattern (no extra Tauri permissions).
  // Plain-browser dev can't reveal, so the paths land in the notices row.
  const exportPlan = async () => {
    const res = resultRef.current;
    const mechs = res?.encounterId === encounterId ? res.mechanics : [];
    if (!res || !mechs.length
        || (draftCount(draft) === 0 && healCount(healDraft) === 0)) return;
    // The premade files' convention: annotate each mit/heal with its ability
    // name (documentation only, ignored by the parser) so even the JSON reads.
    const nameOf = (aid: number): string | undefined =>
      res.abilityMeta[aid]?.name ?? library?.abilityMeta[aid]?.name;
    const plan = draftToPlan(draft, healDraft, mechs, encounterId,
                             activeEnc?.name ?? '');
    const annotated: UserMitPlan = {
      ...plan,
      assignments: plan.assignments.map((e) => ({
        ...e,
        mits: e.mits.map((mit) => {
          const n = nameOf(mit.action_id);
          return n ? { ...mit, _ability: n } : mit;
        }),
        ...(e.gcd_heals ? {
          gcd_heals: e.gcd_heals.map((h) => {
            const n = nameOf(h.action_id);
            return n ? { ...h, _ability: n } : h;
          }),
        } : {}),
      })),
    };
    try {
      const { path } = await sidecar.exportMitPlan({
        encounterId,
        fileName: `${activeEnc?.name ?? `encounter-${encounterId}`} mit plan`,
        plan: annotated,
        readable: renderMitSheet(res),
      });
      try {
        await revealPath(path);
      } catch {
        setLocalNotices((n) => [...n,
          `Plan exported to ${path} (readable .txt copy beside it)`]);
      }
    } catch (e) {
      setError(`Export failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Import: a native file picker (WebView2 supports file inputs) + FileReader.
  // planToDraft does the client-side matching; the backend re-validates on the
  // score that follows, so a hand-edited file degrades to warnings, never an
  // error.
  const onImportFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';   // allow picking the same file again later
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result)) as UserMitPlan;
        if (!parsed || !Array.isArray(parsed.assignments)) {
          setLocalNotices((n) => [...n,
            `"${file.name}" is not a mitigation plan file.`]);
          return;
        }
        const mechs = resultRef.current?.encounterId === encounterId
          ? resultRef.current.mechanics : [];
        if (!mechs.length || !library) {
          setLocalNotices((n) => [...n,
            'Wait for the plan board to finish loading, then import again.']);
          return;
        }
        const pre: string[] = [];
        if (parsed.encounter_id && parsed.encounter_id !== encounterId) {
          pre.push(`"${file.name}" was made for another encounter `
            + `(#${parsed.encounter_id}). Matching by mechanic anyway.`);
        }
        const { draft: imported, heals, notices } = planToDraft(
          parsed, mechs, library);
        setDraft(imported);
        setHealDraft(heals);
        setLocalNotices([...pre, ...notices]);
      } catch {
        setLocalNotices((n) => [...n, `Could not read "${file.name}" as JSON.`]);
      }
    };
    reader.readAsText(file);
  };

  // Rows the dragged palette chip cannot land on (client preview; the backend
  // stays authoritative and warns on anything this misses).
  const blockedRows = useMemo(() => {
    if (source !== 'custom' || !dragAction || !result || !library
        || result.encounterId !== encounterId) {
      return undefined;
    }
    return blockedRowsFor(dragAction.action, dragAction.job, draft,
                          result.mechanics, library, dragAction.from);
  }, [source, dragAction, result, library, draft, encounterId]);

  // The healers' authorable AoE GCD heals (the detail card's incrementer),
  // with palette icons folded in.
  const healerHealOptions = useMemo(() => {
    if (!library) return [];
    return library.slots
      .filter((s) => (s.healOptions ?? []).length > 0)
      .map((s) => ({
        slot: s.slot, job: s.job,
        options: s.healOptions.map((o) => ({
          ...o, iconPath: library.abilityMeta[o.actionId]?.iconPath,
        })),
      }));
  }, [library]);

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
  const notices = result
    ? [...compWarnings, ...result.warnings, ...localNotices] : localNotices;
  const noticeSummary = (() => {
    let skipped = 0, unmatched = 0, pf = 0, user = 0, other = 0;
    for (const w of result?.warnings ?? []) {
      if (w.startsWith('PF plan: no mechanic matched')) unmatched += 1;
      else if (w.startsWith('PF plan:')) pf += 1;
      else if (w.startsWith('Your plan:')) user += 1;
      else if (w.includes('skipped:')) skipped += 1;
      else other += 1;
    }
    const parts: string[] = [];
    if (skipped) parts.push(`${skipped} log${skipped === 1 ? '' : 's'} skipped`);
    if (unmatched) parts.push(`${unmatched} mechanic${unmatched === 1 ? '' : 's'} unmatched`);
    if (pf) parts.push(`${pf} PF plan notice${pf === 1 ? '' : 's'}`);
    if (user) parts.push(`${user} custom plan notice${user === 1 ? '' : 's'}`);
    if (localNotices.length) {
      parts.push(`${localNotices.length} editor notice${localNotices.length === 1 ? '' : 's'}`);
    }
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

          <div className="setup-row">
            <span className="setup-label">Plan source</span>
            <div className="setup-controls">
              <div className="seg-tabs">
                {pfAvailable && (
                  <button
                    className={'seg-tab' + (source === 'pf' ? ' on' : '')}
                    onClick={() => setPlanSource('pf')}
                  >
                    PF mit plan
                  </button>
                )}
                <button
                  className={'seg-tab' + (source === 'sim' ? ' on' : '')}
                  onClick={() => setPlanSource('sim')}
                >
                  Sim plan <span className="seg-suffix">BETA</span>
                </button>
                <button
                  className={'seg-tab' + (source === 'custom' ? ' on' : '')}
                  onClick={() => setPlanSource('custom')}
                >
                  Custom plan <span className="seg-suffix">NEW</span>
                </button>
              </div>
              <span className="setup-hint" style={{ maxWidth: 520 }}>
                {source === 'custom'
                  ? 'Build your own plan: drag abilities from the palette onto '
                    + 'the damage rows below. Cooldowns and resources are '
                    + 'enforced; healing GCDs are computed to survive the rest.'
                  : source === 'pf'
                    ? 'The premade party-finder plan pins which mit covers each '
                      + 'mechanic; the sim still schedules the timing.'
                    : 'The sim schedules mitigation automatically from the '
                      + "encounter's forced damage."}
              </span>
            </div>
          </div>

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
              {source === 'custom'
                ? 'Click a slot to change the job · drag abilities from the palette onto damage rows'
                : swapLive
                  ? 'Click a slot to change the job · drag a card onto another to swap who casts'
                  : 'Click a slot to change the job'}
            </span>
            {source === 'custom' ? (
              <span className="setup-footer-run">
                {scoring && (
                  <span className="mp-scoring">
                    <Loader2 size={12} className="mp-seeding-spin" /> scoring…
                  </span>
                )}
                <button
                  className="btn"
                  disabled={loading || scoring || !encounterId}
                  title="Copy the sim's auto plan into your draft as a starting point"
                  onClick={() => void seedFromSim()}
                >
                  <Wand2 size={13} />
                  Seed from sim plan
                </button>
                <button
                  className="btn"
                  disabled={loading || scoring
                    || (draftCount(draft) === 0 && healCount(healDraft) === 0)}
                  onClick={() => { setDraft({}); setHealDraft({}); }}
                >
                  <Eraser size={13} />
                  Clear plan
                </button>
                <button
                  className="btn"
                  disabled={loading || scoring
                    || (draftCount(draft) === 0 && healCount(healDraft) === 0)}
                  title="Save the plan as a shareable JSON file plus a readable text mit sheet (revealed in Explorer)"
                  onClick={() => void exportPlan()}
                >
                  <Download size={13} />
                  Export
                </button>
                <button
                  className="btn"
                  disabled={loading || scoring || !result}
                  title="Load a previously exported plan file"
                  onClick={() => importInputRef.current?.click()}
                >
                  <Upload size={13} />
                  Import
                </button>
                <input
                  ref={importInputRef}
                  type="file"
                  accept=".json,application/json"
                  style={{ display: 'none' }}
                  onChange={onImportFile}
                />
              </span>
            ) : (dirty || loading) && (
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
              disabled={loading || (source === 'custom' ? scoring : dirty)}
              title={source !== 'custom' && dirty
                ? 'Re-plan first so the plan matches these selections' : undefined}
              onClick={() => onAnalyze!(
                { shieldHealer, regenHealer, tanks, dps }, compAdjusted, usePf,
                source === 'custom'
                  ? draftToPlan(draft, healDraft,
                                result?.encounterId === encounterId
                                  ? result.mechanics : [],
                                encounterId, activeEnc?.name ?? '')
                  : undefined)}
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
              {result.userPlanApplied && (
                <span
                  className="mp-pill covered"
                  title="Scored from your custom plan: only the mits you placed are in it; healing GCDs are computed to survive the rest"
                >
                  <ClipboardList size={11} /> Custom plan
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
                {source === 'custom'
                  ? 'drag abilities onto damage rows · drag a bar to move it · × removes · click a row for details and heals'
                  : 'who covers what, top to bottom — click a row for details'}
              </span>
            </div>
            {source === 'custom' && (
              library ? (
                <MitPalette
                  library={library}
                  onDragStart={(slot, job, action) =>
                    setDragAction({ slot, job, action })}
                  onDragEnd={() => setDragAction(null)}
                />
              ) : (
                <div className="mp-pal-loading mut">
                  <Loader2 size={13} className="mp-seeding-spin" /> Loading
                  ability palette…
                </div>
              )
            )}
            <MitPlanBoard
              result={result}
              editable={source === 'custom'}
              dragAction={source === 'custom' ? dragAction : null}
              blockedRows={blockedRows}
              onDropAction={addToDraft}
              onRemove={source === 'custom' ? removeFromDraft : undefined}
              onCastDragStart={source === 'custom' ? onCastDragStart : undefined}
              onCastDragEnd={() => setDragAction(null)}
              healOptions={source === 'custom' ? healerHealOptions : undefined}
              healDraft={healDraft}
              onHealDelta={source === 'custom' ? onHealDelta : undefined}
            />
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
