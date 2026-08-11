// Summoner frontend profile. SMN ships only the shared analysis views plus two
// declarative cast-count sections (the demi burst kit and the primal phases).
// The heals/shields (Radiant Aegis, Lux Solaris, Rekindle, Physick,
// Resurrection) arrive tagged isDefensive from the backend
// (JobData.defensive_ids), so nonRotationalNames is just a name-based fallback.

import { Flame, Gem } from 'lucide-react';
import type { JobProfile } from './types';

// SMN demi-burst abilities, in display order (mirror
// python/jobs/summoner/data.py). Counts come from the shared Abilities aspect
// state; names + icons from abilityMeta — so this is just the curated id list,
// not a name map.
const SMN_DEMI_IDS = [
  36992, // Summon Solar Bahamut
  7427, // Summon Bahamut
  25831, // Summon Phoenix
  36998, // Enkindle Solar Bahamut
  7429, // Enkindle Bahamut
  16516, // Enkindle Phoenix
  36996, // Sunflare
  3582, // Deathflare
  25801, // Searing Light
  36991, // Searing Flash
];

const SMN_PRIMAL_IDS = [
  25840, // Summon Garuda II
  25838, // Summon Ifrit II
  25839, // Summon Titan II
  25837, // Slipstream
  25835, // Crimson Cyclone
  25885, // Crimson Strike
  25836, // Mountain Buster
  16508, // Energy Drain
  36990, // Necrotize
  7426, // Ruin IV
];

export const summoner: JobProfile = {
  name: 'Summoner',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Demi burst',
      heading: 'Demi summon & burst cast counts',
      icon: Flame,
      abilityIds: SMN_DEMI_IDS,
    },
    {
      sectionTitle: 'Primals',
      heading: 'Primal phase cast counts',
      icon: Gem,
      abilityIds: SMN_PRIMAL_IDS,
    },
  ],
  // Fallback only — the primary defensive filter is the backend isDefensive flag.
  nonRotationalNames: [
    'Radiant Aegis',
    'Lux Solaris',
    'Rekindle',
    'Physick',
    'Resurrection',
  ],
};
