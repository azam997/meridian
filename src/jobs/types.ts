// Frontend job registry — the per-job knowledge the shared analysis views need
// that isn't pure data on the wire. Mirrors the backend's per-job packages
// (python/jobs/{job}): the job-agnostic baseline lives in the shared views, and
// a JobProfile supplies only the deviations.

import type { LucideIcon } from 'lucide-react';
import type { JobPanel } from '../views/jobPanels/types';

export type { JobPanel, JobPanelProps } from '../views/jobPanels/types';

/** Declarative spec for a generic cast-count section card (rendered by the
 *  shared `CastCountPanel`): a curated list of the job's signature abilities,
 *  shown with their cast counts. The counts come from the shared `Abilities`
 *  aspect state and names/icons from `abilityMeta`, so this is pure per-job data
 *  — no bespoke component. Use it for "how many Xs did you press" sections (e.g.
 *  RPR burst); keep a real component panel for anything with richer columns
 *  (MCH Queen/Wildfire). */
export type CastCountPanelSpec = {
  /** Section heading above the card (e.g. "Burst"). */
  sectionTitle: string;
  /** Card title (e.g. "Burst cast counts"). */
  heading: string;
  icon: LucideIcon;
  /** Signature ability ids, in display order. Mirrors the job's data table;
   *  ids with zero casts are omitted, and the whole panel hides if none were
   *  cast. */
  abilityIds: readonly number[];
};

/** A job's override of the generic Potential-Improvements category set
 *  (views/findings.ts::categorizeImprovements). An override either reshapes an
 *  existing generic category id (relabel / re-icon / claim extra kinds) or
 *  introduces a new job section (e.g. MCH "Wildfire & Hypercharge"). Cards are
 *  claimed by `kinds` first (a kind states the loss mechanism), then by
 *  `abilityIds`, before the generic kind→category mapping applies. Jobs
 *  without overrides get the generic set untouched. */
export type ImprovementCategoryOverride = {
  /** Existing generic category id (reshapes it) or a new id (a new section). */
  id: string;
  label: string;
  /** Section-header icon; defaults to the generic category's (or the
   *  "Other" glyph for a new id). */
  icon?: LucideIcon;
  /** Improvement kinds claimed into this category — highest precedence
   *  (an overcap is a gauge problem even when its cast is a tool). */
  kinds?: readonly string[];
  /** Ability ids claimed into this category — checked after every override's
   *  `kinds`, for splitting kinds no override claims (e.g. tool missed-casts
   *  out of "Missed casts"). */
  abilityIds?: readonly number[];
};

export type JobProfile = {
  /** Canonical spec name; matches the backend + components/jobs.ts JOB_META. */
  name: string;
  /** Dashboard section cards rendered below the shared findings, in order. */
  panels: JobPanel[];
  /** Job-flavored Potential-Improvements categories (see
   *  ImprovementCategoryOverride). Absent → the generic category set. */
  improvementCategories?: readonly ImprovementCategoryOverride[];
  /** Declarative cast-count sections, rendered after `panels`. */
  castCountPanels?: CastCountPanelSpec[];
  /** Render a dedicated Defensives lane on the Timeline (tanks). Defensive /
   *  utility casts are normally filtered off every lane via the backend
   *  `isDefensive` flag; when this is set, they're surfaced in their own lane
   *  (under You and each reference) for mitigation review — still excluded from
   *  the DPS cast-diff and scoring. Off (default) for every DPS job. */
  defensiveTimeline?: boolean;
  /** Extra non-rotational cast NAMES beyond SHARED_NON_ROTATIONAL_NAMES. Only a
   *  fallback for casts that arrive without a resolved id — the primary,
   *  data-driven path is the backend `isDefensive` flag on abilityMeta (sourced
   *  from the shared role actions ∪ `JobData.defensive_ids`). Rarely needed. */
  nonRotationalNames?: readonly string[];
  /** Replaces the sub-floor "trolling" easter-egg message on the efficiency
   *  ring when set. Healers HEAL — low *damage* efficiency is expected when the
   *  fight demands throughput healing (the ceiling is damage-optimal and assumes
   *  none), so ribbing it is wrong; a healer surfaces a constructive mitigation
   *  note instead. Off (default) = the easter egg, for DPS/tanks. */
  lowEfficiencyNote?: string;
};

/** Role/utility actions every job shares — the name-based fallback for casts
 *  that arrive without a resolved id (so the backend `isDefensive` flag on
 *  abilityMeta couldn't apply). Kept in sync with the backend's
 *  ROLE_ACTION_IDS (python/jobs/_core/role_actions.py). */
export const SHARED_NON_ROTATIONAL_NAMES: readonly string[] = [
  'Sprint',
  "Arm's Length",
  'Second Wind',
  'Bloodbath',
  'True North',
  'Feint',
  'Leg Sweep',
  'Foot Graze',
  'Leg Graze',
  'Head Graze',
  'Peloton',
  // Tank role actions (defensives / utility).
  'Rampart',
  'Reprisal',
  'Provoke',
  'Shirk',
  'Interject',
  'Low Blow',
  // Magic (caster / healer) role actions. Swiftcast's DPS effect is modeled in
  // the simulator, but the button itself is non-rotational like the rest.
  'Swiftcast',
  'Addle',
  'Lucid Dreaming',
  'Surecast',
  // Healer role actions.
  'Esuna',
  'Rescue',
  'Repose',
];
