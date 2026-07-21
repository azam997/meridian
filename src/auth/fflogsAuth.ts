// Drives the sidecar's FFLogs PKCE sign-in: begin → open the system browser →
// poll until a terminal state. The sidecar owns the loopback listener, the
// token exchange, and persistence (python/fflogs_auth.py); this module is
// just the UI-side poll loop.

import { sidecar } from '../sidecar';
import { openUrl } from '../tauri/openUrl';

const POLL_MS = 1500;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export type SignInCallbacks = {
  /** Listener is up; `authorizeUrl` is also shown in the UI as a fallback
   *  link in case the browser didn't open. */
  onWaiting?: (authorizeUrl: string) => void;
  onDone: (userName: string) => void;
  onExpired: () => void;
  onError: (message: string) => void;
};

export type SignInHandle = { cancel: () => void };

export function startSignIn(cb: SignInCallbacks): SignInHandle {
  let cancelled = false;
  void (async () => {
    try {
      const begin = await sidecar.fflogsAuthBegin();
      if (cancelled) {
        void sidecar.fflogsAuthCancel();
        return;
      }
      cb.onWaiting?.(begin.authorizeUrl);
      try {
        // Rust-side generic opener (bypasses JS capability scoping). Fails in
        // plain-browser dev — the fallback link in the modal covers that.
        await openUrl(begin.authorizeUrl);
      } catch {
        /* fallback link shown in the UI */
      }
      while (!cancelled) {
        await sleep(POLL_MS);
        if (cancelled) break;
        const res = await sidecar.fflogsAuthPoll();
        if (res.status === 'pending') continue;
        if (res.status === 'done') {
          cb.onDone(res.userName);
        } else if (res.status === 'expired') {
          cb.onExpired();
        } else {
          cb.onError(res.message ?? 'sign-in failed');
        }
        return;
      }
    } catch (e) {
      if (!cancelled) cb.onError(e instanceof Error ? e.message : String(e));
    }
  })();
  return {
    cancel: () => {
      cancelled = true;
      void sidecar.fflogsAuthCancel();
    },
  };
}
