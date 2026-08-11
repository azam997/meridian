// Scholar frontend profile — the analyzer's third HEALER (mirrors Astrologian).
// Two deviations from the shared DPS baseline:
//  * the Timeline Defensives lane (`defensiveTimeline`): the healing / mitigation
//    kit + the heal-only fairy (Eos/Selene) commands (Adloquium, Succor, Sacred
//    Soil, Expedient, Whispering Dawn, Summon Seraph …) is surfaced in its own lane
//    (You + each ref) for healing review instead of being filtered off. The damage
//    oGCDs (Chain Stratagem, Baneful Impaction, Energy Drain, Aetherflow) stay on
//    the DPS lanes.
//  * a declarative Burst cast-count section (Chain Stratagem window + Aetherflow).
// No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// SCH signature/burst abilities, in display order (mirror python/jobs/scholar/
// data.py). Counts come from the shared Abilities aspect state; names + icons from
// abilityMeta.
const SCH_BURST_IDS = [
  7436, // Chain Stratagem
  37012, // Baneful Impaction
  166, // Aetherflow
  167, // Energy Drain
];

export const scholar: JobProfile = {
  name: 'Scholar',
  panels: [],
  defensiveTimeline: true,
  // A healer's damage efficiency drops when the fight forces throughput healing
  // (the sim ceiling is damage-optimal), so the sub-80% "trolling" easter egg is
  // unfair here — reframe it as actionable mitigation advice.
  lowEfficiencyNote:
    'Look for opportunities to increase mitigation if being required to heal too much.',
  castCountPanels: [
    {
      sectionTitle: 'Burst & Aetherflow economy',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: SCH_BURST_IDS,
    },
  ],
};
