// Real sidecar client. Spawns the Python core via tauri-plugin-shell and
// pipes NDJSON over stdin/stdout. Active only when running under Tauri —
// for `npm run dev` in a plain browser we fall back to the mock client.
//
// Two spawn paths:
//   - dev (import.meta.env.DEV): `python -m sidecar.main` with cwd set to
//     the python/ subfolder of this repo. The capability scope in
//     src-tauri/capabilities/default.json names this allowance.
//   - prod: `Command.sidecar('binaries/fflogs-efficiency-analyzer-sidecar')`
//     — bundled PyInstaller exe (see src-tauri/tauri.conf.json bundle.externalBin).

import { PROTOCOL_VERSION, SidecarError } from './contract';
import { emitAuthExpired } from './authExpired';
import { flushPendingLogs, logEvent } from '../log';
import type {
  Sidecar, LookupResult, AnalysisResult, Catalog, CacheStats, PrefetchResult, ProgressTask,
  ProgressMeta, HandshakeResult, TheorizeResult, MitPlanResult, SetupData, RankingEntry,
  AuthStatus, AuthBeginResult, AuthPollResult, UserCharactersResult,
  RecentEventsResult, FeedbackBundleResult, ProgPullsResult,
} from './contract';
import type { Region, RefsBucket } from '../state/appState';

type Pending = {
  resolve: (data: unknown) => void;
  reject: (err: Error) => void;
  onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                meta?: ProgressMeta) => void;
  kind: string;
  timer?: ReturnType<typeof setTimeout>;
};

// Inactivity window: if a request gets neither a response nor a progress event
// for this long, the sidecar is assumed hung and the promise rejects so the UI
// shows its error banner instead of an infinite LoadingView spinner. Reset on
// every progress event, so a long-but-progressing analysis is never killed.
// 120s: the heaviest job (Samurai's width-256 diverse beam, doubled by the
// sub-GCD cadence sweep) can take >60s of silence on one ref of a long pull
// before the next ref-completion progress event lands during the warm fan-out.
const IDLE_TIMEOUT_MS = 120_000;

// Cwd for the dev sidecar. `cargo run` launches the dev app with cwd set to
// the Cargo workspace (src-tauri/), so the default is relative to that.
// Override with VITE_SIDECAR_CWD in a .env.local if needed.
const DEV_SIDECAR_CWD =
  (import.meta as ImportMeta & { env: { VITE_SIDECAR_CWD?: string } }).env
    .VITE_SIDECAR_CWD ?? '../python';

const pending = new Map<string, Pending>();
let nextId = 1;

type ChildHandle = {
  write(line: string): Promise<void>;
  kill(): Promise<void>;
};

let child: ChildHandle | null = null;
// Resolves once the child is spawned AND the protocol handshake has passed, so
// no real request is written before the sidecar is confirmed compatible.
// Concurrent first callers share this one promise; it's nulled on close/failure
// so the next call re-spawns.
let childReady: Promise<ChildHandle> | null = null;

// Sealed the moment we begin tearing the sidecar down for an update install.
// NSIS is about to overwrite the sidecar exe + its onedir `_internal/*.dll`, so
// from here on NO request may respawn the process — a respawn re-imports the
// analyzer and prestarts the sim-pool WORKER processes, which re-map (re-lock)
// those exact DLLs mid-write and produce "error opening file for writing". This
// is the respawn race the sim-pool drain alone cannot prevent: draining kills
// the *current* workers, but a background refs warm / cache poll / retry landing
// during the install would spin up a fresh sidecar + workers behind its back.
// Only an ABORTED update clears it (unsealSidecar) — a successful install
// relaunches the app, so the flag never needs resetting on the happy path.
let sealed = false;

/** Whether a sidecar child is currently up. The log sink gates on this so a
 *  fire-and-forget log send can never be the thing that (re)spawns the
 *  sidecar — a crash-logging-respawn loop must be impossible. */
export function isSidecarUp(): boolean {
  return child !== null;
}

// Last stderr lines from the child — crash context for the 'sidecar exited'
// event (a Python traceback goes to stderr right before a hard death).
const STDERR_TAIL_CAP = 40;
const stderrTail: string[] = [];

function ensureChild(): Promise<ChildHandle> {
  // An update install is (about to be) in flight — refuse to respawn so we can't
  // re-lock the DLLs NSIS is overwriting. Callers (background warms, polls) just
  // see a rejected request and move on; the app relaunches on a successful update.
  if (sealed) {
    return Promise.reject(new Error('sidecar sealed: an update install is in progress'));
  }
  if (childReady) return childReady;
  childReady = spawnAndHandshake().catch((e) => {
    // Startup failed (spawn or handshake) — clear so a later call retries with
    // a fresh process instead of being stuck on the rejected promise.
    child = null;
    childReady = null;
    throw e;
  });
  return childReady;
}

async function spawnAndHandshake(): Promise<ChildHandle> {
  const { Command } = await import('@tauri-apps/plugin-shell');

  // Dev mode spawns the user's system Python pointed at the existing repo.
  // Prod mode uses the bundled sidecar binary.
  const cmd = import.meta.env.DEV
    ? Command.create('dev-sidecar-python', ['-m', 'sidecar.main'], {
        cwd: DEV_SIDECAR_CWD,
      })
    : Command.sidecar('binaries/fflogs-efficiency-analyzer-sidecar');

  cmd.stdout.on('data', (line: string) => {
    for (const piece of line.split('\n')) {
      const s = piece.trim();
      if (!s) continue;
      handleLine(s);
    }
  });
  cmd.stderr.on('data', (line: string) => {
    console.warn('[sidecar]', line);
    stderrTail.push(line);
    if (stderrTail.length > STDERR_TAIL_CAP) stderrTail.shift();
  });
  cmd.on('close', () => {
    const wasUp = child !== null;
    child = null;
    childReady = null;
    const pendingKinds = [...pending.values()].map((p) => p.kind);
    for (const { reject, timer } of pending.values()) {
      if (timer) clearTimeout(timer);
      reject(new Error('sidecar exited'));
    }
    pending.clear();
    // An intentional shutdown (updater) nulls `child` before killing — only an
    // unexpected death gets logged. The event queues while the child is down
    // and flushes into the log after the next successful respawn handshake.
    if (wasUp) {
      logEvent('error', 'sidecar', 'sidecar exited unexpectedly',
               { stderrTail: stderrTail.join('\n'), pendingKinds });
    }
  });

  const c = await cmd.spawn();
  child = {
    write: (line: string) => c.write(line),
    kill: () => c.kill(),
  };
  await handshake(child);
  // Deliver anything logged while no sidecar was up — including the crash
  // context of the very death this spawn is recovering from.
  flushPendingLogs();
  return child;
}

// Bounded wait for the sidecar to drain its sim-pool WORKER processes. Those
// are grandchildren of the app that also map the onedir _internal/*.dll files;
// a bare kill of the main sidecar can't reach them, and their parent-death
// watchdog fires asynchronously — so NSIS would race live workers and fail every
// locked file ("error opening file for writing"). Resolves on the sidecar's
// reply or after DRAIN_TIMEOUT_MS, whichever comes first — the caller kills the
// process regardless, so this can only ever delay, never block, the install.
const DRAIN_TIMEOUT_MS = 8_000;
function drainSimPoolWorkers(c: ChildHandle): Promise<void> {
  const id = String(nextId++);
  return new Promise<void>((resolve) => {
    const done = () => {
      const p = pending.get(id);
      if (p?.timer) clearTimeout(p.timer);
      pending.delete(id);
      resolve();
    };
    const timer = setTimeout(done, DRAIN_TIMEOUT_MS);
    // resolve/reject both just settle — a drain error still proceeds to kill.
    pending.set(id, { resolve: done, reject: done, kind: 'prepare_update', timer });
    c.write(JSON.stringify({ id, kind: 'prepare_update' }) + '\n').catch(done);
  });
}

/** Shut the sidecar down before the updater installs, so NSIS never hits a
 *  locked exe / mapped DLL. Three bounded, best-effort steps: (1) ask the
 *  sidecar to drain its sim-pool worker processes and wait for their
 *  _internal/*.dll handles to release — the parent-death watchdog alone races
 *  the installer; (2) hard-kill the main process; (3) let Windows finish
 *  tearing it down before the NSIS handoff. Safe when no child is running. */
export async function shutdownSidecar(): Promise<void> {
  // Seal FIRST — before we even look at the child. From this instant no call may
  // respawn the sidecar (see the `sealed` flag): a warm/poll/retry racing the
  // install is precisely what re-locks the DLLs and reproduces the "error opening
  // file for writing" the drain can't stop on its own.
  sealed = true;
  const c = child;
  if (!c) return;

  // 1. Graceful: drain the worker grandchildren and wait for them to exit.
  try {
    await drainSimPoolWorkers(c);
  } catch {
    // best-effort — the kill below still fires
  }

  // 2. Tear down our own bookkeeping and hard-kill the main process.
  child = null;
  childReady = null;
  for (const { reject, timer } of pending.values()) {
    if (timer) clearTimeout(timer);
    reject(new Error('sidecar shut down'));
  }
  pending.clear();
  try {
    await c.kill();
  } catch {
    // Already exited — nothing to do.
  }

  // 3. Let Windows release the sidecar exe + its _internal/ handles before NSIS
  //    starts overwriting them (kill() returns before teardown completes).
  await new Promise((r) => setTimeout(r, 1200));
}

/** Re-enable sidecar spawning after an update install was ABORTED (a download or
 *  install error). A successful install relaunches the app, so this is only for
 *  the failure path — without it a failed update would leave the still-running
 *  app with a permanently dead sidecar (every action rejecting as "sealed"). */
export function unsealSidecar(): void {
  sealed = false;
}

// Startup protocol check. Sends one `handshake` request directly on the freshly
// spawned child (bypassing `call`, which would re-enter ensureChild) and rejects
// on a version mismatch so the UI surfaces a clear "reinstall" message instead
// of mis-parsing later payloads from a build-skewed sidecar.
async function handshake(c: ChildHandle): Promise<void> {
  // The app version rides along for the sidecar's event log + feedback bundle
  // (Python has no app-version constant). Best-effort — '' if the API fails.
  const appVersion = await import('@tauri-apps/api/app')
    .then((m) => m.getVersion())
    .catch(() => undefined);
  const id = String(nextId++);
  const resp = await new Promise<HandshakeResult>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (d: unknown) => void, reject, kind: 'handshake' });
    armTimeout(id);
    c.write(JSON.stringify({ id, kind: 'handshake', appVersion }) + '\n').catch((e) => {
      const p = pending.get(id);
      if (p?.timer) clearTimeout(p.timer);
      pending.delete(id);
      reject(e);
    });
  });
  if (resp.protocolVersion !== PROTOCOL_VERSION) {
    throw new Error(
      `sidecar protocol mismatch: the UI expects v${PROTOCOL_VERSION} but the ` +
      `bundled analyzer reports v${resp.protocolVersion}. The app and its ` +
      `analyzer component are from different builds — please reinstall.`
    );
  }
}

// (Re)arm the inactivity timer for a pending request.
function armTimeout(id: string): void {
  const p = pending.get(id);
  if (!p) return;
  if (p.timer) clearTimeout(p.timer);
  p.timer = setTimeout(() => {
    pending.delete(id);
    if (p.kind !== 'log_event') {  // a timed-out log send isn't worth a log
      logEvent('error', 'sidecar', 'request timed out', { kind: p.kind });
    }
    p.reject(
      new Error(`sidecar timed out: no response to '${p.kind}' for ${IDLE_TIMEOUT_MS / 1000}s`)
    );
  }, IDLE_TIMEOUT_MS);
}

function handleLine(line: string): void {
  let msg: { id?: string; ok?: boolean; data?: unknown; error?: string;
             errorCode?: string;
             progress?: { pct?: number; stage: string; tasks?: ProgressTask[];
                          step?: number; steps?: string[] } };
  try {
    msg = JSON.parse(line);
  } catch {
    console.warn('[sidecar] bad line:', line);
    logEvent('warn', 'sidecar', 'unparseable stdout line',
             { preview: line.slice(0, 200) });
    return;
  }
  const id = msg.id;
  if (!id) return;
  const p = pending.get(id);
  if (!p) return;

  if (msg.progress) {
    armTimeout(id);  // progress = still alive; reset the inactivity window.
    p.onProgress?.(msg.progress.pct ?? 0, msg.progress.stage, msg.progress.tasks,
                   { step: msg.progress.step, steps: msg.progress.steps });
    return;
  }
  if (p.timer) clearTimeout(p.timer);
  pending.delete(id);
  if (msg.ok && 'data' in msg) {
    p.resolve(msg.data);
  } else {
    // Broadcast expired-sign-in failures so App can flip to the sign-in gate
    // no matter which request (or background warm) tripped it.
    if (msg.errorCode === 'auth_expired') emitAuthExpired();
    p.reject(new SidecarError(msg.error ?? 'unknown sidecar error', msg.errorCode));
  }
}

async function call<T>(
  kind: string,
  payload: Record<string, unknown>,
  onProgress?: (pct: number, stage: string, tasks?: ProgressTask[],
                meta?: ProgressMeta) => void
): Promise<T> {
  const c = await ensureChild();
  const id = String(nextId++);
  const req = JSON.stringify({ id, kind, ...payload }) + '\n';
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (d: unknown) => void, reject, onProgress, kind });
    armTimeout(id);
    c.write(req).catch((e) => {
      const p = pending.get(id);
      if (p?.timer) clearTimeout(p.timer);
      pending.delete(id);
      reject(e);
    });
  });
}

export const ndjsonSidecar: Sidecar = {
  getAuthStatus: () => call<AuthStatus>('get_auth_status', {}),

  fflogsAuthBegin: () => call<AuthBeginResult>('fflogs_auth_begin', {}),

  fflogsAuthPoll: () => call<AuthPollResult>('fflogs_auth_poll', {}),

  fflogsAuthCancel: () => call<void>('fflogs_auth_cancel', {}),

  fflogsLogout: () => call<AuthStatus>('fflogs_logout', {}),

  listUserCharacters: () => call<UserCharactersResult>('list_user_characters', {}),

  lookupCharacter: (name, server, region: Region, spec) =>
    call<LookupResult>('lookup_character', { name, server, region, spec }),

  listEncounters: (lodestoneId, zoneId, spec) =>
    call('list_encounters', { lodestoneId, zoneId, spec }),

  listPulls: (lodestoneId, encounterId, spec) =>
    call('list_pulls', { lodestoneId, encounterId, spec }),

  listProgPulls: (args) =>
    call<ProgPullsResult>('list_prog_pulls', { ...args }),

  listSetup: (lodestoneId, spec) =>
    call<SetupData>('list_setup', { lodestoneId, spec }),

  getCatalog: () => call<Catalog>('get_catalog', {}),

  cacheStats: () => call<CacheStats>('cache_stats', {}),

  setCacheCap: (capMb) => call<CacheStats>('set_cache_cap', { capMb }),

  clearCache: () => call<CacheStats>('clear_cache', {}),

  logEvent: (level, cat, msg, data) =>
    call<void>('log_event', { level, cat, msg, data }),

  getRecentEvents: (limit) =>
    call<RecentEventsResult>('get_recent_events', { limit }),

  exportFeedbackBundle: (args) =>
    call<FeedbackBundleResult>('export_feedback_bundle', { ...args }),

  prefetchRefs: (spec, encounterId, refsBucket: RefsBucket, onProgress) =>
    call<PrefetchResult>('prefetch_refs', { spec, encounterId, refsBucket }, onProgress),

  listRankings: (spec, encounterId) =>
    call<RankingEntry[]>('list_rankings', { spec, encounterId }),

  runAnalysis: (reportCode, fightId, spec, encounterId, refsBucket: RefsBucket, playerName, onProgress, comp, usePfMitPlan) =>
    call<AnalysisResult>(
      'run_analysis',
      // An undefined playerName is dropped by JSON.stringify — the wire shape
      // is unchanged for normal (own-character) runs. The healer flow's comp
      // override + usePfMitPlan spread the same way (absent unless given).
      {
        reportCode, fightId, spec, encounterId, refsBucket, playerName,
        shieldHealer: comp?.shieldHealer, regenHealer: comp?.regenHealer,
        tanks: comp?.tanks, dps: comp?.dps, usePfMitPlan,
      },
      onProgress
    ),

  theorizeKillTime: (spec, encounterId, targetKillSec, rangeSec, partyJobs, onProgress) =>
    call<TheorizeResult>('theorize_kill_time', {
      spec, encounterId, targetKillSec, rangeSec, partyJobs,
    }, onProgress),

  planMitigation: (args, onProgress) =>
    call<MitPlanResult>('plan_mitigation', { ...args }, onProgress),
};
