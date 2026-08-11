// FFXIV job metadata: swatch color, official XIVAPI framed-icon URL, role, and
// the in-game 3-letter abbreviation (icon-outage fallback in JobTile).

const XIVAPI_JOB = 'https://xivapi.com/i/062000';

export type JobRole = 'tank' | 'healer' | 'melee' | 'ranged' | 'caster';

type JobMeta = { color: string; icon: string; role: JobRole; abbr: string };

// Icon IDs use the 0621XX framed-job series (icon = 062100 + ClassJob row).
// Ordered by role (tank → healer → melee → phys ranged → magical ranged) so
// the picker grid reads like the in-game job select.
export const JOB_META: Record<string, JobMeta> = {
  // Tanks
  Paladin:      { color: '#a8d2e6', icon: `${XIVAPI_JOB}/062119.png`, role: 'tank', abbr: 'PLD' },
  Warrior:      { color: '#cf2621', icon: `${XIVAPI_JOB}/062121.png`, role: 'tank', abbr: 'WAR' },
  'Dark Knight':{ color: '#d126cc', icon: `${XIVAPI_JOB}/062132.png`, role: 'tank', abbr: 'DRK' },
  Gunbreaker:   { color: '#796d30', icon: `${XIVAPI_JOB}/062137.png`, role: 'tank', abbr: 'GNB' },
  // Healers
  'White Mage': { color: '#bdb29a', icon: `${XIVAPI_JOB}/062124.png`, role: 'healer', abbr: 'WHM' },
  Scholar:      { color: '#8657ff', icon: `${XIVAPI_JOB}/062128.png`, role: 'healer', abbr: 'SCH' },
  Astrologian:  { color: '#ffe74a', icon: `${XIVAPI_JOB}/062133.png`, role: 'healer', abbr: 'AST' },
  Sage:         { color: '#80a0f0', icon: `${XIVAPI_JOB}/062140.png`, role: 'healer', abbr: 'SGE' },
  // Melee DPS
  Monk:         { color: '#d69c00', icon: `${XIVAPI_JOB}/062120.png`, role: 'melee', abbr: 'MNK' },
  Dragoon:      { color: '#4164cd', icon: `${XIVAPI_JOB}/062122.png`, role: 'melee', abbr: 'DRG' },
  Ninja:        { color: '#af1964', icon: `${XIVAPI_JOB}/062130.png`, role: 'melee', abbr: 'NIN' },
  Samurai:      { color: '#e46d04', icon: `${XIVAPI_JOB}/062134.png`, role: 'melee', abbr: 'SAM' },
  Reaper:       { color: '#965a90', icon: `${XIVAPI_JOB}/062139.png`, role: 'melee', abbr: 'RPR' },
  Viper:        { color: '#108210', icon: `${XIVAPI_JOB}/062141.png`, role: 'melee', abbr: 'VPR' },
  // Physical Ranged DPS
  Bard:         { color: '#91ba5e', icon: `${XIVAPI_JOB}/062123.png`, role: 'ranged', abbr: 'BRD' },
  Machinist:    { color: '#a05d18', icon: `${XIVAPI_JOB}/062131.png`, role: 'ranged', abbr: 'MCH' },
  Dancer:       { color: '#e2b0af', icon: `${XIVAPI_JOB}/062138.png`, role: 'ranged', abbr: 'DNC' },
  // Magical Ranged DPS
  'Black Mage': { color: '#a579d6', icon: `${XIVAPI_JOB}/062125.png`, role: 'caster', abbr: 'BLM' },
  Summoner:     { color: '#2d9b78', icon: `${XIVAPI_JOB}/062127.png`, role: 'caster', abbr: 'SMN' },
  'Red Mage':   { color: '#e87b7b', icon: `${XIVAPI_JOB}/062135.png`, role: 'caster', abbr: 'RDM' },
  Pictomancer:  { color: '#fc92e1', icon: `${XIVAPI_JOB}/062142.png`, role: 'caster', abbr: 'PCT' },
};

export const ROLE_ORDER: JobRole[] = ['tank', 'healer', 'melee', 'ranged', 'caster'];

export const JOBS = Object.keys(JOB_META);
export const jobColor = (job: string) => JOB_META[job]?.color ?? '#888';
export const jobIcon = (job: string) => JOB_META[job]?.icon;
export const jobAbbr = (job: string) =>
  JOB_META[job]?.abbr ?? job.slice(0, 3).toUpperCase();

/** Partition a job list into ROLE_ORDER groups (JOB_META order within each)
 *  for the role-grouped job rail. Empty groups drop; unknown names skip. */
export function groupJobsByRole(jobs: string[]): { role: JobRole; jobs: string[] }[] {
  const wanted = new Set(jobs);
  return ROLE_ORDER
    .map((role) => ({
      role,
      jobs: JOBS.filter((j) => wanted.has(j) && JOB_META[j].role === role),
    }))
    .filter((g) => g.jobs.length > 0);
}

// The planner's slot constraints. H1 is always the shield healer and H2 the
// regen healer, so the two healer lists are disjoint by construction.
export const SHIELD_HEALERS = ['Sage', 'Scholar'] as const;
export const REGEN_HEALERS = ['White Mage', 'Astrologian'] as const;
export const TANK_JOBS = ['Paladin', 'Warrior', 'Dark Knight', 'Gunbreaker'] as const;
export const DPS_JOBS = [
  'Monk', 'Dragoon', 'Ninja', 'Samurai', 'Reaper', 'Viper',
  'Bard', 'Machinist', 'Dancer',
  'Black Mage', 'Summoner', 'Red Mage', 'Pictomancer',
] as const;

// Healers route through the Healing/Mitigation planner: healers have to heal,
// so their honest ceiling is the damage optimum WITH the mit plan's heal GCDs
// locked in — not the pure damage sim. Selecting a healer in Setup leads to
// the planner (encounter/comp preselected from the chosen pull).
export const HEALER_JOBS = new Set(['White Mage', 'Scholar', 'Astrologian', 'Sage']);
export const isHealer = (job: string) => HEALER_JOBS.has(job);

// Healers whose locked-GCD analysis is live (a registered damage simulator +
// the mit-plan integration). All four today — kept as a set because the
// healer flow still branches on it ("Plan & analyze" vs plan-only).
export const ANALYZABLE_HEALERS = new Set(['White Mage', 'Astrologian', 'Scholar', 'Sage']);
