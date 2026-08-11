// Auto-update singleton (tauri-plugin-updater over GitHub Releases, NSIS).
// Checked once on launch in packaged builds only — `check()` is meaningless
// in `tauri dev` and impossible in the browser mock. App renders an
// "Update available" pill from this store; installing kills the sidecar
// first so NSIS never hits a locked exe, then relaunches.
//
// useSyncExternalStore singleton, same shape as state/refsPrefetch.ts.

import { useSyncExternalStore } from 'react';
import { isTauri } from '@tauri-apps/api/core';
import type { Update } from '@tauri-apps/plugin-updater';

import { shutdownSidecar, unsealSidecar } from '../sidecar/ndjson';

export type UpdaterPhase =
  | 'idle'          // no update known (not checked / up to date)
  | 'available'     // update found, waiting on the user
  | 'downloading'
  | 'installing'    // download done, NSIS handoff imminent
  | 'error';

export type UpdaterState = {
  phase: UpdaterPhase;
  /** Version offered by the feed (phase 'available'+). */
  version?: string;
  /** Release notes from latest.json's `notes`. */
  notes?: string;
  /** Download progress 0-100 (phase 'downloading'; absent if size unknown). */
  progressPct?: number;
  error?: string;
  /** True once a launch/manual check has completed (drives "up to date"). */
  checked: boolean;
};

let state: UpdaterState = { phase: 'idle', checked: false };
const listeners = new Set<() => void>();

function setState(next: Partial<UpdaterState>): void {
  state = { ...state, ...next };
  for (const l of listeners) l();
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};
const getSnapshot = () => state;

let update: Update | null = null;

/** True only in a packaged desktop build — dev/mocked contexts never check. */
const canUpdate = () => isTauri() && !import.meta.env.DEV;

export async function checkForUpdate(): Promise<void> {
  if (!canUpdate()) {
    setState({ checked: true });
    return;
  }
  try {
    const { check } = await import('@tauri-apps/plugin-updater');
    const u = await check();
    if (u) {
      update = u;
      setState({ phase: 'available', version: u.version, notes: u.body ?? '',
                 checked: true });
    } else {
      setState({ checked: true });
    }
  } catch (e) {
    // A dead feed must never break the app — log and move on (the pill
    // simply doesn't appear). Manual re-check available from Settings.
    console.warn('[updater] check failed:', e);
    setState({ checked: true });
  }
}

export async function installUpdate(): Promise<void> {
  if (!update || state.phase === 'downloading' || state.phase === 'installing') return;
  setState({ phase: 'downloading', progressPct: undefined, error: undefined });
  try {
    // 1. Download FIRST, with the sidecar still alive. The download is the long,
    //    network-bound half; there's no reason to be down for it, and keeping the
    //    sidecar up means we don't sit sealed (or racing a respawn) for seconds —
    //    only the short install below needs the sidecar gone.
    let total = 0;
    let got = 0;
    await update.download((ev) => {
      if (ev.event === 'Started') {
        total = ev.data.contentLength ?? 0;
      } else if (ev.event === 'Progress') {
        got += ev.data.chunkLength;
        if (total > 0) setState({ progressPct: Math.min(100, (got / total) * 100) });
      } else if (ev.event === 'Finished') {
        setState({ progressPct: 100 });
      }
    });

    // 2. Now tear the sidecar down. shutdownSidecar() SEALS first (no call may
    //    respawn it), then drains the sim-pool WORKER grandchildren that map the
    //    onedir _internal/*.dll and waits for their handles to release. Both the
    //    seal and the drain are load-bearing: the drain kills the current workers,
    //    the seal stops a background warm / cache poll / retry from spinning up a
    //    fresh sidecar + workers that re-lock those DLLs mid-install — the
    //    respawn race that reproduced "error opening file for writing".
    setState({ phase: 'installing' });
    await shutdownSidecar();

    // 3. Install the already-downloaded package. Short window, sidecar sealed —
    //    NSIS overwrites the exe + _internal/ with nothing holding those handles.
    await update.install();

    const { relaunch } = await import('@tauri-apps/plugin-process');
    await relaunch();
  } catch (e) {
    // The update aborted (download or install failed). Un-seal so the still-
    // running app keeps a working sidecar instead of every action rejecting.
    unsealSidecar();
    setState({ phase: 'error', error: e instanceof Error ? e.message : String(e) });
  }
}

export function useUpdater(): UpdaterState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
