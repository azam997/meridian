// Reaper frontend profile: dashboard panels + the declarative burst cast-count
// section. RPR's defensive oGCDs arrive without bundled ids (filtered by the
// shared name baseline + null-id guard), so there's no nonRotationalIds list to
// maintain here yet.

import { Swords } from 'lucide-react';
import { DeathsDesignPanel } from '../views/jobPanels/reaper';
import type { JobProfile } from './types';

// RPR signature/burst abilities, in display order (mirror python/jobs/reaper/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const RPR_BURST_IDS = [
  24405, // Arcane Circle
  24393, // Gluttony
  24385, // Plentiful Harvest
  24394, // Enshroud
  24398, // Communio
  36973, // Perfectio
];

export const reaper: JobProfile = {
  name: 'Reaper',
  panels: [DeathsDesignPanel],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: RPR_BURST_IDS,
    },
  ],
};
