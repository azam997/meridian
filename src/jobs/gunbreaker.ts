// Gunbreaker frontend profile — the analyzer's third TANK. Like Paladin/Warrior its
// one deviation from the shared DPS baseline is the Timeline Defensives lane
// (`defensiveTimeline`): tank mitigation/utility casts (Camouflage, Nebula, Aurora,
// Superbolide, Heart of Light/Corundum …) are surfaced in their own lane instead of
// being filtered off, for mitigation review. Plus a declarative Burst cast-count
// section. No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// GNB signature/burst abilities, in display order (mirror python/jobs/gunbreaker/
// data.py). Counts come from the shared Abilities aspect state; names + icons from
// abilityMeta — so this is just the curated id list, not a name map.
const GNB_BURST_IDS = [
  16138, // No Mercy
  16164, // Bloodfest
  16146, // Gnashing Fang
  25760, // Double Down
  16153, // Sonic Break
  36937, // Reign of Beasts
  16165, // Blasting Zone
  16159, // Bow Shock
];

export const gunbreaker: JobProfile = {
  name: 'Gunbreaker',
  panels: [],
  defensiveTimeline: true,
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: GNB_BURST_IDS,
    },
  ],
};
