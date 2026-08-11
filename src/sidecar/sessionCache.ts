// Session-lifetime promise cache for shared sidecar lookups: dedupes
// concurrent same-key requests and makes revisits instant. (The Pulls list
// itself lives in state/pullsStore.ts, backed by the sidecar's own
// per-character memo.)

import { sidecar } from './index';
import type { UserCharactersResult } from './contract';

const cache = new Map<string, Promise<unknown>>();

function cached<T>(key: string, fetch: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  if (hit) return hit as Promise<T>;
  const p = fetch();
  cache.set(key, p);
  // Don't cache failures — a transient network error shouldn't stick for the
  // whole session.
  p.catch(() => cache.delete(key));
  return p;
}

// The signed-in account's claimed characters — shared by the Encounter page's
// selector, the change-character modal, and App's launch auto-pick, so one
// API round trip serves them all. Cleared on sign-in/sign-out (the list
// belongs to the account, and a client-credentials session caches []).
const USER_CHARS_KEY = 'userCharacters';

export const listUserCharactersCached = (): Promise<UserCharactersResult> =>
  cached(USER_CHARS_KEY, () => sidecar.listUserCharacters());

export const clearUserCharactersCache = (): void => {
  cache.delete(USER_CHARS_KEY);
};
