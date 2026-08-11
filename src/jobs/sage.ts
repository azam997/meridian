// Sage frontend profile — the analyzer's fourth HEALER, second shield healer
// (mirrors Scholar / Astrologian). Two deviations from the shared DPS baseline:
//  * the Timeline Defensives lane (`defensiveTimeline`): the healing / mitigation /
//    utility kit (Eukrasian Prognosis, Kerachole, Taurochole, Holos, Panhaima,
//    Kardia, Soteria …) is surfaced in its own lane (You + each ref) for healing
//    review instead of being filtered off. The damage GCDs (Dosis III, Eukrasian
//    Dosis III, Phlegma III, Dyskrasia II) and Psyche stay on the DPS lanes.
//  * a declarative Burst cast-count section (Phlegma III + Psyche + the DoT).
// No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// SGE signature/burst abilities, in display order (mirror python/jobs/sage/
// data.py). Counts come from the shared Abilities aspect state; names + icons from
// abilityMeta.
const SGE_BURST_IDS = [
  24313, // Phlegma III
  37033, // Psyche
  24314, // Eukrasian Dosis III (the DoT)
];

export const sage: JobProfile = {
  name: 'Sage',
  panels: [],
  defensiveTimeline: true,
  // A healer's damage efficiency drops when the fight forces throughput healing
  // (the sim ceiling is damage-optimal), so the sub-80% "trolling" easter egg is
  // unfair here — reframe it as actionable mitigation advice.
  lowEfficiencyNote:
    'Look for opportunities to increase mitigation if being required to heal too much.',
  castCountPanels: [
    {
      sectionTitle: 'Burst & DoT upkeep',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: SGE_BURST_IDS,
    },
  ],
};
