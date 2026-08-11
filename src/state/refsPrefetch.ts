// Reference warm-cache orchestrator.
//
// The Python sidecar holds a process-lifetime cache of analyzed top-10
// reference logs keyed by (job, encounter). This module decides *what* to
// warm and *in what order*, and exposes a "blocking" state for the loading
// popup:
//
//   - On launch: warm ONLY the player's saved job — its saved/active encounter
//     first, then that job's other tier encounters — silently in the background,
//     no popup. Other jobs are NOT warmed eagerly (that was ~9x the requests for
//     reference sets a session rarely touches, contending with the user's own
//     run); they warm lazily via ensureJob() when the user actually selects
//     them. The app boots straight to the setup page while this runs.
//     (`start(..., false)` selects this non-blocking path; pass `true` to gate
//     the UI with a popup, retained for reuse.) With no saved job (first
//     launch), nothing warms until the user picks one.
//   - When a job is confirmed on the setup screen: that job jumps the line —
//     its active-encounter refs are warmed with the blocking popup, and its
//     other encounters are moved to the front of the background queue.
//
// Concurrent sidecar dispatch + a backend in-flight dedup mean a "jump the
// line" warm just issues its request immediately (it runs alongside whatever
// the background loop is doing) and returns as soon as that key is ready.
//
// State lives in a module-level singleton (one sidecar, one cache); React
// subscribes via useSyncExternalStore.

import { useSyncExternalStore } from 'react';
import { sidecar } from '../sidecar';
import type { Catalog, ProgressTask } from '../sidecar/contract';
import type { RefsBucket } from './appState';

const BUCKET: RefsBucket = 'Top 10';
const keyOf = (job: string, enc: number) => `${job}::${enc}`;

type KeyStatus = 'idle' | 'inflight' | 'ready' | 'error';

/** Drives the blocking loading popup. `active` is false during silent
 *  background warming. */
export type WarmBlocking = {
  active: boolean;
  job: string | null;
  pct: number;
  stage: string;
  tasks?: ProgressTask[];
};

export type WarmSnapshot = {
  blocking: WarmBlocking;
  /** Matrix keys warmed so far / total — handy for a non-blocking indicator. */
  ready: number;
  total: number;
  /** Keys still queued or in flight. Errored keys don't count — the sidebar's
   *  background-fetch indicator must settle even when a warm fails. */
  pending: number;
};

class RefsWarmer {
  private catalog: Catalog | null = null;
  private status = new Map<string, KeyStatus>();
  private promises = new Map<string, Promise<void>>();
  // Latest progress per in-flight key, recorded for *every* warm (blocking or
  // background). Lets a blocking caller that piggybacks on an already-running
  // background warm seed the popup with live task bars instead of a bare
  // spinner. Cleared when the warm settles.
  private lastProgress = new Map<
    string,
    { pct: number; stage: string; tasks?: ProgressTask[] }
  >();
  private queue: { job: string; enc: number }[] = [];
  private started = false;
  private bgRunning = false;

  private listeners = new Set<() => void>();
  private _blocking: WarmBlocking = { active: false, job: null, pct: 0, stage: '' };
  private snapshot: WarmSnapshot = { blocking: this._blocking, ready: 0, total: 0, pending: 0 };

  // --- external store -------------------------------------------------------

  subscribe = (cb: () => void): (() => void) => {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  };

  getSnapshot = (): WarmSnapshot => this.snapshot;

  private commit(): void {
    let ready = 0;
    let pending = 0;
    for (const s of this.status.values()) {
      if (s === 'ready') ready++;
      else if (s !== 'error') pending++;
    }
    this.snapshot = { blocking: this._blocking, ready, total: this.status.size, pending };
    for (const cb of this.listeners) cb();
  }

  private setBlocking(b: Partial<WarmBlocking>): void {
    this._blocking = { ...this._blocking, ...b };
    this.commit();
  }

  // --- public API -----------------------------------------------------------

  /** Idempotent. Loads the catalog, seeds the matrix, warms the priority
   *  (job, encounter) first if given, then kicks the background fill.
   *  Fire-and-forget from the caller's perspective. When `block` is true the
   *  priority warm drives the blocking popup (legacy path, kept for reuse);
   *  when false it runs silently at the head of the background queue. */
  async start(priorityJob?: string, priorityEnc?: number, block = true): Promise<void> {
    if (this.started) return;
    this.started = true;
    try {
      this.catalog = await sidecar.getCatalog();
    } catch (e) {
      console.warn('[refsWarm] catalog lookup failed; warming disabled', e);
      this.started = false; // allow a later retry
      return;
    }

    // Narrow warm: only the saved job's encounters are warmed at launch
    // (priority encounter first). With no saved job, warm nothing — the user's
    // first job pick goes through ensureJob(). Other jobs warm lazily there too.
    if (!priorityJob || !this.catalog.supportedJobs.includes(priorityJob)) {
      return;
    }
    this.enqueueJob(priorityJob, priorityEnc ?? undefined);

    const encIds = this.catalog.encounters.map((e) => e.id);
    if (block && priorityEnc != null && encIds.includes(priorityEnc)) {
      // Blocking path (popup): wait on the priority refs before the background
      // fill. Retained for callers that want to gate the UI.
      await this.warmKey(priorityJob, priorityEnc, true);
    }
    void this.runBackground();
  }

  /** Jump a job to the front: warm its active-encounter refs with the blocking
   *  popup (pass `block=false` to warm ahead silently — e.g. Research's job
   *  pick, where the user is still browsing), and move its other encounters to
   *  the front of the background queue. Resolves once the active-encounter refs
   *  are ready (or immediately if they already are). No-op for unsupported jobs. */
  ensureJob(job: string, preferredEnc?: number, block = true): Promise<void> {
    if (!this.catalog || !this.catalog.supportedJobs.includes(job)) {
      return Promise.resolve();
    }
    const encIds = this.catalog.encounters.map((e) => e.id);
    const enc =
      preferredEnc != null && encIds.includes(preferredEnc) ? preferredEnc : encIds[0];
    if (enc == null) return Promise.resolve();
    // Lazily bring this job into the warm scope: enqueue its other encounters
    // (active one first) for background fill so switching encounters within the
    // job stays instant, then block on the active encounter's refs.
    this.enqueueJob(job, enc);
    this.reorderJobFirst(job);
    void this.runBackground();
    return this.warmKey(job, enc, block);
  }

  /** Whether a (job, encounter) reference set is already warmed. Lets a caller
   *  (e.g. speculative pre-analysis) gate work on the refs being ready. */
  isReady(job: string, enc: number): boolean {
    return this.status.get(keyOf(job, enc)) === 'ready';
  }

  // --- internals ------------------------------------------------------------

  /** Seed status + enqueue every encounter of `job` for background warming
   *  (`frontEnc` first). Idempotent: already-ready or already-queued keys are
   *  skipped. Used by both launch (saved job) and ensureJob (lazy cross-job). */
  private enqueueJob(job: string, frontEnc?: number): void {
    if (!this.catalog) return;
    const encs = this.catalog.encounters.map((e) => e.id);
    const ordered =
      frontEnc != null && encs.includes(frontEnc)
        ? [frontEnc, ...encs.filter((e) => e !== frontEnc)]
        : encs;
    for (const enc of ordered) {
      const k = keyOf(job, enc);
      if (!this.status.has(k)) this.status.set(k, 'idle');
      if (
        this.status.get(k) !== 'ready' &&
        !this.queue.some((q) => q.job === job && q.enc === enc)
      ) {
        this.queue.push({ job, enc });
      }
    }
    this.commit();
  }

  private reorderJobFirst(job: string): void {
    const head = this.queue.filter((q) => q.job === job);
    if (head.length === 0) return;
    const tail = this.queue.filter((q) => q.job !== job);
    this.queue = [...head, ...tail];
  }

  private async runBackground(): Promise<void> {
    if (this.bgRunning) return;
    this.bgRunning = true;
    try {
      // shift() re-reads the front each iteration, so ensureJob()'s reordering
      // takes effect on the next pick. Concurrency 1 — gentle on FFLogs.
      while (this.queue.length) {
        const item = this.queue.shift()!;
        if (this.status.get(keyOf(item.job, item.enc)) === 'ready') continue;
        await this.warmKey(item.job, item.enc, false);
      }
    } finally {
      this.bgRunning = false;
    }
  }

  private warmKey(job: string, enc: number, blocking: boolean): Promise<void> {
    const k = keyOf(job, enc);
    if (this.status.get(k) === 'ready') return Promise.resolve();

    const existing = this.promises.get(k);
    if (existing) {
      // Already warming (e.g. the background loop got here first). Piggyback —
      // claim the popup for this job; the in-flight warm's progress callback
      // always runs and targets the active blocking job, so live task bars
      // flow in from here on. Seed from the last reported progress so the
      // multi-task screen shows immediately instead of an empty bar.
      if (blocking) {
        const last = this.lastProgress.get(k);
        this.setBlocking({
          active: true,
          job,
          pct: last?.pct ?? 0,
          stage: last?.stage ?? `Loading ${job} references…`,
          tasks: last?.tasks,
        });
        existing.finally(() => {
          if (this._blocking.job === job) this.setBlocking({ active: false, job: null });
        });
      }
      return existing;
    }

    this.status.set(k, 'inflight');
    this.commit();
    if (blocking) {
      this.setBlocking({ active: true, job, pct: 5, stage: `Warming ${job} top-10 references…`, tasks: undefined });
    }

    // Capture progress for every warm (blocking or background) so a blocking
    // caller that later piggybacks on a background warm still gets the live
    // multi-task screen. It only drives the popup while this key's job is the
    // one currently blocking.
    const onProgress = (pct: number, stage: string, tasks?: ProgressTask[]) => {
      this.lastProgress.set(k, { pct, stage, tasks });
      if (this._blocking.active && this._blocking.job === job) {
        this.setBlocking({ active: true, job, pct, stage, tasks });
      }
    };

    const run = sidecar
      .prefetchRefs(job, enc, BUCKET, onProgress)
      .then(() => { this.status.set(k, 'ready'); })
      .catch((e) => {
        this.status.set(k, 'error');
        console.warn('[refsWarm] warm failed', job, enc, e);
      })
      .finally(() => {
        this.promises.delete(k);
        this.lastProgress.delete(k);
        if (this._blocking.active && this._blocking.job === job) this.setBlocking({ active: false, job: null });
        this.commit();
      });

    this.promises.set(k, run);
    return run;
  }
}

export const refsWarmer = new RefsWarmer();

export function useRefsWarmer(): WarmSnapshot {
  return useSyncExternalStore(refsWarmer.subscribe, refsWarmer.getSnapshot);
}
