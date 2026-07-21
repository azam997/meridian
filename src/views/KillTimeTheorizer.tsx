import { useEffect, useMemo, useState } from 'react';
import { Clock, FlaskConical, Play, Swords, Target, Users } from 'lucide-react';
import { jobColor, jobIcon, isJobPending, PENDING_JOB_TIP } from '../components/jobs';
import { TimelineShell, type FilterState } from '../components/timeline/TimelineShell';
import { TimelineCast } from '../components/timeline/TimelineCast';
import { clampBubbleLeft, useTimelineScale } from '../components/timeline/scale';
import { fmtClock, fmtDuration, fmtNum } from '../format';
import { sidecar } from '../sidecar';
import type {
  AbilityMetaJson,
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

/** Parse "mm:ss" or a plain seconds count into seconds; null when unparseable. */
function parseClock(raw: string): number | null {
  const s = raw.trim();
  if (!s) return null;
  const m = s.match(/^(\d+):([0-5]?\d)$/);
  if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  if (/^\d+(\.\d+)?$/.test(s)) return Math.round(parseFloat(s));
  return null;
}

/** FFLogs job names are spaceless ("RedMage", "BlackMage"); space them for the
 *  comp chips so they read naturally. */
const prettyJob = (j: string): string => j.replace(/([a-z])([A-Z])/g, '$1 $2');

// --- Ideal-rotation timeline ------------------------------------------------

const THEORIZE_HELP =
  'Hover casts, downtime bands, and raid-buff windows for details.\n' +
  'oGCDs ride the upper band, GCDs the lower.\n' +
  'Click empty track to pin a time; click again to clear.\n' +
  'Gridlines mark the axis ticks.';

/** Which window the pointer is over (drives the info bubble). */
type ThHover = { kind: 'down' | 'buff'; idx: number };

/** The theorized ideal rotation, rendered with the full Timeline-page chrome
 *  (zoom / filter / crosshair / pin / hover bubbles) via the shared
 *  `TimelineShell` — a complete replica of the Timeline view, with a single
 *  "Ideal rotation" lane plus this view's raid-buff windows behind the casts. */
const TheorizedTimeline = ({
  result,
  abilityMeta,
}: {
  result: TheorizeResult;
  abilityMeta: Record<number, AbilityMetaJson>;
}) => {
  const [zoom, setZoom] = useState(1);
  const [filter, setFilter] = useState<FilterState>({ gcd: true, ogcd: true, refs: true });
  const [hover, setHover] = useState<ThHover | null>(null);

  const casts = result.timeline;
  // Single lane; extend the strip to at least the target kill time so its axis
  // marker is always reachable even past the last cast.
  const laneCasts = useMemo(() => [casts], [casts]);
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

  // Raid-buff windows — an accent wash behind the casts (like the Timeline's
  // multi-target zones) so burst visibly aligns into them.
  const backOverlay = buffs.map((w, i) => (
    <div
      key={`buff${i}`}
      className={`tl-buff-zone${hover?.kind === 'buff' && hover.idx === i ? ' on' : ''}`}
      style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
    />
  ));

  const lanes = (
    <div className="tl-row ideal">
      <div className="label">
        <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          Ideal rotation
        </span>
        <span className="badge">Sim</span>
      </div>
      <div className="strip" style={stripStyle}>
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
        {casts.map((c, i) => {
          if (!bandVisible(c.yOffset < 0)) return null;
          const meta = c.abilityId != null ? abilityMeta[c.abilityId] : undefined;
          return (
            <TimelineCast
              key={i}
              cast={c}
              meta={meta}
              scale={scale}
              title={`${meta?.name ?? c.tooltip} @ ${c.startSec.toFixed(1)}s`}
            />
          );
        })}
      </div>
    </div>
  );

  const frontOverlay = (
    <>
      {/* Downtime (boss untargetable) — hoverable bands, like the Timeline's. */}
      {downtime.map((w, i) => (
        <div
          key={`dt${i}`}
          className={`tl-band tier-a${hover?.kind === 'down' && hover.idx === i ? ' on' : ''}`}
          style={{ left: xOf(w.startSec), width: (w.endSec - w.startSec) * pxPerSec }}
          onMouseEnter={() => setHover({ kind: 'down', idx: i })}
          onMouseLeave={() => setHover(null)}
        />
      ))}
      {/* Raid-buff flags — a small hoverable chip at each window's start (the
          zone fill is the back layer), mirroring the Timeline's multi-target flags. */}
      {buffs.map((w, i) => (
        <div key={`bf${i}`} className="tl-mt-mark" style={{ left: xOf(w.startSec) }}>
          <div
            className={`tl-buff-flag${hover?.kind === 'buff' && hover.idx === i ? ' on' : ''}`}
            onMouseEnter={() => setHover({ kind: 'buff', idx: i })}
            onMouseLeave={() => setHover(null)}
          >
            <Users size={10} />
            <span>×{w.multiplier.toFixed(2)}</span>
          </div>
        </div>
      ))}
    </>
  );

  const bubble = (() => {
    if (!hover) return null;
    if (hover.kind === 'down') {
      const w = downtime[hover.idx];
      if (!w) return null;
      return (
        <div className="diff-bubble" style={{ left: clampBubbleLeft(xOf((w.startSec + w.endSec) / 2), stripWidth), top: 30 }}>
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
      <div className="diff-bubble" style={{ left: clampBubbleLeft(xOf((w.startSec + w.endSec) / 2), stripWidth), top: 30 }}>
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
      hasRefs={false}
      helpText={THEORIZE_HELP}
      axisMarks={axisMarks}
      backOverlay={backOverlay}
      lanes={lanes}
      frontOverlay={frontOverlay}
      bubble={bubble}
      embedded
    />
  );
};

// --- Spread sparkline ------------------------------------------------------

/** A tiny ideal-potency-vs-killtime curve across the sampled band. */
const SpreadSparkline = ({ samples }: { samples: TheorizeResult['samples'] }) => {
  if (samples.length < 2) return null;
  const W = 132;
  const H = 34;
  const pad = 3;
  const ps = samples.map((s) => s.idealizedPotency);
  const lo = Math.min(...ps);
  const hi = Math.max(...ps);
  const span = hi - lo || 1;
  const x = (i: number) => pad + (i / (samples.length - 1)) * (W - 2 * pad);
  const y = (p: number) => H - pad - ((p - lo) / span) * (H - 2 * pad);
  const pts = samples.map((s, i) => `${x(i).toFixed(1)},${y(s.idealizedPotency).toFixed(1)}`).join(' ');
  return (
    <svg className="ktt-spark" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
      {samples.map((s, i) => (
        <circle key={i} cx={x(i)} cy={y(s.idealizedPotency)} r={1.6} fill="var(--accent)" />
      ))}
    </svg>
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
  // from it, the result is stale and the Re-run button reappears.
  const [lastRunKey, setLastRunKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState<{ pct: number; stage: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Top-10 average kill time for the chosen (job, encounter), fetched ahead of a
  // run so it can anchor the kill-time control as a tooltip. Keyed by the combo
  // so it's only used when it matches the current selection (no reset needed).
  const [refAvg, setRefAvg] = useState<{ key: string; sec: number } | null>(null);

  // Catalog drives the job + encounter pickers (no character needed). Once it
  // lands, snap the job/encounter to valid choices (keeping passed defaults).
  useEffect(() => {
    let alive = true;
    sidecar
      .getCatalog()
      .then((c) => {
        if (!alive) return;
        setCatalog(c);
        // Skip parked jobs (the healers) both when validating a passed default
        // and when falling back to the first available job.
        setJob((j) =>
          j && c.simBackedJobs.includes(j) && !isJobPending(j)
            ? j
            : c.simBackedJobs.find((x) => !isJobPending(x)) ?? '',
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

  // Pull the chosen (job, encounter)'s top-10 average kill time for the kill-time
  // tooltip. Reuses the warm reference cache (instant when warmed on launch), so
  // it's available before the user runs. Tagged with the combo key so a stale
  // result for a different selection is ignored downstream.
  useEffect(() => {
    if (!job || !encounterId) return;
    const key = `${job}|${encounterId}`;
    let alive = true;
    sidecar
      .prefetchRefs(job, encounterId, 'Top 10')
      .then((r) => {
        if (alive) setRefAvg({ key, sec: r.avgKillSec || 0 });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [job, encounterId]);

  const providers = useMemo(() => catalog?.buffProviders ?? [], [catalog]);
  const encounters = useMemo(() => catalog?.encounters ?? [], [catalog]);
  const simJobs = useMemo(() => catalog?.simBackedJobs ?? [], [catalog]);

  const target = parseClock(raw);
  const targetValid = target != null && target >= 30 && target <= 1800;
  const canRun = !!job && !!encounterId && targetValid && !loading;

  // Signature of the inputs that define a result. When it differs from the last
  // run (or there's been no run), the displayed result is stale → show the
  // Start/Re-run button; once a run matches the inputs, the button hides.
  const runKey = `${job}|${encounterId}|${target}|${[...selected].sort().join(',')}`;
  const dirty = lastRunKey === null || lastRunKey !== runKey;

  // Slider position — the parsed target clamped to the slider's band (the text
  // input remains the source of truth and can hold values outside it).
  const sliderVal = Math.min(
    KT_SLIDER_MAX,
    Math.max(KT_SLIDER_MIN, target ?? DEFAULT_KILL_SEC),
  );

  // Top-10 average kill time to anchor the kill-time control — only when the
  // proactive fetch matches the current (job, encounter). Surfaced as a tooltip.
  const refAvgSec = refAvg && refAvg.key === `${job}|${encounterId}` ? refAvg.sec : 0;
  const avgTip =
    refAvgSec > 0 ? `Top 10 reference average kill time: ${fmtClock(refAvgSec)}` : undefined;

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
  const refProviders = useMemo(
    () => (result?.refPartyJobs ?? []).filter((j) => providers.includes(j)),
    [result, providers],
  );

  return (
    <div className="content">
      <div className="card ktt-card">
        <div className="card-head">
          <FlaskConical size={14} />
          <h2>Kill time theorizer</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            Ideal rotation for a hypothetical kill time
          </span>
        </div>
        <div className="card-body">
          <p className="mut" style={{ fontSize: 12.5, margin: '0 0 14px' }}>
            Pick a job, encounter, kill time, and party buffs, then run — the sim
            builds the best possible rotation and output for that kill, using the
            fight’s real downtime (derived from this encounter’s top reference
            logs). No character or prior analysis required.
          </p>

          <div className="ktt-form">
            {/* Job — a responsive grid that expands (wraps to more rows) as more
                jobs gain simulator support. */}
            <div className="ktt-field-block">
              <span className="field-label">
                <Swords size={12} /> Job
              </span>
              <div className="job-grid">
                {simJobs.length === 0 ? (
                  <span className="mut" style={{ fontSize: 12 }}>Loading jobs…</span>
                ) : (
                  simJobs.map((j) => {
                    const icon = jobIcon(j);
                    const pending = isJobPending(j);
                    return (
                      <button
                        key={j}
                        className={'btn job-tile ' + (job === j ? 'primary ' : '') + (pending ? 'pending' : '')}
                        disabled={pending}
                        title={pending ? PENDING_JOB_TIP : undefined}
                        onClick={() => setJob(j)}
                      >
                        {icon ? (
                          <img src={icon} alt="" width={22} height={22} draggable={false} className="job-tile-icon" />
                        ) : (
                          <span className="job-tile-icon" style={{ background: jobColor(j) }} />
                        )}
                        <span className="job-tile-label">{j}</span>
                      </button>
                    );
                  })
                )}
              </div>
            </div>

            {/* Encounter — vertical stack (label over a full-width select). */}
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

            {/* Kill time — the centerpiece: a large input paired with a slider. */}
            <div className="ktt-field-block">
              <span className="field-label" title={avgTip}>
                <Clock size={12} /> Kill time
              </span>
              <div className="ktt-killtime" title={avgTip}>
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
              </div>
              <span className="ktt-hint">
                m:ss · drag or type · {fmtClock(KT_SLIDER_MIN)}–{fmtClock(KT_SLIDER_MAX)} · evaluates ±{RANGE_SEC / 2}s
              </span>
            </div>

            {/* Party buffs. */}
            <div className="ktt-field-block">
              <span className="field-label">
                <Users size={12} /> Party buffs
              </span>
              <div className="ktt-chips">
                {providers.length === 0 ? (
                  <span className="mut" style={{ fontSize: 12 }}>Loading providers…</span>
                ) : (
                  providers.map((p) => (
                    <button
                      key={p}
                      className={`ktt-chip${selected.has(p) ? ' on' : ''}`}
                      onClick={() => toggle(p)}
                      type="button"
                    >
                      {prettyJob(p)}
                    </button>
                  ))
                )}
              </div>
              {refProviders.length > 0 && (
                <div className="ktt-refcomp mut">
                  Top references ran {refProviders.map(prettyJob).join(' · ')}
                  {' '}
                  <button
                    className="ktt-linkbtn"
                    type="button"
                    onClick={() => setSelected(new Set(refProviders))}
                  >
                    use this comp
                  </button>
                </div>
              )}
            </div>

            {/* Run / Re-run — below all the parameters. */}
            {(dirty || loading) && (
              <div className="ktt-run-row">
                <button className="btn primary ktt-run" disabled={!canRun} onClick={() => void run()}>
                  <Play size={13} />
                  {loading ? 'Running…' : lastRunKey ? 'Re-run analysis' : 'Start analysis'}
                </button>
              </div>
            )}
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
            <div className="ktt-result">
              <div className="ktt-summary">
                <div className="ktt-headline">
                  <div className="ktt-big">
                    {fmtNum(Math.round(result.idealizedPotency))}
                    <span className="ktt-unit">p</span>
                  </div>
                  <div className="ktt-sub">
                    ideal output @ {fmtClock(result.targetKillSec)}
                    {selected.size > 0 ? ` · ${selected.size}-buff comp` : ' · no raid buffs'}
                  </div>
                  <div className="ktt-note mut">
                    {result.downtimeSource === 'references'
                      ? `Downtime from ${result.refCount} top ${job} log${result.refCount === 1 ? '' : 's'} (closest kill ${fmtClock(result.refKillTimeSec)}).`
                      : 'No reference downtime for this encounter — modeling pure uptime.'}
                  </div>
                </div>
                <div className="ktt-spread">
                  <SpreadSparkline samples={result.samples} />
                  <div className="ktt-spread-lbl mut">
                    {fmtNum(Math.round(spreadLo))}–{fmtNum(Math.round(spreadHi))}p across ±{RANGE_SEC / 2}s
                  </div>
                </div>
              </div>
              <TheorizedTimeline result={result} abilityMeta={mergedMeta} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
