// Machinist frontend profile: dashboard panels. The panels themselves live in
// views/jobPanels/machinist.tsx. Defensive casts (Tactician, Dismantle) are now
// flagged by the backend via abilityMeta.isDefensive (JobData.defensive_ids), so
// there's no per-job id list to maintain here.

import { Bot, Flame, Wrench } from 'lucide-react';
import { QueenPanel, WildfirePanel } from '../views/jobPanels/machinist';
import type { JobProfile } from './types';

export const machinist: JobProfile = {
  name: 'Machinist',
  panels: [QueenPanel, WildfirePanel],
  // MCH-flavored Potential-Improvements sections (ids from python/jobs/
  // machinist): burst cards + the burst enablers' missed-casts group under
  // Wildfire; gauge overcaps + Queen under Queen; tool missed-casts and
  // Reassemble misuse under Tools. Everything else (pacing, deaths, generic
  // missed casts) keeps the generic categories.
  improvementCategories: [
    {
      id: 'wildfire',
      label: 'Wildfire & Hypercharge',
      icon: Flame,
      kinds: ['wildfire', 'hypercharge'],
      // Wildfire, Hypercharge, Barrel Stabilizer, Full Metal Field
      abilityIds: [2878, 17209, 7414, 36982],
    },
    {
      id: 'queen',
      label: 'Queen & gauges',
      icon: Bot,
      kinds: ['overcap'],
      // Automaton Queen
      abilityIds: [16501],
    },
    {
      id: 'tools',
      label: 'Tools',
      icon: Wrench,
      // Reassemble misuse cards carry ability_id=0, so they route by kind
      // (safe: DRG's Life Surge split off to its own `lifesurge` kind).
      kinds: ['align'],
      // Drill, Air Anchor, Chain Saw, Excavator, Reassemble — captures the
      // tools' missed-casts. Battery overcaps on the generators still land in
      // Queen & gauges: kinds outrank abilityIds in categorizeImprovements.
      abilityIds: [16498, 16500, 25788, 36981, 2876],
    },
  ],
};
