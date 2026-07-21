// Viper frontend profile: the declarative burst cast-count section. VPR has no
// bespoke dashboard panel in v1 (Hunter's Instinct uptime rides on the Scoring
// state / headline). Its Slither dash arrives with a bundled id tagged isDefensive
// (data.defensive_ids), so there's no nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// VPR signature/burst abilities, in display order (mirror python/jobs/viper/
// data.py::BURST_ABILITIES + the high-value gauge spenders). Counts come from the
// shared Abilities aspect state; names + icons from abilityMeta — so this is just
// the curated id list.
const VPR_BURST_IDS = [
  34647, // Serpent's Ire (2-min burst)
  34626, // Reawaken
  34631, // Ouroboros
  34620, // Vicewinder
  34633, // Uncoiled Fury
];

export const viper: JobProfile = {
  name: 'Viper',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: VPR_BURST_IDS,
    },
  ],
};
