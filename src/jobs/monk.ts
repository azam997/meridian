// Monk frontend profile: the declarative burst cast-count section. MNK has no
// bespoke dashboard panel in v1 (the Riddle of Fire window / Blitz cadence /
// chakra budget ride on the Scoring state / headline). Mantra / Riddle of Earth
// / Earth's Reply / Thunderclap arrive with bundled ids tagged isDefensive
// (data.defensive_ids), so there's no nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// MNK signature/burst abilities, in display order (mirror python/jobs/monk/
// data.py::BURST_ABILITIES + the high-value Blitz payoffs). Counts come from the
// shared Abilities aspect state; names + icons from abilityMeta — so this is
// just the curated id list.
const MNK_BURST_IDS = [
  7395, // Riddle of Fire (the +15% window)
  7396, // Brotherhood (the party buff / 120s cycle)
  69, // Perfect Balance
  25769, // Phantom Rush
  36948, // Elixir Burst
  25768, // Rising Phoenix
  36950, // Fire's Reply
  36949, // Wind's Reply
];

export const monk: JobProfile = {
  name: 'Monk',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: MNK_BURST_IDS,
    },
  ],
};
