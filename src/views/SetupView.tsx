import { useEffect, useState } from 'react';
import { Check, ChevronRight, HeartPulse, Sparkles, Sword, Target, Trophy, User } from 'lucide-react';
import {
  ANALYZABLE_HEALERS, JOBS, jobColor, jobIcon, isHealer, isJobPending,
  PENDING_JOB_TIP,
} from '../components/jobs';
import { CharacterSelect, type CharacterPicked } from '../components/CharacterSelect';
import type { AppState, Encounter, Pull, RefsBucket } from '../state/appState';
import type { EncounterCategory, ProgPull } from '../sidecar/contract';
import { listSetupCached } from '../sidecar/sessionCache';
import { sidecar } from '../sidecar';
import { refsWarmer } from '../state/refsPrefetch';

// References are always the encounter's top-10 logs. The picker was removed
// once "Top 10" was its only option; kept as a constant so the run snapshot
// still carries the bucket the sidecar expects.
const REFS_BUCKET: RefsBucket = 'Top 10';

// An encounter label is `${name} (${kills} kills)`. Match on the name up to the
// " (" boundary so a name that is a prefix of another (e.g. "Lindwurm" vs
// "Lindwurm II") can't swallow the longer one. Tolerant of kill-count drift in
// a saved label since it ignores the count.
const encMatches = (label: string, e: Encounter): boolean =>
  label.startsWith(e.name + ' (');

type Props = {
  /** Which encounter tab this instance is: 'savage' shows the Savage tier,
   *  'ultimate' shows ultimates (prog-heavy → defaults to the wipe source).
   *  App mounts one SetupView per tab, keyed by category. */
  category: EncounterCategory;
  state: AppState;
  setState: (next: AppState) => void;
  /** Accepts an explicit snapshot so the parent doesn't have to wait for
   *  React's setState to propagate before reading the new selection. */
  onRunAnalysis: (snapshot: Partial<AppState>) => void;
  /** Healer flow: route to the Healing/Mitigation planner with the selection
   *  (encounter + pull) so the plan opens preselected — the pull's comp is
   *  resolved backend-side. The analysis (White Mage) then runs from the
   *  planner's "Analyze my pull". */
  onPlanMitigation: (snapshot: Partial<AppState>) => void;
  /** Surfaced from App.tsx when run_analysis fails — shown in the same
   *  error banner as lookup/encounter/pull failures. */
  externalError?: string | null;
  clearExternalError?: () => void;
  /** Landing here without a character shows the inline character select as
   *  step 0 — this hands the pick up to App (same handler as the modal). */
  onCharacterPicked: (c: CharacterPicked) => void;
  /** Fired when the user confirms a job — lets the reference warm-cache jump
   *  that job to the front of the queue (and block with a popup if needed). */
  onJobConfirmed?: (job: string) => void;
  /** Open Submit Feedback prefilled with a failed analysis' error message
   *  (only offered for externalError — an actual crash, not a local
   *  validation miss). */
  onReportError?: (msg: string) => void;
};

export const SetupView = ({
  category,
  state,
  setState,
  onRunAnalysis,
  onPlanMitigation,
  externalError,
  clearExternalError,
  onCharacterPicked,
  onJobConfirmed,
  onReportError,
}: Props) => {
  // state.job is '' on a fresh launch (job selection is per-session — App
  // hydrates it empty so a stale job never auto-looks-up against a new
  // character/tier). It's set mid-session by a run, so returning to this
  // page lands straight on the pull cards for the job just analyzed. Every
  // job is pickable — healers route to the mitigation planner.
  const [job, setJob] = useState(state.job);
  // A job is only "locked in" after the user confirms it — that collapses the
  // job picker and reveals the pull cards.
  const [jobConfirmed, setJobConfirmed] = useState(!!state.job);
  const [encounter, setEncounter] = useState(state.encounter);
  const [pullLabel, setPullLabel] = useState(state.pullId);

  const [encounters, setEncounters] = useState<Encounter[]>([]);
  // Pulls for every encounter the character has logged this tier, keyed by
  // encounter id. We fetch them all up front so "Recent pulls" can span every
  // encounter (not just the selected one) and a click can hot-load instantly.
  const [pullsByEnc, setPullsByEnc] = useState<Record<number, Pull[]>>({});
  const [loadingPulls, setLoadingPulls] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Prog (wipe) pulls — the "In progress" pull source. Deliberately LAZY:
  // nothing is fetched until the user flips the toggle (wipes come from
  // report summaries, not rankings — a separate discovery path), and only
  // the one wipe the user runs is ever analyzed. Keyed by encounter id;
  // `progLoadedEnc` marks encounters already scanned this session.
  // Ultimates are prog-heavy — default that tab to the wipe source so a
  // returning progger lands on their in-progress pulls; Savage keeps kills.
  const [pullSource, setPullSource] = useState<'kills' | 'wipes'>(
    category === 'ultimate' ? 'wipes' : 'kills',
  );
  const [progPullsByEnc, setProgPullsByEnc] = useState<Record<number, ProgPull[]>>({});
  const [progLoadedEnc, setProgLoadedEnc] = useState<Record<number, boolean>>({});
  const [progLoading, setProgLoading] = useState(false);
  const [progError, setProgError] = useState<string | null>(null);
  // Selected wipe, as `${reportCode}:${fightId}` (labels aren't unique enough).
  const [progKey, setProgKey] = useState('');
  const [pasteText, setPasteText] = useState('');
  const [pasteLoading, setPasteLoading] = useState(false);

  // On Savage, healers route straight through the planner and are v1-gated out
  // of prog analysis, so their source derives back to kills (toggle hidden). On
  // the prog-heavy Ultimates tab that gate is dropped: a progging healer needs
  // to pick a wipe (it routes to the mit planner, which handles any pull).
  const activeSource: 'kills' | 'wipes' =
    isHealer(job) && category !== 'ultimate' ? 'kills' : pullSource;

  const characterLoaded = !!state.lodestoneId;
  const step1Done = characterLoaded && !!job && jobConfirmed;
  const step2Done = step1Done && !!encounter
    && (activeSource === 'kills' ? !!pullLabel : !!progKey);

  // When job + character are ready, load the WHOLE setup screen in ONE round
  // trip — the tier's encounters AND every encounter's pulls (encounters.py
  // owns the tier zone/ids backend-side). Drives the encounter dropdown, the
  // "Your pull" select, and the cross-encounter "Recent pulls" list; the pull
  // dropdown + Run become usable the moment this single request returns.
  useEffect(() => {
    if (!step1Done || !state.lodestoneId) return;
    const lid = state.lodestoneId;
    let cancelled = false;
    // Reset stale pulls from the previously-selected job while we reload.
    // Prog lists are job-filtered too, so they reset with the job.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPullsByEnc({});
    setProgPullsByEnc({});
    setProgLoadedEnc({});
    setProgKey('');
    setLoadingPulls(true);
    listSetupCached(lid, job)
      .then((data) => {
        if (cancelled) return;
        // Scope to this tab's category. Absent tag (legacy/mock payload) ⇒
        // 'savage', so an un-tagged encounter still shows on the Savage tab.
        const scoped = data.encounters.filter(
          (e) => (e.category ?? 'savage') === category,
        );
        setEncounters(scoped);
        const map: Record<number, Pull[]> = {};
        for (const e of scoped) {
          map[e.id] = data.pullsByEncounterId[String(e.id)] ?? [];
        }
        setPullsByEnc(map);
        setLoadingPulls(false);
        if (scoped.length === 0) {
          // Ultimates always list a synthesized zero-kill row, so an empty
          // scoped list only happens on Savage (no logs this tier).
          if (category === 'savage') {
            setError(`No ${job} logs found for this character on the current tier.`);
          }
          return;
        }
        setError(null); // clear any stale error now that the lookup succeeded
        // Keep the saved encounter if still present, else the first; seed its
        // first pull if the saved pull doesn't belong to it.
        const current = scoped.find((e) => encMatches(encounter, e)) ?? scoped[0];
        if (current) {
          setEncounter(`${current.name} (${current.totalKills} kills)`);
          const selPulls = map[current.id] ?? [];
          if (selPulls[0] && !selPulls.find((p) => p.label === pullLabel)) {
            setPullLabel(selPulls[0].label);
          }
        }
      })
      .catch((e) => {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        console.error('[listSetup]', e);
        setError(`Setup lookup failed: ${msg}`);
        setLoadingPulls(false);
      });
    return () => {
      cancelled = true;
    };
  }, [step1Done, state.lodestoneId, job]); // eslint-disable-line react-hooks/exhaustive-deps

  // Pulls for the currently-selected encounter (drives the "Your pull" select).
  const selectedEnc = encounters.find((e) => encMatches(encounter, e));
  const pulls = (selectedEnc && pullsByEnc[selectedEnc.id]) || [];
  const progPulls = (selectedEnc && progPullsByEnc[selectedEnc.id]) || [];

  // Warm the SELECTED encounter's references the moment it's chosen, so the run
  // hits the cache instead of re-downloading them mid-analysis. The setup-screen
  // warm popup fires at job-confirm — before an encounter is picked — so it can
  // only target the previously-saved encounter, which on the Ultimates tab is
  // usually a leftover Savage fight. Without this, the real pull's refs were cold
  // at run time: the analysis re-showed "downloading reference logs" and the
  // progress bar reset. Non-blocking (no popup): it just jumps this encounter to
  // the front of the background warm queue. Plan-only healers never analyze, so
  // they're skipped (matches the speculative pre-analysis guard below).
  useEffect(() => {
    if (!step1Done || !selectedEnc) return;
    if (isHealer(job) && !ANALYZABLE_HEALERS.has(job)) return;
    void refsWarmer.ensureJob(job, selectedEnc.id, false);
  }, [step1Done, job, selectedEnc?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Lazy prog-pull discovery: first visit to "In progress" on an encounter
  // scans the character's recent reports for its wipes (one round trip; the
  // summaries ride the backend's batched/cached path). Re-visits are instant.
  useEffect(() => {
    if (activeSource !== 'wipes' || !selectedEnc || !state.lodestoneId) return;
    if (progLoadedEnc[selectedEnc.id]) return;
    const encId = selectedEnc.id;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProgLoading(true);
    setProgError(null);
    sidecar
      .listProgPulls({ lodestoneId: state.lodestoneId, encounterId: encId, spec: job })
      .then((res) => {
        if (cancelled) return;
        setProgPullsByEnc((m) => ({ ...m, [encId]: res.pulls }));
        setProgLoadedEnc((m) => ({ ...m, [encId]: true }));
        setProgLoading(false);
        const first = res.pulls[0];
        if (first) setProgKey(`${first.reportCode}:${first.fightId}`);
      })
      .catch((e) => {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        console.error('[listProgPulls]', e);
        setProgError(`Wipe lookup failed: ${msg}`);
        setProgLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSource, selectedEnc?.id, job, state.lodestoneId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Paste fallback: pull the report code out of an FFLogs URL (or a bare
  // 16-char code) and merge that report's wipes into the current encounter's
  // list — for prog sessions outside the recent-reports window or uploaded
  // by someone else.
  const addPastedReport = () => {
    if (!selectedEnc) return;
    const m = pasteText.match(/reports\/(?:a:)?([A-Za-z0-9]{16})/)
      ?? pasteText.trim().match(/^([A-Za-z0-9]{16})$/);
    if (!m) {
      setProgError('Could not read an FFLogs report code from that link.');
      return;
    }
    const code = m[1];
    const encId = selectedEnc.id;
    setPasteLoading(true);
    setProgError(null);
    sidecar
      .listProgPulls({ reportCode: code, encounterId: encId, spec: job })
      .then((res) => {
        setPasteLoading(false);
        if (res.pulls.length === 0) {
          setProgError(`No ${job} wipes on this encounter in that report.`);
          return;
        }
        setPasteText('');
        setProgPullsByEnc((m0) => {
          const cur = m0[encId] ?? [];
          const seen = new Set(cur.map((p) => `${p.reportCode}:${p.fightId}`));
          const merged = [
            ...cur,
            ...res.pulls.filter((p) => !seen.has(`${p.reportCode}:${p.fightId}`)),
          ];
          merged.sort((a, b) => b.startTimeMs - a.startTimeMs);
          return { ...m0, [encId]: merged };
        });
        setProgKey(`${res.pulls[0].reportCode}:${res.pulls[0].fightId}`);
      })
      .catch((e) => {
        setPasteLoading(false);
        const msg = e instanceof Error ? e.message : String(e);
        setProgError(`Report lookup failed: ${msg}`);
      });
  };

  // Speculative pre-analysis: once a concrete pull is selected AND its reference
  // set is already warm (so the run is cheap), kick the analysis off in the
  // background — debounced against selection churn — to populate the sidecar's
  // result cache so clicking "Run analysis" returns near-instantly (the backend
  // collapses the two identical builds via _result_inflight). Best-effort:
  // result + errors ignored. Skipped when refs aren't warm yet — then the warm
  // itself is the bottleneck, which the explicit Run shows progress for.
  useEffect(() => {
    if (!step2Done || !selectedEnc) return;
    // Plan-only healers never analyze; an analyzable healer's speculative run
    // (no comp override — same cache key as an unadjusted "Analyze my pull")
    // also pre-warms the mit-plan damage model the planner is about to want.
    if (isHealer(job) && !ANALYZABLE_HEALERS.has(job)) return;
    // Look the selection up in the ACTIVE source list (kills or wipes) — a
    // selected wipe pre-warms the same way (still one wipe at a time; the
    // explicit Run collapses onto this build via _result_inflight).
    const pull = activeSource === 'kills'
      ? pulls.find((p) => p.label === pullLabel)
      : progPulls.find((p) => `${p.reportCode}:${p.fightId}` === progKey);
    if (!pull || !refsWarmer.isReady(job, selectedEnc.id)) return;
    const t = setTimeout(() => {
      void sidecar
        .runAnalysis(pull.reportCode, pull.fightId, job, selectedEnc.id, REFS_BUCKET)
        .catch(() => {});
    }, 500);
    return () => clearTimeout(t);
    // selectedEnc?.id (not the object) keeps this from re-arming every render.
  }, [step2Done, job, pullLabel, activeSource, progKey, selectedEnc?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Newest pulls across every encounter, tagged with where they came from so
  // a click can switch both the encounter and the pull.
  type RecentPull = Pull & { encounterId: number; encounterName: string; encounterLabel: string };
  const recentPulls: RecentPull[] = [];
  for (const e of encounters) {
    for (const p of pullsByEnc[e.id] || []) {
      recentPulls.push({
        ...p,
        encounterId: e.id,
        encounterName: e.name,
        encounterLabel: `${e.name} (${e.totalKills} kills)`,
      });
    }
  }
  recentPulls.sort((a, b) => b.startTimeMs - a.startTimeMs);
  const recent = recentPulls.slice(0, 6);

  // Clicking a recent pull hot-loads it into the "Your pull" selection,
  // switching the encounter too if it differs.
  const pickRecent = (rp: RecentPull) => {
    setEncounter(rp.encounterLabel);
    setPullLabel(rp.label);
  };

  const startAnalysis = () => {
    const enc = encounters.find((x) => encMatches(encounter, x));
    const pull = activeSource === 'kills'
      ? pulls.find((p) => p.label === pullLabel)
      : progPulls.find((p) => `${p.reportCode}:${p.fightId}` === progKey);
    if (!enc || !pull) {
      setError('Pick an encounter and pull before running analysis.');
      return;
    }
    const snapshot: Partial<AppState> = {
      job,
      encounter,
      encounterId: enc.id,
      pullId: pull.label,
      pullReportCode: pull.reportCode,
      pullFightId: pull.fightId,
      refsBucket: REFS_BUCKET,
      // Explicitly clear any Research-loaded subject: this run analyzes the
      // loaded character's own pull, and a stale name would split the result-
      // cache key from the (unnamed) speculative pre-analysis above.
      playerName: undefined,
      pullsLoaded: pulls.length > 0,
    };
    setState({ ...state, ...snapshot });
    // Healers go through the Healing/Mitigation planner: the plan opens with
    // this pull's encounter + comp preselected, and (for analyzable healers)
    // "Analyze my pull" there runs the locked-GCD analysis.
    if (isHealer(job)) {
      onPlanMitigation(snapshot);
      return;
    }
    onRunAnalysis(snapshot);
  };

  // Healer shortcut without a pull: open the planner with just the job (and
  // the selected encounter if any) — the planner then uses its own defaults.
  const openPlannerWithoutPull = () => {
    const enc = encounters.find((x) => encMatches(encounter, x));
    const snapshot: Partial<AppState> = {
      job,
      encounter,
      encounterId: enc?.id ?? state.encounterId,
      pullReportCode: '',
      pullFightId: 0,
    };
    setState({ ...state, ...snapshot });
    onPlanMitigation(snapshot);
  };

  return (
    <div className="content narrow">
      <div className="hero compact">
        <h1>
          {!characterLoaded
            ? 'Pick your character'
            : jobConfirmed
              ? 'Pick an encounter'
              : 'Pick a job'}
        </h1>
        <div className="steps">
          <div className={`step-pill ${characterLoaded ? 'done' : 'active'}`}>
            <span className="n">{characterLoaded ? <Check size={11} /> : 1}</span>
            Character
          </div>
          <ChevronRight size={12} className="mut-2" />
          <div className={`step-pill ${step1Done ? 'done' : characterLoaded ? 'active' : ''}`}>
            <span className="n">{step1Done ? <Check size={11} /> : 2}</span>
            Job
          </div>
          <ChevronRight size={12} className="mut-2" />
          <div className={`step-pill ${step2Done ? 'done' : step1Done ? 'active' : ''}`}>
            <span className="n">{step2Done ? <Check size={11} /> : 3}</span>
            Pull
          </div>
        </div>
      </div>

      {(error || externalError) && (
        <div
          className="card"
          style={{
            marginBottom: 14,
            borderColor: 'var(--bad)',
            background: 'var(--bad-soft)',
          }}
        >
          <div className="card-body" style={{ padding: '10px 14px', fontSize: 13, display: 'flex', alignItems: 'flex-start', gap: 10 }}>
            <div style={{ flex: 1 }}>
              <strong style={{ color: 'var(--bad)' }}>Error: </strong>
              {error || externalError}
            </div>
            {externalError && onReportError && (
              <button
                className="btn ghost sm"
                onClick={() => onReportError(externalError)}
                title="Send this error to the developer via Submit Feedback"
                style={{ flexShrink: 0 }}
              >
                Report this
              </button>
            )}
            <button
              className="btn ghost sm"
              onClick={() => { setError(null); clearExternalError?.(); }}
              title="Dismiss"
              style={{ flexShrink: 0 }}
            >
              ×
            </button>
          </div>
        </div>
      )}

      <div className="v-stack">
        {/* Character — the selector stays visible regardless of the current
            pick (the active character is highlighted). Picking a different
            character resets the job/pull flow below (App remounts this view
            keyed by lodestoneId); re-picking the active one is a no-op. */}
        <div className="card">
          <div className="card-head">
            <User size={14} />
            <h2>Character</h2>
          </div>
          <div className="card-body">
            <CharacterSelect
              activeLodestoneId={state.lodestoneId}
              activeCharacter={characterLoaded ? {
                lodestoneId: state.lodestoneId as number,
                name: state.characterName,
                server: state.server,
                dataCenter: state.dataCenter,
                avatarUrl: state.avatarUrl,
              } : undefined}
              initialServer={state.server || undefined}
              initialRegion={state.region}
              onPicked={onCharacterPicked}
            />
          </div>
        </div>

        {/* Job picker — shown once a character is active; collapses to a bar
            after "Confirm job" locks it in and reveals the pull cards. */}
        {characterLoaded && !jobConfirmed && (
          <div className="card">
            <div className="card-head">
              <Sword size={14} />
              <h2>Job</h2>
            </div>
            <div className="card-body">
              <div className="job-grid">
                {JOBS.map((j) => {
                  const icon = jobIcon(j);
                  // Every tile is clickable — healers lead to the mitigation
                  // planner; analysis-pending ones (no sim yet) say so.
                  const pending = isJobPending(j);
                  return (
                    <button
                      key={j}
                      className={'btn job-tile ' + (job === j ? 'primary ' : '')}
                      title={pending ? PENDING_JOB_TIP
                        : isHealer(j)
                          ? 'Healer — leads to the Healing/Mitigation planner'
                          : undefined}
                      onClick={() => setJob(j)}
                    >
                      {icon ? (
                        <img
                          src={icon}
                          alt=""
                          width={22}
                          height={22}
                          draggable={false}
                          className="job-tile-icon"
                        />
                      ) : (
                        <span
                          className="job-tile-icon"
                          style={{ background: jobColor(j) }}
                        />
                      )}
                      <span className="job-tile-label">{j}</span>
                    </button>
                  );
                })}
              </div>
              <div className="row" style={{ marginTop: 14, justifyContent: 'flex-end' }}>
                <span className="mut" style={{ fontSize: 12, marginRight: 'auto' }}>
                  {job ? `${job} selected` : 'Pick a job to continue.'}
                </span>
                <button
                  className="btn primary"
                  disabled={!job}
                  onClick={() => {
                    setJobConfirmed(true);
                    onJobConfirmed?.(job);
                  }}
                >
                  Confirm job
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Collapsed job bar — shown once confirmed, with a backtrack to the
            picker. */}
        {characterLoaded && jobConfirmed && (
          <div className="card">
            <div className="card-body job-selected-row">
              {jobIcon(job) ? (
                <img
                  src={jobIcon(job)!}
                  alt=""
                  width={28}
                  height={28}
                  draggable={false}
                  style={{ borderRadius: 6, boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.25)', flexShrink: 0 }}
                />
              ) : (
                <span
                  style={{
                    width: 24, height: 24, borderRadius: 6,
                    background: jobColor(job), display: 'inline-block',
                    boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.25)', flexShrink: 0,
                  }}
                />
              )}
              <div style={{ fontWeight: 500, minWidth: 0 }}>{job}</div>
              <button
                className="btn ghost sm"
                style={{ marginLeft: 'auto' }}
                onClick={() => setJobConfirmed(false)}
              >
                Change job
              </button>
            </div>
          </div>
        )}

        {/* Encounter / Recent pulls — revealed once a job is chosen, side by
            side to mirror the Job › Pull breadcrumb. */}
        {step1Done && (
          <div className="pull-grid">
            {/* Pull & references */}
            <div className="card">
              <div className="card-head">
                <Target size={14} />
                <h2>Pull &amp; references</h2>
                <span className="sub" style={{ marginLeft: 'auto' }}>
                  {activeSource === 'kills'
                    ? `${pulls.length} pulls available`
                    : `${progPulls.length} wipes found`}
                </span>
              </div>
              <div className="card-body">
                <div className="v-stack">
                  <div>
                    <label className="field-label">Encounter</label>
                    <select
                      className="select"
                      value={encounter}
                      onChange={(e) => {
                        const next = e.target.value;
                        setEncounter(next);
                        // Reseed the pull to the new encounter's first pull —
                        // otherwise a stale pull from the previous encounter
                        // survives and fails the run-time lookup. Same for the
                        // wipe selection (its lazy fetch reseeds after load).
                        const ne = encounters.find((x) => encMatches(next, x));
                        const np = (ne && pullsByEnc[ne.id]) || [];
                        setPullLabel(np[0]?.label ?? '');
                        const nw = (ne && progPullsByEnc[ne.id]) || [];
                        setProgKey(nw[0] ? `${nw[0].reportCode}:${nw[0].fightId}` : '');
                      }}
                    >
                      {encounters.map((e) => (
                        <option key={e.id}>{`${e.name} (${e.totalKills} kills)`}</option>
                      ))}
                    </select>
                  </div>
                  {/* Completed (kills) vs In-progress (wipes). Kills are the
                      default and today's zero-extra-fetch path; wipes fetch
                      lazily on first toggle. Healers get the toggle too now:
                      progging is where healing matters most, and the mit-plan
                      ceiling credits the healing the wipe actually demanded (a
                      wipe still routes healers through the planner → analyze). */}
                  <div>
                    <label className="field-label">Pull type</label>
                    <div className="segctrl">
                      <button
                        className={activeSource === 'kills' ? 'on' : ''}
                        onClick={() => setPullSource('kills')}
                        title="Your ranked kills on this encounter."
                      >
                        Completed
                      </button>
                      <button
                        className={activeSource === 'wipes' ? 'on' : ''}
                        onClick={() => setPullSource('wipes')}
                        title="Progression pulls (wipes) from your recent reports — scored up to your death, with a projected kill time."
                      >
                        In progress
                      </button>
                    </div>
                  </div>
                  {activeSource === 'kills' ? (
                    <div>
                      <label className="field-label">Your pull</label>
                      {pulls.length === 0 ? (
                        <div className="mut" style={{ fontSize: 12, padding: '6px 2px' }}>
                          {category === 'ultimate'
                            ? 'No clears yet — switch to “In progress” above to analyze a wipe.'
                            : 'No completed pulls for this encounter.'}
                        </div>
                      ) : (
                        <select
                          className="select"
                          value={pullLabel}
                          onChange={(e) => setPullLabel(e.target.value)}
                        >
                          {pulls.map((p) => (
                            <option key={p.reportCode + p.fightId} value={p.label}>{p.label}</option>
                          ))}
                        </select>
                      )}
                    </div>
                  ) : (
                    <div className="v-stack" style={{ gap: 8 }}>
                      <div>
                        <label className="field-label">Your wipe</label>
                        {progLoading ? (
                          <div className="mut" style={{ fontSize: 12, padding: '6px 2px' }}>
                            Scanning your recent reports…
                          </div>
                        ) : progPulls.length === 0 ? (
                          <div className="mut" style={{ fontSize: 12, padding: '6px 2px' }}>
                            No wipes on this encounter in your last reports —
                            paste a report link below.
                          </div>
                        ) : (
                          <select
                            className="select"
                            value={progKey}
                            onChange={(e) => setProgKey(e.target.value)}
                          >
                            {progPulls.map((p) => (
                              <option
                                key={p.reportCode + p.fightId}
                                value={`${p.reportCode}:${p.fightId}`}
                              >
                                {p.label}
                              </option>
                            ))}
                          </select>
                        )}
                      </div>
                      <div className="row" style={{ gap: 6 }}>
                        <input
                          className="select"
                          style={{ flex: 1, minWidth: 0 }}
                          placeholder="…or paste an FFLogs report link"
                          value={pasteText}
                          onChange={(e) => setPasteText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && pasteText.trim()) addPastedReport();
                          }}
                        />
                        <button
                          className="btn ghost"
                          onClick={addPastedReport}
                          disabled={!pasteText.trim() || pasteLoading}
                        >
                          {pasteLoading ? 'Loading…' : 'Add'}
                        </button>
                      </div>
                      {progError && (
                        <div className="mut" style={{ fontSize: 12, color: 'var(--bad)' }}>
                          {progError}
                        </div>
                      )}
                    </div>
                  )}
                  <div className="row" style={{ marginTop: 6, gap: 8 }}>
                    <button className="btn primary" onClick={startAnalysis} disabled={!step2Done}>
                      {isHealer(job) ? <HeartPulse size={14} /> : <Sparkles size={14} />}
                      {!isHealer(job)
                        ? 'Run analysis'
                        : ANALYZABLE_HEALERS.has(job)
                          ? 'Plan & analyze'
                          : 'Open mitigation planner'}
                    </button>
                    {isHealer(job) && (
                      <button
                        className="btn ghost"
                        onClick={openPlannerWithoutPull}
                        title="Open the Healing/Mitigation planner with default comp — no pull needed"
                      >
                        Planner only
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* Recent pulls — newest across every encounter; click to load. */}
            <div className="card">
              <div className="card-head">
                <Trophy size={14} />
                <h2>Recent pulls</h2>
                <span className="sub" style={{ marginLeft: 'auto' }}>
                  all encounters
                </span>
              </div>
              <div className="card-body" style={{ padding: 0 }}>
                {loadingPulls && recent.length === 0 ? (
                  <div className="mut" style={{ padding: '14px', fontSize: 12 }}>
                    Loading recent pulls…
                  </div>
                ) : recent.length === 0 ? (
                  <div className="mut" style={{ padding: '14px', fontSize: 12 }}>
                    No recent pulls found.
                  </div>
                ) : (
                  recent.map((p) => {
                    const isKill = p.parsePct > 0;
                    const tone = !isKill ? 'bad' : p.parsePct > 95 ? 'good' : 'warn';
                    const eff = !isKill ? 'wipe' : `${p.parsePct.toFixed(1)}%`;
                    const timeLabel = !isKill
                      ? '—'
                      : `${Math.floor(p.durationS / 60)}:${String(Math.floor(p.durationS % 60)).padStart(2, '0')}`;
                    const active =
                      selectedEnc?.id === p.encounterId && pullLabel === p.label;
                    return (
                      <button
                        key={p.reportCode + p.fightId}
                        className={'recent-pull' + (active ? ' active' : '')}
                        onClick={() => pickRecent(p)}
                      >
                        <div style={{ flex: 1, textAlign: 'left', minWidth: 0 }}>
                          <div
                            style={{
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {p.encounterName}
                          </div>
                          <div className="mut" style={{ fontSize: 11, marginTop: 2 }}>
                            {timeLabel === '—' ? 'wipe' : 'kill ' + timeLabel}
                          </div>
                        </div>
                        <span className={`tag ${tone}`}>
                          <span className="pip" />
                          {eff}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
