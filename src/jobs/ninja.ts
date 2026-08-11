// Ninja frontend profile: the declarative burst cast-count section. NIN has no
// bespoke dashboard panel in v1 (the Kunai's Bane window / mudra economy ride on
// the Scoring state / headline). Shade Shift / Shukuchi / Hide arrive with
// bundled ids tagged isDefensive (data.defensive_ids), so there's no
// nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// NIN signature/burst abilities, in display order (mirror python/jobs/ninja/
// data.py::BURST_ABILITIES + the high-value cycle payoffs). Counts come from the
// shared Abilities aspect state; names + icons from abilityMeta — so this is just
// the curated id list.
const NIN_BURST_IDS = [
  36958, // Kunai's Bane (the +10% window)
  36957, // Dokumori (the party buff / 120s cycle)
  7403, // Ten Chi Jin
  36961, // Tenri Jindo
  16492, // Hyosho Ranryu (the Kassatsu ninjutsu)
  16493, // Bunshin
];

export const ninja: JobProfile = {
  name: 'Ninja',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: NIN_BURST_IDS,
    },
  ],
};
