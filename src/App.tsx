import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeftRight,
  BarChart3,
  Check,
  Cog,
  FlaskConical,
  Gauge,
  HeartPulse,
  History,
  Home,
  Layers,
  MessageSquare,
  Minus,
  Square,
  Swords,
  Trash2,
  Trophy,
  User,
  X,
} from 'lucide-react';

import { fmtNum } from './format';
import { ANALYZABLE_HEALERS, isJobPending } from './components/jobs';
import { CharacterAvatar } from './components/CharacterAvatar';
import { CharacterPickerModal, type CharacterPicked } from './components/CharacterPickerModal';
import { FFLogsSignInModal } from './components/FFLogsSignIn';
import { RefsLoadingModal } from './components/RefsLoadingModal';
import { Splash } from './components/Splash';
import { WhatsNewModal } from './components/WhatsNewModal';
import { APP_VERSION } from './data/changelog';
import type { AuthStatus, MitCompSelection, ProgressTask } from './sidecar/contract';
import { onAuthExpired } from './sidecar/authExpired';
import { HomeView } from './views/HomeView';
import { SetupView } from './views/SetupView';
import { ResearchView } from './views/ResearchView';
import { LoadingView } from './views/LoadingView';
import { DashboardView } from './views/DashboardView';
import { TimelineView, type TimelineFocus } from './views/TimelineView';
import { CountsView } from './views/CountsView';
import { KillTimeTheorizer } from './views/KillTimeTheorizer';
import { MitigationPlanner } from './views/MitigationPlanner';
import { SettingsView } from './views/SettingsView';
import { FeedbackView, type FeedbackPrefill } from './views/FeedbackView';
import { VersionHistoryView } from './views/VersionHistoryView';
import { logEvent } from './log';

import { sidecar } from './sidecar';
import { clearUserCharactersCache, listUserCharactersCached } from './sidecar/sessionCache';
import {
  initialState, toPersistedCharacter,
  type AppState, type Stage, type View,
} from './state/appState';
import { loadLastSelection, loadSetupTab, saveLastSelection, saveSetupTab } from './state/persist';
import {
  MT_MODE_DEFAULT,
  unreachableWindowIds,
  type MtMode,
} from './state/multiTargetModes';
import { applyAccent, loadAccent, saveAccent } from './state/accent';
import { applyZoom, loadZoom, saveZoom } from './state/zoom';
import { refsWarmer, useRefsWarmer } from './state/refsPrefetch';
import { checkForUpdate, installUpdate, useUpdater } from './state/updater';
import { markVersionSeen, pendingReleases } from './state/whatsNew';
import { closeWindow, minimizeWindow, toggleMaximize } from './tauri/window';

type NavEntry = {
  id: View;
  label: string;
  Icon: typeof Home;
  needs: 'character' | 'ready' | null;
  badge?: string;
};

// The Analysis nav section. Home lives outside it, as its own standalone
// item above.
const NAV: NavEntry[] = [
  // Savage / Ultimates both open the encounter picker (no character needed —
  // they open on the character select screen). They render the SAME SetupView,
  // scoped to their encounter category.
  { id: 'setup',     label: 'Savage',      Icon: User,      needs: null },
  { id: 'ultimates', label: 'Ultimates',   Icon: Swords,    needs: null },
  { id: 'dashboard', label: 'Analysis',    Icon: Gauge,     needs: 'ready', badge: '6' },
  { id: 'timeline',  label: 'Timeline',    Icon: BarChart3, needs: 'ready' },
  { id: 'counts',    label: 'Cast counts', Icon: Layers,    needs: 'ready' },
];

/** The two encounter-picker views. Used to gate SetupView rendering and route
 *  "back to setup" fallbacks to whichever tab the user last had open. */
const SETUP_TABS: View[] = ['setup', 'ultimates'];

// Captured once at startup: the last session's job, used ONLY to prioritize
// the reference warm-cache. The UI itself always launches with no job
// selected (state.job hydrates to '' below) so a stale job never auto-runs
// a log lookup against a character who may not play it.
const LAST_SESSION_JOB = loadLastSelection().job ?? '';

/** Status-bar cache size: "0" when empty, whole MB from ~100 up, one
 *  decimal below. */
const fmtCacheMB = (bytes: number): string => {
  const mb = bytes / (1024 * 1024);
  if (mb === 0) return '0';
  return mb >= 100 ? String(Math.round(mb)) : mb.toFixed(1);
};

const App = () => {
  const [view, setViewRaw] = useState<View>('home');
  const [stage, setStage] = useState<Stage>('setup');
  // The encounter-picker tab (Savage / Ultimates) the user last had open.
  // Hydrated from persistence; drives which SetupView is shown and where the
  // "back to setup" fallbacks route. setView tracks it whenever a setup tab is
  // entered so the two stay in sync.
  const [lastSetupTab, setLastSetupTab] = useState<View>(() => loadSetupTab());
  const setView = (v: View) => {
    if (SETUP_TABS.includes(v)) {
      setLastSetupTab(v);
      saveSetupTab(v === 'ultimates' ? 'ultimates' : 'setup');
    }
    setViewRaw(v);
  };
  const [state, setState] = useState<AppState>(() => ({
    ...initialState,
    ...loadLastSelection(),
    // Job selection is per-session — always land on the job picker. (The
    // last session's job still primes the warm via LAST_SESSION_JOB.)
    job: '',
  }));
  const [loadingLabel, setLoadingLabel] = useState<string | undefined>();
  const [progressPct, setProgressPct] = useState<number | undefined>();
  const [progressTasks, setProgressTasks] = useState<ProgressTask[] | undefined>();
  // Per-step pipeline checklist (runAnalysis): the ordered step labels + the active
  // index, rendered as the secondary progress indicator in LoadingView.
  const [progressStep, setProgressStep] = useState<number | undefined>();
  const [progressSteps, setProgressSteps] = useState<string[] | undefined>();
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [accent, setAccentState] = useState<string>(() => loadAccent());
  const [zoom, setZoomState] = useState<number>(() => loadZoom());
  // "Jump to this time in the Timeline", set when the user clicks a Potential
  // Improvement on the dashboard. The nonce bumps on every click so clicking
  // the same suggestion twice re-triggers the scroll. TimelineView reads it to
  // scroll + highlight the nearest cast.
  const [timelineFocus, setTimelineFocus] = useState<TimelineFocus | null>(null);
  // Per-window confirmations (e.g. Flamethrower downtime-edge squeezes the sim
  // detected). Ids the user marked "not possible" → the dashboard removes their
  // tick from the idealized ceiling. Reset on every fresh analysis. Lives here
  // (not in DashboardView) so it survives tab switches within a run. For
  // multi-target windows the entries are OVERRIDES relative to `mtMode`'s
  // default (see state/multiTargetModes.ts).
  const [deniedWindows, setDeniedWindows] = useState<Set<string>>(new Set());
  // Multi-target crediting mode — the global grading basis for the confirmed
  // multi-target windows. Reset with the denials on every fresh analysis;
  // switching it clears the mt@ overrides (their meaning is mode-relative).
  const [mtMode, setMtMode] = useState<MtMode>(MT_MODE_DEFAULT);
  const setMtModeClearingOverrides = (m: MtMode) => {
    setMtMode(m);
    setDeniedWindows((prev) => {
      const next = new Set([...prev].filter((id) => !id.startsWith('mt@')));
      return next.size === prev.size ? prev : next;
    });
  };
  // Seed for the Submit Feedback view — set by the dashboard's over-ceiling
  // nudge and Setup's "Report this" error button before navigating there.
  const [feedbackPrefill, setFeedbackPrefill] = useState<FeedbackPrefill | null>(null);
  const openFeedback = (prefill: FeedbackPrefill | null = null) => {
    setFeedbackPrefill(prefill);
    setView('feedback');
  };
  const toggleWindow = (id: string) =>
    setDeniedWindows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  // The change-character modal, opened from the sidebar character tile.
  // First-run character selection happens inline on the Encounter page
  // (SetupView's CharacterSelect), so nothing auto-opens here.
  const [showCharacterPicker, setShowCharacterPicker] = useState<boolean>(false);
  // True when the current character was picked via manual search from inside
  // the picker — same persistence behavior as the manual mode setting, but
  // transient (one session only). Reset on every fresh pick.
  const [characterTransient, setCharacterTransient] = useState<boolean>(false);
  // Reference warm-cache: blocking-popup state + background fill (see
  // state/refsPrefetch.ts). `warm.blocking` drives RefsLoadingModal.
  const warm = useRefsWarmer();

  // Status-bar cache size. A cheap sidecar-side dir scan, refreshed at the
  // moments the disk cache actually grows: boot, a blocking warm closing,
  // and each completed analysis.
  const [cacheBytes, setCacheBytes] = useState<number | null>(null);
  useEffect(() => {
    let dead = false;
    void sidecar
      .cacheStats()
      .then((s) => {
        // Guard against wire-shape drift — never let NaN reach the footer.
        if (!dead) setCacheBytes(Number.isFinite(s.totalBytes) ? s.totalBytes : 0);
      })
      .catch(() => {}); // stat-only — a failure just leaves the dash
    return () => {
      dead = true;
    };
  }, [warm.blocking.active, state.analysis]);

  // Auto-update state (packaged builds only; a no-op under dev/mock).
  const updater = useUpdater();
  useEffect(() => {
    void checkForUpdate();
  }, []);

  // "What's new": the release notes this install hasn't shown yet (see
  // state/whatsNew.ts). Resolved once at mount — a synchronous localStorage
  // read against the bundled changelog, no network.
  const [whatsNew, setWhatsNew] = useState(pendingReleases);
  useEffect(() => {
    // Nothing to show (fresh install, or already up to date): record the
    // version silently so the NEXT update does pop. When there IS something to
    // show we record on dismiss instead, so quitting before reading it doesn't
    // swallow the notes.
    if (whatsNew.length === 0) markVersionSeen();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const dismissWhatsNew = () => {
    markVersionSeen();
    setWhatsNew([]);
  };

  // FFLogs auth: null = still asking the sidecar; mode 'none' gates the whole
  // app behind the sign-in modal (nothing works without API access).
  // `authExpired` distinguishes a mid-session token death from first run.
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [authExpired, setAuthExpired] = useState(false);
  // Voluntary sign-in (Settings button, or the signed-out character card
  // while dev client-credentials still work) — same modal, but dismissible.
  const [signInRequested, setSignInRequested] = useState(false);
  // When the sign-in came from the character card, continue into the picker
  // once it succeeds — that's what the click was trying to reach.
  const pickAfterSignIn = useRef(false);
  const authOk = auth !== null && auth.mode !== 'none';

  useEffect(() => {
    let dead = false;
    void sidecar
      .getAuthStatus()
      .then((a) => {
        if (!dead) setAuth(a);
      })
      .catch(() => {
        // Sidecar spawn/handshake failure — surface as the sign-in gate; the
        // begin click will re-raise the real error where the user can see it.
        if (!dead) setAuth({ mode: 'none' });
      });
    const off = onAuthExpired(() => {
      setAuthExpired(true);
      setAuth({ mode: 'none' });
    });
    return () => {
      dead = true;
      off();
    };
  }, []);

  // With an FFLogs user signed in and no character loaded, auto-load the
  // account's first claimed character so the sidebar tile shows you as
  // signed in without a trip through any picker. Runs once per auth-mode
  // flip — an explicit "Clear character" is respected (mode didn't change,
  // so this doesn't refire and re-pick it).
  useEffect(() => {
    if (auth?.mode !== 'user') return;
    let dead = false;
    void listUserCharactersCached()
      .then((r) => {
        if (dead || r.characters.length === 0) return;
        const list = r.characters.map(toPersistedCharacter);
        setState((cur) => {
          if (cur.lodestoneId) return cur; // a pick beat us to it
          return {
            ...cur,
            characterName: list[0].name,
            server: list[0].server,
            region: list[0].region,
            lodestoneId: list[0].lodestoneId,
            logsCount: undefined,
            dataCenter: list[0].dataCenter,
            avatarUrl: list[0].avatarUrl,
            fflogsCharacters: list,
          };
        });
      })
      .catch(() => {
        /* selector/error states surface this where the user can act on it */
      });
    return () => {
      dead = true;
    };
    // Reads state.lodestoneId via the functional update only — deliberately
    // not a dependency (this must not refire on character changes).
  }, [auth?.mode]);

  // Warm references once signed in — silently in the background (no popup), so
  // the app boots straight to the setup page. The saved job's saved-encounter
  // refs go first (a returning player's most likely Run is ready soonest); the
  // rest of the tier matrix fills behind it. Refs are independent of the chosen
  // character but DO need FFLogs API access, so this waits on auth. (The
  // blocking popup path still backs ensureJob — see state/refsPrefetch.ts.)
  useEffect(() => {
    if (!authOk) return;
    // Priority = the job last analyzed (state.job launches empty — see
    // LAST_SESSION_JOB). A parked job (the healers) can't be re-picked in the
    // UI, so don't spend the launch warm on its refs — fall through to the
    // matrix fill order.
    const priorityJob =
      LAST_SESSION_JOB && !isJobPending(LAST_SESSION_JOB) ? LAST_SESSION_JOB : undefined;
    void refsWarmer.start(priorityJob, state.encounterId || undefined, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authOk]);

  useEffect(() => {
    // Strip character identity when the pick was Skip-to-manual (transient).
    // Job / encounter / pull selections survive either way.
    if (characterTransient) {
      saveLastSelection({
        ...state,
        characterName: '',
        server: '',
        lodestoneId: undefined,
        logsCount: undefined,
        dataCenter: undefined,
        avatarUrl: undefined,
        fflogsCharacters: undefined,
      });
    } else {
      saveLastSelection(state);
    }
  }, [characterTransient, state.characterName, state.server, state.region, state.job, state.encounter, state.pullId, state.refsBucket]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    applyAccent(accent);
    saveAccent(accent);
  }, [accent]);

  useEffect(() => {
    applyZoom(zoom);
    saveZoom(zoom);
  }, [zoom]);

  const ready = state.analysisStatus === 'ready' && !!state.analysis;

  /** Run analysis. Accepts an optional snapshot to override the (still-
   *  propagating) react state — Setup's button passes the just-resolved
   *  pull / encounter / job because setState is async. `comp` is the healer
   *  flow's (user-adjusted) mit-plan comp override from the planner. */
  const runAnalysis = async (snapshot?: Partial<AppState>, comp?: MitCompSelection,
                             usePfPlan?: boolean) => {
    const s = { ...state, ...(snapshot ?? {}) };

    if (!s.job || !s.pullReportCode || s.pullFightId === 0) {
      setAnalysisError(
        'Missing selection — pick a character, job, and pull before running.'
      );
      setView(lastSetupTab);
      return;
    }
    setStage('loading');
    setView('dashboard');
    setLoadingLabel('Connecting to FFLogs…');
    setProgressPct(0);
    setProgressTasks(undefined);
    setProgressStep(undefined);
    setProgressSteps(undefined);
    setAnalysisError(null);
    setState((cur) => ({ ...cur, ...(snapshot ?? {}), analysisStatus: 'loading' }));
    try {
      const result = await sidecar.runAnalysis(
        s.pullReportCode,
        s.pullFightId,
        s.job,
        s.encounterId,
        s.refsBucket,
        s.playerName,
        (pct, label, tasks, meta) => {
          setProgressPct(pct);
          setLoadingLabel(label);
          setProgressTasks(tasks);
          setProgressStep(meta?.step);
          setProgressSteps(meta?.steps);
        },
        comp,
        usePfPlan
      );
      setState((cur) => ({ ...cur, analysis: result, analysisStatus: 'ready' }));
      // Fresh run: grade at the maximal-credit default, with the geometry
      // advisory's "out of cleave range" windows pre-denied (user-overridable).
      setDeniedWindows(new Set(unreachableWindowIds(result)));
      setMtMode(MT_MODE_DEFAULT);
      setStage('dashboard');
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error('[runAnalysis]', e);
      logEvent('error', 'analysis', msg, {
        job: s.job, encounterId: s.encounterId,
        reportCode: s.pullReportCode, fightId: s.pullFightId,
      });
      setAnalysisError(`Analysis failed: ${msg}`);
      setState((cur) => ({ ...cur, analysisStatus: 'error' }));
      // Drop back to Setup so the user can see the error banner and fix.
      setStage('setup');
      setView(lastSetupTab);
    }
  };

  /** The healer flow's pull context for the Healing/Mitigation planner:
   *  Setup routes a healer's Run here, the planner auto-resolves the pull's
   *  comp, and "Analyze my pull" (analyzable healers) runs the locked-GCD
   *  analysis. Null until a healer routes through; nav-opening the planner
   *  directly keeps the standalone (no-pull) behavior. */
  const [mitPlanContext, setMitPlanContext] = useState<{
    job: string; encounterId: number; reportCode?: string; fightId?: number;
  } | null>(null);

  const onPlanMitigation = (snapshot: Partial<AppState>) => {
    const s = { ...state, ...snapshot };
    setState((cur) => ({ ...cur, ...snapshot }));
    setMitPlanContext({
      job: s.job,
      encounterId: s.encounterId,
      reportCode: s.pullReportCode || undefined,
      fightId: s.pullFightId || undefined,
    });
    setView('mitigation');
  };

  // Clear the loaded character identity AND its character-bound selections
  // (encounter/pull). Job is kept — "log in as an alt on the same role"
  // shouldn't have to redo job picking. Used by the picker's Clear button
  // and by an FFLogs sign-out (those characters belong to that account).
  const clearCharacter = () => {
    setState((cur) => ({
      ...cur,
      characterName: '',
      server: '',
      lodestoneId: undefined,
      logsCount: undefined,
      dataCenter: undefined,
      avatarUrl: undefined,
      fflogsCharacters: undefined,
      encounter: '',
      encounterId: 0,
      pullId: '',
      pullReportCode: '',
      pullFightId: 0,
      pullsLoaded: false,
    }));
  };

  const onCharacterPicked = (c: CharacterPicked) => {
    setCharacterTransient(!!c.transient);
    setState((cur) => {
      // Re-picking the already-active character (the Encounter page keeps
      // the selector visible) is a refresh, not a reset — job/encounter/pull
      // survive. A genuinely different character invalidates every
      // character-bound selection, job included (a new character starts at
      // the job picker; they may not play the prior job at all).
      const same = cur.lodestoneId === c.lodestoneId;
      return {
        ...cur,
        job: same ? cur.job : '',
        characterName: c.name,
        server: c.server,
        region: c.region,
        lodestoneId: c.lodestoneId,
        logsCount: c.logsCount,
        dataCenter: c.dataCenter,
        avatarUrl: c.avatarUrl,
        // FFLogs-list picks (fresh fetch OR quick-switch from alternates)
        // carry the full list to re-persist. Manual picks omit it →
        // cleared on a character change so the quick-switch panel doesn't
        // show another account's alternates.
        fflogsCharacters: c.fflogsCharacters ?? (same ? cur.fflogsCharacters : undefined),
        encounter: same ? cur.encounter : '',
        encounterId: same ? cur.encounterId : 0,
        pullId: same ? cur.pullId : '',
        pullReportCode: same ? cur.pullReportCode : '',
        pullFightId: same ? cur.pullFightId : 0,
        pullsLoaded: same ? cur.pullsLoaded : false,
      };
    });
    setShowCharacterPicker(false);
    // If user landed on the picker from a non-setup view (or first launch),
    // route them to the encounter picker (their last-open Savage/Ultimates tab).
    if (!SETUP_TABS.includes(view) && view !== 'settings') setView(lastSetupTab);
  };

  return (
    <div className="app">
      {/* Title bar */}
      <div className="titlebar" data-tauri-drag-region>
        <div className="brand">
          <img
            src="/meridian.svg"
            alt=""
            width={18}
            height={18}
            draggable={false}
            style={{ flexShrink: 0 }}
          />
          Meridian
          <span className="mut-2" style={{ marginLeft: 8, fontSize: 11 }}>· v{APP_VERSION}</span>
          {(updater.phase === 'available' || updater.phase === 'downloading' ||
            updater.phase === 'installing') && (
            <button
              className="btn primary"
              style={{ marginLeft: 10, padding: '2px 10px', fontSize: 11, height: 22 }}
              disabled={updater.phase !== 'available'}
              onClick={() => void installUpdate()}
              title={updater.notes || 'Download and install the update'}
            >
              {updater.phase === 'available' && `Update to v${updater.version}`}
              {updater.phase === 'downloading' &&
                `Downloading…${updater.progressPct != null ? ` ${Math.round(updater.progressPct)}%` : ''}`}
              {updater.phase === 'installing' && 'Restarting…'}
            </button>
          )}
        </div>
        <span className="spacer" />
        <div className="winctrls">
          <button title="Minimize" onClick={minimizeWindow}><Minus size={14} /></button>
          <button title="Maximize" onClick={toggleMaximize}><Square size={13} /></button>
          <button className="close" title="Close" onClick={closeWindow}><X size={14} /></button>
        </div>
      </div>

      {/* Sidebar */}
      <div className="sidebar">
        <button
          className="character-card"
          onClick={() => {
            // Signed-out card (no character, no FFLogs user): the useful
            // first step is the FFLogs sign-in — the picker's character
            // list comes from that account. Flows into the picker on
            // success via pickAfterSignIn.
            if (!state.lodestoneId && auth !== null && auth.mode !== 'user') {
              pickAfterSignIn.current = true;
              setSignInRequested(true);
            } else {
              setShowCharacterPicker(true);
            }
          }}
          title={state.lodestoneId ? 'Change character' : 'Pick a character'}
        >
          <CharacterAvatar
            name={state.characterName || '?'}
            avatarUrl={state.avatarUrl}
            size={36}
          />
          <div className="info">
            <div className="name">{state.characterName || 'No character'}</div>
            <div className="meta">
              {state.lodestoneId
                ? `${state.server} · ${state.region}${state.job ? ` · ${state.job}` : ''}`
                : 'Click to get started'}
            </div>
          </div>
          <span className="swap" aria-hidden="true">
            <ArrowLeftRight size={12} />
          </span>
        </button>

        <div className="nav-section">
          <div
            className={'nav-item ' + (view === 'home' ? 'active' : '')}
            onClick={() => setView('home')}
          >
            <Home className="icon" size={16} />
            <span>Home</span>
          </div>
        </div>

        <div className="nav-section">
          <div className="nav-label">Analysis</div>
          {NAV.map((n) => {
            // The character-bound items grey without a character; the
            // analysis-bound ones also need a completed run. User re-opens
            // the picker via the sidebar character tile. Settings stays
            // accessible because it doesn't depend on the loaded character.
            const disabled =
              (n.needs === 'character' && !state.lodestoneId) ||
              (n.needs === 'ready' && (!state.lodestoneId || !ready));
            const Icn = n.Icon;
            return (
              <div
                key={n.id}
                className={'nav-item ' + (view === n.id ? 'active' : '')}
                style={disabled ? { opacity: 0.4, pointerEvents: 'none' } : undefined}
                onClick={() => {
                  if (disabled) return;
                  setView(n.id);
                }}
              >
                <Icn className="icon" size={16} />
                <span>{n.label}</span>
                {n.badge && ready && view !== n.id && n.id === 'dashboard' && (
                  <span className="badge">{n.badge}</span>
                )}
              </div>
            );
          })}
        </div>

        <div className="nav-section">
          <div className="nav-label">Resources</div>
          <div
            className={'nav-item ' + (view === 'research' ? 'active' : '')}
            onClick={() => setView('research')}
          >
            <Trophy className="icon" size={16} />
            <span>Research</span>
          </div>
          <div
            className={'nav-item ' + (view === 'theorizer' ? 'active' : '')}
            onClick={() => setView('theorizer')}
          >
            <FlaskConical className="icon" size={16} />
            <span>Kill time theorizer</span>
          </div>
          <div
            className={'nav-item ' + (view === 'mitigation' ? 'active' : '')}
            onClick={() => setView('mitigation')}
          >
            <HeartPulse className="icon" size={16} />
            <span>Healing / Mitigation</span>
          </div>
          <div
            className={'nav-item ' + (view === 'settings' ? 'active' : '')}
            onClick={() => setView('settings')}
          >
            <Cog className="icon" size={16} />
            <span>Settings</span>
          </div>
          <div
            className={'nav-item ' + (view === 'feedback' ? 'active' : '')}
            onClick={() => openFeedback(null)}
          >
            <MessageSquare className="icon" size={16} />
            <span>Submit feedback</span>
          </div>
          <div
            className={'nav-item ' + (view === 'changelog' ? 'active' : '')}
            onClick={() => setView('changelog')}
          >
            <History className="icon" size={16} />
            <span>Version history</span>
          </div>
        </div>

        <div className="footer">
          {warm.pending > 0 ? (
            <>
              <span className="status-dot" />
              <span>Fetching background reference data…</span>
            </>
          ) : (
            <>
              <Check className="status-check" size={12} />
              <span>Reference data ready</span>
            </>
          )}
        </div>
      </div>

      {/* Main — each view carries its own title/actions; no shared topbar. */}
      <div className="main">
        {view === 'home' && (
          <HomeView
            characterLoaded={!!state.lodestoneId}
            ready={ready}
            // Feedback goes through openFeedback so a stale prefill from an
            // earlier "Report this" flow never leaks into a fresh visit.
            onNavigate={(v) => (v === 'feedback' ? openFeedback(null) : setView(v))}
          />
        )}
        {(view === 'setup' || view === 'ultimates') && (
          <SetupView
            // Keyed by character AND tab: switching characters or tabs remounts
            // the view so its local job/encounter/pull state resets and re-scopes
            // to the tab's encounter category.
            key={`${state.lodestoneId ?? 'none'}:${view}`}
            category={view === 'ultimates' ? 'ultimate' : 'savage'}
            state={state}
            setState={setState}
            onRunAnalysis={runAnalysis}
            onPlanMitigation={onPlanMitigation}
            externalError={analysisError}
            clearExternalError={() => setAnalysisError(null)}
            onCharacterPicked={onCharacterPicked}
            onJobConfirmed={(job) => {
              // Lift the confirmed job into App state so the sibling tab mounts
              // already-confirmed (SetupView's jobConfirmed is component-local,
              // and each tab is a fresh instance).
              setState((cur) => ({ ...cur, job }));
              // Jump this job to the front of the warm queue and block on its
              // active-encounter refs (popup) until ready. On the Ultimates tab
              // the saved encounterId is usually a Savage fight, so a *blocking*
              // warm here would download the wrong reference set (and the real
              // ultimate would then re-download mid-analysis). Warm non-blocking
              // there and let the encounter-select warm (SetupView) front the
              // right one; the Savage tab keeps the popup on its (usually-correct)
              // saved encounter.
              const block = view !== 'ultimates';
              void refsWarmer.ensureJob(job, state.encounterId || undefined, block);
            }}
            onReportError={(msg) =>
              openFeedback({ category: 'bug', summary: msg })
            }
          />
        )}
        {view === 'dashboard' && stage === 'loading' && (
          <LoadingView label={loadingLabel} progressPct={progressPct} tasks={progressTasks}
            step={progressStep} steps={progressSteps} />
        )}
        {view === 'dashboard' && stage === 'dashboard' && ready && state.analysis && (
          <DashboardView
            analysis={state.analysis}
            setView={setView}
            job={state.job}
            onRerun={() => runAnalysis()}
            deniedWindows={deniedWindows}
            onToggleWindow={toggleWindow}
            mtMode={mtMode}
            onSetMtMode={setMtModeClearingOverrides}
            onJumpToTime={(t, opts) => {
              setTimelineFocus({ timeSec: t, nonce: Date.now(), ...opts });
              setView('timeline');
            }}
            onReportAnomaly={() => {
              const ca = state.analysis?.headline.ceilingAnomaly;
              openFeedback({
                category: 'anomaly',
                summary: ca
                  ? `${ca.job} on ${ca.encounterName || 'this encounter'} scored ` +
                    `${ca.maxEffPct.toFixed(2)}% efficiency (${ca.reportCode}#${ca.fightId}).`
                  : undefined,
              });
            }}
          />
        )}
        {view === 'dashboard' && !ready && stage !== 'loading' && (
          <div className="content">
            <EmptyHint onGo={() => setView(lastSetupTab)} />
          </div>
        )}
        {view === 'timeline' && state.analysis && (
          <TimelineView
            analysis={state.analysis}
            job={state.job}
            focus={timelineFocus}
            deniedWindows={deniedWindows}
            mtMode={mtMode}
          />
        )}
        {view === 'timeline' && !state.analysis && (
          <div className="content">
            <EmptyHint onGo={() => setView(lastSetupTab)} />
          </div>
        )}
        {view === 'counts' && state.analysis && <CountsView analysis={state.analysis} />}
        {view === 'counts' && !state.analysis && (
          <div className="content">
            <EmptyHint onGo={() => setView(lastSetupTab)} />
          </div>
        )}
        {view === 'research' && (
          <ResearchView
            defaultJob={state.job}
            defaultEncounterId={state.encounterId}
            onRunAnalysis={runAnalysis}
          />
        )}
        {view === 'theorizer' && (
          <KillTimeTheorizer
            defaultJob={state.job}
            defaultEncounterId={state.encounterId}
            // A prog (wipe) analysis seeds the theorizer with its PROJECTED
            // kill — the truncated wipe duration isn't a kill time.
            defaultKillSec={state.analysis?.headline.projectedKillTimeSec
              ?? state.analysis?.headline.killTimeSec}
          />
        )}
        {view === 'mitigation' && (
          <MitigationPlanner
            defaultEncounterId={state.encounterId}
            pullContext={mitPlanContext ?? undefined}
            onAnalyze={
              mitPlanContext && ANALYZABLE_HEALERS.has(mitPlanContext.job)
                && mitPlanContext.reportCode
                ? (comp, compAdjusted, usePfPlan) =>
                    runAnalysis(undefined, compAdjusted ? comp : undefined, usePfPlan)
                : undefined
            }
          />
        )}
        {view === 'settings' && (
          <SettingsView
            accent={accent}
            setAccent={setAccentState}
            zoom={zoom}
            setZoom={setZoomState}
            onCacheChanged={setCacheBytes}
            auth={auth}
            onAuthChanged={(a) => {
              setAuth(a);
              if (a.mode !== 'none') setAuthExpired(false);
              // Signing out of FFLogs drops the account's characters too —
              // they belong to the account that just left. The session cache
              // of the list goes with them.
              if (a.mode !== 'user') {
                clearUserCharactersCache();
                clearCharacter();
              }
            }}
            onRequestSignIn={() => setSignInRequested(true)}
          />
        )}
        {view === 'feedback' && (
          <FeedbackView prefill={feedbackPrefill} state={state} />
        )}
        {view === 'changelog' && <VersionHistoryView />}

        <div className="statusbar">
          <div className="l">
            <span className="dot" />
            <span>{ready ? 'Comparison ready.' : 'Idle.'}</span>
            {ready && state.analysis && (
              <>
                <span className="mut-2">·</span>
                <span>
                  {fmtNum(state.analysis.you.deliveredPotency)}p delivered ·{' '}
                  <span className="num" style={{ color: 'var(--text-2)' }}>
                    {fmtNum(state.analysis.you.efficiencyPct)}%
                  </span>{' '}
                  efficient · {state.analysis.refs.length} refs
                </span>
              </>
            )}
          </div>
          <div className="r">
            <span className="mut-2">
              Cache: {cacheBytes === null ? '—' : fmtCacheMB(cacheBytes)} MB
            </span>
            <button
              className="statusbar-btn"
              title="Clear the cached FFLogs data (re-downloaded as needed)"
              onClick={() =>
                void sidecar
                  .clearCache()
                  .then((s) => setCacheBytes(s.totalBytes))
                  .catch(() => {})
              }
            >
              <Trash2 size={11} />
              Clear
            </button>
          </div>
        </div>
      </div>

      <RefsLoadingModal blocking={warm.blocking} />

      {/* Post-update release notes. Held back until the app is actually usable:
          never stacked on top of the sign-in gate, and never underneath the
          (z-200) blocking refs warm. */}
      {whatsNew.length > 0 && authOk && !warm.blocking.active && (
        <WhatsNewModal
          releases={whatsNew}
          onClose={dismissWhatsNew}
          onViewHistory={() => {
            dismissWhatsNew();
            setView('changelog');
          }}
        />
      )}

      {(auth?.mode === 'none' || signInRequested) && (
        <FFLogsSignInModal
          expired={authExpired}
          onSignedIn={(userName) => {
            // The pre-sign-in cache may hold an empty list (fetched under
            // client-credentials, which has no user) — bust it so the new
            // account's characters load.
            clearUserCharactersCache();
            setAuth({ mode: 'user', userName });
            setAuthExpired(false);
            setSignInRequested(false);
            if (pickAfterSignIn.current) {
              pickAfterSignIn.current = false;
              setShowCharacterPicker(true);
            }
          }}
          // Dismissible only when opened voluntarily — the hard gate
          // (nothing configured) stays blocking.
          onDismiss={auth?.mode !== 'none' ? () => {
            pickAfterSignIn.current = false;
            setSignInRequested(false);
          } : undefined}
        />
      )}

      <CharacterPickerModal
        open={authOk && showCharacterPicker}
        required={!state.lodestoneId}
        initialName={state.characterName}
        initialServer={state.server}
        initialRegion={state.region}
        initialDataCenter={state.dataCenter}
        initialAvatarUrl={state.avatarUrl}
        fflogsCharacters={state.fflogsCharacters}
        onClose={() => setShowCharacterPicker(false)}
        onPicked={onCharacterPicked}
        onLogout={clearCharacter}
      />

      {/* Startup splash — over everything (incl. the sign-in gate) until the
          sidecar handshake + auth check resolve, capped at 2s. */}
      <Splash ready={auth !== null} />
    </div>
  );
};

const EmptyHint = ({ onGo }: { onGo: () => void }) => (
  <div
    style={{
      display: 'grid',
      placeItems: 'center',
      padding: '80px 20px',
      textAlign: 'center',
    }}
  >
    <div style={{ maxWidth: 380 }}>
      <img
        src="/meridian.svg"
        alt=""
        width={54}
        height={54}
        draggable={false}
        style={{ display: 'block', margin: '0 auto 14px' }}
      />
      <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>No analysis loaded</div>
      <p className="mut" style={{ fontSize: 13, marginTop: 0 }}>
        Pick a character, job and pull, then run the analysis to see your performance breakdown.
      </p>
      <button className="btn primary" onClick={onGo}>
        Start setup
      </button>
    </div>
  </div>
);

export default App;
