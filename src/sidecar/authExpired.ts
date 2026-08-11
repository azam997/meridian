// Tiny event bus for the 'auth_expired' error code. The ndjson client emits
// here whenever ANY request fails with errorCode 'auth_expired' (the FFLogs
// sign-in is gone or unrefreshable), so App can flip to the sign-in gate
// without every caller having to inspect its own errors.

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe; returns an unsubscribe function (effect-friendly). */
export function onAuthExpired(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function emitAuthExpired(): void {
  for (const fn of listeners) fn();
}
