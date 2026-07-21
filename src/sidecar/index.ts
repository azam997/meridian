// Sidecar entry point. Picks the real NDJSON-over-Tauri client when running
// under Tauri; falls back to the mock client for `npm run dev` in a plain
// browser.

import { isTauri } from '@tauri-apps/api/core';
import { mockSidecar } from './mock';
import { ndjsonSidecar, isSidecarUp } from './ndjson';
import { setLogSink } from '../log';
import type { Sidecar } from './contract';

export const sidecar: Sidecar = isTauri() ? ndjsonSidecar : mockSidecar;
export type { Sidecar } from './contract';
export type { LookupResult, AnalysisResult } from './contract';

// Wire src/log.ts to the sidecar's event log. Rejecting while the child is
// down (instead of calling, which would respawn it via ensureChild) is what
// makes a crash-logging-respawn loop impossible: the event queues in log.ts
// and ndjson.ts flushes it after the next REAL request respawns the sidecar.
setLogSink((e) => {
  if (isTauri() && !isSidecarUp()) {
    return Promise.reject(new Error('sidecar down'));
  }
  return sidecar.logEvent(e.level, e.cat, e.msg, e.data);
});
