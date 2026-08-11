// Formatting helpers for the Pulls screen: date grouping, row meta lines, and
// the human pull label written into `state.pullId` (persist.ts still stores a
// label; the backend used to build these server-side for <select> options).

import type { PullRow } from '../sidecar/contract';

export const fmtClock = (s: number): string =>
  `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

export const fmtTimeOfDay = (ms: number): string => {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const fmtYmdHm = (ms: number): string => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-`
    + `${String(d.getDate()).padStart(2, '0')} ${fmtTimeOfDay(ms)}`;
};

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Today" / "Yesterday" / "Apr 19" / "Apr 19, 2025" (other years). */
export function dateGroupLabel(ms: number, now: number = Date.now()): string {
  const d = new Date(ms);
  const n = new Date(now);
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
  if (sameDay(d, n)) return 'Today';
  const yday = new Date(now - 24 * 3600_000);
  if (sameDay(d, yday)) return 'Yesterday';
  const md = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === n.getFullYear() ? md : `${md}, ${d.getFullYear()}`;
}

export type PullGroup = { label: string; rows: PullRow[] };

/** Group consecutive rows (assumed sorted) by their local calendar day. */
export function groupRowsByDate(rows: PullRow[], now: number = Date.now()): PullGroup[] {
  const groups: PullGroup[] = [];
  for (const r of rows) {
    const label = dateGroupLabel(r.startTimeMs, now);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.rows.push(r);
    else groups.push({ label, rows: [r] });
  }
  return groups;
}

/** Strip a trailing parenthetical from a catalog encounter name for row
 *  display: "Lindwurm (M12S P1)" -> "Lindwurm", "Dancing Mad (Ultimate)" ->
 *  "Dancing Mad". FFLogs ranking names arrive bare already. */
export const encounterShortName = (name: string): string =>
  name.replace(/\s*\([^)]*\)\s*$/, '');

/** Split a catalog encounter name into chip label + mono short code:
 *  "Lindwurm II (M12S P2)" -> { label: "Lindwurm II", code: "M12S P2" }.
 *  A code that just restates the category tab ("Dancing Mad (Ultimate)")
 *  is dropped — the tab already says it. */
export function splitEncounterName(name: string): { label: string; code?: string } {
  const m = name.match(/^(.*?)\s*\(([^)]+)\)\s*$/);
  if (!m || !m[1]) return { label: name };
  const code = m[2].trim();
  if (/^(savage|ultimate)$/i.test(code)) return { label: m[1] };
  return { label: m[1], code };
}

/** The row's second line: "Machinist · kill 8:12 · 36.6k dps" or
 *  "Machinist · wipe 13:45 · reached P4 · 39% left". Parts degrade when the
 *  data is absent (pasted kills have no dps; unphased wipes no phase). */
export function rowMetaLine(r: PullRow): string {
  const parts = [r.job];
  if (r.kill) {
    parts.push(`kill ${fmtClock(r.durationS)}`);
    if (r.dps != null && r.dps > 0) parts.push(`${(r.dps / 1000).toFixed(1)}k dps`);
  } else {
    parts.push(`wipe ${fmtClock(r.durationS)}`);
    if ((r.lastPhase ?? 0) >= 1) parts.push(`reached P${r.lastPhase}`);
    if (r.fightPercentage != null) parts.push(`${Math.round(r.fightPercentage)}% left`);
  }
  return parts.join(' · ');
}

/** The label written into `state.pullId` for the run snapshot — mirrors the
 *  wire labels the old list_setup/list_prog_pulls built server-side, so
 *  persistence and FeedbackView context read the same. */
export function rowPullLabel(r: PullRow): string {
  const when = fmtYmdHm(r.startTimeMs);
  if (r.kill) {
    const pp = r.parsePct != null ? `${r.parsePct.toFixed(1)}%` : `kill ${fmtClock(r.durationS)}`;
    const dd = r.dps != null && r.dps > 0 ? `${(r.dps / 1000).toFixed(1)}k dps` : '—';
    return `${when}  —  ${pp}  —  ${dd}`;
  }
  const parts = [when, fmtClock(r.durationS)];
  if (r.fightPercentage != null) {
    let left = `${Math.round(r.fightPercentage)}% left`;
    if ((r.lastPhase ?? 0) >= 1) left += ` (P${r.lastPhase})`;
    parts.push(left);
  }
  return parts.join('  —  ');
}

/** Metric tone for a kill's parse bar (SetupView's recent-pull thresholds). */
export const parseTone = (pct: number): 'good' | 'warn' =>
  pct > 95 ? 'good' : 'warn';

/** Report-code extraction for the search box's paste path — the same regex
 *  pair SetupView's addPastedReport used. */
export function extractReportCode(text: string): string | null {
  const m = text.match(/reports\/(?:a:)?([A-Za-z0-9]{16})/)
    ?? text.trim().match(/^([A-Za-z0-9]{16})$/);
  return m ? m[1] : null;
}
