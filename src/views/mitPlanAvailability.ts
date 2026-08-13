// Client-side availability preview for the custom mit-plan editor: while the
// user drags an ability, rows where it cannot be up grey out. Ports the
// planner's ActionTimeline (charge-aware trailing window) and ResourcePool
// (token bucket) feasibility checks over the draft's DERIVED cast times
// (max(minCast, firstHit − castLead) — the lead policy ships per action from
// the backend, so timing lives in one place). This is a preview only: the
// backend planner stays authoritative and drops an infeasible cast with a
// "Your plan:" warning on the next score.

import type { MitDraft } from './mitPlanDraft';
import type {
  MitLibraryAction, MitLibraryResult, MitMechanic,
} from '../sidecar/contract';

const firstHitSec = (m: MitMechanic): number => m.hits[0]?.timeSec ?? m.timeSec;

const castAtFor = (a: MitLibraryAction, m: MitMechanic): number =>
  Math.max(a.minCastSec, firstHitSec(m) - a.castLeadSec);

/** Port of planner.ActionTimeline._ok: for each cast, the number of casts in
 *  its trailing `cooldown × charges` window must not exceed the charges. */
function chargeOk(casts: number[], cooldownSec: number, charges: number): boolean {
  const cap = Math.max(1, charges);
  const window = cooldownSec * cap;
  if (window <= 0) return true;
  const sorted = [...casts].sort((x, y) => x - y);
  for (let i = 0; i < sorted.length; i++) {
    let n = 0;
    for (let k = 0; k <= i; k++) {
      if (sorted[k] > sorted[i] - window + 1e-9) n += 1;
    }
    if (n > cap) return false;
  }
  return true;
}

/** Port of planner.ResourcePool._ok: token bucket with a per-pool starting
 *  stock (lilies start at ZERO — combat-only accrual), continuous regen
 *  between casts; infeasible when a cast finds less than one token. */
function poolOk(casts: number[], capacity: number, regenSec: number,
                startTokens: number): boolean {
  const sorted = [...casts].sort((x, y) => x - y);
  let tokens = startTokens;
  let prev = 0;
  for (const t of sorted) {
    const at = Math.max(0, t);
    if (regenSec > 0) {
      tokens = Math.min(capacity, tokens + Math.max(0, at - prev) / regenSec);
    }
    if (tokens < 1 - 1e-9) return false;
    tokens -= 1;
    prev = Math.max(prev, at);
  }
  return true;
}

/** Co-weave tolerance for prerequisite pairs (mirror of the planner's
 *  REQUIRES_CO_WEAVE_TOL_S): the dependent's shield lead may put its derived
 *  cast a few seconds BEFORE its enabler on the same mechanic. */
const REQUIRES_TOL_SEC = 6;

/** Row indexes (into `mechanics`) where dropping `action` for `job` would be
 *  infeasible given the current draft: hpSet rows, rows already holding this
 *  (job, action), cooldown/charge violations, and resource-pool exhaustion.
 *  `excludeMechId` (moving an existing cast): that mechanic's copy of this
 *  cast vacates, so it must not block its own relocation — its timeline and
 *  resource-pool contributions are omitted, and its row stays droppable
 *  (dropping back home is a no-op). */
export function blockedRowsFor(
  action: MitLibraryAction, job: string, draft: MitDraft,
  mechanics: MitMechanic[], library: MitLibraryResult,
  excludeMechId?: string,
): Set<number> {
  const blocked = new Set<number>();
  const jobPalette = new Map<number, MitLibraryAction>();
  for (const s of library.slots) {
    if (s.job !== job) continue;
    for (const a of s.actions) jobPalette.set(a.actionId, a);
  }
  const mechById = new Map(mechanics.map((m) => [m.id, m]));

  // Derived cast times of the draft's existing casts that share this action's
  // timeline (same job+action), its resource pool (same job+resource), or
  // enable it (its `requires` prerequisite — Temperance casts for a dragged
  // Divine Caress).
  const sameCasts: number[] = [];
  const poolCasts: number[] = [];
  const enablerCasts: number[] = [];
  for (const [mechId, mits] of Object.entries(draft)) {
    const m = mechById.get(mechId);
    if (!m) continue;
    for (const mit of mits) {
      if (mit.job !== job) continue;
      if (mechId === excludeMechId && mit.actionId === action.actionId) {
        continue;   // the moving cast itself — being vacated
      }
      const a = jobPalette.get(mit.actionId);
      if (!a) continue;
      const t = castAtFor(a, m);
      if (mit.actionId === action.actionId) sameCasts.push(t);
      if (action.resource && a.resource === action.resource) poolCasts.push(t);
      if (action.requiresActionId != null
          && mit.actionId === action.requiresActionId) {
        enablerCasts.push(t);
      }
    }
  }
  const pool = action.resource
    ? library.resourcePools[action.resource] : undefined;

  mechanics.forEach((m, r) => {
    if (m.kind === 'hpSet') {
      blocked.add(r);
      return;
    }
    // Invulns protect the tank alone; the planner only models them on tank
    // busters — block everywhere else (mirrors the backend rule, so a drop
    // can never create a placement the backend refuses).
    if (action.tier === 'invuln' && m.kind !== 'tankbuster') {
      blocked.add(r);
      return;
    }
    if (m.id !== excludeMechId && draft[m.id]?.some(
      (x) => x.job === job && x.actionId === action.actionId)) {
      blocked.add(r);
      return;
    }
    const t = castAtFor(action, m);
    if (!chargeOk([...sameCasts, t], action.cooldownSec, action.charges)) {
      blocked.add(r);
      return;
    }
    if (pool && !poolOk([...poolCasts, t], pool.capacity, pool.regenSec,
                        pool.startTokens ?? pool.capacity)) {
      blocked.add(r);
      return;
    }
    // Prerequisite chain (Divine Caress → Temperance): a drop is legal only
    // with an enabling cast within the pairing window of this row's derived
    // cast time (mirrors planner._requires_ok).
    if (action.requiresActionId != null
        && !enablerCasts.some((u) =>
          t - u >= -REQUIRES_TOL_SEC && t - u <= action.requiresWithinSec)) {
      blocked.add(r);
    }
  });
  return blocked;
}
