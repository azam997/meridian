// Release notes, bundled with the build.
//
// `changelog.json` is the SINGLE source for what a release says: the in-app
// "What's new" popup and Version history tab read it here, and
// scripts/make_latest_json.py reads the same file at release time to write the
// GitHub release body (--notes-file) and the updater feed's one-line `notes`.
// Write the notes once, in the JSON, before cutting a build.
//
// Newest entry first. `summary` is the lead paragraph (and the whole note for
// a prose-only release); `sections` adds structure when a release needs it.
// `**bold**` is the only inline markup — see components/ReleaseNotes.tsx.

import raw from './changelog.json';

export type ReleaseSection = {
  /** Renders as a section heading. Omit for a bare paragraph. */
  heading?: string;
  /** Intro/standalone paragraph, before any bullets. */
  body?: string;
  /** Bullet list. */
  items?: string[];
};

export type Release = {
  /** Dotted version, no leading "v". */
  version: string;
  /** ISO yyyy-mm-dd — the release's publish date. */
  date: string;
  summary: string;
  sections?: ReleaseSection[];
};

/** Every shipped release, newest first. Annotated (not cast) on purpose — a
 *  malformed changelog.json then fails `npm run build` instead of the app. */
export const CHANGELOG: Release[] = raw;

/** The running app's version.
 *
 *  Derived from the newest changelog entry so the version we display can never
 *  disagree with the notes we show for it. package.json / package-lock.json /
 *  src-tauri/tauri.conf.json still carry it too; make_latest_json.py hard-fails
 *  the release if the changelog and tauri.conf.json disagree. */
export const APP_VERSION: string = CHANGELOG[0].version;

/** Numeric dotted-version compare: <0 if a is older, 0 if equal, >0 if newer. */
export const cmpVersion = (a: string, b: string): number => {
  const pa = a.split('.');
  const pb = b.split('.');
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (Number(pa[i]) || 0) - (Number(pb[i]) || 0);
    if (d !== 0) return d;
  }
  return 0;
};

/** "2026-07-20" → "Jul 20, 2026". Parsed as local midnight so the displayed
 *  day never slips a timezone. */
export const fmtReleaseDate = (iso: string): string => {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
};
