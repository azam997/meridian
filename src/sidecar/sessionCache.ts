// Session-lifetime cache for SetupView's character data. SetupView loads its
// whole screen (the tier's encounters + every encounter's pulls) in one
// `listSetup` round trip; caching the promise here makes a revisited job
// instant and dedupes concurrent requests for the same key. The backend also
// memoizes it per process (SessionCachedClient.get_character_setup); both share
// the same session-lifetime staleness — new pulls appear after an app restart.

import { sidecar } from './index';
import type { SetupData, UserCharactersResult } from './contract';

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

export const listSetupCached = (lodestoneId: number, job: string): Promise<SetupData> =>
  cached(`setup:${lodestoneId}:${job}`, () => sidecar.listSetup(lodestoneId, job));

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
