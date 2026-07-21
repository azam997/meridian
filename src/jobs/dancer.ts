// Dancer frontend profile. DNC ships only the shared analysis views plus a
// declarative burst cast-count section; its proc-utilization data is on the wire
// (aspectStates.Procs) for a future dedicated panel. The utility oGCDs (En Avant,
// Curing Waltz, …) arrive tagged isDefensive from the backend (JobData.defensive_ids),
// so nonRotationalNames is just a name-based fallback for unresolved ids.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// DNC signature/burst abilities, in display order (mirror python/jobs/dancer/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const DNC_BURST_IDS = [
  15998, // Technical Step
  16011, // Devilment
  16013, // Flourish
  36985, // Dance of the Dawn
  16005, // Saber Dance
  25790, // Tillana
  36983, // Last Dance
  25792, // Starfall Dance
  15991, // Reverse Cascade
  15992, // Fountainfall
  16009, // Fan Dance III
];

export const dancer: JobProfile = {
  name: 'Dancer',
  panels: [],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: DNC_BURST_IDS,
    },
  ],
  // Fallback only — the primary defensive filter is the backend isDefensive flag.
  nonRotationalNames: [
    'Closed Position',
    'En Avant',
    'Curing Waltz',
    'Improvisation',
    'Shield Samba',
  ],
};
