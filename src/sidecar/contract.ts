// JSON contract between the React UI and the Python sidecar process.
// Transport: NDJSON over stdin/stdout. Each request is one JSON line; each
// response is one JSON line tagged with the same `id`.
//
// Pass-through philosophy: the sidecar returns the AspectComparison +
// per-aspect state largely verbatim, so the UI can flatten / re-render
// freely without round-tripping the backend.
//
// The Python core's `AspectResult.state` (see python/jobs/) is the authoritative
// per-aspect data shape — this contract is a flattened projection of it.

import type { Region, RefsBucket } from '../state/appState';

/** NDJSON contract version, mirrored in python/sidecar/version.py. The UI sends
 *  a `handshake` request once on spawn and aborts if the sidecar reports a
 *  different number — that means the app shell and its bundled analyzer came
 *  from different builds (e.g. a partial update). Bump here AND in version.py
 *  together, but only on an *incompatible* wire-shape change; additive
 *  pass-through data (a new job's aspect states) does not need a bump. */
export const PROTOCOL_VERSION = 1;

export type Req =
  /** `appVersion` (the Tauri app version) rides along for the sidecar's event
   *  log + feedback bundle — Python has no app-version constant of its own. */
  | { id: string; kind: 'handshake'; appVersion?: string }
  | { id: string; kind: 'get_auth_status' }
  | { id: string; kind: 'fflogs_auth_begin' }
  | { id: string; kind: 'fflogs_auth_poll' }
  | { id: string; kind: 'fflogs_auth_cancel' }
  | { id: string; kind: 'fflogs_logout' }
  | { id: string; kind: 'list_user_characters' }
  | { id: string; kind: 'lookup_character'; name: string; server: string; region: Region; spec: string }
  | { id: string; kind: 'list_encounters'; lodestoneId: number; zoneId: number; spec: string }
  | { id: string; kind: 'list_pulls'; lodestoneId: number; encounterId: number; spec: string }
  | {
      id: string;
      kind: 'list_prog_pulls';
      /** Discovery mode A: scan this character's recent reports for wipes on
       *  the encounter (pre-filtered to the encounter's zone backend-side). */
      lodestoneId?: number;
      /** Discovery mode B: list one explicitly pasted report's wipes instead
       *  (a bare 16-char code — the frontend extracts it from a URL). Takes
       *  precedence over lodestoneId. */
      reportCode?: string;
      encounterId: number;
      spec: string;
    }
  | { id: string; kind: 'list_setup'; lodestoneId: number; spec: string }
  | { id: string; kind: 'get_catalog' }
  | { id: string; kind: 'cache_stats' }
  | { id: string; kind: 'set_cache_cap'; capMb: number }
  | { id: string; kind: 'clear_cache' }
  | { id: string; kind: 'log_event'; level: LogLevel; cat: string; msg: string;
      data?: Record<string, unknown> }
  | { id: string; kind: 'get_recent_events'; limit?: number }
  | { id: string; kind: 'export_feedback_bundle'; category: FeedbackCategory;
      description?: string; analysisContext?: Record<string, unknown> }
  | { id: string; kind: 'prefetch_refs'; spec: string; encounterId: number; refsBucket: RefsBucket }
  | { id: string; kind: 'list_rankings'; spec: string; encounterId: number }
  | {
      id: string;
      kind: 'run_analysis';
      reportCode: string;
      fightId: number;
      spec: string;
      encounterId: number;
      refsBucket: RefsBucket;
      /** Analyze this named player instead of the first same-job actor in the
       *  fight — set by Research loads (a ranked fight can hold two players of
       *  the same job). Omitted for the normal own-character flow. */
      playerName?: string;
      /** Healer flow (ultimates only): lock the hand-authored premade ("PF")
       *  mit plan into the ceiling instead of the auto-derived one. */
      usePfMitPlan?: boolean;
    }
  | {
      id: string;
      kind: 'theorize_kill_time';
      spec: string;
      /** Encounter to model — the backend derives this fight's downtime from the
       *  reference logs (no character/pull needed). 0 ⇒ pure-uptime ceiling. */
      encounterId: number;
      /** Theorized kill time (seconds). Backend clamps to [30, 1800]. */
      targetKillSec: number;
      /** Width (s) of the spread sampled around the target (default ~7). */
      rangeSec: number;
      /** Party composition (FFLogs job names) whose raid buffs to model;
       *  only buff providers matter. Empty ⇒ buff-agnostic ceiling. */
      partyJobs: string[];
    }
  | {
      id: string;
      kind: 'plan_mitigation';
      /** Encounter to plan — the forced-damage timeline is aggregated from its
       *  top-ranked kill logs (no character/pull needed). */
      encounterId: number;
      /** The healer duo: one shield ('Sage' | 'Scholar') + one regen
       *  ('White Mage' | 'Astrologian'). */
      shieldHealer: string;
      regenHealer: string;
      /** The rest of the comp: exactly two tank jobs + four DPS jobs. */
      tanks: string[];
      dps: string[];
      /** Use the encounter's hand-authored premade ("PF") plan (ultimates only)
       *  instead of the auto-derived one. */
      usePfMitPlan?: boolean;
    };

/** Per-parallel-task progress entry. Emitted while downloading reference
 *  logs (the 6-way ThreadPoolExecutor in the sidecar) so the LoadingView
 *  can show one mini bar per in-flight network request. */
export type ProgressTask = {
  label: string;
  state: 'pending' | 'in_flight' | 'done' | 'failed';
};

/** Per-step pipeline progress (runAnalysis only): the ordered step labels plus the
 *  active index, so the LoadingView can render a checklist (done / running / queued)
 *  alongside the overall bar. `step === steps.length` means every step finished. */
export type ProgressMeta = {
  step?: number;
  steps?: string[];
};

export type Resp<T = unknown> =
  | { id: string; ok: true; data: T }
  /** `errorCode` is an optional machine-readable discriminator; today only
   *  'auth_expired' (the FFLogs sign-in is gone/unrefreshable → the UI shows
   *  a re-sign-in prompt instead of a generic error banner). */
  | { id: string; ok: false; error: string; errorCode?: string }
  | { id: string; progress: { stage: string; pct?: number; tasks?: ProgressTask[];
      step?: number; steps?: string[] } };

/** Error thrown by the ndjson client — `code` carries the wire `errorCode`. */
export class SidecarError extends Error {
  code?: string;
  constructor(message: string, code?: string) {
    super(message);
    this.code = code;
  }
}

// --- Handshake --------------------------------------------------------------

/** Reply to the startup `handshake` request. `protocolVersion` is checked
 *  against the UI's `PROTOCOL_VERSION`; `python` is the sidecar's runtime
 *  version, surfaced only for diagnostics. */
export type HandshakeResult = {
  protocolVersion: number;
  python: string;
};

// --- FFLogs sign-in (OAuth PKCE) --------------------------------------------

/** How the sidecar will authenticate FFLogs API calls.
 *  - 'user': a persisted PKCE sign-in (auth.json) — the shipped-app path.
 *  - 'client_credentials': legacy dev creds in config.json (no sign-in UI).
 *  - 'none': nothing configured — the UI must gate on the sign-in card. */
export type AuthStatus = {
  mode: 'user' | 'client_credentials' | 'none';
  /** FFLogs account name (user mode; may be '' if the name fetch failed). */
  userName?: string;
};

/** Reply to `fflogs_auth_begin`: the URL to open in the default browser and
 *  the loopback port the sidecar is listening on. */
export type AuthBeginResult = {
  authorizeUrl: string;
  port: number;
  expiresInSec: number;
};

export type AuthPollResult =
  | { status: 'pending' }
  | { status: 'done'; userName: string }
  | { status: 'expired' }
  | { status: 'error'; message?: string };

/** One character claimed on the signed-in FFLogs account (the character
 *  picker's list — replaces the old XIVAuth character list). `region` is
 *  FFLogs' compactName (NA/EU/JP/OC); `dataCenter` its subregion name. */
export type UserCharacter = {
  name: string;
  server: string;
  region: Region;
  dataCenter?: string;
  lodestoneId: number;
  /** Lodestone face image (sidecar-scraped + cached; FFLogs itself exposes
   *  no images). Absent on fetch failure → UI falls back to initials. */
  avatarUrl?: string;
};

export type UserCharactersResult = {
  characters: UserCharacter[];
};

// --- Character / encounter / pulls -----------------------------------------

export type LookupResult = {
  found: boolean;
  lodestoneId?: number;
  name?: string;
  serverName?: string;
  region?: string;
  logsCount?: number;
};

/** Which Setup tab an encounter belongs to. Absent on legacy/mock payloads ⇒
 *  treat as 'savage'. Source: encounters.py::encounter_category. */
export type EncounterCategory = 'savage' | 'ultimate';

/** One encounter the character has logged this tier (>= 1 kill), OR a
 *  synthesized zero-kill ultimate row (ultimates are prog-heavy, so the tab
 *  lists them even before a clear — see list_setup). */
export type SetupEncounter = {
  id: number;
  name: string;
  totalKills: number;
  bestParsePct: number | null;
  category?: EncounterCategory;
};

/** One ranked pull (kill) of an encounter. */
export type SetupPull = {
  reportCode: string;
  fightId: number;
  startTimeMs: number;
  durationS: number;
  parsePct: number;
  dps: number;
  label: string;
};

/** Everything SetupView needs for a (character, job), in one round trip:
 *  the tier's encounters AND each encounter's pulls. `pullsByEncounterId` is
 *  keyed by encounter id as a STRING (JSON object keys are strings) — index it
 *  with `String(encounterId)`. Source: sidecar/main.py::list_setup. */
export type SetupData = {
  encounters: SetupEncounter[];
  pullsByEncounterId: Record<string, SetupPull[]>;
};

/** One in-progress (wipe) pull of an encounter — the prog-log discovery list.
 *  Wipes never appear in rankings, so these come from report summaries
 *  (sidecar/main.py::list_prog_pulls). */
export type ProgPull = {
  reportCode: string;
  fightId: number;
  startTimeMs: number;
  durationS: number;
  /** FFLogs fightPercentage: phase-weighted % of the WHOLE fight remaining
   *  at the wipe (null on old logs that lack it). */
  fightPercentage: number | null;
  /** Current boss's HP % remaining (display-only). */
  bossPercentage: number | null;
  /** Phase the wipe happened in (0 = unphased/unknown). */
  lastPhase: number;
  label: string;
};

export type ProgPullsResult = {
  pulls: ProgPull[];
  /** Where the list came from: the character's recent reports, or one
   *  explicitly pasted report code. */
  source: 'recent' | 'report';
};

// --- Catalog / reference warm-cache ----------------------------------------

/** Static catalog used by the reference warm-cache to build its matrix:
 *  which jobs have analyzer support × the current tier's encounters.
 *  Source: sidecar/main.py::get_catalog. */
export type Catalog = {
  supportedJobs: string[];
  /** Supported jobs that also have a rotation simulator — the Kill Time
   *  Theorizer's job picker (it needs a sim to produce an ideal rotation). */
  simBackedJobs: string[];
  encounters: {
    id: number; name: string; category?: EncounterCategory;
    /** A hand-authored premade ("PF") mit plan ships for this encounter
     *  (ultimates only) — gates the planner's "Use PF mit plan" toggle. */
    hasPfPlan?: boolean;
  }[];
  /** Raid-buff provider jobs (FFLogs names) — the selectable set for the Kill
   *  Time Theorizer's party-composition picker. Only providers affect the sim. */
  buffProviders: string[];
};

/** On-disk FFLogs response cache state (sidecar/main.py::cache_stats) —
 *  feeds the status-bar "Cache: N MB" stat and the Settings size slider. */
export type CacheStats = {
  totalBytes: number;
  /** The user's size cap (MB) — slider range CACHE_CAP_MIN_MB..CACHE_CAP_MAX_MB. */
  capMb: number;
};

/** Settings slider bounds — mirror _CACHE_CAP_MIN_MB/_CACHE_CAP_MAX_MB in
 *  sidecar/main.py (the backend clamps to the same range). Entries are
 *  gzipped (~63 KB per analyzed pull), so even the 10 MB floor holds ~160
 *  pulls — about two fully-warmed jobs. */
export const CACHE_CAP_MIN_MB = 10;
export const CACHE_CAP_MAX_MB = 100;
/** Fresh-install default — mirror _CACHE_CAP_DEFAULT_MB in sidecar/main.py. */
export const CACHE_CAP_DEFAULT_MB = 15;
/** The slider's selectable notches: 5 MB pitch through 10–30 (where one
 *  job-tier of headroom matters), 10 MB pitch from 30 to the cap. */
export const CACHE_CAP_TICKS_MB: readonly number[] = [
  10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100,
];

// --- Diagnostics: event log + user-submitted feedback ------------------------

export type LogLevel = 'info' | 'warn' | 'error';

/** What the user is submitting: a bug report, general feedback, or an
 *  over-ceiling anomaly (>100% efficiency — the dashboard nudge's prefill). */
export type FeedbackCategory = 'bug' | 'feedback' | 'anomaly';

/** One event-log line (sidecar/event_log.py). `t` is epoch ms; `cat` is a
 *  dotted category — frontend-forwarded events carry a `ui.` prefix. */
export type AppEvent = {
  t: number;
  lv: LogLevel;
  cat: string;
  msg: string;
  data?: Record<string, unknown>;
};

/** Reply to `get_recent_events` — the log tail, oldest first (log order). */
export type RecentEventsResult = {
  events: AppEvent[];
};

/** Reply to `export_feedback_bundle`: where the diagnostics zip landed plus
 *  the prefilled GitHub issue text (built backend-side; the UI only
 *  URL-encodes it into the new-issue link). */
export type FeedbackBundleResult = {
  path: string;
  issueTitle: string;
  issueBody: string;
};

/** One row of an encounter's job rankings — the Research tab's list. Carries
 *  the log identity (reportCode + fightId) so the row can be loaded straight
 *  into runAnalysis (with `playerName` for actor disambiguation).
 *  Source: sidecar/main.py::list_rankings. */
export type RankingEntry = {
  rank: number;
  name: string;
  server?: string;
  reportCode: string;
  fightId: number;
  durationMs?: number;
  /** Metric value (rdps). */
  amount?: number;
  percentile?: number;
};

/** Result of warming one (job, encounter) reference set. `fromCache` is true
 *  when the set was already resident (no FFLogs fetch). */
export type PrefetchResult = {
  spec: string;
  encounterId: number;
  count: number;
  fromCache: boolean;
  /** Average kill time (s) across the warmed reference logs — surfaced by the
   *  Kill Time Theorizer as a benchmark for choosing a target. 0 when none. */
  avgKillSec: number;
};

// --- Analysis result -------------------------------------------------------

export type FindingTag =
  | 'opener'
  | 'clip'
  | 'drift'
  | 'overcap'
  | 'align'
  | 'model'
  | 'positional'
  | 'deaths-design'
  | 'surging-tempest'
  | 'multi-target';

/** Master ability lookup: id → name + XIVAPI icon path (relative). */
export type AbilityMetaJson = {
  id: number;
  name: string;
  iconPath: string;
  isOgcd: boolean;
  /** True for reactive defensive/utility/movement actions (role actions +
   *  the job's own defensive oGCDs) — excluded from the DPS timeline + cast-diff.
   *  Backend-owned (sidecar tags it from the shared role-action set ∪
   *  `JobData.defensive_ids`), so the frontend filters off this one flag instead
   *  of a hand-maintained per-job id list. Optional for resilience against a
   *  build-skewed sidecar: absent → falls back to the shared-name check. */
  isDefensive?: boolean;
};

/** One cast on a timeline lane. Mirrors backend TrackEvent. */
export type CastEvent = {
  startSec: number;
  endSec: number;
  abilityId?: number;
  label: string;
  tooltip: string;
  color: string;
  iconPath?: string;
  yOffset: number;
  /** Multi-target annotation (present only on splash casts inside a confirmed
   *  window): `mtHit` targets struck of `mtMax` the window afforded; `mtLost` =
   *  splash potency dropped when mtHit < mtMax. The idealized lane is always
   *  full (mtHit == mtMax). Drives the per-cast timeline highlight + hover. */
  mtHit?: number;
  mtMax?: number;
  mtLost?: number;
};

/** Per-run summary shared by `you` and each `refs[]` entry. */
export type RunSummary = {
  label: string;
  fightDurationSec: number;
  deliveredPotency: number;
  /** Perfect-sim idealized potency for THIS run's duration + downtime.
   *  Used as the denominator for `efficiencyPct`. 0 if no simulator. */
  idealizedPotency: number;
  efficiencyPct: number;
  killTimeSec: number;
  /** Populated for `you` AND each ref — both run the full aspect pipeline, so
   *  refs carry their own Abilities track for the Timeline's reference lanes
   *  (sidecar/main.py::_build_response). */
  abilitiesTrack?: CastEvent[];
  /** This run's own tincture (Medicated) windows — drawn on its own Timeline
   *  lane (you / each ref), so pot timings are per-actor, not a fight-wide band.
   *  Empty when the job doesn't pot or none was used. */
  tinctureWindows?: TinctureWindow[];
  /** Multi-target pulls only: which basis `deliveredPotency` /
   *  `idealizedPotency` are on. True = the credited (splash-inclusive) pair;
   *  false = the single-target pair (this run's own splash exceeded its
   *  ceiling, so it was left uncredited). Absent on single-target pulls. The
   *  crediting modes reconstruct an uncredited run's splash-inclusive pair from
   *  the per-window `ref*` deltas — if this pair ever becomes splash-inclusive
   *  for uncredited runs backend-side, that reconstruction would double-count;
   *  this flag is the guard. */
  multiTargetCredited?: boolean;
};

/** Top-of-dashboard KPIs that the sidecar computes once.
 *
 *  Ranking is by EFFICIENCY (delivered / idealized@own-duration), not by
 *  raw potency — fights with different durations would otherwise sort
 *  arbitrarily. See sidecar/main.py::_headline. */
/** One confirmed boss-untargetable window (Tier A). */
export type DowntimeTierAWindow = {
  startSec: number;
  endSec: number;
};

/** One consensus-suspected forced-downtime window (Tier B). `nIdle` /
 *  `nTotal` is the worst-case agreement across the window — the most
 *  honest confidence to show the user. */
export type DowntimeTierBWindow = {
  startSec: number;
  endSec: number;
  nIdle: number;
  nTotal: number;
};

/** One consensus ranged-filler window (Tier B's sibling): a stretch where
 *  the reference players bridged a forced melee disconnect with their
 *  ranged filler (e.g. RPR Harpe) instead of going idle. The lenient
 *  ceiling swaps melee GCDs for the filler here; strict and the delivered
 *  side never see it. `nCasting` / `nTotal` is the worst-case agreement. */
export type RangedFillerWindow = {
  startSec: number;
  endSec: number;
  nCasting: number;
  nTotal: number;
};

/** Forced "melee downtime" credited onto the STRICT / rank ceiling: the potency
 *  (and % of the pre-credit ceiling) that came off because the player was forced
 *  out of melee into their ranged filler, over consensus-confirmed windows
 *  self-limited to the player's OWN disengages (a melee-stayer is never credited,
 *  and the credit never drops the ceiling below delivered, so ≤100% holds).
 *  Unlike `RangedFillerWindow` (lenient, uniform), this moves the headline/rank
 *  number. Present only when credited (jobs with a `ranged_filler_id`, e.g. RPR). */
export type MeleeDowntime = {
  /** Potency removed from the strict ceiling. */
  potency: number;
  /** That potency as a % of the pre-credit strict ceiling. */
  pct: number;
  /** The player's own forced sub-windows the credit was applied over. */
  windows: { startSec: number; endSec: number }[];
};

/** One player-death window. `timeSec` is the moment of death (click target
 *  for the Timeline); `durationSec` is how long the player stayed dead
 *  (until they resumed casting). Distinct from downtime — death is the
 *  player's fault and is priced as its own Improvement card. */
export type DeathWindow = {
  timeSec: number;
  durationSec: number;
};

/** One confirmed multi-target window the splash credit was applied over.
 *  `targetCount` (N) is the modal number of targets the references' splash
 *  casts hit inside it. `deliveredSplash` / `ceilingSplash` are the potency the
 *  window contributed to each side — so denying a window in the trim UI can
 *  recompute efficiency two-sidedly. The `ref*` arrays are each reference's own
 *  per-window deltas (indexed like `AnalysisResult.refs`), and
 *  `refAvgDeliveredSplash` their mean delivered splash — together they let the
 *  crediting modes (see state/multiTargetModes.ts) recompute both your and the
 *  displayed reference efficiencies fully client-side. */
export type MultiTargetWindow = {
  startSec: number;
  endSec: number;
  targetCount: number;
  deliveredSplash: number;
  ceilingSplash: number;
  refDeliveredSplash?: number[];
  refCeilingSplash?: number[];
  refAvgDeliveredSplash?: number;
  /** Advisory position-derived feasibility for THIS pull (backend
   *  jobs/_core/cleave_geometry.py): could a target-centered cleave ever have
   *  hit a second enemy here? Renders as a chip; 'unreachable' auto-defaults
   *  the window to Not possible (user-overridable). Absent when the position
   *  fetch failed or produced thin evidence — never affects credit math. */
  cleaveGeometry?: {
    verdict: 'reachable' | 'unreachable' | 'unknown';
    detail?: string;
  };
};

/** One tincture (Medicated) window the player popped — drawn as a back-layer
 *  zone on the Timeline. `multiplier` is the per-window damage factor (~1.08,
 *  job-dependent) for the hover. */
export type TinctureWindow = {
  startSec: number;
  endSec: number;
  multiplier: number;
};

/** One situational window the user confirms or denies on the dashboard
 *  (currently the MCH Flamethrower downtime-edge squeeze). `id` is the stable
 *  key the denied-set is keyed on AND matches the time of the corresponding cast
 *  on the idealized lane; `abilityId` + `timeSec` let the timeline locate that
 *  cast. `potency` is removed from the relevant side(s) when denied. */
export type ReviewableWindow = {
  id: string;
  timeSec: number;
  abilityId?: number;
  potency: number;
  title: string;
  detail?: string;
};

/** A group of same-kind reviewable windows. `kind` selects the frontend prose /
 *  icon (src/views/reviewableWindows). `side` says which efficiency side a
 *  denial adjusts: 'ceiling' (the squeeze was only ever an assumed ceiling gain,
 *  e.g. Flamethrower) or 'both' (it counted on delivered too). The multi-target
 *  windows ride their own headline path; this covers the ceiling-only squeezes
 *  that used to be hardcoded as Flamethrower in the shared views. */
export type ReviewableWindowGroup = {
  kind: string;
  side: 'ceiling' | 'both';
  windows: ReviewableWindow[];
};

/** One over-ceiling subject inside a CeilingAnomaly ('you' or a reference). */
export type CeilingAnomalyEntry = {
  who: 'you' | 'ref';
  label: string;
  effPct: number;
  effLenientPct: number;
};

/** The ceiling-invariant watchdog's headline stamp: someone in this analysis
 *  scored over 100% efficiency, which the ceiling construction says is a
 *  modeling bug. Drives the dashboard's "please submit this" nudge; the full
 *  detail (potencies, downtime source…) is in the sidecar event log the
 *  feedback bundle ships. Present only when tripped (like meleeDowntime). */
export type CeilingAnomaly = {
  maxEffPct: number;
  entries: CeilingAnomalyEntry[];
  job: string;
  encounterId: number;
  encounterName: string;
  reportCode: string;
  fightId: number;
};

export type HeadlineKPIs = {
  percentile: number;
  rank: { you: number; total: number };
  beat: { count: number; of: number };
  effectiveGcdSec: number;
  yourPotency: number;
  /** Idealized potency, strict (Tier A only). Used as the primary
   *  denominator for `efficiencyPct`. */
  yourIdealizedPotency: number;
  /** Idealized potency, lenient (Tier A + Tier B consensus). Always
   *  <= strict; equal when no Tier B windows were produced. */
  yourIdealizedPotencyLenient: number;
  refAvgPotency: number;
  refAvgIdealizedPotency: number;
  /** Primary efficiency (strict). */
  efficiencyPct: number;
  /** Alias of efficiencyPct. */
  efficiencyPctStrict: number;
  /** Efficiency with consensus-suspected forced downtime excluded —
   *  >= strict. Shown as a secondary badge when the delta is notable. */
  efficiencyPctLenient: number;
  /** Delivered potency scored under the raid buffs actually present. */
  deliveredObserved: number;
  /** Idealized ceiling under the buffs actually present (the fair,
   *  player-accountable ceiling). */
  idealizedObserved: number;
  /** Idealized ceiling assuming party buffs on the perfect 2-min cadence. */
  idealizedMaster: number;
  /** Efficiency vs the buffs you actually got — "given your circumstances". */
  efficiencyPctObserved: number;
  /** Efficiency vs the party-perfect ceiling. Below observed when the
   *  party's buffs were late/short (context, not the player's fault). */
  efficiencyPctMaster: number;
  refEfficiencyPct: number;
  killTimeSec: number;
  refKillTimeSec: number;
  /** Observed party composition (FFLogs job names) — seeds the Kill Time
   *  Theorizer's comp selector with the comp this pull actually had. */
  partyJobs?: string[];
  /** Where Tier A windows came from. "targetability" = confirmed via
   *  boss `targetabilityupdate` events. "fallback_heuristic" = no
   *  targetability data, so we fell back to a conservative cast-gap
   *  heuristic. */
  downtimeSource: 'targetability' | 'fallback_heuristic';
  downtimeTierA: DowntimeTierAWindow[];
  downtimeTierB: DowntimeTierBWindow[];
  /** High-confidence (near-unanimous consensus) sub-cores of Tier B — the
   *  genuinely-forced stretches the idealized rotation skips and that are never
   *  scored against the player. A subset of `downtimeTierB`, trimmed to where the
   *  ref pool truly agrees. The Timeline renders these as a firmer band and the
   *  remaining "ambiguous" Tier-B time as the lighter suspected hatch. */
  downtimeTierBHigh: DowntimeTierBWindow[];
  /** Consensus ranged-filler windows (lenient ceiling only). Empty for
   *  jobs without a `ranged_filler_id` or when no consensus formed. */
  rangedWindows: RangedFillerWindow[];
  /** Forced melee-downtime credited onto the strict/rank ceiling (potency + %
   *  of the pre-credit ceiling). Present only when credited — see MeleeDowntime. */
  meleeDowntime?: MeleeDowntime;
  /** Player deaths in this pull (death → resurrection). Empty for clean
   *  pulls. Drives the headline "Deaths" indicator; each death is also a
   *  priced card in `improvements` (kind "death"). */
  deaths: DeathWindow[];
  /** Total potency attributed to deaths — the sum of the kind="death"
   *  Improvement cards, so the headline number matches the panel. */
  deathsLostPotency: number;
  /** True when this pull has substantial multi-target output (>= 2 enemies
   *  simultaneously targetable for a sustained span) AND the splash credit
   *  could NOT be confirmed by ref consensus — so the efficiency is the
   *  understated single-target number. False once credited (or single-target).
   *  The improvements panel is suppressed on any substantially multi-target
   *  pull regardless, since the sim-diff is single-target-based. */
  multiTargetDisclaimed?: boolean;
  /** True when splash was credited on both delivered and the ceiling over the
   *  confirmed windows — the efficiency above is then a fair multi-target
   *  number, not the single-target understatement. */
  multiTargetCredited?: boolean;
  /** The confirmed multi-target windows the splash credit was applied over
   *  (empty unless credited). Surfaced for the WindowReview trim UI. */
  multiTargetWindows?: MultiTargetWindow[];
  /** Someone here scored over 100% efficiency (a modeling bug worth a user
   *  report) — the dashboard nudge. Absent on clean runs. */
  ceilingAnomaly?: CeilingAnomaly;
  /** Healer runs: hide the rank/percentile-vs-refs headline and the
   *  counts-vs-ref-median comparison — top-parsing healers force DPS into
   *  heal windows, so neither is a fair reference. Refs still power the
   *  Tier-B downtime consensus and the reference timeline lanes. */
  rankSuppressed?: boolean;
  /** True when the mitigation plan's heal GCDs were LOCKED into the ceiling —
   *  the "honest maximum" that already pays the healing tax. Exceeding it
   *  (efficiency > 100%) means planned healing was sacrificed for damage and
   *  is framed, not flagged as a ceiling anomaly. Absent on non-healer runs
   *  and when the plan couldn't be built (analysis then ran unlocked). */
  healLocksApplied?: boolean;
  /** Planned heal GCDs locked into the ceiling (count / lost DPS potency). */
  healLockCount?: number;
  healLockPotency?: number;
  /** The comp the plan was built with (shield, regen, tanks, dps) and where
   *  it came from: the pull's actors, the planner's user-adjusted override,
   *  or the planner defaults. */
  mitPlanComp?: string[];
  mitPlanCompSource?: 'pull' | 'override' | 'defaults';
  mitPlanWarnings?: string[];
  /** Prog (wipe) pull — the scored window was truncated at the player's
   *  terminal death, and rank/percentile/vs-refs comparisons are unfair
   *  against kill references (hide them like rankSuppressed, which stays a
   *  separate flag because it implies the healer heal-lock framing).
   *  `killTimeSec` is the SCORED (truncated) duration; the full pull length
   *  lives in `pullDurationSec`. Absent on kills. */
  isProgPull?: boolean;
  /** Full wipe duration (pull start → wipe), independent of truncation. */
  pullDurationSec?: number;
  /** FFLogs fightPercentage: phase-weighted % of the whole fight remaining. */
  fightPercentage?: number;
  /** Current boss's HP % remaining at the wipe (display-only). */
  bossPercentage?: number;
  /** Phase the wipe happened in (absent when unphased/unknown). */
  lastPhase?: number;
  /** When the scored window was truncated: the terminal death (seconds into
   *  the pull). Absent if the player was alive/acting at the wipe. */
  terminalDeathSec?: number;
  /** Projected kill time from the party's output up to the wipe point
   *  (active-rate model + the reference kill's remaining downtime). Absent
   *  when no honest projection exists (no fightPercentage / no refs /
   *  negligible progress) — show pullDurationSec only. */
  projectedKillTimeSec?: number;
  projectionMeta?: {
    method: string;
    refCount: number;
    /** The reference kill whose beyond-the-wipe downtime was credited. */
    refKillSec: number;
    /** The wipe's active seconds (duration minus its own Tier-A downtime). */
    activeSec: number;
    /** Ref downtime credited past the wipe point. */
    downtimeBeyondSec: number;
    /** % of the whole fight the party burned before wiping. */
    burnedPct: number;
  };
};

/** Verbatim serialization of AspectComparison. */
export type AspectComparisonJson = {
  aspectName: string;
  /** Each is already "[tag] description ... ≈ -800p" — UI tags + tints. */
  findings: string[];
  detailColumns: string[];
  yourDetailRows: (string | number | boolean | null)[][];
  /** Parallel to yourDetailRows; hex tint or null. */
  yourDetailRowColors: (string | null)[];
  summaryLines: string[];
};

// --- Per-aspect structured state -------------------------------------------

export type AbilitiesState = {
  abilityCounts: Record<number, number>;
  totalCasts: number;
};

export type DriftFinding = {
  abilityId: number;
  abilityName: string;
  casts: number;
  cappedSeconds: number;
  lostCasts: number;
  lostPotency: number;
  cdrOverflowSeconds: number;
  /** Casts of consumers sharing this ability's charge pool (e.g.
   *  MCH Bioblaster on Drill). Was `bioblasterConsumed: boolean` in
   *  the pre-split contract. */
  sharedConsumers: number;
};

/** Output of `_aspects/clipping.py::ClippingFinding`. Splits GCD-gap excess
 *  into *time spent idle* (gaps the pilot left empty) and true *GCD clipping*
 *  (an oGCD over-weave that pushed the next GCD late) — separately priced. */
export type ClippingFinding = {
  effectiveGcdSec: number;
  avgGcdPotency: number;
  // Time spent idle.
  totalIdleSec: number;
  idleLostGcds: number;
  idleLostPotency: number;
  /** (timeSec, idleSeconds) for the worst pairs, desc. */
  worstIdle: [number, number][];
  // True GCD clipping (over-weaving).
  totalClipSec: number;
  clipLostGcds: number;
  clipLostPotency: number;
  /** (timeSec, clipSeconds, nOgcdsInPair) for the worst pairs, desc. */
  worstClips: [number, number, number][];
};

export type OvercapFinding = {
  /** Job-defined gauge name (e.g. "heat" / "battery" for MCH, "soul" / "shroud"
   *  for RPR). The backend emits one finding per `JobData.gauges` entry, so this
   *  is an open string — never narrow it to one job's gauges. */
  gauge: string;
  timeSec: number;
  abilityId: number;
  abilityName: string;
  wasted: number;
  lostPotency: number;
};

export type AlignmentFinding = {
  kind: string;
  timeSec: number;
  summary: string;
  lostPotency: number;
};

export type OpenerFinding = {
  position: number;
  expectedId: number;
  actualId: number;
  summary: string;
  lostPotency: number;
};

/** Output of a job's `Scoring` aspect (e.g. `jobs/machinist/scoring.py`,
 *  `jobs/reaper/scoring.py`). Jobs without a Scoring aspect emit no efficiency,
 *  so the dashboard renders '—'. The shape is job-agnostic; per-job potency math
 *  lives behind it. */
export type ScoringState = {
  deliveredPotency: number;
  idealizedPotency: number;
  /** Alias of `idealizedPotency` (strict — Tier A only). */
  idealizedStrict: number;
  /** Idealized with Tier A + Tier B consensus windows. Filled in by the
   *  sidecar post-ref-fetch; equals strict when no Tier B windows. */
  idealizedLenient: number;
  queenBatterySpent: number;
  downtimeWindows: [number, number][];
  downtimeSource: 'targetability' | 'fallback_heuristic';
  downtimeTierB: DowntimeTierBWindow[];
  /** Canonical (master) raid-buff multiplier timeline — `(start, end, mult)`
   *  segments with the opener phased per provider (~3rd GCD). The sidecar uses
   *  it to render the buff-aware Idealized Timeline lane; the frontend doesn't
   *  read it directly. Empty when no providers were present. */
  masterBuffIntervals?: [number, number, number][];
  fightDurationSec: number;
};

/** Output of `jobs/machinist/reassemble.py::ReassembleAspect`. */
export type ReassembleAspectState = {
  findings: AlignmentFinding[];
};

export type QueenCastJson = {
  timeSec: number;
  bucket: number;
  battery: number;
  petDamage: number;
  durationSec: number;
  finished: boolean;
};

export type QueenState = {
  queens: QueenCastJson[];
  totalQueenDamage: number;
};

export type WildfireWindowJson = {
  castTimeSec: number;
  hits: number;
  bucket: number;
};

export type WildfireState = {
  windows: WildfireWindowJson[];
};

export type HyperchargeWindowJson = {
  castTimeSec: number;
  hits: number;
  bucket: number;
  /** Window curtailed by the kill / downtime / death — underfill not blamed. */
  cutShort: boolean;
  /** Cast time of the last Blazing Shot in the window (drives the bar width). */
  lastShotSec: number;
};

export type HyperchargeState = {
  windows: HyperchargeWindowJson[];
};

export type ToolsState = Record<string, never>; // placeholder — backend exposes none yet

// --- Reaper-specific states -----------------------------------------------

/** Output of `jobs/reaper/death_design.py::DeathsDesignAspect`. Measured
 *  Death's Design uptime on the boss + the 10% amp lost to its downtime. */
export type DeathsDesignState = {
  coveragePct: number;
  coveredUptimeS: number;
  uptimeS: number;
  uncoveredWindows: [number, number][];
  lostPotency: number;
};

/** Output of `jobs/reaper/positionals.py::PositionalAspect` (only present when
 *  the live probe confirmed FFLogs exposes the positional bonus byte). */
export type PositionalsState = {
  detected: boolean;
  total: number;
  missed: number;
  missedByAbility: Record<number, number>;
  missTimes: number[];
  lostPotency: number;
};

/** Output of `jobs/redmage/procs.py::ProcsAspect` — Verfire/Verstone proc
 *  utilization. A wasted proc (overwritten before spending, or expired) costs
 *  the small premium of the Verfire/Verstone over its Jolt III filler, so
 *  `lostPotency` is low by nature; the counts are the story. */
export type ProcsState = {
  verfireGrants: number;
  verfireUsed: number;
  verfireWasted: number;
  verstoneGrants: number;
  verstoneUsed: number;
  verstoneWasted: number;
  overwrites: number;
  totalGrants: number;
  totalUsed: number;
  totalWasted: number;
  utilizationPct: number;
  lostPotency: number;
};

// --- Split-aspect states (per the job-generic refactor) -------------------

/** Output of `_aspects/drift.py::DriftAspect`. */
export type DriftAspectState = {
  findings: DriftFinding[];
  downtimeWindows: [number, number][];
  fightDurationSec: number;
};

/** Output of `_aspects/clipping.py::ClippingAspect`. */
export type ClippingAspectState = {
  clipping: ClippingFinding | null;
};

/** Output of `_aspects/overcap.py::OvercapAspect`. */
export type OvercapAspectState = {
  findings: OvercapFinding[];
};

/** Output of `_aspects/opener.py::OpenerAspect`. */
export type OpenerAspectState = {
  findings: OpenerFinding[];
};

/** Output of `_aspects/alignment.py::AlignmentAspect`. */
export type AlignmentAspectState = {
  findings: AlignmentFinding[];
};

/** One party raid-buff timing note. `kind` is "missing" (a provider's buff
 *  never landed) or "gap" (it landed late / fell off early). Pure CONTEXT —
 *  describes the PARTY's buffs, never the analyzed player's mistakes.
 *  `summary` is already human-readable and suffixed with "(context)". */
export type BuffDriftFinding = {
  kind: string;
  provider: string;
  timeS: number;
  summary: string;
};

/** Output of the BuffDrift aspect. Party raid-buff timing context. */
export type BuffDriftAspectState = {
  findings: BuffDriftFinding[];
};

/** Discriminated union — name (the aspect's key) tells you which shape. */
export type AspectStateJson =
  | AbilitiesState
  | ScoringState
  | ReassembleAspectState
  | QueenState
  | WildfireState
  | HyperchargeState
  | ToolsState
  | DeathsDesignState
  | PositionalsState
  | ProcsState
  | DriftAspectState
  | ClippingAspectState
  | OvercapAspectState
  | OpenerAspectState
  | AlignmentAspectState
  | BuffDriftAspectState;

// --- Potential Improvements (sim-diff) -------------------------------------

/** One concrete, located, actionable suggestion derived from diffing the
 *  player's casts against the job's idealized sim timeline. `timeSec` is
 *  where the missed cast belonged — the UI links it to the timeline.
 *  Output of `python/jobs/_core/improvements.py::Improvement`. Empty for
 *  jobs without a simulator. */
export type Improvement = {
  /** Loss category. Priced cards: "death" (idealized rotation lost while the
   *  player was dead — usually the dominant loss), "missed_cast" (sim-diff),
   *  "missed_enabler" (skipped Hypercharge/Wildfire/… priced at sim-derived
   *  net value), "wildfire" (a Wildfire window underfilled, (6−hits)×240p),
   *  "hypercharge" (a Hypercharge window that fired <5 Blazing Shots, priced at
   *  the sim-derived per-shot value), "idle" (time spent idle — GCD gaps the
   *  pilot left empty), "clip" (true GCD
   *  clipping from over-weaving), "overcap", "align" (Reassemble misuse),
   *  "lifesurge" (DRG Life Surge on a non-optimal GCD),
   *  "drift" (sim-less fallback), and "residual" (the un-itemized remainder that
   *  makes the panel sum to the measured gap). Zero-priced *diagnostics*
   *  (`lostPotency <= 0`): "opener" (ordering note) and below-floor enabler
   *  misses. */
  kind: string;
  abilityId: number;
  abilityName: string;
  /** Where it belonged — seconds into the fight. Click target for Timeline.
   *  `<= 0` for aggregate / note items (drift, residual) → non-clickable.
   *  Exception: kind "opener" is genuinely located at 0.0 (the pull start) and
   *  the UI treats it as clickable, jumping to 0:00. */
  timeSec: number;
  /** Direct-damage opportunity cost in potency (net of the filler that
   *  backfills a missed GCD). `0` for diagnostics, which point without pricing. */
  lostPotency: number;
  summary: string;
  /** Optional breakdown for aggregate cards (grouped "×N" rows, the idle / clip
   *  totals, and the "Other" residual). Each child is a located, individually-
   *  priced contributor the UI reveals in an expandable dropdown. Absent/empty
   *  for leaf cards. */
  children?: Improvement[];
};

// --- Top-level Analysis response -------------------------------------------

export type AnalysisResult = {
  you: RunSummary;
  refs: RunSummary[];
  headline: HeadlineKPIs;
  /** Unified, located, grouped suggestions ranked by lost potency (desc).
   *  Spans every loss category (sim-diff missed casts + clip/overcap/align/
   *  opener; drift stands in for jobs without a simulator). */
  improvements: Improvement[];
  /** Where the sim would pot on the idealized lane — best placement by potency
   *  density. Drawn on the Sim lane (its own pot timing). [] when no tincture. */
  idealizedTinctureWindows?: TinctureWindow[];
  /** The job's idealized perfect-sim cast timeline, as a comparison lane for
   *  the Timeline view. [] when the job has no simulator. This is the
   *  throughput-OPTIMAL line (burst on the sim's best cadence). */
  idealizedTrack: CastEvent[];
  /** Alternate idealized lane: the CANONICAL 'hold burst for the 2-minute
   *  window' line — the job's burst enablers cast inside the raid-buff windows
   *  so their payloads are buffed (for MCH that's Wildfire + Barrel Stabilizer).
   *  The Timeline lets the player toggle between this and `idealizedTrack`. []
   *  when no party buffs were present (then it's identical to optimal and the
   *  toggle is hidden). Optional so the mock client (and any pre-feature
   *  payload) need not supply it. */
  idealizedTrackCanonical?: CastEvent[];
  /** The SINGLE-TARGET idealized lane — the same display sim WITHOUT the
   *  multi-target schedule. Only populated on a credited multi-target pull (else
   *  []/absent, since it would equal `idealizedTrack`). The Timeline caches it
   *  alongside `idealizedTrack` and splices it into any multi-target window the
   *  user marks "not possible", so the idealized lane visibly reverts to
   *  single-target there without a re-sim. */
  idealizedTrackStrict?: CastEvent[];
  /** Situational ceiling squeezes the user can confirm/deny (MCH Flamethrower
   *  today). One generic WindowReview is rendered per group; denying a window
   *  drops its potency from the ceiling. Optional — absent for jobs/pulls with
   *  none, and from the mock client. */
  reviewableWindows?: ReviewableWindowGroup[];
  comparisons: Record<string, AspectComparisonJson>;
  /** Keyed by aspect name. Specific UIs cast to the right shape. */
  aspectStates: Record<string, AspectStateJson>;
  /** id → metadata for every ability that appears in tracks / counts / drift. */
  abilityMeta: Record<number, AbilityMetaJson>;
  /** Boss phase segments (phasic analysis). Present ONLY on phased fights
   *  (ultimates and multi-phase encounters); absent for single-phase Savage
   *  pulls. Drives the Timeline phase bands and the prog "which phase you
   *  wiped in" framing. Ordered by start time. */
  phases?: PhaseInfoJson[];
  /** Per-phase execution metrics + ref-pattern deviation callouts (the phasic
   *  dashboard panel). Present only alongside `phases`. */
  phaseAnalysis?: PhaseAnalysisJson;
};

/** median / p25 / p75 of a per-phase ref metric. */
export type Stat3 = { median: number; p25: number; p75: number };

/** Per-phase analysis: the subject's execution, the ref medians, and the
 *  "you're saving/spending abnormally vs the top clears" callouts. */
export type PhaseAnalysisJson = {
  user: {
    phaseId: number;
    partial: boolean;
    activeSec: number;
    gcdCasts: number;
    totalCasts: number;
    /** Delivered potency inside this phase (same scorer as the headline). */
    deliveredPotency: number;
    gauges: {
      name: string; entry: number; exit: number;
      generated: number; spent: number; overcapped: number;
    }[];
    potUsed: boolean;
  }[];
  refs: {
    phaseId: number;
    refCount: number;
    gcdCasts: Stat3;
    gcdRate: Stat3;
    deliveredPotency: Stat3;
    gauges: { name: string; exit: Stat3; overcapped: Stat3; spent: Stat3; generated: Stat3 }[];
    /** Fraction of refs that popped a tincture in this phase (0–1). */
    potPct: number;
    /** Abilities where the subject's cast count deviates from the ref median
     *  by ≥ 2 (most-deviant first). */
    notableCasts: { abilityId: number; yourCasts: number; refMedian: number }[];
  }[];
  deviations: {
    phaseId: number;
    kind: 'gauge_exit' | 'overcap_phase' | 'pot_phase' | 'gcd_pace' | 'potency_low';
    gauge?: string;
    abilityId?: number;
    yourValue: number;
    refValue: number;
    text: string;
  }[];
};

/** One boss phase in the analyzed pull, in fight-relative seconds. */
export type PhaseInfoJson = {
  /** FFLogs phase id (stable within an encounter; also the aggregation key
   *  for per-phase ref comparison). */
  id: number;
  /** Human name, e.g. "P4: Kefka Says" (falls back to "P{id}"). */
  name: string;
  startSec: number;
  endSec: number;
  isIntermission: boolean;
  /** Tier-A downtime seconds overlapping this phase (boss untargetable). */
  downtimeSec: number;
  /** The subject reached this phase (its start is within the full pull/wipe
   *  span). Always true on a kill. */
  reached: boolean;
  /** The phase completed within the SCORED window (terminal-death-clamped on a
   *  wipe). A wipe's final, partial phase is reached but not completed. */
  completed: boolean;
};

// --- Kill Time Theorizer ---------------------------------------------------

/** One point on the ideal-potency-vs-killtime spread the theorizer samples
 *  across a narrow (~7s) band around the entered target. */
export type TheorizeSample = {
  killSec: number;
  idealizedPotency: number;
};

/** Result of `theorize_kill_time` — the sim's ideal output + cast timeline for a
 *  hypothetical kill time, under the pull's (clipped) downtime and a chosen
 *  party comp's raid buffs. `unsupported` is true for jobs without a simulator
 *  (the card is hidden in that case, so it's a defensive fallback). */
export type TheorizeResult = {
  unsupported?: boolean;
  targetKillSec: number;
  idealizedPotency: number;
  /** The ideal rotation's cast lane for the target (a single timeline lane). */
  timeline: CastEvent[];
  /** Observed downtime clipped to the target — drawn as bands on the lane. */
  downtimeWindows: DowntimeTierAWindow[];
  /** Modeled raid-buff windows for the chosen comp — drawn as bands. */
  buffWindows: TinctureWindow[];
  /** Optimal tincture placement on the ideal lane (empty for non-potting jobs). */
  tinctureWindows: TinctureWindow[];
  /** Ideal potency across the ~7s band (1s grid, incl. the target). */
  samples: TheorizeSample[];
  /** Metadata for any ability id in `timeline` (self-contained). */
  abilityMeta: Record<number, AbilityMetaJson>;
  /** Where the downtime came from: "references" (derived from this encounter's
   *  top logs), "none" (no refs available — pure uptime), or "explicit". */
  downtimeSource: 'references' | 'none' | 'explicit';
  /** Number of reference logs the downtime was derived from. */
  refCount: number;
  /** Kill time (s) of the reference the downtime was taken from (closest to the
   *  target) — shown in the "based on N refs (closest kill m:ss)" disclosure. */
  refKillTimeSec: number;
  /** Average kill time (s) across the references — a benchmark for the target. */
  refAvgKillSec: number;
  /** That reference's party composition (FFLogs job names) — surfaced as a
   *  "top players ran…" hint for the comp picker. */
  refPartyJobs: string[];
};

/** Per-role amounts (HP units from the source logs' gear). */
export type RoleAmounts = { tank: number; healer: number; dps: number };

export type MitAssignment = {
  /** Party slot: 'T1' | 'T2' | 'H1' | 'H2' | 'D1'..'D4'. */
  slot: string;
  job: string;
  actionId: number;
  name: string;
  castAtSec: number;
  durationSec: number;
  target: 'party' | 'self' | 'tank' | 'enemy';
  /** Effective % vs this mechanic's damage school (0..1). */
  mitPct: number;
  /** Per-target HP absorbed / restored (0 when N/A). */
  shieldAmount: number;
  healAmount: number;
  hotHps: number;
  isGcd: boolean;
  castTimeSec: number;
  /** Tank personals / invulns — feasibility-checked but rendered as advice. */
  isSuggestion: boolean;
  /** Hit times (sec) this cast covers within its mechanic. */
  covers: number[];
  /** Cast for an earlier mechanic; its duration also blankets this one
   *  (mit% credited, shield assumed spent). Rendered dimmed. */
  isCarryover: boolean;
};

export type MitGcdHeal = {
  slot: string;
  job: string;
  actionId: number;
  name: string;
  castAtSec: number;
  count: number;
  castTimeSec: number;
  /** Party-wide HP restored per cast. */
  healAmount: number;
};

export type MitMechanic = {
  /** `${bossAbilityId}#${ordinal}` — links a damage marker to its card. */
  id: string;
  timeSec: number;
  endSec: number;
  name: string;
  bossAbilityIds: number[];
  /** `hpSet` = the mechanic SETS the party's HP to ~1 (unmitigable; detected
   *  from the healing stream — it emits no damage events). */
  kind: 'raidwide' | 'tankbuster' | 'bleed' | 'multiHit' | 'other' | 'hpSet';
  school: 'physical' | 'magical' | 'special' | 'mixed' | 'unknown';
  /** Per-burst profile (trains/bleeds); single entry otherwise. */
  hits: { timeSec: number; unmitigated: RoleAmounts }[];
  /** Median per-PERSON total across the instance (what one hit player takes). */
  unmitigated: RoleAmounts;
  unmitigatedP90: RoleAmounts;
  /** 1 − median(multiplier) across the source logs — context only. */
  observedMitPct: number;
  presenceRatio: number;
  tankTargets: number;
  assignments: MitAssignment[];
  gcdHeals: MitGcdHeal[];
  /** Post-plan damage per role (after mit + shields). */
  predicted: RoleAmounts;
  /** Simulated HP entering the next gap (HP-sweep output). */
  hpAfter: RoleAmounts;
  status: 'covered' | 'tight' | 'uncovered';
  notes: string[];
};

export type MitPlanLane = {
  slot: string;
  job: string;
  label: string;
  casts: CastEvent[];
};

export type MitDamageMarker = {
  mechanicId: string;
  timeSec: number;
  endSec: number;
  name: string;
  kind: MitMechanic['kind'];
  school: MitMechanic['school'];
  status: MitMechanic['status'];
  /** Party-wide unmitigated total (severity sizing for the marker). */
  unmitTotal: number;
};

/** Result of `plan_mitigation` — the encounter's forced-damage timeline
 *  (aggregated + voted across top kill logs) and the deterministic
 *  mitigation/recovery plan for the chosen duo + comp. */
export type MitPlanResult = {
  encounterId: number;
  encounterName: string;
  shieldHealer: string;
  regenHealer: string;
  /** Resolved comp in slot order T1,T2,H1,H2,D1..D4. */
  partyJobs: string[];
  /** Median kill time of the source logs — the timeline's extent. */
  modelKillSec: number;
  refCount: number;
  refAvgKillSec: number;
  /** Damage instances excluded as avoidable (inconsistent across logs). */
  avoidableCount: number;
  /** Max-HP assumptions used (median from logs, or authored constants). */
  roleHp: RoleAmounts;
  hpSource: 'logs' | 'constants';
  summary: {
    mechanicCount: number;
    raidwideCount: number;
    tankbusterCount: number;
    bleedCount: number;
    multiHitCount: number;
    coveredCount: number;
    tightCount: number;
    uncoveredCount: number;
    gcdHealCount: number;
    gcdHealTimeSec: number;
    gcdHealPotencyLost: number;
    totalUnmitigated: number;
    totalPredicted: number;
  };
  mechanics: MitMechanic[];
  lanes: MitPlanLane[];
  damageMarkers: MitDamageMarker[];
  /** No-enemy-targetable stretches (context bands on the timeline). */
  downtimeWindows: DowntimeTierAWindow[];
  /** Metadata for every plan-action id in `lanes`/`assignments`. */
  abilityMeta: Record<number, AbilityMetaJson>;
  warnings: string[];
  /** Where the planned comp came from: resolved from a pull's actors
   *  (healer-flow preselection), the request's explicit comp, or defaults. */
  compSource?: 'pull' | 'request' | 'defaults';
  /** Substitutions made resolving a non-standard pull comp (double-shield /
   *  double-regen duos, missing players). */
  compWarnings?: string[];
  /** True when `usePfMitPlan` was requested AND a premade plan actually applied
   *  (ultimate + a shipped premade/<id>.json). False ⇒ fell back to the auto
   *  plan. Any PF match / comp-mismatch notes ride `warnings`. */
  pfPlanApplied?: boolean;
};

/** The healer duo + party the mitigation plan is built for — also the comp
 *  override `runAnalysis` carries so the locked ceiling matches the plan the
 *  user reviewed in the planner. */
export type MitCompSelection = {
  shieldHealer: string;
  regenHealer: string;
  tanks: string[];
  dps: string[];
};

export type MitPlanArgs = {
  encounterId: number;
  /** Explicit comp (the planner's selectors). When present, pull resolution
   *  is skipped. */
  shieldHealer?: string;
  regenHealer?: string;
  tanks?: string[];
  dps?: string[];
  /** Resolve the comp from this pull's actors instead (the healer-flow
   *  preselection). Only consulted when no explicit comp is given. */
  reportCode?: string;
  fightId?: number;
  /** The analyzed player's job — kept in their own slot when a non-standard
   *  duo forces a substitution. */
  spec?: string;
  /** Use the encounter's hand-authored premade ("PF") plan (ultimates only)
   *  instead of the auto-derived one. */
  usePfMitPlan?: boolean;
};

export interface Sidecar {
  /** Current FFLogs auth mode — the UI gates the launch ref-warm and
   *  SetupView on `mode !== 'none'`. */
  getAuthStatus(): Promise<AuthStatus>;
  /** Start a PKCE sign-in: the sidecar binds a loopback listener and returns
   *  the authorize URL for the frontend to open in the default browser.
   *  A new begin cancels any prior in-flight attempt. */
  fflogsAuthBegin(): Promise<AuthBeginResult>;
  /** Poll the in-flight sign-in (the UI loops ~1.5s until non-pending). */
  fflogsAuthPoll(): Promise<AuthPollResult>;
  /** Abandon the in-flight sign-in (modal closed). */
  fflogsAuthCancel(): Promise<void>;
  /** Delete the persisted sign-in; returns the resulting auth status
   *  (falls back to client_credentials when config.json has dev creds). */
  fflogsLogout(): Promise<AuthStatus>;
  /** Characters claimed on the signed-in FFLogs account (user mode; empty
   *  in client-credentials mode) — the character picker's quick list. */
  listUserCharacters(): Promise<UserCharactersResult>;
  lookupCharacter(name: string, server: string, region: Region, spec: string): Promise<LookupResult>;
  listEncounters(lodestoneId: number, zoneId: number, spec: string): Promise<{ id: number; name: string; totalKills: number; bestParsePct: number | null }[]>;
  listPulls(lodestoneId: number, encounterId: number, spec: string): Promise<{ reportCode: string; fightId: number; startTimeMs: number; durationS: number; parsePct: number; dps: number; label: string }[]>;
  /** In-progress (wipe) pulls on an encounter — lazy prog-log discovery.
   *  Pass lodestoneId to scan the character's recent reports, or reportCode
   *  (a bare 16-char code) to list one pasted report's wipes. */
  listProgPulls(args: { lodestoneId?: number; reportCode?: string; encounterId: number; spec: string }): Promise<ProgPullsResult>;
  /** One round trip for the whole SetupView: the tier's encounters + every
   *  encounter's pulls (supersedes listEncounters + per-encounter listPulls). */
  listSetup(lodestoneId: number, spec: string): Promise<SetupData>;
  /** Supported jobs × tier encounters — used to build the reference warm-cache matrix. */
  getCatalog(): Promise<Catalog>;
  /** Size + cap of the on-disk FFLogs response cache — the status-bar footer
   *  stat and the Settings slider's current value. */
  cacheStats(): Promise<CacheStats>;
  /** Persist a new cache size cap (MB) and evict down to it immediately. */
  setCacheCap(capMb: number): Promise<CacheStats>;
  /** Delete every cached FFLogs response (the footer's Clear button). */
  clearCache(): Promise<CacheStats>;
  /** Fire-and-forget: append one frontend event to the sidecar's event log
   *  (lands with a `ui.` category prefix). Callers must swallow rejections —
   *  use src/log.ts::logEvent rather than calling this directly. */
  logEvent(level: LogLevel, cat: string, msg: string,
           data?: Record<string, unknown>): Promise<void>;
  /** The event-log tail (oldest first) — the Feedback view's recent-events
   *  list. */
  getRecentEvents(limit?: number): Promise<RecentEventsResult>;
  /** Build the user-submitted diagnostics zip + prefilled GitHub issue text.
   *  Does NOT post anywhere — the UI reveals the zip and opens the browser. */
  exportFeedbackBundle(args: {
    category: FeedbackCategory;
    description?: string;
    analysisContext?: Record<string, unknown>;
  }): Promise<FeedbackBundleResult>;
  /** Warm (and cache) the top-10 reference set for one (job, encounter). Streams
   *  per-task progress like runAnalysis' ref-download phase. */
  prefetchRefs(
    spec: string,
    encounterId: number,
    refsBucket: RefsBucket,
    onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                  meta?: ProgressMeta) => void
  ): Promise<PrefetchResult>;
  /** Top-ranked players for a (job, encounter) — the Research tab's list.
   *  Cheap after a refs warm: both read the same cached rankings blob. */
  listRankings(spec: string, encounterId: number): Promise<RankingEntry[]>;
  runAnalysis(
    reportCode: string,
    fightId: number,
    spec: string,
    encounterId: number,
    refsBucket: RefsBucket,
    playerName?: string,
    onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                  meta?: ProgressMeta) => void,
    /** Healer flow: the planner's (possibly user-adjusted) comp, so the
     *  mit-plan locked ceiling matches the plan the user reviewed. Omitted →
     *  the backend resolves the comp from the pull's actors. */
    comp?: MitCompSelection,
    /** Healer flow (ultimates only): lock the premade ("PF") mit plan into the
     *  ceiling instead of the auto-derived one. */
    usePfMitPlan?: boolean
  ): Promise<AnalysisResult>;
  /** Compute the sim's ideal output + timeline for a theorized kill time. The
   *  encounter's downtime is derived backend-side from its reference logs (no
   *  character/pull needed), so this may stream progress while those load. */
  theorizeKillTime(
    spec: string,
    encounterId: number,
    targetKillSec: number,
    rangeSec: number,
    partyJobs: string[],
    onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                  meta?: ProgressMeta) => void
  ): Promise<TheorizeResult>;
  /** Build the encounter's forced-damage timeline from its top kill logs and
   *  schedule the optimal mitigation/recovery plan for the chosen healer duo
   *  + comp. Streams per-log download progress on a cold damage model; re-runs
   *  with a different duo reuse the cached model and return near-instantly.
   *  With `reportCode`/`fightId` (and no explicit comp) the comp is resolved
   *  from that pull's actors — the healer-flow preselection. */
  planMitigation(
    args: MitPlanArgs,
    onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                  meta?: ProgressMeta) => void
  ): Promise<MitPlanResult>;
}
