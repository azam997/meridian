// Lightweight localStorage persistence for the user's last selection.
// Cache of character lookups and analysis results is deferred to Tauri's
// plugin-store (or platform appdata) once the shell is in place.

import type { AppState, SetupTab } from './appState';

// Bumped to v3 when `lodestoneId` / `logsCount` were added — the prior
// shape lacked them, so a v2 read would leave the character "unloaded"
// every restart and force a fresh lookup.
const KEY = 'fflogs.efficiency.analyzer.lastSelection.v3';

type Persisted = Pick<
  AppState,
  | 'characterName'
  | 'server'
  | 'region'
  | 'lodestoneId'
  | 'logsCount'
  | 'dataCenter'
  | 'avatarUrl'
  // Renamed from xivauthCharacters when XIVAuth was replaced by the FFLogs
  // sign-in — an old persisted list is simply dropped on load (the picker
  // re-fetches from FFLogs), while the rest of the selection survives.
  | 'fflogsCharacters'
  | 'job'
  | 'encounter'
  | 'encounterId'
  | 'pullId'
  | 'pullReportCode'
  | 'pullFightId'
  | 'refsBucket'
>;

/** True when this install has run before (a selection was saved at some point).
 *  Used to tell a fresh install from an upgrade — see state/whatsNew.ts, which
 *  stays silent on a first-ever launch. */
export const hasPriorSession = (): boolean => {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return false;
  }
};

export const loadLastSelection = (): Partial<Persisted> => {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Partial<Persisted>) : {};
  } catch {
    return {};
  }
};

// Which encounter-picker tab (Savage / Ultimates) the user last had open, so
// a returning ultimate progger re-lands on Ultimates. Stored under its own key
// (it's view state, not part of the analysis selection).
const SETUP_TAB_KEY = 'fflogs.efficiency.analyzer.setupTab.v1';

export const loadSetupTab = (): SetupTab => {
  try {
    return localStorage.getItem(SETUP_TAB_KEY) === 'ultimates' ? 'ultimates' : 'setup';
  } catch {
    return 'setup';
  }
};

export const saveSetupTab = (tab: SetupTab): void => {
  try {
    localStorage.setItem(SETUP_TAB_KEY, tab);
  } catch {
    /* ignore quota / private-mode errors */
  }
};

export const saveLastSelection = (s: AppState): void => {
  const slim: Persisted = {
    characterName: s.characterName,
    server: s.server,
    region: s.region,
    lodestoneId: s.lodestoneId,
    logsCount: s.logsCount,
    dataCenter: s.dataCenter,
    avatarUrl: s.avatarUrl,
    fflogsCharacters: s.fflogsCharacters,
    job: s.job,
    encounter: s.encounter,
    encounterId: s.encounterId,
    pullId: s.pullId,
    pullReportCode: s.pullReportCode,
    pullFightId: s.pullFightId,
    refsBucket: s.refsBucket,
  };
  try {
    localStorage.setItem(KEY, JSON.stringify(slim));
  } catch {
    /* ignore quota / private-mode errors */
  }
};
