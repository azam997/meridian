// Pulls-screen data store — a module singleton on the refsPrefetch.ts pattern
// (useSyncExternalStore, immutable snapshots). Owns the job-agnostic pull list
// (`list_character_pulls`), the client-side filters, and the merge paths
// (pasted reports, per-encounter wipe scans). A store rather than view state
// because the sidebar's Pulls badge needs the row count and the filters must
// survive navigation; all of it is session-only by design (the app's "job is
// per-session" stance — persist.ts keeps only the run snapshot).

import { useSyncExternalStore } from 'react';

import { sidecar } from '../sidecar';
import type { PullRow, SetupEncounter } from '../sidecar/contract';
import { logEvent } from '../log';

export type PullsSort = 'newest' | 'oldest' | 'parse';

export type PullsFilters = {
  /** null = all jobs. */
  jobFilter: string | null;
  categoryFilter: 'all' | 'savage' | 'ultimate';
  outcomeFilter: 'all' | 'kills' | 'wipes';
  /** Free-text filter (encounter/job name). Report-link pastes are detected
   *  by the view and routed to mergePasted instead. */
  query: string;
  sort: PullsSort;
};

export type PullsSnapshot = {
  rows: PullRow[];
  encounters: SetupEncounter[];
  /** Jobs with >= 1 row, role order — the job pill's menu. */
  jobs: string[];
  syncedAtMs: number | null;
  recentLimit: number;
  loading: boolean;
  /** Backend progress stage while loading ("Scanning jobs (2/5)"…). */
  loadingStage: string;
  /** In-flight paste/scan (row-merge) work — smaller spinner, list stays. */
  merging: boolean;
  error: string | null;
  lodestoneId: number | null;
  /** Encounters the user explicitly deep-scanned this session. */
  scannedEncounters: ReadonlySet<number>;
  filters: PullsFilters;
};

const DEFAULT_FILTERS: PullsFilters = {
  jobFilter: null,
  categoryFilter: 'all',
  outcomeFilter: 'all',
  query: '',
  sort: 'newest',
};

/** "Load older pulls" ladder for the recent-report wipe scan. */
const RECENT_LIMITS = [10, 25, 50];

const rowKey = (r: { reportCode: string; fightId: number }): string =>
  `${r.reportCode}:${r.fightId}`;

class PullsStore {
  private rows: PullRow[] = [];
  private encounters: SetupEncounter[] = [];
  private jobs: string[] = [];
  private syncedAtMs: number | null = null;
  private recentLimit = RECENT_LIMITS[0];
  private loading = false;
  private loadingStage = '';
  private merging = false;
  private error: string | null = null;
  private lodestoneId: number | null = null;
  private characterName = '';
  private server = '';
  private scanned = new Set<number>();
  private filters: PullsFilters = { ...DEFAULT_FILTERS };
  private loadSeq = 0;

  private listeners = new Set<() => void>();
  private snapshot: PullsSnapshot = this.buildSnapshot();

  // --- external store --------------------------------------------------------

  subscribe = (cb: () => void): (() => void) => {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  };

  getSnapshot = (): PullsSnapshot => this.snapshot;

  private buildSnapshot(): PullsSnapshot {
    return {
      rows: this.rows,
      encounters: this.encounters,
      jobs: this.jobs,
      syncedAtMs: this.syncedAtMs,
      recentLimit: this.recentLimit,
      loading: this.loading,
      loadingStage: this.loadingStage,
      merging: this.merging,
      error: this.error,
      lodestoneId: this.lodestoneId,
      scannedEncounters: this.scanned,
      filters: this.filters,
    };
  }

  private commit(): void {
    this.snapshot = this.buildSnapshot();
    for (const cb of this.listeners) cb();
  }

  // --- public API -------------------------------------------------------------

  /** Load (or reuse) the list for a character. A different lodestoneId resets
   *  everything and refetches; the same one is a no-op unless nothing loaded
   *  yet. Fire-and-forget. */
  load(lodestoneId: number, characterName: string, server: string): void {
    if (this.lodestoneId === lodestoneId
        && (this.loading || this.syncedAtMs !== null)) return;
    if (this.lodestoneId !== lodestoneId) {
      this.rows = [];
      this.encounters = [];
      this.jobs = [];
      this.syncedAtMs = null;
      this.scanned = new Set();
      this.recentLimit = RECENT_LIMITS[0];
      this.filters = { ...DEFAULT_FILTERS };
    }
    this.lodestoneId = lodestoneId;
    this.characterName = characterName;
    this.server = server;
    void this.fetch({ forceRefresh: false, recentLimit: this.recentLimit });
  }

  /** The Refresh button: bust the backend memo and replace the list. */
  refresh(): void {
    if (this.lodestoneId == null || this.loading) return;
    void this.fetch({ forceRefresh: true, recentLimit: this.recentLimit });
  }

  /** "Load older pulls": deepen the wipe scan (10 -> 25 -> 50 recent reports).
   *  Returns false when already at the deepest rung. */
  loadOlder(): boolean {
    if (this.lodestoneId == null || this.loading) return false;
    const next = RECENT_LIMITS[RECENT_LIMITS.indexOf(this.recentLimit) + 1];
    if (!next) return false;
    void this.fetch({ forceRefresh: false, recentLimit: next });
    return true;
  }

  private async fetch(opts: { forceRefresh: boolean; recentLimit: number }):
      Promise<void> {
    const seq = ++this.loadSeq;
    const { lodestoneId, characterName, server } = this;
    if (lodestoneId == null) return;
    this.loading = true;
    this.loadingStage = '';
    this.error = null;
    this.commit();
    try {
      const res = await sidecar.listCharacterPulls(
        { lodestoneId, characterName, server, ...opts },
        (_pct, stage) => {
          if (seq !== this.loadSeq) return;
          this.loadingStage = stage;
          this.commit();
        });
      if (seq !== this.loadSeq) return; // superseded by a newer load
      this.rows = res.pulls;
      this.encounters = res.encounters;
      this.jobs = res.jobs;
      this.syncedAtMs = res.syncedAtMs;
      this.recentLimit = res.recentLimit || opts.recentLimit;
    } catch (e) {
      if (seq !== this.loadSeq) return;
      this.error = e instanceof Error ? e.message : String(e);
      logEvent('warn', 'pulls', 'list_character_pulls failed',
               { error: this.error });
    } finally {
      if (seq === this.loadSeq) {
        this.loading = false;
        this.loadingStage = '';
        this.commit();
      }
    }
  }

  /** Pasted-report mode: fetch one report's rows and merge them in. Existing
   *  rows win on collision (a ranked kill row is richer than its pasted twin).
   *  Resolves to the number of NEW rows; throws with the backend's reason on
   *  a bad code / no matching fights. */
  async mergePasted(reportCode: string): Promise<number> {
    if (this.lodestoneId == null) throw new Error('No character loaded');
    this.merging = true;
    this.error = null;
    this.commit();
    try {
      const res = await sidecar.listCharacterPulls({
        lodestoneId: this.lodestoneId,
        characterName: this.characterName,
        server: this.server,
        reportCode,
      });
      return this.mergeRows(res.pulls);
    } finally {
      this.merging = false;
      this.commit();
    }
  }

  /** Deep-scan one encounter for wipes beyond the recent-report window: the
   *  existing per-(encounter, job) prog discovery, run for each job seen in
   *  the list, results merged as wipe rows. */
  async scanEncounter(encounterId: number): Promise<number> {
    if (this.lodestoneId == null) return 0;
    const jobs = this.jobs.length ? this.jobs : [];
    this.merging = true;
    this.commit();
    try {
      const results = await Promise.allSettled(jobs.map((job) =>
        sidecar.listProgPulls({ lodestoneId: this.lodestoneId as number,
                                encounterId, spec: job })
          .then((r) => ({ job, pulls: r.pulls }))));
      const rows: PullRow[] = [];
      for (const r of results) {
        if (r.status !== 'fulfilled') continue;
        for (const p of r.value.pulls) {
          rows.push({
            reportCode: p.reportCode, fightId: p.fightId, encounterId,
            job: r.value.job, kill: false,
            startTimeMs: p.startTimeMs, durationS: p.durationS,
            parsePct: null, dps: null,
            fightPercentage: p.fightPercentage,
            bossPercentage: p.bossPercentage,
            lastPhase: p.lastPhase,
          });
        }
      }
      const added = this.mergeRows(rows);
      this.scanned = new Set([...this.scanned, encounterId]);
      return added;
    } finally {
      this.merging = false;
      this.commit();
    }
  }

  /** Dedupe-merge new rows into the list (existing rows win), keep newest-
   *  first, refresh the jobs menu. Returns how many rows were actually new. */
  private mergeRows(incoming: PullRow[]): number {
    const seen = new Set(this.rows.map(rowKey));
    const fresh = incoming.filter((r) => !seen.has(rowKey(r)));
    if (fresh.length) {
      this.rows = [...this.rows, ...fresh]
        .sort((a, b) => b.startTimeMs - a.startTimeMs);
      const jobsSeen = new Set(this.rows.map((r) => r.job));
      // Keep the backend's role ordering, append any newly-seen job.
      this.jobs = [...this.jobs.filter((j) => jobsSeen.has(j)),
                   ...[...jobsSeen].filter((j) => !this.jobs.includes(j))];
    }
    return fresh.length;
  }

  setFilters(patch: Partial<PullsFilters>): void {
    this.filters = { ...this.filters, ...patch };
    this.commit();
  }

  clearError(): void {
    if (this.error === null) return;
    this.error = null;
    this.commit();
  }
}

export const pullsStore = new PullsStore();

export function usePulls(): PullsSnapshot {
  return useSyncExternalStore(pullsStore.subscribe, pullsStore.getSnapshot);
}
