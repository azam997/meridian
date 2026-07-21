// Frontend event logger. Events land in the sidecar's persistent event log
// via the `log_event` request; this module owns the fire-and-forget plumbing
// so callers never await, never throw, and never spawn the sidecar:
//
//   - a ring buffer of the last events (the Feedback view's fallback when the
//     sidecar is unreachable / mock mode)
//   - a pending queue for events that couldn't be delivered (sidecar down) —
//     ndjson.ts flushes it after the next successful handshake, which is how
//     "the sidecar crashed" context survives the crash it describes
//   - a rate limit so a hot error loop can't flood the log
//
// Runtime-import-free of src/sidecar/ (type imports only) — ndjson.ts imports
// this module, and src/sidecar/index.ts injects the actual sink.

import type { LogLevel } from './sidecar/contract';

export type UiLogEvent = {
  level: LogLevel;
  cat: string;
  msg: string;
  data?: Record<string, unknown>;
};

const RING_CAP = 200;
const PENDING_CAP = 100;
const SENDS_PER_MIN = 60;

const ring: UiLogEvent[] = [];
const pendingQueue: UiLogEvent[] = [];
let sink: ((e: UiLogEvent) => Promise<void>) | null = null;

let windowStart = 0;
let windowCount = 0;
let floodNoted = false;

/** Injected once by src/sidecar/index.ts. The sink may reject (sidecar down);
 *  rejected events are queued and re-sent by flushPendingLogs. */
export function setLogSink(fn: (e: UiLogEvent) => Promise<void>): void {
  sink = fn;
}

/** The in-memory tail — the Feedback view's fallback source when
 *  get_recent_events is unavailable (mock mode / sidecar down). */
export function getBufferedEvents(): UiLogEvent[] {
  return [...ring];
}

/** Re-send events that couldn't be delivered. Called by ndjson.ts right after
 *  a successful sidecar handshake (fresh spawn or respawn-after-crash). */
export function flushPendingLogs(): void {
  if (!sink || pendingQueue.length === 0) return;
  const toSend = pendingQueue.splice(0, pendingQueue.length);
  for (const e of toSend) {
    void sink(e).catch(() => {
      if (pendingQueue.length < PENDING_CAP) pendingQueue.push(e);
    });
  }
}

/** Record one event. Synchronous, never throws, never awaits — safe from any
 *  context including error handlers and the sidecar transport itself. */
export function logEvent(level: LogLevel, cat: string, msg: string,
                         data?: Record<string, unknown>): void {
  try {
    const e: UiLogEvent = data ? { level, cat, msg, data } : { level, cat, msg };
    ring.push(e);
    if (ring.length > RING_CAP) ring.shift();

    // Rate-limit the SENDS only — the ring above keeps everything.
    const now = Date.now();
    if (now - windowStart > 60_000) {
      windowStart = now;
      windowCount = 0;
      floodNoted = false;
    }
    windowCount += 1;
    if (windowCount > SENDS_PER_MIN) {
      if (!floodNoted && sink) {
        floodNoted = true;
        void sink({ level: 'warn', cat: 'log',
                    msg: 'log flood: suppressing sends for the rest of the minute' })
          .catch(() => {});
      }
      return;
    }

    if (!sink) {
      if (pendingQueue.length < PENDING_CAP) pendingQueue.push(e);
      return;
    }
    void sink(e).catch(() => {
      if (pendingQueue.length < PENDING_CAP) pendingQueue.push(e);
    });
  } catch {
    // Logging must never break the app.
  }
}
