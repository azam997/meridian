// Mock sidecar — returns plausible AnalysisResult shaped data so the UI
// works in browser preview (`npm run dev` without Tauri) and in tests.

import type {
  AbilityMetaJson,
  AnalysisResult,
  AppEvent,
  Catalog,
  CastEvent,
  MitAssignment,
  MitMechanic,
  MitPlanLane,
  MitPlanResult,
  PrefetchResult,
  ProgPull,
  ProgPullsResult,
  ProgressTask,
  RankingEntry,
  SetupData,
  Sidecar,
  TheorizeResult,
} from './contract';
import type { Encounter, Pull, Region, RefsBucket } from '../state/appState';
import { APP_VERSION } from '../data/changelog';

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Mutable cache state so the footer stat, Clear button, and Settings slider
// behave plausibly in browser preview.
const mockCache = { totalBytes: 12_582_912, capMb: 15 }; // ~12 MB, default cap

// Session-local event tail so FeedbackView renders plausibly in browser
// preview. Seeded with one ceiling anomaly (the interesting row style).
const mockEvents: AppEvent[] = [
  { t: Date.now() - 90_000, lv: 'info', cat: 'lifecycle', msg: 'sidecar started' },
  { t: Date.now() - 60_000, lv: 'warn', cat: 'ceiling_anomaly',
    msg: 'Machinist MOCK1234#1 exceeded the ceiling',
    data: { job: 'Machinist', reportCode: 'MOCK1234', fightId: 1 } },
  { t: Date.now() - 30_000, lv: 'info', cat: 'analysis', msg: 'run_analysis done',
    data: { job: 'Machinist', durationS: 4.2 } },
];

// --- Ability metadata (mirrors jobs/ability_metadata.py::BUNDLED for MCH) --

const META: AbilityMetaJson[] = [
  { id: 2876,  name: 'Reassemble',         iconPath: '/i/003000/003022.png', isOgcd: true },
  { id: 2878,  name: 'Wildfire',           iconPath: '/i/003000/003018.png', isOgcd: true },
  { id: 7411,  name: 'Heated Split Shot',  iconPath: '/i/003000/003031.png', isOgcd: false },
  { id: 7412,  name: 'Heated Slug Shot',   iconPath: '/i/003000/003032.png', isOgcd: false },
  { id: 7413,  name: 'Heated Clean Shot',  iconPath: '/i/003000/003033.png', isOgcd: false },
  { id: 16498, name: 'Drill',              iconPath: '/i/003000/003043.png', isOgcd: false },
  { id: 16500, name: 'Air Anchor',         iconPath: '/i/003000/003045.png', isOgcd: false },
  { id: 16501, name: 'Automaton Queen',    iconPath: '/i/003000/003501.png', isOgcd: true },
  { id: 17209, name: 'Hypercharge',        iconPath: '/i/003000/003041.png', isOgcd: true },
  { id: 25788, name: 'Chain Saw',          iconPath: '/i/003000/003048.png', isOgcd: false },
  { id: 36978, name: 'Blazing Shot',       iconPath: '/i/003000/003506.png', isOgcd: false },
  { id: 36979, name: 'Double Check',       iconPath: '/i/003000/003507.png', isOgcd: true },
  { id: 36980, name: 'Checkmate',          iconPath: '/i/003000/003508.png', isOgcd: true },
  { id: 36981, name: 'Excavator',          iconPath: '/i/003000/003500.png', isOgcd: false },
  { id: 36982, name: 'Full Metal Field',   iconPath: '/i/003000/003049.png', isOgcd: false },
];

const META_BY_ID: Record<number, AbilityMetaJson> = Object.fromEntries(
  META.map((m) => [m.id, m])
);

// --- Synthetic abilities track ---------------------------------------------

const TRACK_PATTERN = [
  2876, 16498, 36978, 17209, 16500, 36978, 2878, 36982, 36981, 16498,
  36978, 36978, 36979, 36980, 7411, 7412, 7413, 36978, 16498, 2876,
  36978, 36978, 36979, 36980, 7411, 7412, 7413, 36978, 16498, 2878,
  36978, 36978, 36979, 36980, 7411, 7412, 7413, 36978, 16498, 17209,
];

function buildAbilitiesTrack(): CastEvent[] {
  const out: CastEvent[] = [];
  let t = 0;
  const GCD = 2.5;
  for (const aid of TRACK_PATTERN) {
    const m = META_BY_ID[aid];
    if (!m) continue;
    const dur = m.isOgcd ? 0.6 : 1.2;
    out.push({
      startSec: t,
      endSec: t + dur,
      abilityId: aid,
      label: m.name.slice(0, 3),
      tooltip: `${m.name} @ ${t.toFixed(1)}s`,
      color: m.isOgcd ? '#f59e0b' : '#3b82f6',
      iconPath: m.iconPath,
      yOffset: m.isOgcd ? -0.55 : 0,
    });
    t += m.isOgcd ? 0.9 : GCD;
  }
  return out;
}

// The idealized rotation reuses your timing exactly, then swaps a few filler
// Heated/Blazing GCDs for the optimal ability ("you should have hit Drill /
// Full Metal Field here"). Same timing + same GCD band keeps the lanes aligned
// so the Timeline's cast-diff lights up cleanly in dev: those slots show as
// missed (idealized) + extra (you).
function buildIdealizedTrack(): CastEvent[] {
  const out = buildAbilitiesTrack();
  const swap = (idx: number, aid: number): void => {
    const m = META_BY_ID[aid];
    if (!out[idx] || !m) return;
    out[idx] = {
      ...out[idx],
      abilityId: aid,
      label: m.name.slice(0, 3),
      tooltip: `${m.name} @ ${out[idx].startSec.toFixed(1)}s`,
      iconPath: m.iconPath,
    };
  };
  // Indices 10/20/30 are filler Blazing Shots (GCDs) in TRACK_PATTERN.
  swap(10, 16498); // Drill instead of filler
  swap(20, 36982); // Full Metal Field instead of filler
  swap(30, 16498); // Drill instead of filler
  return out;
}

// --- SetupView fixtures (shared by listEncounters / listPulls / listSetup) --

const MOCK_ENCOUNTERS: Encounter[] = [
  { id: 101, name: 'Vamp Fatale (M9S)',            totalKills: 12, bestParsePct: 66.6, category: 'savage' },
  { id: 102, name: 'Red Hot and Deep Blue (M10S)', totalKills: 7,  bestParsePct: 100,  category: 'savage' },
  { id: 103, name: 'The Tyrant (M11S)',            totalKills: 3,  bestParsePct: 41.2, category: 'savage' },
  { id: 104, name: 'Lindwurm (M12S P1)',           totalKills: 5,  bestParsePct: 88.4, category: 'savage' },
  { id: 105, name: 'Lindwurm II (M12S P2)',        totalKills: 2,  bestParsePct: 72.1, category: 'savage' },
  { id: 1085, name: 'Dancing Mad (Ultimate)',      totalKills: 1,  bestParsePct: 55.0, category: 'ultimate' },
];

const MOCK_PULLS: Pull[] = [
  {
    reportCode: 'RzHAyvkP2w7Y4T8F',
    fightId: 14,
    startTimeMs: Date.parse('2026-04-19T14:33:00'),
    durationS: 520,
    parsePct: 66.6,
    dps: 36600,
    label: '2026-04-19 14:33 — 66.6% — 36.6k dps',
  },
  {
    reportCode: 'wJk8mPxz3vN5Q1R7',
    fightId: 9,
    startTimeMs: Date.parse('2026-04-18T22:11:00'),
    durationS: 510,
    parsePct: 100,
    dps: 38100,
    label: '2026-04-18 22:11 — 100% — 38.1k dps (kill)',
  },
  {
    reportCode: 'aBcDeFg2HiJkL3Mn',
    fightId: 22,
    startTimeMs: Date.parse('2026-04-17T21:02:00'),
    durationS: 540,
    parsePct: 41.2,
    dps: 33400,
    label: '2026-04-17 21:02 — 41.2% — 33.4k dps',
  },
];

// In-progress (wipe) pulls — the prog-log discovery list. Running one of
// these reportCodes through the mock runAnalysis triggers the prog headline
// variant (truncated window + projected kill), so the whole prog UX
// exercises under `npm run dev`.
const MOCK_PROG_PULLS: ProgPull[] = [
  {
    reportCode: 'ProgMockRep1AaBb',
    fightId: 31,
    startTimeMs: Date.parse('2026-04-20T21:40:00'),
    durationS: 412,
    fightPercentage: 41.3,
    bossPercentage: 55.2,
    lastPhase: 3,
    label: '2026-04-20 21:40  —  6:52  —  41% left (P3)',
  },
  {
    reportCode: 'ProgMockRep1AaBb',
    fightId: 28,
    startTimeMs: Date.parse('2026-04-20T21:22:00'),
    durationS: 233,
    fightPercentage: 68,
    bossPercentage: 12.4,
    lastPhase: 2,
    label: '2026-04-20 21:22  —  3:53  —  68% left (P2)',
  },
  {
    reportCode: 'ProgMockRep2CcDd',
    fightId: 4,
    startTimeMs: Date.parse('2026-04-19T20:05:00'),
    durationS: 95,
    fightPercentage: null,
    bossPercentage: null,
    lastPhase: 0,
    label: '2026-04-19 20:05  —  1:35',
  },
];

const MOCK_PROG_CODES = new Set(MOCK_PROG_PULLS.map((p) => p.reportCode));

// --- Sidecar implementation ------------------------------------------------

export const mockSidecar: Sidecar = {
  // Browser preview is always "signed in" so the app boots straight to Setup.
  async getAuthStatus() {
    await delay(80);
    return { mode: 'user' as const, userName: 'Mock User' };
  },

  async fflogsAuthBegin() {
    await delay(120);
    return { authorizeUrl: 'https://example.invalid/authorize', port: 53682, expiresInSec: 300 };
  },

  async fflogsAuthPoll() {
    await delay(300);
    return { status: 'done' as const, userName: 'Mock User' };
  },

  async fflogsAuthCancel() {
    await delay(50);
  },

  async fflogsLogout() {
    await delay(80);
    return { mode: 'none' as const };
  },

  async listUserCharacters() {
    await delay(400);
    return {
      characters: [
        { name: 'Mock Machinist', server: 'Hyperion', region: 'NA' as const,
          dataCenter: 'Primal', lodestoneId: 12345678 },
        { name: 'Alt Reaper', server: 'Gilgamesh', region: 'NA' as const,
          dataCenter: 'Aether', lodestoneId: 87654321 },
      ],
    };
  },

  async lookupCharacter(name: string, server: string, region: Region, spec: string) {
    await delay(700);
    // Derive a stable id from the query so a manual search resolves to a
    // DISTINCT character from the mock account list (12345678 / 87654321) —
    // else `same` in onCharacterPicked is always true and the pick appears to
    // silently revert to the logged-in character (mirrors the real backend,
    // which returns the searched character's own lodestoneID).
    let h = 0;
    for (const ch of `${name.toLowerCase()}@${server.toLowerCase()}`) {
      h = (h * 31 + ch.charCodeAt(0)) & 0x7fffffff;
    }
    const lodestoneId = 100000000 + (h % 800000000);
    return {
      found: true,
      lodestoneId,
      name,
      serverName: server,
      region,
      logsCount: spec ? 218 : 0,
    };
  },

  async listEncounters(): Promise<Encounter[]> {
    await delay(300);
    return MOCK_ENCOUNTERS;
  },

  async listPulls(): Promise<Pull[]> {
    await delay(300);
    return MOCK_PULLS;
  },

  async listProgPulls(args): Promise<ProgPullsResult> {
    await delay(500);
    if (args.reportCode) {
      // Pasted-report mode: pretend the code resolved to one report's wipes.
      return { pulls: MOCK_PROG_PULLS.slice(2), source: 'report' };
    }
    return { pulls: MOCK_PROG_PULLS, source: 'recent' };
  },

  async listSetup(): Promise<SetupData> {
    await delay(300);
    return {
      encounters: MOCK_ENCOUNTERS,
      pullsByEncounterId: Object.fromEntries(
        MOCK_ENCOUNTERS.map((e) => [String(e.id), MOCK_PULLS]),
      ),
    };
  },

  async getCatalog(): Promise<Catalog> {
    await delay(120);
    return {
      supportedJobs: ['Machinist', 'Samurai', 'Reaper', 'Red Mage'],
      simBackedJobs: ['Machinist', 'Reaper', 'Red Mage'],
      encounters: [
        { id: 101, name: 'Vamp Fatale (M9S)', category: 'savage' },
        { id: 102, name: 'Red Hot and Deep Blue (M10S)', category: 'savage' },
        { id: 103, name: 'The Tyrant (M11S)', category: 'savage' },
        { id: 104, name: 'Lindwurm (M12S P1)', category: 'savage' },
        { id: 105, name: 'Lindwurm II (M12S P2)', category: 'savage' },
        { id: 1085, name: 'Dancing Mad (Ultimate)', category: 'ultimate', hasPfPlan: true },
      ],
      buffProviders: [
        'Bard', 'Dancer', 'Astrologian', 'Scholar', 'Dragoon', 'Monk',
        'Ninja', 'Reaper', 'Summoner', 'RedMage', 'Pictomancer',
      ],
    };
  },

  async cacheStats() {
    await delay(60);
    return { totalBytes: mockCache.totalBytes, capMb: mockCache.capMb };
  },

  async setCacheCap(capMb) {
    await delay(60);
    mockCache.capMb = capMb;
    // Mimic the backend's evict-down-to-90%-of-cap on a lowered cap.
    mockCache.totalBytes = Math.min(mockCache.totalBytes, capMb * 1024 * 1024 * 0.9);
    return { totalBytes: mockCache.totalBytes, capMb: mockCache.capMb };
  },

  async clearCache() {
    await delay(60);
    mockCache.totalBytes = 0;
    return { totalBytes: 0, capMb: mockCache.capMb };
  },

  async logEvent(level, cat, msg, data) {
    // Browser preview has no persistent log — keep a session-local tail so
    // getRecentEvents shows what the app actually did.
    mockEvents.push(data ? { t: Date.now(), lv: level, cat: `ui.${cat}`, msg, data }
                         : { t: Date.now(), lv: level, cat: `ui.${cat}`, msg });
    if (mockEvents.length > 200) mockEvents.shift();
  },

  async getRecentEvents(limit = 100) {
    await delay(60);
    return { events: mockEvents.slice(-limit) };
  },

  async exportFeedbackBundle(args) {
    await delay(400);
    const title =
      args.category === 'anomaly'
        ? '[Anomaly] Machinist · Dancing Green (Savage) — efficiency 100.42%'
        : `[${args.category === 'bug' ? 'Bug' : 'Feedback'}] ` +
          (args.description?.slice(0, 60) || '(no description)');
    return {
      path: 'C:\\Users\\you\\.fflogs_efficiency_analyzer\\feedback\\meridian-feedback-20260716-120000.zip',
      issueTitle: title,
      issueBody:
        '## What happened\n\n' + (args.description || '(no description provided)') +
        `\n\n## Context\n\n- App: Meridian ${APP_VERSION} (mock)\n\n## Diagnostics\n\n` +
        '`meridian-feedback-20260716-120000.zip`\n\n' +
        '**Please attach that zip to this issue** (drag & drop it into this ' +
        'text box). It contains the app\'s event log and analysis context — ' +
        'no FFLogs credentials.',
    };
  },

  async prefetchRefs(
    spec,
    encounterId,
    _refsBucket: RefsBucket,
    onProgress
  ): Promise<PrefetchResult> {
    // Mirror runAnalysis' ref-download phase: a 10-task fetch behind a
    // 6-worker pool so the blocking warm popup exercises the same task-bar UI.
    const REF_NAMES = [
      'Aymeric de Borel', 'Yshtola Rhul', 'Estinien Wyrmblood',
      'Alphinaud Leveilleur', 'Alisaie Leveilleur', 'Thancred Waters',
      'Urianger Augurelt', 'Lyna', "G'raha Tia", 'Krile Mayer Baldesion',
    ];
    const tasks: ProgressTask[] = REF_NAMES.map((label) => ({ label, state: 'pending' }));
    const emit = (): void => {
      const doneN = tasks.filter((t) => t.state === 'done' || t.state === 'failed').length;
      const pct = Math.round((100 * doneN) / tasks.length);
      onProgress?.(pct, `Warming ${spec} references — ${doneN} of ${tasks.length}…`, [...tasks]);
    };
    onProgress?.(5, `Warming ${spec} top-10 references…`);
    emit();
    const POOL = 6;
    let next = 0;
    const worker = async (): Promise<void> => {
      while (true) {
        const i = next++;
        if (i >= tasks.length) return;
        tasks[i] = { ...tasks[i], state: 'in_flight' };
        emit();
        await delay(120 + Math.random() * 280);
        tasks[i] = { ...tasks[i], state: 'done' };
        emit();
      }
    };
    await Promise.all(Array.from({ length: POOL }, worker));
    onProgress?.(100, 'Ready');
    return { spec, encounterId, count: tasks.length, fromCache: false, avgKillSec: 632 };
  },

  async listRankings(): Promise<RankingEntry[]> {
    // Same roster as the ref-warm mock, with the SetupView mock report codes so
    // loading a row exercises the mock runAnalysis end-to-end in dev.
    await delay(350);
    const names = [
      'Aymeric de Borel', 'Yshtola Rhul', 'Estinien Wyrmblood',
      'Alphinaud Leveilleur', 'Alisaie Leveilleur', 'Thancred Waters',
      'Urianger Augurelt', 'Lyna', "G'raha Tia", 'Krile Mayer Baldesion',
    ];
    return names.map((name, i) => {
      const pull = MOCK_PULLS[i % MOCK_PULLS.length];
      return {
        rank: i + 1,
        name,
        server: ['Gilgamesh', 'Chaos', 'Tonberry'][i % 3],
        reportCode: pull.reportCode,
        fightId: pull.fightId,
        durationMs: (505 + i * 3) * 1000,
        amount: 39200 - i * 180,
        percentile: 100 - i * 0.1,
      };
    });
  },

  async runAnalysis(
    _reportCode,
    _fightId,
    _spec,
    _encounterId,
    _refsBucket: RefsBucket,
    _playerName,
    onProgress,
    comp
  ): Promise<AnalysisResult> {
    // Mirror the sidecar's pipeline steps so the LoadingView checklist exercises
    // in dev. Healer runs prepend the mit-plan phase (locked-GCD flow).
    const isHealer = ['White Mage', 'Scholar', 'Astrologian', 'Sage'].includes(_spec);
    const S = isHealer ? 1 : 0;
    const STEPS = [
      ...(isHealer ? ['Mitigation plan'] : []),
      'Your pull', 'Reference logs', 'Downtime ceiling',
      'Multi-target check', 'Compare to top parses', 'Ideal rotations',
    ];
    if (isHealer) {
      onProgress?.(2, 'Building the mitigation plan…', undefined, { step: 0, steps: STEPS });
      await delay(300);
    }
    onProgress?.(5, 'Downloading your log from FFLogs…', undefined, { step: 0 + S, steps: STEPS });
    await delay(400);

    // Fake out a 10-task fetch with a 6-worker pool so the multi-bar UI
    // exercises in dev. Each task transitions pending → in_flight → done
    // with a randomized lag matching how real network jitter looks.
    const REF_NAMES = [
      'Aymeric de Borel', 'Yshtola Rhul', 'Estinien Wyrmblood',
      'Alphinaud Leveilleur', 'Alisaie Leveilleur', 'Thancred Waters',
      'Urianger Augurelt', 'Lyna', "G'raha Tia", 'Krile Mayer Baldesion',
    ];
    const tasks: ProgressTask[] = REF_NAMES.map((label) => ({
      label,
      state: 'pending',
    }));
    const emit = (): void => {
      const doneN = tasks.filter((t) => t.state === 'done' || t.state === 'failed').length;
      const pct = 20 + Math.round((60 * doneN) / tasks.length);
      onProgress?.(pct, `Downloaded ${doneN} of ${tasks.length} reference logs from FFLogs…`,
        [...tasks], { step: 1 + S, steps: STEPS });
    };
    emit();

    const POOL = 6;
    let next = 0;
    const worker = async (): Promise<void> => {
      while (true) {
        const i = next++;
        if (i >= tasks.length) return;
        tasks[i] = { ...tasks[i], state: 'in_flight' };
        emit();
        await delay(350 + Math.random() * 500);
        tasks[i] = { ...tasks[i], state: 'done' };
        emit();
      }
    };
    await Promise.all(Array.from({ length: POOL }, worker));

    onProgress?.(82, 'Modeling forced-downtime ceiling…', undefined, { step: 2 + S, steps: STEPS });
    await delay(150);
    onProgress?.(85, 'Checking multi-target windows…', undefined, { step: 3 + S, steps: STEPS });
    await delay(150);
    onProgress?.(88, 'Comparing to top parses…', undefined, { step: 4 + S, steps: STEPS });
    await delay(200);
    onProgress?.(93, 'Modeling ideal-rotation lanes…', undefined, { step: 5 + S, steps: STEPS });
    await delay(200);
    onProgress?.(100, 'Done', undefined, { step: STEPS.length, steps: STEPS });

    const yourPotency = 205_216;
    const refPotencies = [210_200, 207_440, 211_800, 208_100, 209_500, 205_900, 213_400, 206_700, 212_100, 208_900];
    const refAvg = refPotencies.reduce((a, b) => a + b, 0) / refPotencies.length;
    const youEff = 99.2;
    const refEff = 99.7;
    const youKill = 655; // 10:55
    const refKill = 632; // 10:32

    const yourIdealized = Math.round(yourPotency / (youEff / 100));
    const refIdealized = Math.round(refAvg / (refEff / 100));

    // Buff-window-aware lens. `deliveredObserved` is scored under the raid
    // buffs that actually landed; `idealizedObserved` is the fair ceiling
    // under those same buffs; `idealizedMaster` assumes party buffs on a
    // perfect 2-minute cadence. Master efficiency sits a touch below observed
    // here because one party buff drifted (see BuffDrift below).
    const deliveredObserved = 206_900;
    const idealizedObserved = Math.round(deliveredObserved / 0.991); // ~99.1%
    const idealizedMaster = Math.round(deliveredObserved / 0.987); // ~98.7%
    const efficiencyPctObserved =
      (deliveredObserved / idealizedObserved) * 100;
    const efficiencyPctMaster = (deliveredObserved / idealizedMaster) * 100;
    const result: AnalysisResult = {
      you: {
        label: 'You',
        fightDurationSec: youKill,
        deliveredPotency: yourPotency,
        idealizedPotency: yourIdealized,
        efficiencyPct: youEff,
        killTimeSec: youKill,
        abilitiesTrack: buildAbilitiesTrack(),
        tinctureWindows: [
          { startSec: 2, endSec: 32, multiplier: 1.082 },
          { startSec: 242, endSec: 272, multiplier: 1.082 },
        ],
      },
      refs: refPotencies.map((p, i) => ({
        label: `#${i + 1}`,
        fightDurationSec: refKill,
        deliveredPotency: p,
        idealizedPotency: refIdealized,
        efficiencyPct: refEff,
        killTimeSec: refKill,
        // Which basis the pair above is on (see contract). One uncredited ref
        // exercises the crediting modes' pair-reconstruction path in dev.
        multiTargetCredited: i !== 7,
        // Reference rotation lanes for the Timeline (top 3 in dev — the real
        // backend ships every fetched ref's track).
        abilitiesTrack: i < 3 ? buildAbilitiesTrack() : undefined,
        tinctureWindows: i < 3
          ? [
              { startSec: 1, endSec: 31, multiplier: 1.082 },
              { startSec: 300, endSec: 330, multiplier: 1.082 },
            ]
          : [],
      })),
      // Unified, located, ranked suggestions (as the backend emits post-
      // grouping). Times fall within the synthetic ~90s track so the
      // click-to-timeline jump lands on a real cast in `npm run dev`.
      improvements: [
        {
          kind: 'death',
          abilityId: 0,
          abilityName: '',
          timeSec: 52,
          lostPotency: 3200,
          summary: 'Died at 0:52 — 14s recovering, ~6 GCDs lost',
        },
        {
          kind: 'tincture',
          abilityId: 0,
          abilityName: 'Tincture',
          timeSec: 242,
          lostPotency: 540,
          summary: 'Used 1 of 2 tinctures — fitting 1 more on cooldown recovers this',
        },
        {
          kind: 'missed_cast',
          abilityId: 17209,
          abilityName: 'Hypercharge',
          timeSec: 62,
          lostPotency: 1180,
          summary: 'Missed Hypercharge — fit one around 1:02 (~-1180p)',
        },
        {
          // The grouped multi-target card (children match the headline windows
          // by timeSec) so the crediting modes' repricing renders in dev.
          kind: 'multitarget',
          abilityId: 0,
          abilityName: '',
          timeSec: 95,
          lostPotency: 700,
          summary:
            'Multi-target: you hit fewer targets than the optimal AoE line across 2 windows — spread damage to every enemy in these windows',
          children: [
            { kind: 'multi_target', abilityId: 0, abilityName: '', timeSec: 95, lostPotency: 400, summary: '1:35–2:30: hit fewer than 2 targets — ~400p of cleave left on the table' },
            { kind: 'multi_target', abilityId: 0, abilityName: '', timeSec: 305, lostPotency: 300, summary: '5:05–6:00: hit fewer than 2 targets — ~300p of cleave left on the table' },
          ],
        },
        {
          kind: 'opener',
          abilityId: 36982,
          abilityName: 'Full Metal Field',
          timeSec: 18,
          lostPotency: 660,
          summary:
            'Opener slot #8: cast Blazing Shot (240p), canonical uses Full Metal Field (900p) — -660p',
        },
        {
          kind: 'idle',
          abilityId: 0,
          abilityName: '',
          timeSec: 45,
          lostPotency: 590,
          summary: 'Time spent idle: 3.5s (1.4 GCDs)',
          children: [
            { kind: 'idle', abilityId: 0, abilityName: '', timeSec: 45, lostPotency: 320, summary: 'Idle 1.9s at 0:45' },
            { kind: 'idle', abilityId: 0, abilityName: '', timeSec: 71, lostPotency: 180, summary: 'Idle 1.1s at 1:11' },
            { kind: 'idle', abilityId: 0, abilityName: '', timeSec: 58, lostPotency: 90, summary: 'Idle 0.5s at 0:58' },
          ],
        },
        {
          kind: 'missed_cast',
          abilityId: 16498,
          abilityName: 'Drill',
          timeSec: 33,
          lostPotency: 340,
          summary: 'Missed Drill — fit one around 0:33 (~-340p)',
        },
        {
          kind: 'hypercharge',
          abilityId: 17209,
          abilityName: 'Hypercharge',
          timeSec: 64,
          lostPotency: 262,
          summary: 'Hypercharge at 1:04 fired 3/5 Blazing Shots (short 2)',
        },
        {
          kind: 'residual',
          abilityId: 0,
          abilityName: '',
          timeSec: 0,
          lostPotency: 300,
          summary:
            'Other — 3 small located losses below the listing threshold, plus small losses scattered across the fight (resource/burst spacing & sequencing)',
          children: [
            { kind: 'overcap', abilityId: 0, abilityName: '', timeSec: 80, lostPotency: 120, summary: 'Heat overcap at 1:20 — Blazing Shot (wasted 5)' },
            { kind: 'missed_cast', abilityId: 36979, abilityName: 'Double Check', timeSec: 64, lostPotency: 180, summary: 'Missed Double Check — fit one around 1:04' },
            { kind: 'align', abilityId: 0, abilityName: '', timeSec: 38, lostPotency: 90, summary: 'Reassemble misaligned at 0:38' },
          ],
        },
        {
          kind: 'align',
          abilityId: 0,
          abilityName: '',
          timeSec: 27,
          lostPotency: 180,
          summary: 'Wildfire at 0:27 could shift into the burst window (~-180p)',
        },
        {
          kind: 'clip',
          abilityId: 0,
          abilityName: '',
          timeSec: 62,
          lostPotency: 160,
          summary: 'GCD clipping: 0.9s (0.4 GCDs) from over-weaving',
          children: [
            { kind: 'clip', abilityId: 0, abilityName: '', timeSec: 62, lostPotency: 95, summary: 'Clipped 0.55s — 3 oGCDs weaved at 1:02' },
            { kind: 'clip', abilityId: 0, abilityName: '', timeSec: 41, lostPotency: 65, summary: 'Clipped 0.38s — 3 oGCDs weaved at 0:41' },
          ],
        },
      ],
      // Idealized comparison lane for the Timeline (reuses the synthetic track
      // shape so the lane renders in dev).
      idealizedTrack: buildIdealizedTrack(),
      idealizedTinctureWindows: [
        { startSec: 0, endSec: 30, multiplier: 1.082 },
        { startSec: 300, endSec: 330, multiplier: 1.082 },
      ],
      headline: {
        percentile: 80,
        rank: { you: 3, total: refPotencies.length + 1 },
        beat: { count: 8, of: refPotencies.length },
        effectiveGcdSec: 2.49,
        yourPotency,
        yourIdealizedPotency: yourIdealized,
        yourIdealizedPotencyLenient: yourIdealized,
        refAvgPotency: Math.round(refAvg),
        refAvgIdealizedPotency: refIdealized,
        efficiencyPct: youEff,
        efficiencyPctStrict: youEff,
        efficiencyPctLenient: youEff,
        deliveredObserved,
        idealizedObserved,
        idealizedMaster,
        efficiencyPctObserved,
        efficiencyPctMaster,
        refEfficiencyPct: refEff,
        killTimeSec: youKill,
        refKillTimeSec: refKill,
        downtimeSource: 'targetability',
        downtimeTierA: [],
        downtimeTierB: [],
        downtimeTierBHigh: [],
        rangedWindows: [],
        partyJobs: ['Machinist', 'Dragoon', 'Ninja', 'Bard', 'Pictomancer', 'Warrior', 'Scholar', 'Astrologian'],
        deaths: [{ timeSec: 52, durationSec: 14 }],
        deathsLostPotency: 3200,
        // Multi-target: a credited pull with two confirmed windows so the
        // WindowReview trim panel + credited efficiency hint + the crediting
        // modes all render in dev. Window 1's ref average (4420) sits ABOVE
        // your delivered (a residual gap under the top-10 cap); window 2's
        // (2800) sits BELOW yours (the warned-about >100% outlier case).
        multiTargetDisclaimed: false,
        multiTargetCredited: true,
        multiTargetWindows: [
          {
            startSec: 95, endSec: 150, targetCount: 2,
            deliveredSplash: 4200, ceilingSplash: 4600,
            refDeliveredSplash: [4600, 4500, 4300, 4550, 4350, 4400, 4450, 4250, 4500, 4300],
            refCeilingSplash: [4600, 4600, 4600, 4600, 4600, 4600, 4600, 4600, 4600, 4600],
            refAvgDeliveredSplash: 4420,
            cleaveGeometry: {
              verdict: 'reachable',
              detail: 'targets within 9y for 82% of 41 samples (closest 5.1y)',
            },
          },
          {
            startSec: 305, endSec: 360, targetCount: 2,
            deliveredSplash: 3100, ceilingSplash: 3400,
            refDeliveredSplash: [2900, 2700, 2850, 2750, 2800, 2900, 2650, 2850, 2800, 2800],
            refCeilingSplash: [3400, 3400, 3400, 3400, 3400, 3400, 3400, 3400, 3400, 3400],
            refAvgDeliveredSplash: 2800,
            cleaveGeometry: {
              verdict: 'unreachable',
              detail: 'targets never within 9y of each other (closest 14.2y across 28 samples)',
            },
          },
        ],
      },
      comparisons: {
        Drift: {
          aspectName: 'Drift',
          findings: [
            '[drift] Drill: 27 casts, drifted 8.2s ≈ -272p (ref avg -100p)',
          ],
          detailColumns: ['Ability', 'Casts', 'Drift (s)', 'Lost (p)', 'Ref avg (p)', 'Δ'],
          yourDetailRows: [
            ['Drill',      27, 8.2,  272, 100, '+172'],
            ['Air Anchor', 13, 5.3,  84,  93,  '-9'],
            ['Excavator',  12, 4.1,  60,  78,  '-18'],
          ],
          yourDetailRowColors: ['#c0392b', null, null],
          summaryLines: [
            'Total drift cost: -416p',
            `Reference median: -271p (across ${refPotencies.length} runs)`,
          ],
        },
        Clipping: {
          aspectName: 'Clipping',
          findings: [
            '[idle] 3.5s idle (1.4 GCDs) ≈ -590p',
            '[clip] 0.9s GCD clipping from over-weaving (0.4 GCDs) ≈ -160p',
          ],
          detailColumns: ['#', 'Time (s)', 'Clip (s)', 'oGCDs'],
          yourDetailRows: [
            [1, 62, 0.55, 3],
            [2, 41, 0.38, 3],
          ],
          yourDetailRowColors: [],
          summaryLines: [
            'Effective GCD: 2.49s',
            'Avg GCD potency: 422p',
            'Reference median idle+clip cost: -320p',
          ],
        },
        Overcap: {
          aspectName: 'Overcap',
          findings: [
            '[overcap] Heat overcap at 4:32 (Blazing Shot, -60p)',
          ],
          detailColumns: ['Time', 'Gauge', 'Ability', 'Wasted', 'Lost (p)'],
          yourDetailRows: [
            ['4:32', 'heat', 'Blazing Shot', 5, 60],
          ],
          yourDetailRowColors: [],
          summaryLines: ['Total overcap cost: -60p', '  Heat: -60p'],
        },
        Opener: {
          aspectName: 'Opener',
          findings: [
            '[opener] Slot #7: cast Blazing Shot (240p), canonical uses Excavator (660p) — -420p',
            '[opener] Slot #8: cast Blazing Shot (240p), canonical uses Full Metal Field (900p) — -660p',
          ],
          detailColumns: ['Slot', 'Expected', 'Actual', 'Lost (p)'],
          yourDetailRows: [
            [7, 'Excavator', 'Blazing Shot', 420],
            [8, 'Full Metal Field', 'Blazing Shot', 660],
          ],
          yourDetailRowColors: [],
          summaryLines: ['Your opener cost: 1080p'],
        },
        Alignment: {
          aspectName: 'Alignment',
          findings: [
            '[align] Wildfire at 4:12 could have shifted into burst window at 4:20 (~+90p)',
          ],
          detailColumns: ['Time', 'Summary', 'Benefit (p)'],
          yourDetailRows: [],
          yourDetailRowColors: [],
          summaryLines: ['Total potential benefit: +90p'],
        },
        Reassemble: {
          aspectName: 'Reassemble',
          findings: ['Reassemble targeting looks clean.'],
          detailColumns: ['Time', 'Summary', 'Lost (p)'],
          yourDetailRows: [],
          yourDetailRowColors: [],
          summaryLines: ['Total Reassemble misalign: -0p'],
        },
        Scoring: {
          aspectName: 'Scoring',
          findings: [],
          detailColumns: [],
          yourDetailRows: [],
          yourDetailRowColors: [],
          summaryLines: [],
        },
        Queen: {
          aspectName: 'Queen',
          findings: ['[align] Q2 fired at 4:48 with battery 90 — clean'],
          detailColumns: ['#', 'Time', 'Battery', 'Dur', 'Finish', 'Pet dmg'],
          yourDetailRows: [
            [1, '0:18',  60, 9.8,  '✓', 18_500],
            [2, '4:48',  90, 12.4, '✓', 31_200],
            [3, '8:22', 100, 14.1, '✓', 36_800],
          ],
          yourDetailRowColors: [null, null, null],
          summaryLines: ['3 queens fired, total pet damage 86,500'],
        },
        Wildfire: {
          aspectName: 'Wildfire',
          findings: [
            'Wildfire at 4:12 captured 4/6 hits — short 2 × 240 ≈ -480p',
            'Wildfire at 8:35 captured 6/6 hits — clean',
          ],
          detailColumns: ['#', 'Time', 'Hits', 'Hits/6', 'Lost (p)'],
          yourDetailRows: [
            [1, '0:14', 6, '6/6', 0],
            [2, '4:12', 4, '4/6', 480],
            [3, '8:35', 6, '6/6', 0],
          ],
          yourDetailRowColors: [null, '#c0392b', null],
          summaryLines: ['1 of 3 Wildfires undercut.'],
        },
        Tools: {
          aspectName: 'Tools',
          findings: [],
          detailColumns: [],
          yourDetailRows: [],
          yourDetailRowColors: [],
          summaryLines: [],
        },
        Abilities: {
          aspectName: 'Abilities',
          findings: [],
          detailColumns: ['Ability', 'Your casts', 'Ref median'],
          yourDetailRows: [
            ['Blazing Shot',      70, 70],
            ['Double Check',      55, 54],
            ['Checkmate',         55, 54],
            ['Heated Split Shot', 35, 34],
            ['Heated Clean Shot', 33, 33],
            ['Heated Slug Shot',  33, 34],
            ['Drill',             27, 27],
            ['Hypercharge',       14, 14],
            ['Air Anchor',        13, 13],
            ['Wildfire',           5,  5],
            ['Reassemble',        10, 10],
            ['Excavator',         12, 13],
            ['Full Metal Field',   2,  3],
          ],
          yourDetailRowColors: Array(13).fill(null),
          summaryLines: ['397 total casts.'],
        },
      },
      aspectStates: {
        Scoring: {
          deliveredPotency: yourPotency,
          idealizedPotency: Math.round(yourPotency / 0.998),
          idealizedStrict: Math.round(yourPotency / 0.998),
          idealizedLenient: Math.round(yourPotency / 0.998),
          queenBatterySpent: 250,
          downtimeWindows: [],
          downtimeSource: 'targetability',
          downtimeTierB: [],
          fightDurationSec: youKill,
        },
        Drift: {
          findings: [
            { abilityId: 16498, abilityName: 'Drill',      casts: 27, cappedSeconds: 8.2, lostCasts: 0, lostPotency: 272, cdrOverflowSeconds: 0, sharedConsumers: 0 },
            { abilityId: 16500, abilityName: 'Air Anchor', casts: 13, cappedSeconds: 5.3, lostCasts: 0, lostPotency: 84,  cdrOverflowSeconds: 0, sharedConsumers: 0 },
            { abilityId: 36981, abilityName: 'Excavator',  casts: 12, cappedSeconds: 4.1, lostCasts: 0, lostPotency: 60,  cdrOverflowSeconds: 0, sharedConsumers: 0 },
          ],
          downtimeWindows: [],
          fightDurationSec: youKill,
        },
        Clipping: {
          clipping: {
            effectiveGcdSec: 2.49,
            avgGcdPotency: 422,
            totalIdleSec: 3.5, idleLostGcds: 1.4, idleLostPotency: 590,
            worstIdle: [[45, 1.9], [71, 1.1], [58, 0.5]],
            totalClipSec: 0.9, clipLostGcds: 0.4, clipLostPotency: 160,
            worstClips: [[62, 0.55, 3], [41, 0.38, 3]],
          },
        },
        Overcap: {
          findings: [
            { gauge: 'heat', timeSec: 272, abilityId: 36978, abilityName: 'Blazing Shot', wasted: 5, lostPotency: 60 },
          ],
        },
        Opener: {
          findings: [
            { position: 7, expectedId: 36981, actualId: 36978, summary: 'Slot #7 — Blazing Shot instead of Excavator', lostPotency: 420 },
            { position: 8, expectedId: 36982, actualId: 36978, summary: 'Slot #8 — Blazing Shot instead of Full Metal Field', lostPotency: 660 },
          ],
        },
        Alignment: {
          findings: [
            { kind: 'burst_misalign', timeSec: 252, summary: 'Wildfire outside burst window', lostPotency: 90 },
            { kind: 'burst_misalign', timeSec: 515, summary: 'Wildfire outside burst window', lostPotency: 90 },
          ],
        },
        Hypercharge: {
          windows: [
            { castTimeSec: 18, hits: 5, bucket: 0, cutShort: false, lastShotSec: 25.5 },
            { castTimeSec: 64, hits: 3, bucket: 2, cutShort: false, lastShotSec: 68.5 },
            { castTimeSec: 88, hits: 4, bucket: 2, cutShort: true, lastShotSec: 94.5 },
          ],
        },
        Reassemble: {
          findings: [],
        },
        BuffDrift: {
          findings: [
            {
              kind: 'gap',
              provider: 'Battle Litany',
              timeS: 252,
              summary:
                'Battle Litany landed ~6s into the 4:00 burst window — partial overlap (context)',
            },
            {
              kind: 'missing',
              provider: 'Technical Step',
              timeS: 480,
              summary:
                'Technical Step never applied during the 8:00 window (context)',
            },
          ],
        },
      },
      abilityMeta: META_BY_ID,
    };
    if (isHealer) {
      // Healer (mit-plan locked) variant: rank suppression, the heal-lock
      // chip, the beyond-plan heal card, and an over-100% efficiency display
      // (the "above the honest ceiling" framing) all exercise in dev.
      const h = result.headline;
      h.rankSuppressed = true;
      h.healLocksApplied = true;
      h.healLockCount = 6;
      h.healLockPotency = 2100;
      h.mitPlanComp = comp
        ? [comp.shieldHealer, comp.regenHealer, ...comp.tanks, ...comp.dps]
        : ['Sage', 'White Mage', 'Paladin', 'Dark Knight',
           'Samurai', 'Dragoon', 'Bard', 'Pictomancer'];
      h.mitPlanCompSource = comp ? 'override' : 'pull';
      h.efficiencyPct = 100.8;
      h.efficiencyPctStrict = 100.8;
      h.efficiencyPctLenient = 100.9;
      result.improvements = [
        {
          kind: 'extra_heal_gcds', abilityId: 37010, abilityName: 'Medica III',
          timeSec: 214, lostPotency: 700,
          summary: 'Healing GCDs beyond the mitigation plan ×2 — the plan '
            + 'needs 4 costed heal GCDs (+2 slack), you cast 8',
          children: [
            { kind: 'extra_heal_gcds', abilityId: 37010, abilityName: 'Medica III', timeSec: 214, lostPotency: 350, summary: 'Medica III at 3:34 — a damage GCD the plan didn\'t need' },
            { kind: 'extra_heal_gcds', abilityId: 37010, abilityName: 'Medica III', timeSec: 305, lostPotency: 350, summary: 'Medica III at 5:05 — a damage GCD the plan didn\'t need' },
          ],
        },
        ...result.improvements,
      ];
    }
    if (MOCK_PROG_CODES.has(_reportCode)) {
      // Prog (wipe) variant: truncated scored window, full-pull framing, and
      // the projected kill — exercises the prog dashboard (Pull duration +
      // Projected kill KPIs, framing banner, rank suppression) in dev.
      const h = result.headline;
      h.isProgPull = true;
      h.pullDurationSec = 412;
      h.killTimeSec = 397;              // scored (truncated) window
      h.terminalDeathSec = 397;
      h.fightPercentage = 41.3;
      h.bossPercentage = 55.2;
      h.lastPhase = 3;
      h.projectedKillTimeSec = 634;
      h.projectionMeta = {
        method: 'active_rate_v1', refCount: 10, refKillSec: 622,
        activeSec: 371, downtimeBeyondSec: 45, burnedPct: 58.7,
      };
    }
    return result;
  },

  async theorizeKillTime(
    spec: string,
    encounterId: number,
    targetKillSec: number,
    rangeSec: number,
    partyJobs: string[],
    onProgress,
  ): Promise<TheorizeResult> {
    // Mirror the backend's stages so the in-card progress UI exercises in dev.
    onProgress?.(8, 'Loading reference fight data…');
    await delay(250);
    onProgress?.(60, 'Downloaded 10 of 10 reference logs…');
    await delay(200);
    onProgress?.(92, 'Computing ideal rotation…');
    await delay(150);
    const empty = {
      targetKillSec, idealizedPotency: 0, timeline: [] as CastEvent[],
      downtimeWindows: [], buffWindows: [], tinctureWindows: [], samples: [],
      abilityMeta: {}, downtimeSource: 'none' as const, refCount: 0,
      refKillTimeSec: 0, refAvgKillSec: 0, refPartyJobs: [],
    };
    if (spec === 'Samurai') {
      onProgress?.(100, 'Done');
      return { unsupported: true, ...empty };
    }
    // Scale a base ceiling by kill time, nudged up when a comp brings buffs, so
    // the spread + the no-comp/comp delta both render in `npm run dev`.
    const observed = 655;
    const base = 205_216;
    const buffBoost = partyJobs.length ? 1.04 : 1.0;
    const idealAt = (k: number) => Math.round(base * (k / observed) * buffBoost);
    const half = rangeSec / 2;
    const samples = [];
    for (let d = Math.round(targetKillSec - half); d <= Math.round(targetKillSec + half); d++) {
      samples.push({ killSec: d, idealizedPotency: idealAt(d) });
    }
    // Encounter chosen ⇒ pretend we derived downtime from its references.
    const hasRefs = encounterId > 0;
    onProgress?.(100, 'Done');
    return {
      targetKillSec,
      idealizedPotency: idealAt(targetKillSec),
      timeline: buildIdealizedTrack(),
      downtimeWindows: hasRefs ? [{ startSec: 40, endSec: 46 }] : [],
      buffWindows: partyJobs.length
        ? [
            { startSec: 7, endSec: 27, multiplier: 1.04 },
            { startSec: 60, endSec: 80, multiplier: 1.04 },
          ]
        : [],
      tinctureWindows: [{ startSec: 0, endSec: 30, multiplier: 1.082 }],
      samples,
      abilityMeta: META_BY_ID,
      downtimeSource: hasRefs ? 'references' : 'none',
      refCount: hasRefs ? 10 : 0,
      refKillTimeSec: hasRefs ? targetKillSec + 3 : 0,
      refAvgKillSec: hasRefs ? 632 : 0,
      refPartyJobs: hasRefs ? ['Bard', 'Dragoon', 'Ninja', 'Scholar'] : [],
    };
  },

  async planMitigation(args, onProgress): Promise<MitPlanResult> {
    const {
      encounterId,
      shieldHealer = 'Sage',
      regenHealer = 'White Mage',
      tanks = ['Paladin', 'Dark Knight'],
      dps = ['Samurai', 'Dragoon', 'Bard', 'Pictomancer'],
    } = args;
    // Mirror the backend's stages (rankings → per-log downloads → classify →
    // plan) so the in-card progress UI exercises in `npm run dev`.
    onProgress?.(5, 'Fetching encounter rankings…');
    await delay(200);
    onProgress?.(40, 'Downloading top logs (5/10)…');
    await delay(250);
    onProgress?.(85, 'Classifying mechanics…');
    await delay(150);
    onProgress?.(92, 'Scheduling mitigation plan…');
    await delay(120);
    const roleHp = { tank: 325_000, healer: 205_000, dps: 226_000 };
    const mkAssign = (
      slot: string, job: string, actionId: number, name: string,
      castAtSec: number, over: Partial<MitAssignment> = {},
    ): MitAssignment => ({
      slot, job, actionId, name, castAtSec, durationSec: 15, target: 'party',
      mitPct: 0.1, shieldAmount: 0, healAmount: 0, hotHps: 0, isGcd: false,
      castTimeSec: 0, isSuggestion: false, covers: [], isCarryover: false,
      ...over,
    });
    const mechanics: MitMechanic[] = [
      {
        id: '1001#0', timeSec: 24, endSec: 24.4, name: 'Mock Raidwide',
        bossAbilityIds: [1001], kind: 'raidwide', school: 'magical',
        hits: [{ timeSec: 24, unmitigated: { tank: 105_000, healer: 168_000, dps: 176_000 } }],
        unmitigated: { tank: 105_000, healer: 168_000, dps: 176_000 },
        unmitigatedP90: { tank: 112_000, healer: 175_000, dps: 184_000 },
        observedMitPct: 0.27, presenceRatio: 1, tankTargets: 1,
        assignments: [
          mkAssign('T1', tanks[0], 7535, 'Reprisal', 22, { target: 'enemy', covers: [24] }),
          mkAssign('H1', shieldHealer, 24298, 'Kerachole', 22, { covers: [24] }),
        ],
        gcdHeals: [],
        predicted: { tank: 74_000, healer: 122_000, dps: 128_000 },
        hpAfter: { tank: 251_000, healer: 83_000, dps: 98_000 },
        status: 'covered', notes: [],
      },
      {
        id: '1002#0', timeSec: 58, endSec: 59.8, name: 'Mock Tankbuster',
        bossAbilityIds: [1002], kind: 'tankbuster', school: 'physical',
        hits: [{ timeSec: 58, unmitigated: { tank: 390_000, healer: 0, dps: 0 } }],
        unmitigated: { tank: 390_000, healer: 0, dps: 0 },
        unmitigatedP90: { tank: 405_000, healer: 0, dps: 0 },
        observedMitPct: 0.55, presenceRatio: 1, tankTargets: 1,
        assignments: [
          mkAssign('T1', tanks[0], 30, 'Hallowed Ground', 57,
                   { target: 'self', mitPct: 0, durationSec: 10, isSuggestion: true, covers: [58] }),
        ],
        gcdHeals: [],
        predicted: { tank: 0, healer: 0, dps: 0 },
        hpAfter: { tank: 325_000, healer: 205_000, dps: 226_000 },
        status: 'covered',
        notes: ['Hallowed Ground — party tools saved for raid damage.',
                'Swap the suggested tank to match your own swap plan.'],
      },
      {
        id: 'hpset#0', timeSec: 78, endSec: 79, name: 'Mock HP Set',
        bossAbilityIds: [], kind: 'hpSet', school: 'unknown',
        hits: [{ timeSec: 78, unmitigated: { tank: 0, healer: 0, dps: 0 } }],
        unmitigated: { tank: 0, healer: 0, dps: 0 },
        unmitigatedP90: { tank: 0, healer: 0, dps: 0 },
        observedMitPct: 0, presenceRatio: 1, tankTargets: 1,
        assignments: [],
        gcdHeals: [],
        predicted: { tank: 0, healer: 0, dps: 0 },
        hpAfter: { tank: 1, healer: 1, dps: 1 },
        status: 'covered',
        notes: ['Sets the party to 1 HP — unmitigable; heal up after.'],
      },
      {
        id: '1003#0', timeSec: 95, endSec: 107, name: 'Mock Bleed',
        bossAbilityIds: [1003], kind: 'bleed', school: 'magical',
        hits: [
          { timeSec: 95, unmitigated: { tank: 18_000, healer: 27_000, dps: 29_000 } },
          { timeSec: 98, unmitigated: { tank: 18_000, healer: 27_000, dps: 29_000 } },
          { timeSec: 101, unmitigated: { tank: 18_000, healer: 27_000, dps: 29_000 } },
          { timeSec: 104, unmitigated: { tank: 18_000, healer: 27_000, dps: 29_000 } },
        ],
        unmitigated: { tank: 72_000, healer: 108_000, dps: 116_000 },
        unmitigatedP90: { tank: 78_000, healer: 114_000, dps: 121_000 },
        observedMitPct: 0.18, presenceRatio: 0.9, tankTargets: 1,
        assignments: [
          mkAssign('H1', shieldHealer, 3585, 'Sacred Soil', 93,
                   { mitPct: 0.1, covers: [95, 98, 101, 104] }),
        ],
        gcdHeals: [{
          slot: 'H2', job: regenHealer, actionId: 37010, name: 'Medica III',
          castAtSec: 88, count: 2, castTimeSec: 2.5, healAmount: 21_000,
        }],
        predicted: { tank: 61_000, healer: 92_000, dps: 99_000 },
        hpAfter: { tank: 210_000, healer: 96_000, dps: 105_000 },
        status: 'covered', notes: [],
      },
    ];
    const lanes: MitPlanLane[] = ['T1', 'T2', 'H1', 'H2', 'D1', 'D2', 'D3', 'D4'].map((slot, i) => {
      const jobs = [tanks[0], tanks[1], shieldHealer, regenHealer, ...dps];
      const casts: CastEvent[] = [];
      for (const m of mechanics) {
        for (const a of m.assignments) {
          if (a.slot !== slot || a.isCarryover) continue;
          casts.push({
            startSec: a.castAtSec, endSec: a.castAtSec + Math.max(a.durationSec, 1.5),
            abilityId: a.actionId, label: a.name,
            tooltip: `${a.name} — ${m.name}`,
            color: a.isSuggestion ? '#565f89' : '#9ece6a', yOffset: -1,
          });
        }
        for (const gh of m.gcdHeals) {
          if (gh.slot !== slot) continue;
          for (let k = 0; k < gh.count; k++) {
            casts.push({
              startSec: gh.castAtSec + k * gh.castTimeSec,
              endSec: gh.castAtSec + (k + 1) * gh.castTimeSec,
              abilityId: gh.actionId, label: gh.name,
              tooltip: `${gh.name} — top-up before ${m.name}`,
              color: '#9ece6a', yOffset: 0,
            });
          }
        }
      }
      return { slot, job: jobs[i], label: `${slot} · ${jobs[i]}`, casts };
    });
    onProgress?.(100, 'Done');
    return {
      encounterId, encounterName: 'Mock Encounter (M9S)',
      shieldHealer, regenHealer,
      partyJobs: [tanks[0], tanks[1], shieldHealer, regenHealer, ...dps],
      modelKillSec: 526, refCount: 10, refAvgKillSec: 531, avoidableCount: 87,
      roleHp, hpSource: 'logs',
      summary: {
        mechanicCount: 4, raidwideCount: 1, tankbusterCount: 1, bleedCount: 1,
        multiHitCount: 0, coveredCount: 4, tightCount: 0, uncoveredCount: 0,
        gcdHealCount: 2, gcdHealTimeSec: 5, gcdHealPotencyLost: 700,
        totalUnmitigated: 2_900_000, totalPredicted: 1_500_000,
      },
      mechanics,
      lanes,
      damageMarkers: mechanics.map((m) => ({
        mechanicId: m.id, timeSec: m.timeSec, endSec: m.endSec, name: m.name,
        kind: m.kind, school: m.school, status: m.status,
        unmitTotal: m.unmitigated.tank * 2 + m.unmitigated.healer * 2 + m.unmitigated.dps * 4,
      })),
      downtimeWindows: [{ startSec: 68, endSec: 74 }],
      abilityMeta: META_BY_ID,
      warnings: [],
      // Pull-context requests resolve the comp from the pull's actors.
      compSource: args.reportCode ? 'pull'
        : (args.shieldHealer ? 'request' : 'defaults'),
      // Dev-only: echo the PF-plan toggle (id 1085 is the mock's ultimate).
      pfPlanApplied: !!args.usePfMitPlan && encounterId === 1085,
    };
  },
};
