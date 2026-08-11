import type { MitMechanic, RoleAmounts } from '../sidecar/contract';

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
