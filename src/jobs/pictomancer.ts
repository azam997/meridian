// Pictomancer frontend profile. PCT ships only the shared analysis views plus
// two declarative cast-count sections (the Starry Muse burst kit and the
// canvas -> muse ladder). The utility oGCDs (Tempera Coat/Grassa, Smudge)
// arrive tagged isDefensive from the backend (JobData.defensive_ids), so
// nonRotationalNames is just a name-based fallback.

import { Paintbrush, Swords } from 'lucide-react';
import type { JobProfile } from './types';

// PCT signature/burst abilities, in display order (mirror
// python/jobs/pictomancer/data.py). Counts come from the shared Abilities
// aspect state; names + icons from abilityMeta — so this is just the curated
// id list, not a name map.
const PCT_BURST_IDS = [
  34675, // Starry Muse
  34681, // Star Prism
  34663, // Comet in Black
  34676, // Mog of the Ages
  34677, // Retribution of the Madeen
  34683, // Subtractive Palette
  34688, // Rainbow Drip
];

const PCT_CANVAS_IDS = [
  34674, // Striking Muse
  34678, // Hammer Stamp
  34679, // Hammer Brush
  34680, // Polishing Hammer
  34670, // Pom Muse
  34671, // Winged Muse
  34672, // Clawed Muse
  34673, // Fanged Muse
];

export const pictomancer: JobProfile = {
  name: 'Pictomancer',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Starry Muse burst cast counts',
      icon: Swords,
      abilityIds: PCT_BURST_IDS,
    },
    {
      sectionTitle: 'Canvas',
      heading: 'Muse & hammer cast counts',
      icon: Paintbrush,
      abilityIds: PCT_CANVAS_IDS,
    },
  ],
  // Fallback only — the primary defensive filter is the backend isDefensive flag.
  nonRotationalNames: ['Tempera Coat', 'Tempera Grassa', 'Smudge'],
};
