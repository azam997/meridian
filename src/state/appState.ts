// Top-level app state.

// Type-only imports — erased at compile time, so the contract.ts ↔ appState.ts
// reference cycle never exists at runtime.
import type { AnalysisResult, EncounterCategory, UserCharacter } from '../sidecar/contract';

export type Region = 'NA' | 'EU' | 'JP' | 'OC';
// Only "Top 10" is wired up for now. Top 50/100 were duplicates of Top 10 and
// Median/Job-median were mis-implemented (they pulled the *best* logs, not the
// middle) — dropped pending a better ref-sampling design.
export type RefsBucket = 'Top 10';
export type View =
  | 'home'
  | 'setup'
  | 'ultimates'
  | 'dashboard'
  | 'timeline'
  | 'counts'
  | 'research'
  | 'theorizer'
  | 'mitigation'
  | 'settings'
  | 'feedback'
  | 'changelog';
/** The two encounter-picker tabs. Both render the same SetupView, scoped by
 *  encounter category. */
export type SetupTab = 'setup' | 'ultimates';
export type Stage = 'setup' | 'loading' | 'dashboard';
export type AnalysisStatus = 'idle' | 'loading' | 'ready' | 'error';

export type Pull = {
  reportCode: string;
  fightId: number;
  startTimeMs: number;
  durationS: number;
  parsePct: number;
  dps: number;
  label: string;
};

export type Encounter = {
  id: number;
  name: string;
  totalKills: number;
  bestParsePct: number | null;
  category?: EncounterCategory;
};

/** Slim shape of a claimed FFLogs character — enough to render the picker's
 *  quick-switch list and rehydrate the active selection. FFLogs has no
 *  avatar URL; the UI falls back to a generated initial chip. */
export type PersistedCharacter = {
  lodestoneId: number;
  name: string;
  server: string;
  dataCenter?: string;
  region: Region;
  avatarUrl?: string;
};

/** Slim a sidecar UserCharacter down to the persisted shape. Shared by the
 *  character select flow and App's launch auto-pick. */
export const toPersistedCharacter = (c: UserCharacter): PersistedCharacter => ({
  lodestoneId: c.lodestoneId,
  name: c.name,
  server: c.server,
  dataCenter: c.dataCenter,
  region: c.region,
  avatarUrl: c.avatarUrl,
});

export type AppState = {
  characterName: string;
  server: string;
  region: Region;
  /** Resolved on first successful lookup. Persisted so the character
   *  survives across app restarts without a re-lookup; presence of this
   *  field is the authoritative "character is loaded" signal. */
  lodestoneId?: number;
  logsCount?: number;
  /** Datacenter (FFLogs subregion, e.g. "Aether"). Manual mode doesn't
   *  populate this. Informational — used in the sidebar tile. */
  dataCenter?: string;
  /** Avatar image URL. FFLogs provides none, so this is normally unset and
   *  the UI falls back to a generated letter chip (kept for forward compat
   *  and for old persisted XIVAuth-era selections). */
  avatarUrl?: string;
  /** Characters claimed on the signed-in FFLogs account, snapshot at last
   *  picker fetch. Persisted so the logged-in panel can quick-switch without
   *  a round-trip. Absent for manual-mode picks. */
  fflogsCharacters?: PersistedCharacter[];

  job: string;
  /** Display label, e.g. "Vamp Fatale (M9S)". */
  encounter: string;
  /** The encounter's numeric ID (FFLogs encounter ID). */
  encounterId: number;
  /** Display label for the chosen pull. */
  pullId: string;
  /** Resolved (report_code, fight_id) for the chosen pull. */
  pullReportCode: string;
  pullFightId: number;
  refsBucket: RefsBucket;
  /** Analyze this named player instead of the loaded character — set only by
   *  Research loads (the subject is someone else's ranked pull), cleared by
   *  Setup runs. Not persisted. */
  playerName?: string;

  pullsLoaded: boolean;
  analysisStatus: AnalysisStatus;

  analysis?: AnalysisResult;
};

export const initialState: AppState = {
  characterName: '',
  server: '',
  region: 'NA',
  job: '',
  encounter: '',
  encounterId: 0,
  pullId: '',
  pullReportCode: '',
  pullFightId: 0,
  refsBucket: 'Top 10',
  pullsLoaded: false,
  analysisStatus: 'idle',
};
