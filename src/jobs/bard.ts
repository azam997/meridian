// Bard frontend profile. BRD ships only the shared analysis views plus two
// declarative cast-count sections (the 2-min burst kit and the song cycle);
// its budget scalars (refulgent/pp/apex/heartbreak) are on the wire
// (aspectStates.Scoring) for a future dedicated panel. The utility oGCDs
// (Troubadour, Nature's Minne, …) arrive tagged isDefensive from the backend
// (JobData.defensive_ids), so nonRotationalNames is just a name-based fallback.

import { Music, Swords } from 'lucide-react';
import type { JobProfile } from './types';

// BRD signature/burst abilities, in display order (mirror python/jobs/bard/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const BRD_BURST_IDS = [
  101, // Raging Strikes
  118, // Battle Voice
  25785, // Radiant Finale
  107, // Barrage
  36977, // Radiant Encore
  36976, // Resonant Arrow
  16496, // Apex Arrow
  25784, // Blast Arrow
  3562, // Sidewinder
  3558, // Empyreal Arrow
];

const BRD_SONG_IDS = [
  3559, // the Wanderer's Minuet
  114, // Mage's Ballad
  116, // Army's Paeon
  7404, // Pitch Perfect
  3560, // Iron Jaws
];

export const bard: JobProfile = {
  name: 'Bard',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: BRD_BURST_IDS,
    },
    {
      sectionTitle: 'Songs',
      heading: 'Song cycle cast counts',
      icon: Music,
      abilityIds: BRD_SONG_IDS,
    },
  ],
  // Fallback only — the primary defensive filter is the backend isDefensive flag.
  nonRotationalNames: [
    'Troubadour',
    "Nature's Minne",
    "the Warden's Paean",
    "The Warden's Paean",
    'Repelling Shot',
  ],
};
