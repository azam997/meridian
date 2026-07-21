// Red Mage frontend profile: the proc-utilization + mana-balance dashboard
// panels and the declarative burst cast-count section. RDM's defensive oGCD
// (Magick Barrier) arrives tagged isDefensive from the backend, so there's no
// nonRotationalNames list to maintain here.

import { Swords } from 'lucide-react';
import { ManaBalancePanel, ProcUtilizationPanel } from '../views/jobPanels/redmage';
import type { JobProfile } from './types';

// RDM signature/burst abilities, in display order (mirror python/jobs/redmage/
// data.py). Counts come from the shared Abilities aspect state; names + icons
// from abilityMeta — so this is just the curated id list, not a name map.
const RDM_BURST_IDS = [
  7520, // Embolden
  7521, // Manafication
  7517, // Fleche
  7519, // Contre-sixte
  7526, // Verholy
  7525, // Verflare
  16530, // Scorch
  25858, // Resolution
  37006, // Grand Impact
  37005, // Vice of Thorns
  37007, // Prefulgence
];

export const redmage: JobProfile = {
  name: 'Red Mage',
  panels: [ProcUtilizationPanel, ManaBalancePanel],
  castCountPanels: [
    {
      sectionTitle: 'Burst',
      heading: 'Burst cast counts',
      icon: Swords,
      abilityIds: RDM_BURST_IDS,
    },
  ],
};
