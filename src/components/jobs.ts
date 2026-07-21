// FFXIV job metadata: swatch color + official XIVAPI framed-icon URL.

const XIVAPI_JOB = 'https://xivapi.com/i/062000';

type JobMeta = { color: string; icon: string };

// Icon IDs use the 0621XX framed-job series (icon = 062100 + ClassJob row).
// Ordered by role (tank → healer → melee → phys ranged → magical ranged) so
// the picker grid reads like the in-game job select.
export const JOB_META: Record<string, JobMeta> = {
  // Tanks
  Paladin:      { color: '#a8d2e6', icon: `${XIVAPI_JOB}/062119.png` },
  Warrior:      { color: '#cf2621', icon: `${XIVAPI_JOB}/062121.png` },
  'Dark Knight':{ color: '#d126cc', icon: `${XIVAPI_JOB}/062132.png` },
  Gunbreaker:   { color: '#796d30', icon: `${XIVAPI_JOB}/062137.png` },
  // Healers
  'White Mage': { color: '#bdb29a', icon: `${XIVAPI_JOB}/062124.png` },
  Scholar:      { color: '#8657ff', icon: `${XIVAPI_JOB}/062128.png` },
  Astrologian:  { color: '#ffe74a', icon: `${XIVAPI_JOB}/062133.png` },
  Sage:         { color: '#80a0f0', icon: `${XIVAPI_JOB}/062140.png` },
  // Melee DPS
  Monk:         { color: '#d69c00', icon: `${XIVAPI_JOB}/062120.png` },
  Dragoon:      { color: '#4164cd', icon: `${XIVAPI_JOB}/062122.png` },
  Ninja:        { color: '#af1964', icon: `${XIVAPI_JOB}/062130.png` },
  Samurai:      { color: '#e46d04', icon: `${XIVAPI_JOB}/062134.png` },
  Reaper:       { color: '#965a90', icon: `${XIVAPI_JOB}/062139.png` },
  Viper:        { color: '#108210', icon: `${XIVAPI_JOB}/062141.png` },
  // Physical Ranged DPS
  Bard:         { color: '#91ba5e', icon: `${XIVAPI_JOB}/062123.png` },
  Machinist:    { color: '#a05d18', icon: `${XIVAPI_JOB}/062131.png` },
  Dancer:       { color: '#e2b0af', icon: `${XIVAPI_JOB}/062138.png` },
  // Magical Ranged DPS
  'Black Mage': { color: '#a579d6', icon: `${XIVAPI_JOB}/062125.png` },
  Summoner:     { color: '#2d9b78', icon: `${XIVAPI_JOB}/062127.png` },
  'Red Mage':   { color: '#e87b7b', icon: `${XIVAPI_JOB}/062135.png` },
  Pictomancer:  { color: '#fc92e1', icon: `${XIVAPI_JOB}/062142.png` },
};

export const JOBS = Object.keys(JOB_META);
export const jobColor = (job: string) => JOB_META[job]?.color ?? '#888';
export const jobIcon = (job: string) => JOB_META[job]?.icon;

// Healers route through the Healing/Mitigation planner: healers have to heal,
// so their honest ceiling is the damage optimum WITH the mit plan's heal GCDs
// locked in — not the pure damage sim. Selecting a healer in Setup leads to
// the planner (encounter/comp preselected from the chosen pull).
export const HEALER_JOBS = new Set(['White Mage', 'Scholar', 'Astrologian', 'Sage']);
export const isHealer = (job: string) => HEALER_JOBS.has(job);

// Healers whose locked-GCD analysis is live (a registered damage simulator +
// the mit-plan integration). The rest get the plan view only for now.
export const ANALYZABLE_HEALERS = new Set(['White Mage', 'Astrologian', 'Scholar', 'Sage']);

// Analysis-pending healers: plan-only until their simulators ship. Tiles stay
// clickable (they lead to the planner) but can't run an analysis.
export const PENDING_JOBS = new Set(
  [...HEALER_JOBS].filter((j) => !ANALYZABLE_HEALERS.has(j)));
export const isJobPending = (job: string) => PENDING_JOBS.has(job);
export const PENDING_JOB_TIP =
  'Full analysis is coming later — the Healing/Mitigation planner covers this job today.';
