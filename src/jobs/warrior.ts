// Warrior frontend profile — the analyzer's second TANK. Like Paladin its one
// deviation from the shared DPS baseline is the Timeline Defensives lane
// (`defensiveTimeline`): tank mitigation/utility casts (Rampart, Thrill of
// Battle, Bloodwhetting, Holmgang …) are surfaced in their own lane instead of
// being filtered off, for mitigation review. Plus a declarative Burst cast-count
// section. No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// WAR signature/burst abilities, in display order (mirror python/jobs/warrior/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const WAR_BURST_IDS = [
  7389, // Inner Release
  52, // Infuriate
  16465, // Inner Chaos
  25753, // Primal Rend
  36925, // Primal Ruination
  36924, // Primal Wrath
];

export const warrior: JobProfile = {
  name: 'Warrior',
  panels: [],
  defensiveTimeline: true,
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: WAR_BURST_IDS,
    },
  ],
};
