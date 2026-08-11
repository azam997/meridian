// Dragoon frontend profile: the declarative burst cast-count section. DRG has no
// bespoke dashboard panel in v1 — its self-buffs (Power Surge / Lance Charge / Life
// of the Dragon) and the Chaotic Spring DoT ride on the Scoring state / headline, and
// its positional + Life Surge findings surface through the generic aspect tabs
// (Positionals / LifeSurge). Its Elusive Jump / Winged Glide arrive with bundled ids
// tagged isDefensive (data.defensive_ids), so there's no nonRotationalNames to keep.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// DRG signature/burst abilities, in display order (mirror python/jobs/dragoon/
// data.py::BURST_ABILITIES + the Life-of-the-Dragon chain). Counts come from the
// shared Abilities aspect state; names + icons from abilityMeta — so this is just
// the curated id list.
const DRG_BURST_IDS = [
  3555, // Geirskogul (activates Life of the Dragon)
  7400, // Nastrond
  16480, // Stardiver
  85, // Lance Charge
  3557, // Battle Litany
  83, // Life Surge
  96, // Dragonfire Dive
  25773, // Wyrmwind Thrust
];

export const dragoon: JobProfile = {
  name: 'Dragoon',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: DRG_BURST_IDS,
    },
  ],
};
