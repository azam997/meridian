// White Mage frontend profile — the analyzer's first HEALER. Two deviations
// from the shared DPS baseline:
//  * the Timeline Defensives lane (`defensiveTimeline`): the healing /
//    mitigation kit (Cure III, Medica III, Tetragrammaton, Temperance …) is
//    surfaced in its own lane (You + each ref) for healing review instead of
//    being filtered off — the healer analog of the tank mitigation lane. The
//    lily heals + Misery stay on the DPS lanes (they're the damage economy).
//  * a declarative Burst cast-count section (PoM window + lily payoff).
// No bespoke dashboard panels yet.

import { Swords } from 'lucide-react';
import type { JobProfile } from './types';

// WHM signature/burst abilities, in display order (mirror
// python/jobs/whitemage/data.py). Counts come from the shared Abilities aspect
// state; names + icons from abilityMeta.
const WHM_BURST_IDS = [
  136, // Presence of Mind
  37009, // Glare IV
  16535, // Afflatus Misery
  3571, // Assize
  16531, // Afflatus Solace
  16534, // Afflatus Rapture
];

export const whitemage: JobProfile = {
  name: 'White Mage',
  panels: [],
  defensiveTimeline: true,
  // A healer's damage efficiency drops when the fight forces throughput healing
  // (the sim ceiling is damage-optimal), so the sub-80% "trolling" easter egg is
  // unfair here — reframe it as actionable mitigation advice.
  lowEfficiencyNote:
    'Look for opportunities to increase mitigation if being required to heal too much.',
  castCountPanels: [
    {
      sectionTitle: 'Burst & lily economy',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: WHM_BURST_IDS,
    },
  ],
};
