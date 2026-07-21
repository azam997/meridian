// Dark Knight frontend profile — the analyzer's fourth TANK. Like Paladin/
// Warrior/Gunbreaker its one deviation from the shared DPS baseline is the
// Timeline Defensives lane (`defensiveTimeline`): tank mitigation/utility casts
// (The Blackest Night, Shadowed Vigil, Dark Mind, Oblation, Dark Missionary,
// Living Dead …) are surfaced in their own lane instead of being filtered off,
// for mitigation review. Plus a declarative Burst cast-count section. No
// bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// DRK signature/burst abilities, in display order (mirror python/jobs/
// darkknight/data.py). Counts come from the shared Abilities aspect state;
// names + icons from abilityMeta — so this is just the curated id list, not a
// name map.
const DRK_BURST_IDS = [
  7390, // Delirium
  16472, // Living Shadow
  36932, // Disesteem
  3639, // Salted Earth
  25755, // Salt and Darkness
  3643, // Carve and Spit
  25757, // Shadowbringer
  16470, // Edge of Shadow
  7392, // Bloodspiller
];

export const darkknight: JobProfile = {
  name: 'Dark Knight',
  panels: [],
  defensiveTimeline: true,
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: DRK_BURST_IDS,
    },
  ],
};
