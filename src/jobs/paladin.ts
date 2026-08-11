// Paladin frontend profile — the analyzer's first TANK. Its one deviation from
// the shared DPS baseline is the Timeline Defensives lane (`defensiveTimeline`):
// tank mitigation/utility casts (Rampart, Sentinel, Holy Sheltron …) are surfaced
// in their own lane instead of being filtered off, for mitigation review. Plus a
// declarative Burst cast-count section. No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// PLD signature/burst abilities, in display order (mirror python/jobs/paladin/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const PLD_BURST_IDS = [
  20, // Fight or Flight
  36921, // Imperator
  3538, // Goring Blade
  16459, // Confiteor
  36922, // Blade of Honor
];

export const paladin: JobProfile = {
  name: 'Paladin',
  panels: [],
  defensiveTimeline: true,
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: PLD_BURST_IDS,
    },
  ],
};
