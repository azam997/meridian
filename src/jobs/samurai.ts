// Samurai frontend profile: the declarative burst cast-count section. SAM has
// no bespoke dashboard panel in v1 (Fugetsu uptime + Tengentsu Kenki ride on the
// Scoring state / headline). Its defensives + role actions arrive with bundled
// ids tagged isDefensive, so there's no nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// SAM signature/burst abilities, in display order (mirror python/jobs/samurai/
// data.py::BURST_ABILITIES). Counts come from the shared Abilities aspect state;
// names + icons from abilityMeta — so this is just the curated id list.
const SAM_BURST_IDS = [
  16482, // Ikishoten
  7499,  // Meikyo Shisui
  25781, // Ogi Namikiri
  36966, // Tendo Setsugekka
  7487,  // Midare Setsugekka
  16481, // Hissatsu: Senei
  36964, // Zanshin
  16487, // Shoha
];

export const samurai: JobProfile = {
  name: 'Samurai',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: SAM_BURST_IDS,
    },
  ],
};
