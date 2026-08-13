// Draft model + conversions for the custom mit-plan editor. The draft is the
// editor's source of truth (per mechanic id, the mits the user placed); the
// wire/export form is the premade-v3 schema (see UserMitPlan in contract.ts —
// snake_case by design, it IS the file format). All pure functions.

import { JOB_META } from '../components/jobs';
import type {
  MitLibraryResult, MitMechanic, MitPlanResult, UserMitPlan, UserMitPlanEntry,
} from '../sidecar/contract';

export type DraftMit = { job: string; actionId: number };
/** mechanicId → the mits the user placed on it. */
export type MitDraft = Record<string, DraftMit[]>;

export type DraftHeal = { job: string; actionId: number; count: number };
/** mechanicId → the GCD top-up heals the user authored for the gap BEFORE
 *  that mechanic's hit (the +/- incrementer). Plan content like the mits. */
export type MitHealDraft = Record<string, DraftHeal[]>;

export const draftCount = (draft: MitDraft): number =>
  Object.values(draft).reduce((n, mits) => n + mits.length, 0);

export const healCount = (heals: MitHealDraft): number =>
  Object.values(heals).reduce(
    (n, hs) => n + hs.reduce((k, h) => k + h.count, 0), 0);

/** Occurrence = index among mechanics sharing the boss id, in array (time)
 *  order — provably equal to the backend's per-ability ordinal, and the
 *  premade schema's stable disambiguator across damage-model rebuilds. */
function occurrenceOf(m: MitMechanic, mechanics: MitMechanic[]): number {
  const bid = m.bossAbilityIds[0];
  let n = 0;
  for (const x of mechanics) {
    if (x.id === m.id) return n;
    if (x.bossAbilityIds[0] === bid) n += 1;
  }
  return n;
}

export function draftToPlan(
  draft: MitDraft, heals: MitHealDraft, mechanics: MitMechanic[],
  encounterId: number, encounterName: string,
): UserMitPlan {
  const assignments: UserMitPlanEntry[] = [];
  for (const m of mechanics) {
    const mits = draft[m.id] ?? [];
    const hs = heals[m.id] ?? [];
    if ((!mits.length && !hs.length) || m.kind === 'hpSet'
        || !m.bossAbilityIds.length) continue;
    const entry: UserMitPlanEntry = {
      mechanic: m.name,
      name: m.name,
      boss_ability_id: m.bossAbilityIds[0],
      occurrence: occurrenceOf(m, mechanics),
      at_sec: m.timeSec,
      mits: mits.map((x) => ({ job: x.job, action_id: x.actionId })),
    };
    if (hs.length) {
      entry.gcd_heals = hs.map((h) => ({
        job: h.job, action_id: h.actionId, count: h.count,
      }));
    }
    assignments.push(entry);
  }
  return {
    encounter_id: encounterId,
    encounter_name: encounterName,
    source: 'Meridian custom plan',
    assignments,
  };
}

/** Convert a scored result into a draft — the "Seed from sim plan" path.
 *  Suggestions (tank personals, invuln escalations, amp riders) and carryover
 *  blanket credit are the generator's advice, not placements. The sim plan's
 *  inserted GCD heals DO seed (as authored heals) — they become the user's to
 *  keep, trim, or grow. */
export function seedFromResult(result: MitPlanResult): {
  mits: MitDraft; heals: MitHealDraft;
} {
  const mits: MitDraft = {};
  const heals: MitHealDraft = {};
  for (const m of result.mechanics) {
    if (m.kind === 'hpSet') continue;
    const rows: DraftMit[] = [];
    for (const a of m.assignments) {
      if (a.isSuggestion || a.isCarryover) continue;
      if (rows.some((x) => x.job === a.job && x.actionId === a.actionId)) continue;
      rows.push({ job: a.job, actionId: a.actionId });
    }
    if (rows.length) mits[m.id] = rows;
    const hs: DraftHeal[] = [];
    for (const g of m.gcdHeals) {
      const prior = hs.find((x) => x.job === g.job && x.actionId === g.actionId);
      if (prior) prior.count += g.count;
      else hs.push({ job: g.job, actionId: g.actionId, count: g.count });
    }
    if (hs.length) heals[m.id] = hs;
  }
  return { mits, heals };
}

// --- Draft autosave ----------------------------------------------------------
// Crash/nav protection, NOT a plan library: one slot per encounter, stored in
// the serialized (premade-schema) form so it survives damage-model rebuilds —
// MitigationPlanner unmounts on nav and an authored draft must not die to a
// stray click. Export/import files remain the sharing mechanism.

const STORE_KEY = 'fflogs.efficiency.analyzer.mitdraft.v1';

function loadSavedPlans(): Record<string, UserMitPlan> {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, UserMitPlan>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function loadPlanDraft(encounterId: number): UserMitPlan | null {
  return loadSavedPlans()[String(encounterId)] ?? null;
}

/** Persist (or, with null / an empty plan, remove) the encounter's autosave. */
export function savePlanDraft(encounterId: number,
                              plan: UserMitPlan | null): void {
  try {
    const all = loadSavedPlans();
    if (plan && plan.assignments.length) all[String(encounterId)] = plan;
    else delete all[String(encounterId)];
    localStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch {
    /* ignore */
  }
}

const norm = (s: string) => s.trim().replace(/\s+/g, ' ').toLowerCase();

/** Import: match a plan file's entries onto the current damage model. Client
 *  mirror of the backend's best-effort matching (boss id → occurrence, else
 *  nearest at_sec, else normalized name); unmatched/off-comp rows drop with a
 *  notice. The backend re-validates on the next score regardless. */
export function planToDraft(
  plan: UserMitPlan, mechanics: MitMechanic[], library: MitLibraryResult,
): { draft: MitDraft; heals: MitHealDraft; notices: string[] } {
  const notices: string[] = [];
  const draft: MitDraft = {};
  const heals: MitHealDraft = {};

  const jobActions = new Map<string, Set<number>>();
  const jobHeals = new Map<string, Set<number>>();
  const compJobsInOrder: string[] = [];
  for (const s of library.slots) {
    compJobsInOrder.push(s.job);
    const set = jobActions.get(s.job) ?? new Set<number>();
    for (const a of s.actions) set.add(a.actionId);
    jobActions.set(s.job, set);
    const hset = jobHeals.get(s.job) ?? new Set<number>();
    for (const o of s.healOptions ?? []) hset.add(o.actionId);
    jobHeals.set(s.job, hset);
  }

  const byBoss = new Map<number, MitMechanic[]>();
  const byName = new Map<string, MitMechanic[]>();
  for (const m of mechanics) {
    for (const bid of m.bossAbilityIds) {
      byBoss.set(bid, [...(byBoss.get(bid) ?? []), m]);
    }
    byName.set(norm(m.name), [...(byName.get(norm(m.name)) ?? []), m]);
  }

  for (const entry of plan.assignments ?? []) {
    const label = entry.mechanic || entry.name
      || String(entry.boss_ability_id ?? '?');
    let cands = entry.boss_ability_id != null
      ? byBoss.get(entry.boss_ability_id) ?? [] : [];
    if (!cands.length && entry.name) cands = byName.get(norm(entry.name)) ?? [];
    if (!cands.length) {
      notices.push(`No mechanic matched "${label}". Dropped.`);
      continue;
    }
    let target: MitMechanic | undefined;
    if (entry.occurrence != null) {
      target = cands[entry.occurrence];
      if (!target) {
        notices.push(`"${label}" occurrence #${entry.occurrence} not found. Dropped.`);
        continue;
      }
    } else if (entry.at_sec != null) {
      const at = entry.at_sec;
      target = cands.reduce((best, x) =>
        Math.abs(x.timeSec - at) < Math.abs(best.timeSec - at) ? x : best);
    } else {
      target = cands[0];
      if (cands.length > 1) {
        notices.push(`"${label}" matched ${cands.length} mechanics. Applied to the first.`);
      }
    }
    for (const mit of entry.mits ?? []) {
      const aid = mit.action_id;
      let job = mit.job;
      if (!job && mit.role) {
        job = compJobsInOrder.find(
          (j) => JOB_META[j]?.role === mit.role && jobActions.get(j)?.has(aid));
      }
      if (!job || !jobActions.get(job)?.has(aid)) {
        notices.push(`${mit.job ?? mit.role ?? '?'} #${aid} is not available in `
          + `this party. Dropped from "${label}".`);
        continue;
      }
      const mits = (draft[target.id] ??= []);
      if (!mits.some((x) => x.job === job && x.actionId === aid)) {
        mits.push({ job, actionId: aid });
      }
    }
    for (const h of entry.gcd_heals ?? []) {
      if (!h.job || !jobHeals.get(h.job)?.has(h.action_id)) {
        notices.push(`${h.job ?? '?'} heal #${h.action_id} is not available `
          + `in this party. Dropped from "${label}".`);
        continue;
      }
      const hs = (heals[target.id] ??= []);
      const prior = hs.find(
        (x) => x.job === h.job && x.actionId === h.action_id);
      const count = Math.max(1, Math.min(8, h.count || 1));
      if (prior) prior.count = Math.min(8, prior.count + count);
      else hs.push({ job: h.job, actionId: h.action_id, count });
    }
  }
  return { draft, heals, notices };
}
