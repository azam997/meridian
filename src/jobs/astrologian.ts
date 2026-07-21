// Astrologian frontend profile — the analyzer's second HEALER (mirrors White
// Mage). Two deviations from the shared DPS baseline:
//  * the Timeline Defensives lane (`defensiveTimeline`): the healing /
//    mitigation kit + the card-draw / ally-buff oGCDs (Benefic, Helios, Essential
//    Dignity, Astral/Umbral Draw, Play, the mit cards …) is surfaced in its own
//    lane (You + each ref) for healing review instead of being filtered off. The
//    damage oGCDs (Divination, Oracle, Earthly Star, Lord of Crowns) stay on the
//    DPS lanes.
//  * a declarative Burst cast-count section (Divination window + card payoff).
// No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// AST signature/burst abilities, in display order (mirror
// python/jobs/astrologian/data.py). Counts come from the shared Abilities aspect
// state; names + icons from abilityMeta.
const AST_BURST_IDS = [
  16552, // Divination
  37029, // Oracle
  7444, // Lord of Crowns
  7439, // Earthly Star
  37022, // Minor Arcana
];

export const astrologian: JobProfile = {
  name: 'Astrologian',
  panels: [],
  defensiveTimeline: true,
  // A healer's damage efficiency drops when the fight forces throughput healing
  // (the sim ceiling is damage-optimal), so the sub-80% "trolling" easter egg is
  // unfair here — reframe it as actionable mitigation advice.
  lowEfficiencyNote:
    'Look for opportunities to increase mitigation if being required to heal too much.',
  castCountPanels: [
    {
      sectionTitle: 'Burst & card economy',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: AST_BURST_IDS,
    },
  ],
};
