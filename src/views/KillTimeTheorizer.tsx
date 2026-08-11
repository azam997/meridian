import { useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { ChevronDown, Pin, Play, Users } from 'lucide-react';
import { groupJobsByRole } from '../components/jobs';
import { JobTile } from '../components/JobTile';
import { EncounterPicker } from '../components/EncounterPicker';
import { TimelineShell, type FilterState } from '../components/timeline/TimelineShell';
import { TimelineCast } from '../components/timeline/TimelineCast';
import { clampBubbleLeft, useTimelineScale } from '../components/timeline/scale';
import { fmtClock, fmtDuration, fmtNum } from '../format';
import { sidecar } from '../sidecar';
import { nonRotationalNames } from '../jobs';
import type {
  AbilityMetaJson,
  CastEvent,
  Catalog,
  TheorizeResult,
} from '../sidecar/contract';

type Props = {
  /** Optional defaults from the app's current selection — the page works
   *  without them (no character/analysis required). */
  defaultJob?: string;
  defaultEncounterId?: number;
  /** If the user arrived from a finished analysis, seed the kill-time input
   *  with that pull's kill time; otherwise a neutral default. */
  defaultKillSec?: number;
};

// Width (s) of the kill-time band the backend samples around the entered target.
const RANGE_SEC = 7;
const DEFAULT_KILL_SEC = 480; // 8:00 — neutral starting point when nothing seeds it.
// Kill-time slider bounds (1:00–15:00); the text input still accepts the full
// backend-clamped [30, 1800] range for anything outside the slider.
const KT_SLIDER_MIN = 60;
const KT_SLIDER_MAX = 900;
const KT_SLIDER_STEP = 5;
// Reference lanes rendered before the "Show N more references" disclosure.
const REFS_COLLAPSED = 4;

/** Parse "mm:ss" or a plain seconds count into seconds; null when unparseable. */
function parseClock(raw: string): number | null {
  const s = raw.trim();
  if (!s) return null;
  const m = s.match(/^(\d+):([0-5]?\d)$/);
  if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  if (/^\d+(\.\d+)?$/.test(s)) return Math.round(parseFloat(s));
  return null;
}

/** FFLogs job names are spaceless ("RedMage", "BlackMage"); space them so they
 *  read naturally and match the JOB_META keys. */
const prettyJob = (j: string): string => j.replace(/([a-z])([A-Z])/g, '$1 $2');

// --- Rotation timeline ------------------------------------------------------

const THEORIZE_HELP =
  'Hover casts, downtime bands, and raid-buff flags for details.\n' +
  'oGCDs ride the upper band, GCDs the lower.\n' +
  'Reference lanes stack below the ideal line, in ranking order (top 10 by rDPS).\n' +
  'Click empty track to pin a time; click again to clear.\n' +
  'Gridlines mark the axis ticks.';

/** Which window the pointer is over (drives the info bubble). `scrollTop` is
 *  the strip's vertical scroll captured at enter time — the bubble overlay
 *  scrolls with the grid while the flag band / axis stay stuck, so a bubble
 *  parked near the top of the plot adds it to stay in view. */
type ThHover = { kind: 'down' | 'buff'; idx: number; scrollTop: number };

/** Vertical scroll of the surrounding strip at hover time. */
const scrollTopOf = (e: ReactMouseEvent): number =>
  e.currentTarget.closest('.timeline-scroll')?.scrollTop ?? 0;

/** Resolve a cast's display name: prefer the metadata map, else the tooltip's
 *  leading token (how name-only casts arrive). Mirrors TimelineView. */
function castDisplayName(c: CastEvent, abilityMeta: Record<number, AbilityMetaJson>): string {
  if (c.abilityId != null) {
    const n = abilityMeta[c.abilityId]?.name;
    if (n) return n;
  }
  return c.tooltip?.split('  ')[0]?.trim() ?? '';
}

/** A cast belongs on the rotation lanes unless it's defensive/utility — the
 *  backend `isDefensive` flag, or the shared name fallback for unresolved
 *  casts. Mirrors TimelineView's ref-lane filtering. */
const isRotational = (
  c: CastEvent,
  abilityMeta: Record<number, AbilityMetaJson>,
  nonRotNames: Set<string>,
): boolean => {
  if (c.abilityId != null) {
    const m = abilityMeta[c.abilityId];
    if (m?.isDefensive) return false;
    if (m) return true;
  }
  return !nonRotNames.has(castDisplayName(c, abilityMeta));
};

/** The theorized rotation with the full Timeline-page chrome: the ideal lane on
 *  top (pinned by default), every reference kill stacked below it, the flag
 *  band (POT + raid-buff chips), and the Burst-usage toggle — the same shell
 *  the Timeline page uses. */
const TheorizedTimeline = ({
  result,
  abilityMeta,
  job,
}: {
  result: TheorizeResult;
  abilityMeta: Record<number, AbilityMetaJson>;
  job: string;
}) => {
  const [zoom, setZoom] = useState(1);
  const [filter, setFilter] = useState<FilterState>({ gcd: true, ogcd: true, refs: true });
  const [hover, setHover] = useState<ThHover | null>(null);
  const [burstMode, setBurstMode] = useState<'sim' | 'canonical'>('sim');
  const [showAllRefs, setShowAllRefs] = useState(false);
  // The ideal lane's pin toggle (in its label): pinned parks it under the axis
  // while the reference lanes scroll. Default pinned.
  const [pinIdeal, setPinIdeal] = useState(true);

  const canonical = result.timelineCanonical ?? [];
  const hasCanonical = canonical.length > 0;
  const casts = burstMode === 'canonical' && hasCanonical ? canonical : result.timeline;

  // Reference lanes: rotational casts only, in the backend's ranking order
  // (top 10 by rDPS — the lane rank matches the FFLogs ranking).
  const nonRotNames = useMemo(() => nonRotationalNames(job), [job]);
  const refLanes = useMemo(
    () =>
      (result.refs ?? [])
        .map((r) => ({
          name: (r.label ?? '').replace(/^#\d+\s*/, ''),
          eff: r.efficiencyPct,
          kill: r.killTimeSec,
          track: (r.abilitiesTrack ?? []).filter((c) => isRotational(c, abilityMeta, nonRotNames)),
          pots: r.tinctureWindows ?? [],
        }))
        .filter((r) => r.track.length > 0),
    [result.refs, abilityMeta, nonRotNames],
  );
  const visibleRefs = showAllRefs ? refLanes : refLanes.slice(0, REFS_COLLAPSED);
  const hiddenRefs = refLanes.length - visibleRefs.length;

  // Scale over EVERY ref lane (not just the visible ones) so disclosing the
  // rest never re-scales the strip.
  const laneCasts = useMemo(
    () => [casts, ...refLanes.map((r) => r.track)],
    [casts, refLanes],
  );
  const scale = useTimelineScale(zoom, laneCasts, result.targetKillSec);
  const { xOf, pxPerSec, stripWidth, stripStyle } = scale;
  const bandVisible = (isOgcd: boolean) => (isOgcd ? filter.ogcd : filter.gcd);

  const downtime = result.downtimeWindows;
  const buffs = result.buffWindows;
  const pots = result.tinctureWindows;

  // The target kill time, drawn as an accent axis tick alongside the grid ticks.
  const axisMarks = [
    { sec: result.targetKillSec, label: fmtClock(result.targetKillSec), className: 'target' },
  ];

  // Burst usage — Simulated (throughput-optimal) vs Canonical (standard 2-min
  // burst timing). Same control as the Timeline page; only shown when a comp
  // gives the sim buff windows to hold for.
  const toolbarExtra = hasCanonical ? (
    <div className="row" style={{ gap: 8, alignItems: 'center' }}>
      <span className="mut" style={{ fontSize: 11.5 }}>Burst usage</span>
      <div className="segctrl">
        <button
          className={burstMode === 'sim' ? 'on' : ''}
          onClick={() => setBurstMode('sim')}
          title="The simulator tends to estimate immediate burst usage generates more potency than during buff use — especially in a weak party buff scenario."
        >
          Simulated
        </button>
        <button
          className={burstMode === 'canonical' ? 'on' : ''}
          onClick={() => setBurstMode('canonical')}
          title="Force the simulator to use standard opener burst timing."
        >
          Canonical
        </button>
      </div>
    </div>
  ) : undefined;

  // Flag band: POT chips at each ideal tincture window + raid-buff chips at
  // each modeled window start. The zone fills stay behind the casts.
  const flags = (
    <>
      {pots.map((w, i) => (
        <div
          key={`pot${i}`}
          className="tl-flag pot"
          style={{ left: xOf(w.startSec) }}
          title={`Tincture ×${w.multiplier.toFixed(3)} (${fmtClock(w.startSec)}–${fmtClock(w.endSec)})`}
        >
          pot
        </div>
      ))}
      {buffs.map((w, i) => (
        <div
          key={`bf${i}`}
          className={`tl-flag buff${hover?.kind === 'buff' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec) }}
          onMouseEnter={(e) => setHover({ kind: 'buff', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        >
          <Users size={9} />
          <span>×{w.multiplier.toFixed(2)}</span>
        </div>
      ))}
    </>
  );

  // Raid-buff windows — an accent wash behind the casts so burst visibly
  // aligns into them.
  const backOverlay = buffs.map((w, i) => (
    <div
      key={`buff${i}`}
      className={`tl-buff-zone${hover?.kind === 'buff' && hover.idx === i ? ' on' : ''}`}
      style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
    />
  ));

  const renderCasts = (track: CastEvent[], keyPfx: string) =>
    track.map((c, i) => {
      if (!bandVisible(c.yOffset < 0)) return null;
      const meta = c.abilityId != null ? abilityMeta[c.abilityId] : undefined;
      return (
        <TimelineCast
          key={`${keyPfx}${i}`}
          cast={c}
          meta={meta}
          scale={scale}
          title={`${meta?.name ?? c.tooltip} @ ${c.startSec.toFixed(1)}s`}
        />
      );
    });

  const lanes = (
    <>
      <div className={`tl-row ideal${pinIdeal ? ' pinned' : ''}`}>
        <div className="label">
          <div className="lbl-lines">
            <span className="lbl-name">
              {burstMode === 'canonical' && hasCanonical ? 'Ideal (canonical)' : 'Ideal rotation'}
            </span>
            <span className="lbl-meta">
              <span className="badge">Sim</span>
              <span className="meta-txt">
                {fmtClock(result.targetKillSec)} · {fmtNum(Math.round(result.idealizedPotency))}p
              </span>
            </span>
            {refLanes.length > 0 && (
              <button
                className={`tl-lane-pin${pinIdeal ? ' on' : ''}`}
                aria-pressed={pinIdeal}
                title="Keep this lane parked under the axis while the reference lanes scroll"
                onClick={() => setPinIdeal((v) => !v)}
              >
                <Pin size={9} />
                {pinIdeal ? 'Pinned' : 'Pin'}
              </button>
            )}
          </div>
          <span className="band-lbl ogcd">oGCD</span>
          <span className="band-lbl gcd">GCD</span>
        </div>
        <div className="strip" style={stripStyle}>
          {/* Pinned strips are opaque (they park over scrolling refs), so the
              overlay bands can't show through — re-render the context bands
              inside the strip, exactly like the Timeline page's stripTints.
              Gated on the pin toggle: unpinned, the real bands show through. */}
          {pinIdeal && refLanes.length > 0 && (
            <>
              {scale.prezoneSec > 0 && (
                <div className="tl-strip-tint prezone" style={{ left: 0, width: xOf(0) }} />
              )}
              {buffs.map((w, i) => (
                <div
                  key={`tb${i}`}
                  className="tl-strip-tint buff"
                  style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
                />
              ))}
              {downtime.map((w, i) => (
                <div
                  key={`td${i}`}
                  className={`tl-strip-tint down down-a${hover?.kind === 'down' && hover.idx === i ? ' on' : ''}`}
                  style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
                  onMouseEnter={(e) => setHover({ kind: 'down', idx: i, scrollTop: scrollTopOf(e) })}
                  onMouseLeave={() => setHover(null)}
                />
              ))}
            </>
          )}
          {pots.map((w, i) => (
            <div
              key={`pot${i}`}
              className="tl-pot ideal"
              title={`Tincture ×${w.multiplier.toFixed(3)}`}
              style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
            >
              <span className="lbl">pot</span>
            </div>
          ))}
          {renderCasts(casts, 'c')}
        </div>
      </div>

      {refLanes.length > 0 && filter.refs && (
        <div className="tl-row ref-divider">
          <div className="label">{refLanes.length} references</div>
          <div className="strip">
            <span className="sort-note">top 10 by rDPS ↓</span>
          </div>
        </div>
      )}
      {filter.refs &&
        visibleRefs.map((r, ri) => (
          <div className="tl-row ref" key={`ref${ri}`}>
            <div className="label" title={r.name}>
              <div className="lbl-lines">
                <span className="lbl-name mono">
                  #{ri + 1} {r.eff > 0 ? `${r.eff.toFixed(1)}%` : ''}
                </span>
                <span className="lbl-meta">
                  <span className="badge">Ref</span>
                  <span className="meta-txt">kill {fmtClock(r.kill)}</span>
                </span>
              </div>
              <span className="band-lbl ogcd">oGCD</span>
              <span className="band-lbl gcd">GCD</span>
            </div>
            <div className="strip" style={stripStyle}>
              {r.pots.map((w, i) => (
                <div
                  key={`rp${i}`}
                  className="tl-pot ref"
                  title={`Tincture ×${w.multiplier.toFixed(3)}`}
                  style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
                >
                  <span className="lbl">pot</span>
                </div>
              ))}
              {renderCasts(r.track, `r${ri}`)}
            </div>
          </div>
        ))}
      {filter.refs && refLanes.length > REFS_COLLAPSED && (
        <div className="tl-row refs-footer">
          <div className="label" />
          <div className="strip">
            <span className="refs-footer-inner">
              <button className="refs-more" onClick={() => setShowAllRefs((v) => !v)}>
                {showAllRefs
                  ? 'Show fewer references'
                  : `Show ${hiddenRefs} more reference${hiddenRefs === 1 ? '' : 's'}`}
                <ChevronDown size={11} className={showAllRefs ? 'flip' : undefined} />
              </button>
              <span className="refs-count mut">
                {visibleRefs.length} of {refLanes.length} shown
              </span>
            </span>
          </div>
        </div>
      )}
    </>
  );

  const frontOverlay = (
    <>
      {/* Downtime (boss untargetable) — hoverable bands, like the Timeline's. */}
      {downtime.map((w, i) => (
        <div
          key={`dt${i}`}
          className={`tl-band tier-a${hover?.kind === 'down' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={(e) => setHover({ kind: 'down', idx: i, scrollTop: scrollTopOf(e) })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
    </>
  );

  const bubble = (() => {
    if (!hover) return null;
    // The bubble overlay scrolls with the grid; parking near the top of the
    // VIEW means adding the scroll captured at hover time.
    const bubTop = 30 + hover.scrollTop;
    if (hover.kind === 'down') {
      const w = downtime[hover.idx];
      if (!w) return null;
      return (
        <div className="diff-bubble" style={{ left: clampBubbleLeft(xOf((w.startSec + w.endSec) / 2), stripWidth), top: bubTop }}>
          <div className="bub-head"><div><div className="bub-kind">Boss untargetable</div></div></div>
          <div className="bub-body">
            No enemy targetable from {fmtClock(w.startSec)} to {fmtClock(w.endSec)} ({fmtDuration(w.endSec - w.startSec)}).
            The ideal rotation pauses here.
          </div>
        </div>
      );
    }
    const w = buffs[hover.idx];
    if (!w) return null;
    return (
      <div className="diff-bubble" style={{ left: clampBubbleLeft(xOf((w.startSec + w.endSec) / 2), stripWidth), top: bubTop }}>
        <div className="bub-head"><div><div className="bub-kind">Raid buffs ×{w.multiplier.toFixed(3)}</div></div></div>
        <div className="bub-body">
          {fmtClock(w.startSec)}–{fmtClock(w.endSec)} ({fmtDuration(w.endSec - w.startSec)}). Burst aligned into this
          window is multiplied.
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
      hasRefs={refLanes.length > 0}
      toolbarExtra={toolbarExtra}
      helpText={THEORIZE_HELP}
      axisMarks={axisMarks}
      flags={flags}
      labelWidth={200}
      backOverlay={backOverlay}
      lanes={lanes}
      frontOverlay={frontOverlay}
      bubble={bubble}
      embedded
      pinnedLanes={pinIdeal && refLanes.length > 0}
    />
  );
};

// --- Spread bars ------------------------------------------------------------

/** Ideal output across the sampled kill-time band, as a tiny bar row. */
const SpreadBars = ({ samples }: { samples: TheorizeResult['samples'] }) => {
  if (samples.length < 2) return null;
  const ps = samples.map((s) => s.idealizedPotency);
  const lo = Math.min(...ps);
  const hi = Math.max(...ps);
  const span = hi - lo || 1;
  return (
    <div className="ktt-bars" aria-hidden>
      {samples.map((s, i) => (
        <span
          key={i}
          style={{ height: 6 + ((s.idealizedPotency - lo) / span) * 26 }}
          title={`${fmtClock(s.killSec)}: ${fmtNum(Math.round(s.idealizedPotency))}p`}
        />
      ))}
    </div>
  );
};

// --- The view --------------------------------------------------------------

export const KillTimeTheorizer = ({ defaultJob, defaultEncounterId, defaultKillSec }: Props) => {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [job, setJob] = useState<string>(defaultJob ?? '');
  const [encounterId, setEncounterId] = useState<number>(defaultEncounterId ?? 0);
  const [raw, setRaw] = useState(() =>
    fmtClock(defaultKillSec && defaultKillSec > 0 ? defaultKillSec : DEFAULT_KILL_SEC),
  );
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<TheorizeResult | null>(null);
  // The input signature of the last successful run. When the current inputs drift
  // from it, the result is stale and the Run button reappears.
  const [lastRunKey, setLastRunKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState<{ pct: number; stage: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Reference facts for the chosen (job, encounter), fetched ahead of a run:
  // per-ref kill times (the slider's tick marks) + the top comp (the
  // provenance line), so both render before the first run. Keyed by the combo
  // so a stale fetch for a different selection is ignored.
  const [refInfo, setRefInfo] = useState<{
    key: string;
    ticks: number[];
    comp: string[];
  } | null>(null);

  // Catalog drives the job + encounter pickers (no character needed). Once it
  // lands, snap the job/encounter to valid choices (keeping passed defaults).
  useEffect(() => {
    let alive = true;
    sidecar
      .getCatalog()
      .then((c) => {
        if (!alive) return;
        setCatalog(c);
        setJob((j) =>
          j && c.simBackedJobs.includes(j) ? j : c.simBackedJobs[0] ?? '',
        );
        setEncounterId((e) =>
          e && c.encounters.some((x) => x.id === e) ? e : c.encounters[0]?.id ?? 0,
        );
      })
      .catch(() => setError('Could not load the job / encounter catalog.'));
    return () => {
      alive = false;
    };
  }, []);

  // Warm the (job, encounter) reference set and keep its kill times + top comp
  // for the slider ticks and the comp provenance. Instant when already warmed.
  useEffect(() => {
    if (!job || !encounterId) return;
    const key = `${job}|${encounterId}`;
    let alive = true;
    sidecar
      .prefetchRefs(job, encounterId, 'Top 10')
      .then((r) => {
        if (alive) {
          setRefInfo({ key, ticks: r.refKillTimesSec ?? [], comp: r.refPartyJobs ?? [] });
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [job, encounterId]);

  const providers = useMemo(() => catalog?.buffProviders ?? [], [catalog]);
  const encounters = useMemo(() => catalog?.encounters ?? [], [catalog]);
  const simJobs = useMemo(() => catalog?.simBackedJobs ?? [], [catalog]);
  const roleGroups = useMemo(() => groupJobsByRole(simJobs), [simJobs]);

  // Buff providers grouped by role for the tile strip. JOB_META keys are the
  // spaced display names, so group by the pretty name and keep the raw FFLogs
  // name (what the backend expects) alongside.
  const providerGroups = useMemo(() => {
    const rawByPretty = new Map(providers.map((p) => [prettyJob(p), p]));
    return groupJobsByRole([...rawByPretty.keys()]).map((g) => ({
      role: g.role,
      jobs: g.jobs.map((pretty) => ({ pretty, raw: rawByPretty.get(pretty) ?? pretty })),
    }));
  }, [providers]);

  const target = parseClock(raw);
  const targetValid = target != null && target >= 30 && target <= 1800;
  const canRun = !!job && !!encounterId && targetValid && !loading;

  // Signature of the inputs that define a result. When it differs from the last
  // run (or there's been no run), the displayed result is stale → show the
  // Run button; once a run matches the inputs, the button hides.
  const runKey = `${job}|${encounterId}|${target}|${[...selected].sort().join(',')}`;
  const dirty = lastRunKey === null || lastRunKey !== runKey;

  // Slider position — the parsed target clamped to the slider's band (the text
  // input remains the source of truth and can hold values outside it).
  const sliderVal = Math.min(
    KT_SLIDER_MAX,
    Math.max(KT_SLIDER_MIN, target ?? DEFAULT_KILL_SEC),
  );

  // Reference kill times for the current combo — the slider's tick marks.
  const refTicks =
    refInfo && refInfo.key === `${job}|${encounterId}`
      ? refInfo.ticks.filter((t) => t >= KT_SLIDER_MIN && t <= KT_SLIDER_MAX)
      : [];

  const mergedMeta = useMemo(() => result?.abilityMeta ?? {}, [result]);

  const toggle = (j: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(j)) next.delete(j);
      else next.add(j);
      return next;
    });

  const run = async () => {
    if (!canRun || target == null) return;
    setLoading(true);
    setError(null);
    setProgress({ pct: 0, stage: 'Starting…' });
    try {
      const res = await sidecar.theorizeKillTime(
        job,
        encounterId,
        target,
        RANGE_SEC,
        [...selected],
        (pct, stage) => setProgress({ pct, stage }),
      );
      if (res.unsupported) {
        setError('This job has no rotation simulator yet.');
        setResult(null);
      } else {
        setResult(res);
        setLastRunKey(runKey); // result now matches these inputs → button hides
      }
    } catch (e) {
      setError(`Theorize failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
      setProgress(null);
    }
  };

  const spreadLo = result?.samples.length
    ? Math.min(...result.samples.map((s) => s.idealizedPotency))
    : 0;
  const spreadHi = result?.samples.length
    ? Math.max(...result.samples.map((s) => s.idealizedPotency))
    : 0;

  // The top references' comp (providers only) — offered as a one-click fill.
  // After a run, the theorize response's closest-to-target comp wins; before
  // one, the prefetched top-ref comp fills in.
  const refProviders = useMemo(() => {
    const src =
      result?.refPartyJobs ??
      (refInfo && refInfo.key === `${job}|${encounterId}` ? refInfo.comp : []);
    return src.filter((j) => providers.includes(j));
  }, [result, refInfo, job, encounterId, providers]);

  return (
    <div className="content wide ktt-page">
      <div className="page-title-row">
        <div>
          <h1>Kill time theorizer</h1>
          <p className="page-meta">
            The best possible rotation for a hypothetical kill, built on the fight's real
            downtime from its top reference logs. No character or prior analysis required.
          </p>
        </div>
        <div className="ktt-head-right">
          {dirty && lastRunKey != null && <span className="setup-dirty">Inputs changed</span>}
          {(dirty || loading) && (
            <button className="btn primary" disabled={!canRun} onClick={() => void run()}>
              <Play size={13} />
              {loading ? 'Running…' : 'Run sim'}
            </button>
          )}
        </div>
      </div>

      <div className="setup-panel">
        <div className="setup-row">
          <span className="setup-label">Job</span>
          <div className="setup-controls">
            {roleGroups.length === 0 ? (
              <span className="mut" style={{ fontSize: 12 }}>Loading jobs…</span>
            ) : (
              <div className="job-rail">
                {roleGroups.map((g) => (
                  <div className="job-rail-group" key={g.role}>
                    {g.jobs.map((j) =>
                      j === job ? (
                        <button className="job-pill" key={j} onClick={() => setJob(j)}>
                          <JobTile job={j} size={26} iconInset={2} />
                          <span>{j}</span>
                        </button>
                      ) : (
                        <button className="job-cell" key={j} title={j} onClick={() => setJob(j)}>
                          <JobTile job={j} size={34} />
                        </button>
                      ),
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="setup-row">
          <span className="setup-label">Encounter</span>
          <div className="setup-controls">
            <EncounterPicker
              encounters={encounters}
              encounterId={encounterId}
              onPick={setEncounterId}
            />
          </div>
        </div>

        <div className="setup-row">
          <span className="setup-label">Kill time</span>
          <div className="setup-controls">
            <div className="ktt-killtime">
              <input
                className={`ktt-input ktt-killtime-input${!targetValid && raw ? ' invalid' : ''}`}
                value={raw}
                onChange={(e) => setRaw(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && canRun && dirty) void run();
                }}
                inputMode="numeric"
                spellCheck={false}
              />
              <span className="ktt-slider-end mono">{fmtClock(KT_SLIDER_MIN)}</span>
              <div className="ktt-slider-wrap">
                <input
                  type="range"
                  className="ktt-slider"
                  min={KT_SLIDER_MIN}
                  max={KT_SLIDER_MAX}
                  step={KT_SLIDER_STEP}
                  value={sliderVal}
                  onChange={(e) => setRaw(fmtClock(Number(e.target.value)))}
                  aria-label="Kill time"
                />
                <div className="ktt-slider-ticks">
                  {refTicks.map((t, i) => (
                    <span
                      key={i}
                      title={fmtClock(t)}
                      style={{
                        left: `${((t - KT_SLIDER_MIN) / (KT_SLIDER_MAX - KT_SLIDER_MIN)) * 100}%`,
                      }}
                    />
                  ))}
                </div>
              </div>
              <span className="ktt-slider-end mono">{fmtClock(KT_SLIDER_MAX)}</span>
            </div>
            <span className="setup-hint">
              {refTicks.length > 0
                ? `ticks are the ${refTicks.length} reference kills · evaluates ±${RANGE_SEC / 2}s`
                : `m:ss · drag or type · evaluates ±${RANGE_SEC / 2}s`}
            </span>
          </div>
        </div>

        <div className="setup-row">
          <span className="setup-label">Party buffs</span>
          <div className="setup-controls">
            {providerGroups.length === 0 ? (
              <span className="mut" style={{ fontSize: 12 }}>Loading providers…</span>
            ) : (
              <div className="job-rail buffs">
                {providerGroups.map((g) => (
                  <div className="job-rail-group" key={g.role}>
                    {g.jobs.map(({ pretty, raw: rawName }) => (
                      <button
                        key={rawName}
                        className={`job-cell buff${selected.has(rawName) ? ' on' : ''}`}
                        title={pretty}
                        onClick={() => toggle(rawName)}
                        type="button"
                      >
                        <JobTile job={pretty} size={32} />
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            )}
            {refProviders.length > 0 && (
              <>
                <span className="enc-tabs-rule" />
                <span className="ktt-refcomp mut">
                  Top references ran{' '}
                  <strong>{refProviders.map(prettyJob).join(' · ')}</strong>{' '}
                  <button
                    className="ktt-linkbtn"
                    type="button"
                    onClick={() => setSelected(new Set(refProviders))}
                  >
                    use this comp
                  </button>
                </span>
              </>
            )}
          </div>
        </div>
      </div>

      {loading && progress && (
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

      {result && !result.unsupported && (
        <>
          <div className="ktt-strip">
            <div className="ktt-big">
              {fmtNum(Math.round(result.idealizedPotency))}
              <span className="ktt-unit">p</span>
            </div>
            <div className="ktt-strip-caption">
              <div className="ktt-sub">
                ideal output @ {fmtClock(result.targetKillSec)}
                {selected.size > 0 ? ` · ${selected.size}-buff comp` : ' · no raid buffs'}
              </div>
              <div className="ktt-note mut">
                {result.downtimeSource === 'references'
                  ? `Downtime derived from ${result.refCount} top ${job} log${result.refCount === 1 ? '' : 's'} · closest reference kill ${fmtClock(result.refKillTimeSec)}`
                  : 'No reference downtime for this encounter · modeling pure uptime'}
              </div>
            </div>
            <div className="ktt-spread">
              <SpreadBars samples={result.samples} />
              <span className="ktt-spread-lbl mut">
                {fmtNum(Math.round(spreadLo))}–{fmtNum(Math.round(spreadHi))}p across ±{RANGE_SEC / 2}s
              </span>
            </div>
            <div className="ktt-pills">
              {result.downtimeSource === 'references' && (
                <span className="ktt-pill">{result.refCount} references</span>
              )}
              <span className="ktt-pill">
                {result.downtimeWindows.length} downtime window{result.downtimeWindows.length === 1 ? '' : 's'}
              </span>
            </div>
          </div>

          <div className="ktt-rotation-head">
            <span className="ktt-rotation-title">Rotation</span>
            <span className="ktt-rotation-sub mut">
              the ideal line against every reference kill · same timeline as Analysis
            </span>
          </div>
          <div className="ktt-rotation">
            <TheorizedTimeline result={result} abilityMeta={mergedMeta} job={job} />
          </div>
        </>
      )}
    </div>
  );
};
