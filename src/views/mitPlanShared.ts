import { jobAbbr } from '../components/jobs';
import { fmtClock } from '../format';
import type { MitMechanic, MitPlanResult, RoleAmounts } from '../sidecar/contract';

/** Shared formatting bits for the mitigation planner's timeline + board. */

export const fmtK = (n: number): string =>
  n >= 1000 ? `${Math.round(n / 1000)}k` : `${Math.round(n)}`;

export const roleLine = (r: RoleAmounts): string =>
  `T ${fmtK(r.tank)} · H ${fmtK(r.healer)} · D ${fmtK(r.dps)}`;

export const KIND_LABEL: Record<MitMechanic['kind'], string> = {
  raidwide: 'Raidwide', tankbuster: 'Tank buster', bleed: 'Bleed',
  multiHit: 'Multi-hit', other: 'Shared', hpSet: 'HP set',
};

export const SCHOOL_LABEL: Record<MitMechanic['school'], string> = {
  physical: 'Physical', magical: 'Magical', special: 'Special',
  mixed: 'Mixed', unknown: '—',
};

/** Render a scored plan as a plain-text mit sheet — the human-readable export
 *  written beside the .json (and the shape people paste into Discord). This is
 *  a render of the SCORED plan, so it can show what the schema JSON cannot:
 *  statuses, carryover coverage, and the GCD heals (plan content the group
 *  must actually cast on a custom plan; advisory inserts on a sim plan).
 *  Not importable; the header says so. */
export const renderMitSheet = (result: MitPlanResult): string => {
  const who = (slot: string, job: string) => `${slot} ${jobAbbr(job)}`;
  const castList = (rows: { name: string; slot: string; job: string }[]) =>
    rows.map((a) => `${a.name} (${who(a.slot, a.job)})`).join(' · ');
  const party = result.partyJobs.map(
    (j, i) => who(['T1', 'T2', 'H1', 'H2', 'D1', 'D2', 'D3', 'D4'][i] ?? '?', j));

  const lines: string[] = [
    `${result.encounterName} mit plan`,
    'Made in Meridian. This copy is for reading; the .json file beside it '
      + 'imports back into Meridian.',
    `Party: ${party.slice(0, 2).join('  ')}  |  ${party.slice(2, 4).join('  ')}`
      + `  |  ${party.slice(4).join('  ')}`,
    `Damage timeline from ${result.refCount} top kills, median kill `
      + `${fmtClock(result.modelKillSec)}.`,
    '',
  ];
  for (const m of result.mechanics) {
    const t = fmtClock(m.timeSec).padStart(5);
    if (m.kind === 'hpSet') {
      lines.push(`${t}  ${m.name}  (sets the party to ~1 HP, unmitigable)`);
      continue;
    }
    const school = m.school !== 'unknown'
      ? `, ${SCHOOL_LABEL[m.school].toLowerCase()}` : '';
    const tag = m.status !== 'covered' ? `  [${m.status.toUpperCase()}]` : '';
    lines.push(`${t}  ${m.name}  (${KIND_LABEL[m.kind].toLowerCase()}${school})${tag}`);
    const placed = m.assignments.filter((a) => !a.isCarryover && !a.isSuggestion);
    const suggested = m.assignments.filter((a) => a.isSuggestion && !a.isCarryover);
    const carried = m.assignments.filter((a) => a.isCarryover);
    if (placed.length) lines.push(`         ${castList(placed)}`);
    if (suggested.length) lines.push(`         suggested: ${castList(suggested)}`);
    if (carried.length) lines.push(`         still active: ${castList(carried)}`);
    if (m.gcdHeals.length) {
      lines.push(`         heals: ${m.gcdHeals.map((g) =>
        `${g.name} ×${g.count} (${who(g.slot, g.job)})`).join(' · ')}`);
    }
    if (!placed.length && !suggested.length && !carried.length
        && !m.gcdHeals.length) {
      lines.push('         (nothing placed)');
    }
  }
  const s = result.summary;
  lines.push('');
  lines.push(`${s.coveredCount} covered, ${s.tightCount} tight, `
    + `${s.uncoveredCount} uncovered.`);
  lines.push('');
  return lines.join('\n');
};
