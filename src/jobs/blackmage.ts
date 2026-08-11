// Black Mage frontend profile — the analyzer's second caster (first MP-economy
// job). No bespoke dashboard panels yet; just a declarative Burst cast-count
// section (the 2-min enablers + payoff). BLM's utility oGCDs (Manaward, the
// teleports) arrive tagged isDefensive from the backend, so there's no
// nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// BLM signature/burst abilities, in display order (mirror
// python/jobs/blackmage/data.py). Counts come from the shared Abilities aspect
// state; names + icons from abilityMeta — so this is just the curated id list.
const BLM_BURST_IDS = [
  158, // Manafont
  3573, // Ley Lines
  25796, // Amplifier
  36989, // Flare Star
  16507, // Xenoglossy
  16505, // Despair
  36986, // High Thunder
];

export const blackmage: JobProfile = {
  name: 'Black Mage',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: BLM_BURST_IDS,
    },
  ],
};
