// UI helpers shared by the dashboard's findings/improvements rendering and the
// timeline's hover bubbles.
//
// Severity is assigned in the UI from lostPotency thresholds (the backend
// never sets it): <200p info, 200-499 warn, >=500 bad.

import {
  Battery,
  Clock,
  Crosshair,
  FlaskConical,
  Flame,
  MoreHorizontal,
  Move,
  RefreshCw,
  Skull,
  Sparkles,
  Target,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import type { FindingTag, Improvement } from '../sidecar/contract';
import type { ImprovementCategoryOverride } from '../jobs/types';

export type Severity = 'bad' | 'warn' | 'info';

export const severityFor = (lostPotency: number): Severity => {
  const p = Math.abs(lostPotency);
  if (p >= 500) return 'bad';
  if (p >= 200) return 'warn';
  return 'info';
};

/** Single source of truth for how a loss/finding `kind` renders: its human
 *  label (timeline bubble headers) and its 3-letter badge (severity chips).
 *  Spans every `Improvement.kind` AND every `FindingTag` — both are free-form
 *  strings the backend invents, so unknown values degrade via `kindLabel` /
 *  `kindBadge` rather than failing. Job-specific flavor is layered on top via
 *  the job registry's `improvementCategories` overrides (src/jobs/types.ts),
 *  consumed by `categorizeImprovements` below; this is the job-agnostic
 *  baseline. */
type KindMeta = { label: string; badge: string };

const KIND_META: Record<string, KindMeta> = {
  // Improvement kinds (sim-diff decomposition)
  death:           { label: 'Death',              badge: 'DTH' },
  missed_cast:     { label: 'Missed cast',        badge: 'MIS' },
  missed_enabler:  { label: 'Missed enabler',     badge: 'MIS' },
  wildfire:        { label: 'Wildfire window',    badge: 'WF' },
  hypercharge:     { label: 'Hypercharge window', badge: 'HYP' },
  idle:            { label: 'Idle time',          badge: 'IDL' },
  clip:            { label: 'GCD clip',           badge: 'CLI' },
  overcap:         { label: 'Overcap',            badge: 'OVC' },
  align:           { label: 'Reassemble misuse',  badge: 'ALI' },
  lifesurge:       { label: 'Life Surge misuse',  badge: 'LSG' },
  opener:          { label: 'Opener',             badge: 'OPE' },
  drift:           { label: 'Drift',              badge: 'DRI' },
  filler:          { label: 'Filler quality',     badge: 'FIL' },
  multi_target:    { label: 'Multi-target',       badge: 'MTG' },
  multitarget:     { label: 'Multi-target',       badge: 'MTG' },
  proc:            { label: 'Wasted procs',       badge: 'PRC' },
  extra_heal_gcds: { label: 'Healing beyond plan', badge: 'HEA' },
  pacing:          { label: 'GCD uptime & pacing', badge: 'PAC' },
  cadence:         { label: 'Loose pacing',       badge: 'PAC' },
  burst:           { label: 'Burst resource',     badge: 'BST' },
  residual_tail:   { label: 'Diffuse remainder',  badge: 'OTH' },
  residual:        { label: 'Other',              badge: 'OTH' },
  flamethrower:    { label: 'Flamethrower window', badge: 'FLM' },
  tincture:        { label: 'Tincture',            badge: 'TIN' },
  surging_tempest: { label: 'Surging Tempest',    badge: 'SRG' },
  deaths_design:   { label: "Death's Design uptime", badge: 'DDS' },
  darkside:        { label: 'Darkside uptime',    badge: 'DKS' },
  // FindingTag values (aspect findings / context)
  model:           { label: 'Model',              badge: 'MOD' },
  positional:      { label: 'Positional',         badge: 'POS' },
  'deaths-design': { label: "Death's Design",     badge: 'DDS' },
  'surging-tempest': { label: 'Surging Tempest',  badge: 'SRG' },
  'multi-target':  { label: 'Multi-target',       badge: 'MTG' },
};

/** Human label for a finding/improvement `kind`. Unknown kinds fall back to
 *  `fallback` (default "Other"), so a new job's novel kind never renders blank. */
export const kindLabel = (kind: string, fallback = 'Other'): string =>
  KIND_META[kind]?.label ?? fallback;

/** 3-letter badge for a finding/improvement `kind`. Unknown kinds fall back to
 *  `fallback` (default "IMP"). */
export const kindBadge = (kind: string, fallback = 'IMP'): string =>
  KIND_META[kind]?.badge ?? fallback;

/** 3-letter badge per the FindingTag (back-compat alias over `kindBadge`;
 *  FindingTag is a closed subset of the registry keys). */
export const tagBadge = (t: FindingTag): string => kindBadge(t);

// --- Potential-Improvements categories --------------------------------------
// The dashboard groups improvement cards under a few larger headings so the
// panel reads as organized themes (uptime, burst, resources…) instead of one
// flat ranked list. The generic set below covers every known kind; a job can
// reshape it via `JobProfile.improvementCategories` (src/jobs/types.ts — e.g.
// MCH claims wildfire/hypercharge into a "Wildfire & Hypercharge" section).

export type ImprovementCategoryDef = {
  id: string;
  label: string;
  icon: LucideIcon;
};

const CATEGORY_DEFS: readonly ImprovementCategoryDef[] = [
  { id: 'deaths',    label: 'Deaths',                  icon: Skull },
  { id: 'uptime',    label: 'GCD uptime & pacing',     icon: Clock },
  { id: 'missed',    label: 'Missed casts & drift',    icon: Target },
  { id: 'burst',     label: 'Burst & buff windows',    icon: Zap },
  { id: 'upkeep',    label: 'Buff upkeep',             icon: RefreshCw },
  { id: 'resource',  label: 'Resource & procs',        icon: Battery },
  { id: 'mechanics', label: 'Targeting & positionals', icon: Crosshair },
  { id: 'other',     label: 'Other',                   icon: MoreHorizontal },
];

/** Generic kind → category id. Kinds absent here (a future job's novel kind)
 *  land in `other`. */
const KIND_CATEGORY: Record<string, string> = {
  death: 'deaths',
  pacing: 'uptime',
  idle: 'uptime',
  clip: 'uptime',
  cadence: 'uptime',
  missed_cast: 'missed',
  missed_enabler: 'missed',
  drift: 'missed',
  filler: 'missed',
  flamethrower: 'missed',
  wildfire: 'burst',
  hypercharge: 'burst',
  align: 'burst',
  lifesurge: 'burst',
  tincture: 'burst',
  burst: 'burst',
  deaths_design: 'upkeep',
  darkside: 'upkeep',
  surging_tempest: 'upkeep',
  overcap: 'resource',
  proc: 'resource',
  positional: 'mechanics',
  multitarget: 'mechanics',
  multi_target: 'mechanics',
  extra_heal_gcds: 'mechanics',
  residual: 'other',
  residual_tail: 'other',
  opener: 'other',
};

/** Sharper per-kind Lucide glyph for the card icon cell when a card carries no
 *  resolvable ability icon (death, tincture, aggregates…). Falls back to the
 *  kind's category icon, then the "Other" glyph. */
const KIND_GLYPHS: Record<string, LucideIcon> = {
  death: Skull,
  tincture: FlaskConical,
  wildfire: Flame,
  proc: Sparkles,
  positional: Move,
};

export const kindGlyph = (kind: string): LucideIcon => {
  const g = KIND_GLYPHS[kind];
  if (g) return g;
  const cat = KIND_CATEGORY[kind];
  return CATEGORY_DEFS.find((d) => d.id === cat)?.icon ?? MoreHorizontal;
};

export type ImprovementCategory = {
  def: ImprovementCategoryDef;
  cards: Improvement[];
  subtotal: number;
};

/** Group (already repriced / denial-filtered) improvement cards into ordered
 *  category sections. Resolution per card: the note rule (zero-priced
 *  diagnostics + the residual read quieter under "Other") → job-override
 *  kinds → job-override abilityIds → the generic kind map → "Other".
 *  Kinds outrank abilityIds because a kind states the loss MECHANISM — an
 *  overcap involving a tool cast is still a gauge problem, so MCH's
 *  `kinds: ['overcap']` claim must beat its Tools section's ability list;
 *  abilityIds then split only the kinds no override claims (missed casts by
 *  ability). Categories order by subtotal desc with "Other" pinned last;
 *  cards keep their potency ranking within each. Pure — safe to call inline. */
export function categorizeImprovements(
  cards: Improvement[],
  overrides?: readonly ImprovementCategoryOverride[],
): ImprovementCategory[] {
  const defs = new Map<string, ImprovementCategoryDef>();
  for (const d of CATEGORY_DEFS) defs.set(d.id, d);
  for (const o of overrides ?? []) {
    const base = defs.get(o.id);
    defs.set(o.id, {
      id: o.id,
      label: o.label,
      icon: o.icon ?? base?.icon ?? MoreHorizontal,
    });
  }

  const resolve = (im: Improvement): string => {
    if (
      im.lostPotency <= 0 ||
      im.kind === 'residual' ||
      im.kind === 'residual_tail' ||
      im.kind === 'opener'
    ) {
      return 'other';
    }
    for (const o of overrides ?? []) {
      if (o.kinds?.includes(im.kind)) return o.id;
    }
    if (im.abilityId > 0) {
      for (const o of overrides ?? []) {
        if (o.abilityIds?.includes(im.abilityId)) return o.id;
      }
    }
    return KIND_CATEGORY[im.kind] ?? 'other';
  };

  const buckets = new Map<string, Improvement[]>();
  for (const im of cards) {
    const id = resolve(im);
    const b = buckets.get(id);
    if (b) b.push(im);
    else buckets.set(id, [im]);
  }

  const out: ImprovementCategory[] = [];
  for (const [id, cs] of buckets) {
    out.push({
      def: defs.get(id) ?? { id, label: id, icon: MoreHorizontal },
      cards: [...cs].sort((x, y) => y.lostPotency - x.lostPotency),
      subtotal: cs.reduce((acc, c) => acc + Math.max(0, c.lostPotency), 0),
    });
  }
  out.sort((x, y) => {
    if (x.def.id === 'other') return 1;
    if (y.def.id === 'other') return -1;
    return y.subtotal - x.subtotal;
  });
  return out;
}
