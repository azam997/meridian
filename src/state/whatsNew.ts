// "What's new" gate: which release notes this install still owes the user.
//
// Same load/save idiom as state/accent.ts and state/zoom.ts — one localStorage
// key, every access try/catch-wrapped (private mode / quota must never break
// the launch path).

import { APP_VERSION, CHANGELOG, cmpVersion, type Release } from '../data/changelog';
import { hasPriorSession } from './persist';

const KEY = 'fflogs.efficiency.analyzer.lastSeenVersion.v1';

/** How many stacked releases the popup will show at once. A long skip gets the
 *  most recent few rather than a wall of text; the rest live in the tab. */
const MAX_STACKED = 5;

export const loadLastSeenVersion = (): string | null => {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
};

/** Record that the user has been shown notes up to the running version. */
export const markVersionSeen = (): void => {
  try {
    localStorage.setItem(KEY, APP_VERSION);
  } catch {
    /* ignore quota / private-mode errors */
  }
};

/** Releases to pop up on launch, newest first. Empty when there is nothing to
 *  say — which the caller still records (markVersionSeen) so the NEXT update
 *  fires.
 *
 *  - Up to date            → nothing.
 *  - No key, first ever run → nothing. A fresh install has no "what changed";
 *                             it's also sitting behind the sign-in gate.
 *  - No key, but the app has run before → the user updated from a build that
 *                             predates this feature. We can't know which one,
 *                             so show just the version they landed on.
 *  - Key older than APP_VERSION → every entry in between. */
export const pendingReleases = (): Release[] => {
  const seen = loadLastSeenVersion();
  if (seen === APP_VERSION) return [];
  if (seen === null) {
    return hasPriorSession() ? CHANGELOG.slice(0, 1) : [];
  }
  return CHANGELOG.filter(
    (r) => cmpVersion(r.version, seen) > 0 && cmpVersion(r.version, APP_VERSION) <= 0
  ).slice(0, MAX_STACKED);
};
