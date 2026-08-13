"""Pure planner: DamageModel + healer duo + comp -> a deterministic, cooldown-
feasible mitigation & recovery plan.

Passes (constants in the tunables block):
  1. Severity order — the scariest mechanics (p90 damage / role HP) pick
     cooldowns first; that IS the long-CD reservation mechanism.
  2. Tiered greedy assignment per mechanic until the survival goal is met:
     other-jobs' party mitigation first (the "actively include other classes"
     requirement), then healer oGCDs, then healer GCD shields — each GCD
     priced in lost damage potency. Cooldown/charge feasibility on per-(slot,
     action) timelines; Addersgall/Aetherflow/Lily as token buckets.
  3. Chronological HP sweep: recovery between hits from the duo's oGCD HPS
     budget; where oGCDs can't repay the debt in time, AoE GCD heals are
     inserted (counted + priced).
  4. Statuses (covered / tight / uncovered), lanes, summary, validation.

Everything stable-sorted; two runs over the same model are byte-identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .classify import DamageModel, Mechanic
from .library import (
    ACTIONS, FILLER_GCD_POTENCY, RESOURCE_POOLS, ROLE_JOBS, MitAction, Target,
    Tier, actions_for_job, role_for_job,
)

# --- tunables ---------------------------------------------------------------
SAFE_HIT_FRACTION = 0.65        # post-plan damage goal, fraction of role HP
BLEED_HIT_FRACTION = 0.90       # bleeds are healed through while ticking
LETHAL_FRACTION = 0.95          # buster still above this after suggestions -> invuln
LEAD_MIT_S = 2.0                # cast leads before the first covered hit
LEAD_SHIELD_S = 4.0
LEAD_REGEN_S = 8.0
SHIELD_PREPULL_MIN_S = -10.0    # shields may go out before the pull
MAX_ASSIGN_PER_MECHANIC = 6     # non-suggestion actions (followability)
MAX_SUGGEST_PER_BUSTER = 2
RECOVERY_EFFICIENCY = 0.7       # fraction of the oGCD HPS budget assumed used
POST_HIT_BUFFER_FRAC = 0.05     # HP margin required beyond predicted damage
MAX_GCD_HEALS_PER_GAP = 4
MIN_MARGINAL_PREVENTED = 500.0  # skip assignments preventing less than this
TOPUP_MAX_ROUNDS = 4            # pass-2.5 oGCD top-up hill-climb rounds
# Invuln-first policy (round 2 — research: parties at every rank depth spend
# their invulns roughly on cooldown, ~2 per tank per fight; severity placement
# puts them on the biggest busters):
POST_DEBT_WINDOW_S = 12.0       # post_hp1 invulns need this much quiet after
POST_DEBT_TANK_FRAC = 0.3       # ...quiet = no tank hit above this HP fraction
# A mechanic this close before an HP-set only needs to be SURVIVED — the party
# is reset to 1 HP regardless, so comfort there is wasted mitigation.
HPSET_RELAX_WINDOW_S = 30.0
SURVIVAL_ONLY_FRACTION = 0.97

_ROLE_COUNTS = {"tank": 2, "healer": 2, "dps": 4}
ROLE_KEYS = ("tank", "healer", "dps")

_ACTION_BY_JOB_ID: dict[tuple[str, int], MitAction] = {
    (a.job, a.action_id): a for a in ACTIONS
}
_ACTION_BY_JOB_NAME: dict[tuple[str, str], MitAction] = {
    (a.job, a.name): a for a in ACTIONS
}

# Prerequisite chains (`MitAction.requires`): the dependent may be cast this
# many seconds BEFORE its enabler and still count as the same weave window —
# the derived per-class cast leads (shield 4s vs mit 2s) would otherwise
# order a same-mechanic Temperance -> Divine Caress pair "backwards".
REQUIRES_CO_WEAVE_TOL_S = 6.0


# --- feasibility structures ---------------------------------------------------

class ActionTimeline:
    """Cast times for one (slot, action); charge-aware feasibility."""

    def __init__(self, action: MitAction) -> None:
        self.action = action
        self.casts: list[float] = []

    def _ok(self, casts: list[float]) -> bool:
        a = self.action
        if a.cooldown_s <= 0:
            return True
        window = a.cooldown_s * a.charges
        for i, t in enumerate(casts):
            n = sum(1 for u in casts if t - window < u <= t)
            if n > a.charges:
                return False
        return True

    def can_add(self, t: float) -> bool:
        return self._ok(sorted(self.casts + [t]))

    def add(self, t: float) -> None:
        self.casts.append(t)
        self.casts.sort()


class ResourcePool:
    """Token bucket (Addersgall/Aetherflow/Lily): continuous regen, one token
    per cast, shared across a healer's tagged actions. `start` is the stock at
    pull start — Addersgall/Aetherflow enter full, LILIES enter at zero (they
    accrue in combat only, so no lily heal can exist before ~20s)."""

    def __init__(self, capacity: int, regen_s: float,
                 start: int | None = None) -> None:
        self.capacity = capacity
        self.regen_s = regen_s
        self.start = capacity if start is None else start
        self.casts: list[float] = []

    def _ok(self, casts: list[float]) -> bool:
        tokens = float(self.start)
        prev = 0.0
        for t in sorted(casts):
            tokens = min(self.capacity, tokens + max(0.0, t - prev) / self.regen_s)
            if tokens < 1.0 - 1e-9:
                return False
            tokens -= 1.0
            prev = max(prev, t)
        return True

    def can_add(self, t: float) -> bool:
        return self._ok(self.casts + [max(0.0, t)])

    def add(self, t: float) -> None:
        self.casts.append(max(0.0, t))
        self.casts.sort()


@dataclass
class Assignment:
    slot: str
    job: str
    action_id: int
    name: str
    cast_at_s: float
    duration_s: float
    target: str                 # party | self | tank | enemy
    mit_pct: float
    shield_amount: float
    heal_amount: float
    hot_hps: float
    is_gcd: bool
    cast_time_s: float
    is_suggestion: bool
    covers: list[float] = field(default_factory=list)  # covered hit times
    # A cast made for an earlier mechanic whose duration also blankets this
    # one. Carryovers credit their mit% here but never their shield (assumed
    # consumed by the mechanic they were cast for), and render dimmed.
    is_carryover: bool = False
    # The HoT's real span when shorter than duration_s (Shake It Off: 15s of
    # ticks under a 30s barrier). 0 -> duration_s; only the sweep reads it.
    hot_window_s: float = 0.0


@dataclass
class GcdHeal:
    slot: str
    job: str
    action_id: int
    name: str
    cast_at_s: float
    count: int
    cast_time_s: float
    heal_amount: float


@dataclass
class PlanMechanic:
    mech: Mechanic
    assignments: list[Assignment] = field(default_factory=list)
    gcd_heals: list[GcdHeal] = field(default_factory=list)
    predicted: dict[str, float] = field(default_factory=dict)
    hp_after: dict[str, float] = field(default_factory=dict)
    status: str = "covered"
    notes: list[str] = field(default_factory=list)
    stop_reason: str = ""
    invulned: bool = False          # buster covered by a scheduled invuln
    invuln_post_hp1: bool = False   # ...that leaves the tank at ~1 HP


@dataclass
class Plan:
    slots: list[tuple[str, str]]            # (slot, job) in T1..D4 order
    mechanics: list[PlanMechanic]
    summary: dict
    warnings: list[str]


# --- effect math --------------------------------------------------------------

def eff_mit(a: MitAction, school: str) -> float:
    if school == "physical":
        return a.mit_all + a.mit_phys
    if school == "magical":
        return a.mit_all + a.mit_magic
    # special / mixed / unknown: only the generic part is certain; Feint/Addle
    # style split tools contribute their smaller side (darkness convention).
    return a.mit_all + min(a.mit_phys, a.mit_magic)


class _Ctx:
    """Resolved comp + magnitudes for one plan run."""

    def __init__(self, model: DamageModel, slots: list[tuple[str, str]]) -> None:
        self.model = model
        self.slots = slots
        self.role_hp = model.role_hp
        self.timelines: dict[tuple[str, int], ActionTimeline] = {}
        self.pools: dict[tuple[str, str], ResourcePool] = {}

    def hp_per_potency(self, job: str) -> float:
        return self.model.hp_per_potency.get(
            job, self.model.hp_per_potency.get("_default", 55.0))

    def timeline(self, slot: str, a: MitAction) -> ActionTimeline:
        key = (slot, a.action_id)
        if key not in self.timelines:
            self.timelines[key] = ActionTimeline(a)
        return self.timelines[key]

    def pool(self, slot: str, a: MitAction) -> ResourcePool | None:
        if not a.resource:
            return None
        key = (slot, a.resource)
        if key not in self.pools:
            cap, regen, start = RESOURCE_POOLS[a.resource]
            self.pools[key] = ResourcePool(cap, regen, start)
        return self.pools[key]

    def shield_hp(self, a: MitAction, target_role: str) -> float:
        if a.shield_pct_maxhp > 0:
            # Divine Veil is sized off the caster (a tank); the rest scale off
            # each target's own max HP.
            role = "tank" if a.name == "Divine Veil" else target_role
            return a.shield_pct_maxhp * self.role_hp.get(role, 0.0)
        observed = self.model.magnitudes.get("shield_hp_by_status", {})
        for status in a.status_names:
            if status in observed:
                return observed[status]
        return a.shield_potency * self.hp_per_potency(a.job)

    def heal_hp(self, a: MitAction, target_role: str) -> float:
        hp = a.heal_potency * self.hp_per_potency(a.job)
        if a.heal_pct_maxhp > 0:
            hp += a.heal_pct_maxhp * self.role_hp.get(target_role, 0.0)
        return hp

    def regen_total_hp(self, a: MitAction) -> float:
        return (a.regen_potency_per_tick * a.regen_ticks
                * self.hp_per_potency(a.job))


# --- pass 2 helpers -----------------------------------------------------------

def _lead_for(a: MitAction) -> float:
    if a.is_shield and not a.is_mit:
        return LEAD_SHIELD_S
    if a.regen_potency_per_tick > 0 and not a.is_mit and not a.is_shield:
        return LEAD_REGEN_S
    return LEAD_MIT_S


def cast_lead_for(a: MitAction) -> float:
    """Public alias for the derived-cast lead: how many seconds before a
    mechanic's first hit the planner schedules this action. The sidecar ships it
    per palette action so the UI's availability preview shares one timing
    policy with the planner."""
    return _lead_for(a)


def _covered_hits(a: MitAction, cast_at: float, hit_times: list[float]) -> list[float]:
    if a.duration_s <= 0:
        return hit_times[:1] if hit_times else []
    return [t for t in hit_times
            if cast_at <= t <= cast_at + a.duration_s + 1e-9]


def _cast_at_for(a: MitAction, first_hit: float) -> float:
    """Derived cast time: a fixed per-action-class lead before the first hit
    (shields may go pre-pull; everything else clamps to >= 0)."""
    cast_at = first_hit - _lead_for(a)
    if a.is_shield and not a.is_mit:
        return max(SHIELD_PREPULL_MIN_S, cast_at)
    return max(0.0, cast_at)


def _requires_ok(ctx: "_Ctx", slot: str, a: MitAction, cast_at: float) -> bool:
    """Prerequisite chain (Divine Caress needs Temperance): a cast is legal
    only within [-co-weave tol, requires_within_s] of a same-slot cast of the
    enabling action. Reads the timelines without creating them."""
    if not a.requires:
        return True
    partner = _ACTION_BY_JOB_NAME.get((a.job, a.requires))
    if partner is None:
        return False
    tl = ctx.timelines.get((slot, partner.action_id))
    if tl is None or not tl.casts:
        return False
    return any(-REQUIRES_CO_WEAVE_TOL_S <= cast_at - u <= a.requires_within_s
               for u in tl.casts)


def _build_assignment(ctx: _Ctx, slot: str, job: str, a: MitAction,
                      mech: Mechanic, cast_at: float,
                      covers: list[float]) -> Assignment:
    """One non-suggestion cast, effects resolved against this mechanic's school
    + the target role. Shared by the greedy tier loop and the pinned (PF-plan)
    pass so both produce byte-identical Assignments."""
    hot_window = a.regen_window_s if a.regen_window_s > 0 else a.duration_s
    return Assignment(
        slot=slot, job=job, action_id=a.action_id, name=a.name,
        cast_at_s=cast_at, duration_s=a.duration_s,
        target=("tank" if a.target == Target.SINGLE else a.target.value),
        mit_pct=eff_mit(a, mech.school),
        shield_amount=ctx.shield_hp(
            a, "tank" if a.target in (Target.SINGLE, Target.SELF) else "dps"),
        heal_amount=ctx.heal_hp(
            a, "tank" if a.target in (Target.SINGLE, Target.SELF) else "dps"),
        hot_hps=(ctx.regen_total_hp(a) / hot_window
                 if hot_window > 0 else 0.0),
        hot_window_s=hot_window,
        is_gcd=a.is_gcd, cast_time_s=a.cast_time_s,
        is_suggestion=False, covers=covers,
    )


def _predicted_for(mech: Mechanic, assigns: list[Assignment],
                   ctx: _Ctx) -> dict[str, float]:
    """Post-plan damage per role per target, walking the mechanic's hits with
    multiplicative mitigation (stack groups deduped) and depleting shields."""
    by_action = {(a.slot, a.action_id): a for a in assigns}
    shields: dict[str, float] = {r: 0.0 for r in ROLE_KEYS}
    for a in assigns:
        if a.shield_amount <= 0:
            continue    # carryovers/suggestion markers carry no barrier
        act = _ACTION_BY_JOB_ID.get((a.job, a.action_id))
        role_scope = ROLE_KEYS if a.target in ("party", "enemy") else ("tank",)
        for r in role_scope:
            amt = a.shield_amount
            # Party %-max-HP barriers (Shake It Off, Tempera Grassa) scale off
            # each TARGET's own max HP — a tank's barrier is tank-sized, not
            # the dps-sized number the display carries. Divine Veil alone is
            # caster-sized (shield_hp already handled it). Safe to resize
            # directly: no shield rider amps a %-max-HP barrier.
            if (act is not None and act.shield_pct_maxhp > 0
                    and a.target in ("party", "enemy")
                    and act.name != "Divine Veil"):
                amt = act.shield_pct_maxhp * ctx.role_hp.get(r, 0.0)
            shields[r] += amt
    out = {r: 0.0 for r in ROLE_KEYS}
    remaining = dict(shields)
    for hit in mech.hits:
        t = hit["time_s"]
        active: list[Assignment] = []
        seen_groups: set[str] = set()
        for a in sorted(by_action.values(),
                        key=lambda x: (-x.mit_pct, x.slot, x.action_id)):
            if a.mit_pct <= 0 or t not in a.covers:
                continue
            g = _ACTION_BY_JOB_ID[(a.job, a.action_id)].stack_group
            if g is not None:
                if g in seen_groups:
                    continue
                seen_groups.add(g)
            active.append(a)
        for r in ROLE_KEYS:
            unmit = float(hit["unmitigated"].get(r) or 0.0)
            if unmit <= 0:
                continue
            mult = 1.0
            for a in active:
                if a.target in ("party", "enemy") or r == "tank":
                    mult *= (1.0 - a.mit_pct)
            dmg = unmit * mult
            soak = min(remaining[r], dmg)
            remaining[r] -= soak
            out[r] += dmg - soak
    return out


def _mech_hit_times(mech: Mechanic) -> list[float]:
    return [h["time_s"] for h in mech.hits]


def _severity(mech: Mechanic, role_hp: dict[str, float]) -> float:
    sev = 0.0
    for r in ROLE_KEYS:
        hp = role_hp.get(r, 0.0)
        if hp > 0:
            sev = max(sev, float(mech.unmitigated_p90.get(r) or 0.0) / hp)
    return sev


def _role_headcount(mech: Mechanic, r: str) -> int:
    """People of role `r` actually in danger — `tank_targets` (1 or 2) on a
    buster, else the full role count. Weights prevented damage by who is really
    hit, so a party tool on a 1-target buster isn't valued as if it shielded two
    tanks."""
    if r == "tank" and mech.kind == "tankbuster":
        return mech.tank_targets
    return _ROLE_COUNTS[r]


def _goal_fraction(mech: Mechanic) -> float:
    return BLEED_HIT_FRACTION if mech.kind == "bleed" else SAFE_HIT_FRACTION


# --- premade ("PF") plan resolution -------------------------------------------

def _norm(name: str) -> str:
    return " ".join((name or "").split()).lower()


def _select_instances(cands: list[Mechanic], entry, warnings: list[str],
                      prefix: str = "PF plan") -> list[Mechanic]:
    """Which matched mechanic(s) a premade entry applies to. `cands` is sorted
    by time. `occurrence` (index) / `at_sec` (nearest time) disambiguate a
    recurring mechanic; with neither and several matches we broadcast + warn."""
    if entry.occurrence is not None:
        if 0 <= entry.occurrence < len(cands):
            return [cands[entry.occurrence]]
        warnings.append(f"{prefix}: {entry.label!r} occurrence "
                        f"#{entry.occurrence} not found ({len(cands)} matched).")
        return []
    if entry.at_sec is not None:
        return [min(cands, key=lambda m: abs(m.time_s - entry.at_sec))]
    if len(cands) > 1:
        warnings.append(f"{prefix}: {entry.label!r} matched {len(cands)} "
                        "mechanics — applied to all (add an occurrence/at_sec "
                        "to target one).")
    return list(cands)


def _resolve_pinned(pinned, plan_mechs: list[PlanMechanic],
                    slots: list[tuple[str, str]], warnings: list[str],
                    prefix: str = "PF plan", exclusive: bool = False
                    ) -> dict[str, list[tuple]]:
    """Map a premade plan's entries to placement specs per `Mechanic.id`. Duck-
    typed over `mitplan.premade.PremadePlan` (planner stays JSON-agnostic). Two
    spec kinds, resolved by the pre-pass:
      ("job", slot, MitAction)      — a specific job (healer mits).
      ("role", role, action_id)     — a shared-id party mit (Feint/Addle/
                                       Reprisal) resolved to an eligible comp job.

    "Apply matches, warn on rest": a mit whose job/role isn't in this comp is
    skipped SILENTLY (a sheet lists every healer + a party mit per role; only the
    pull's jobs apply). We warn ONCE per analyzed-healer slot (H1/H2) the plan
    never covers — that healer's mechanics then fall back to the auto plan.

    `exclusive` (user-authored plans): the plan is the user's own draft, so an
    off-comp mit going missing must be LOUD (per-mit warning instead of the
    silent sheet-convention skip), and the H1/H2 "uses the auto plan" warning is
    skipped — there is no auto fallback in exclusive mode.

    Returns (mits_by_mech_id, heals_by_mech_id): the second maps mechanic ids
    to the entry's authored GCD top-up heals as (slot, job, MitAction, count) —
    only meaningful for user plans; PF files never author heals today."""
    out: dict[str, list[tuple]] = {}
    heals_out: dict[str, list[tuple]] = {}
    if pinned is None:
        return out, heals_out
    job_to_slot = {job: slot for slot, job in slots}
    comp_jobs = {job for _, job in slots}
    by_boss: dict[int, list[Mechanic]] = {}
    by_name: dict[str, list[Mechanic]] = {}
    for pm in plan_mechs:
        for bid in pm.mech.boss_ability_ids:
            by_boss.setdefault(int(bid), []).append(pm.mech)
        by_name.setdefault(_norm(pm.mech.name), []).append(pm.mech)
    for lst in by_boss.values():
        lst.sort(key=lambda m: (m.time_s, m.id))
    for lst in by_name.values():
        lst.sort(key=lambda m: (m.time_s, m.id))

    entries = tuple(getattr(pinned, "entries", ()))
    covered_slots: set[str] = set()
    for entry in entries:
        if entry.boss_ability_id is not None:
            cands = by_boss.get(int(entry.boss_ability_id), [])
        elif entry.name:
            cands = by_name.get(_norm(entry.name), [])
        else:
            cands = []
        if not cands:
            warnings.append(f"{prefix}: no mechanic matched {entry.label!r}.")
            continue
        targets = _select_instances(cands, entry, warnings, prefix)
        for sel, action_id in entry.mits:
            if sel.startswith("@"):                     # role-generic party mit
                role = sel[1:]
                if not any(j in comp_jobs and (j, action_id) in _ACTION_BY_JOB_ID
                           for j in ROLE_JOBS.get(role, ())):
                    if exclusive:
                        warnings.append(f"{prefix}: no {role} job in this comp "
                                        f"brings #{action_id} ({entry.label!r}).")
                    continue   # no comp job of this role brings it — skip
                for m in targets:
                    out.setdefault(m.id, []).append(("role", role, action_id))
            else:                                        # specific job (healers)
                slot = job_to_slot.get(sel)
                if slot is None:
                    if exclusive:
                        warnings.append(f"{prefix}: {sel} is not in this comp "
                                        f"({entry.label!r}).")
                    continue   # job not in this comp — silently skipped
                a = _ACTION_BY_JOB_ID.get((sel, action_id))
                if a is None:
                    continue
                covered_slots.add(slot)
                for m in targets:
                    out.setdefault(m.id, []).append(("job", slot, a))
        for job, action_id, count in getattr(entry, "heals", ()):
            slot = job_to_slot.get(job)
            if slot not in ("H1", "H2"):
                if exclusive:
                    warnings.append(f"{prefix}: {job} is not a healer in this "
                                    f"comp — heal dropped ({entry.label!r}).")
                continue
            a = _ACTION_BY_JOB_ID.get((job, action_id))
            if (a is None or not a.is_gcd or a.heal_potency <= 0
                    or a.is_mit or a.is_shield):
                # Shields (EDiag/Adloquium) are mits — they ride the mits
                # path, not the heal incrementer.
                if exclusive:
                    warnings.append(f"{prefix}: #{action_id} is not a GCD heal "
                                    f"— dropped ({entry.label!r}).")
                continue
            for m in targets:
                heals_out.setdefault(m.id, []).append((slot, job, a, count))

    if entries and not exclusive:
        for slot, job in slots:
            if slot in ("H1", "H2") and slot not in covered_slots:
                warnings.append(f"{prefix}: no {job} ({slot}) assignments — that "
                                "healer's mitigation uses the auto plan.")
    return out, heals_out


# --- the planner ---------------------------------------------------------------

def _run_sweep(plan_mechs: list["PlanMechanic"], chrono: list[int], ctx: "_Ctx",
               model: DamageModel, party_hps: float, tank_extra_hps: float,
               aoe_rows: list[MitAction], regen_healer: str,
               amp_windows: tuple = (), flat_windows: tuple = ()
               ) -> tuple[dict[int, dict[str, float]], dict[int, list["GcdHeal"]]]:
    """Chronological HP sweep + AoE GCD-heal budgeting, as a pure evaluator. It
    snapshots and restores the H2 pool it consumes and RETURNS results instead of
    writing them onto the PlanMechanics, so the top-up search (pass 2.5) can score
    a trial mit configuration by its costed GCD-heal bill without side effects. The
    two internal walks mirror the historic behaviour — walk 1 decides the inserted
    heals from the running HP, walk 2 re-walks with them fixed.

    `amp_windows` are (slot, scope, mult, start, end, role_scope) healing-buff
    windows (WHM Temperance +heal%, SGE Physis/Krasis heal-received, …) applied as
    multipliers on the healing the sweep does: caster windows scale heals OWNED by
    their slot, receiver windows scale all incoming heals to the buffed roles.

    `flat_windows` are (slot, bonus_hp, host_names, start, end) flat heal RIDERS
    (WHM Plenary's Confession): each qualifying host GCD heal credited inside the
    window heals bonus_hp more. Pair-gated by construction — no host cast in the
    window, no credit. GcdHeal.heal_amount stays the BASE amount; both the flat
    bonus and the % windows are applied at credit time, symmetrically for the
    sweep's own inserted heals and a user plan's authored ones."""
    pool_snap = {k: list(p.casts) for k, p in ctx.pools.items()}
    gcd_by_idx: dict[int, list[GcdHeal]] = {
        i: list(plan_mechs[i].gcd_heals) for i in range(len(plan_mechs))}
    hp_by_idx: dict[int, dict[str, float]] = {}

    def heal_scale(t: float, role: str, owner_slot: str | None) -> float:
        m = 1.0
        for (slot, scope, mult, start, end, role_scope) in amp_windows:
            if not (start <= t <= end) or role not in role_scope:
                continue
            if scope == "receiver" or (scope == "caster" and owner_slot == slot):
                m *= (1.0 + mult)
        return m

    def flat_bonus(t: float, heal_name: str) -> float:
        b = 0.0
        for (_slot, bonus_hp, hosts, start, end) in flat_windows:
            if heal_name in hosts and start <= t <= end:
                b += bonus_hp
        return b

    def _walk(insert: bool) -> None:
        hp = {r: ctx.role_hp[r] for r in ROLE_KEYS}
        prev_t = 0.0
        for ci, i in enumerate(chrono):
            pm = plan_mechs[i]
            t = pm.mech.time_s
            next_gap = (plan_mechs[chrono[ci + 1]].mech.time_s - t
                        if ci + 1 < len(chrono) else 60.0)
            gap = max(0.0, t - prev_t)
            for r in ROLE_KEYS:
                heal_in = party_hps * gap
                if r == "tank":
                    heal_in += tank_extra_hps * gap
                # receiver heal-buffs enlarge incoming recovery for the role
                heal_in *= heal_scale(t, r, None)
                regen = heal_in - (model.tank_drain_hps * gap
                                   if r == "tank" else 0.0)
                hp[r] = min(ctx.role_hp[r], hp[r] + max(-hp[r], regen))
            if insert and aoe_rows:
                worst_shortfall = 0.0
                for r in ROLE_KEYS:
                    need = (pm.predicted.get(r, 0.0)
                            + POST_HIT_BUFFER_FRAC * ctx.role_hp[r])
                    if float(pm.mech.unmitigated.get(r) or 0.0) <= 0:
                        continue
                    worst_shortfall = max(worst_shortfall, need - hp[r])
                heals: list[GcdHeal] = []
                if worst_shortfall > 0:
                    remaining = worst_shortfall
                    casts_left = MAX_GCD_HEALS_PER_GAP
                    cast_no = 0
                    while remaining > 0 and casts_left > 0:
                        row = None
                        cast_at = max(0.0, t - LEAD_SHIELD_S
                                      - (cast_no + 1) * 2.5)
                        for cand in aoe_rows:
                            pool = ctx.pool("H2", cand)
                            if pool is None or pool.can_add(cast_at):
                                row = cand
                                if pool is not None:
                                    pool.add(cast_at)
                                break
                        if row is None:
                            break
                        # inserts are the regen-healer's (H2) party GCD heals: a
                        # caster window on H2, a party receiver window and a flat
                        # rider (Plenary) enlarge each cast, so fewer are needed.
                        # Scale at the MECHANIC time (the heal is FOR this
                        # mechanic) — the synthetic pre-cast time can fall just
                        # outside the buff window. heal_amount stores the BASE;
                        # the credit loop below re-applies the same scaling.
                        base_cast = (ctx.heal_hp(row, "dps")
                                     + ctx.regen_total_hp(row))
                        per_cast = ((base_cast + flat_bonus(t, row.name))
                                    * heal_scale(t, "dps", "H2"))
                        if heals and heals[-1].action_id == row.action_id:
                            heals[-1].count += 1
                        else:
                            heals.append(GcdHeal(
                                slot="H2", job=regen_healer,
                                action_id=row.action_id, name=row.name,
                                cast_at_s=cast_at, count=1,
                                cast_time_s=max(row.cast_time_s, 2.5),
                                heal_amount=base_cast,
                            ))
                        remaining -= per_cast
                        casts_left -= 1
                        cast_no += 1
                gcd_by_idx[i] = heals
            for gh in gcd_by_idx[i]:
                # Single-target GCD heals (authored tank top-ups) heal the
                # tank alone; party heals credit everyone. Heal-buff windows
                # and flat riders (Plenary's Confession) apply at credit time,
                # identically for inserted and authored heals.
                row = _ACTION_BY_JOB_ID.get((gh.job, gh.action_id))
                gh_roles = (("tank",) if row is not None
                            and row.target is Target.SINGLE else ROLE_KEYS)
                per = gh.heal_amount + flat_bonus(t, gh.name)
                for r in gh_roles:
                    hp[r] = min(ctx.role_hp[r],
                                hp[r] + gh.count * per
                                * heal_scale(t, r, gh.slot))
            for r in ROLE_KEYS:
                hp[r] = max(0.0, hp[r] - pm.predicted.get(r, 0.0))
            if pm.mech.kind == "hpSet":
                for r in ROLE_KEYS:
                    hp[r] = min(hp[r], 1.0)
            elif pm.invulned and pm.invuln_post_hp1:
                hp["tank"] = min(hp["tank"], 1.0)
            for r in ROLE_KEYS:
                burst = 0.0
                for a in pm.assignments:
                    if a.is_suggestion or a.is_carryover:
                        continue
                    # Party-wide heals credit everyone; single-target and
                    # self heals (healer ST tools, tank self-sustain) credit
                    # the TANK — the buster top-up they exist for.
                    if a.target in ("party", "enemy"):
                        pass
                    elif a.target in ("self", "tank") and r == "tank":
                        pass
                    else:
                        continue
                    window = a.hot_window_s if a.hot_window_s > 0 else a.duration_s
                    base = (a.heal_amount
                            + a.hot_hps * min(window, max(0.0, next_gap)))
                    burst += base * heal_scale(t, r, a.slot)
                hp[r] = min(ctx.role_hp[r], hp[r] + burst)
            hp_by_idx[i] = dict(hp)
            prev_t = t

    _walk(insert=True)
    _walk(insert=False)
    # Restore every pool the walk consumed; pools first created during insertion
    # (a lazily-made H2 lily bucket) are cleared back to empty.
    for k, p in ctx.pools.items():
        p.casts = pool_snap.get(k, [])
    return hp_by_idx, gcd_by_idx


def _gcd_potency(gh: "GcdHeal") -> float:
    """The costed DPS potency of one inserted GCD heal (0 for free lily heals)."""
    row = _ACTION_BY_JOB_ID.get((gh.job, gh.action_id))
    return (row.gcd_cost_potency if row is not None
            else FILLER_GCD_POTENCY.get(gh.job, 300.0))


def _costed_potency(plan_mechs: list["PlanMechanic"],
                    gcd_by_idx: dict[int, list["GcdHeal"]]) -> float:
    """Total costed GCD potency of a plan (matches pass 4 / plan_gcd_cost): the
    DPS tax the objective minimises — inserted AoE GCD heals plus GCD-shield mit
    assignments, each priced by its `gcd_cost_potency` (0 for free lily heals)."""
    total = 0.0
    for i, pm in enumerate(plan_mechs):
        for gh in gcd_by_idx.get(i, ()):
            total += gh.count * _gcd_potency(gh)
        for a in pm.assignments:
            if a.is_gcd and not a.is_suggestion and not a.is_carryover:
                total += _ACTION_BY_JOB_ID[(a.job, a.action_id)].gcd_cost_potency
    return total


def _apply_add(plan_mechs: list["PlanMechanic"], owner_idx: int, slot: str,
               job: str, a: MitAction, cast_at: float, covers: list[float],
               ctx: "_Ctx", all_casts: list) -> "callable":
    """Tentatively add one cast of `a` to plan_mechs[owner_idx], crediting its
    carryover forward to later covered mechanics (mit only, shields spent), and
    recompute the touched mechanics' predicted damage. Returns an `undo()` that
    restores assignments, the timeline/pool, all_casts and predicted exactly —
    so the top-up search can score a trial and roll it back byte-for-byte."""
    pm_owner = plan_mechs[owner_idx]
    primary = _build_assignment(ctx, slot, job, a, pm_owner.mech, cast_at, covers)
    touched: list[tuple] = [(pm_owner, primary)]
    pm_owner.assignments.append(primary)
    if a.is_mit:
        for pm2 in plan_mechs:
            if pm2 is pm_owner:
                continue
            cov2 = _covered_hits(a, cast_at, _mech_hit_times(pm2.mech))
            if not cov2:
                continue
            if any(x.slot == slot and x.action_id == a.action_id
                   for x in pm2.assignments):
                continue
            carry = Assignment(
                slot=slot, job=job, action_id=a.action_id, name=a.name,
                cast_at_s=cast_at, duration_s=a.duration_s,
                target=("tank" if a.target in (Target.SINGLE, Target.SELF)
                        else a.target.value),
                mit_pct=eff_mit(a, pm2.mech.school), shield_amount=0.0,
                heal_amount=0.0, hot_hps=0.0, is_gcd=a.is_gcd,
                cast_time_s=a.cast_time_s, is_suggestion=False,
                covers=cov2, is_carryover=True)
            pm2.assignments.append(carry)
            touched.append((pm2, carry))
    tl = ctx.timeline(slot, a)
    tl_snap = list(tl.casts)
    tl.add(cast_at)
    pool = ctx.pool(slot, a)
    pool_snap = list(pool.casts) if pool is not None else None
    if pool is not None:
        pool.add(cast_at)
    all_casts.append((slot, job, a, cast_at, False))
    pred_snap = [(pm2, dict(pm2.predicted)) for pm2, _ in touched]
    for pm2, _ in touched:
        pm2.predicted = _predicted_for(pm2.mech, pm2.assignments, ctx)

    def undo() -> None:
        for pm2, asn in touched:
            pm2.assignments[:] = [x for x in pm2.assignments if x is not asn]
        tl.casts = tl_snap
        if pool is not None:
            pool.casts = pool_snap
        all_casts.pop()
        for pm2, snap in pred_snap:
            pm2.predicted = snap

    return undo


def _topup_ogcd(plan_mechs: list["PlanMechanic"],
                candidates: list[tuple[str, str, MitAction]],
                ctx: "_Ctx", all_casts: list, sweep_args: tuple,
                pinned_ids: frozenset[str]) -> None:
    """Pass 2.5 — maximal-oGCD top-up. The greedy stops at bare survival, leaving
    the residual to pass 3's COSTED GCD heals. Here we spend spare free oGCD
    mitigation — off cooldown and feasible — on the mechanics that would force GCD
    heals, keeping an add only while it strictly shrinks the costed GCD-heal bill.
    This realises 'exhaust oGCD before dipping into GCD resources': survival is
    untouched (added mit only raises HP), feasibility rides the same timeline/pool
    checks the greedy uses, and the cooldown timelines cap each tool's reuse.
    Deterministic first-improvement climb."""
    def cost() -> float:
        _, gcd_by_idx = _run_sweep(plan_mechs, *sweep_args)
        return _costed_potency(plan_mechs, gcd_by_idx)

    best = cost()
    if best <= 0:
        return
    free = [(s, j, a) for (s, j, a) in candidates
            if not a.is_gcd and a.gcd_cost_potency <= 0 and a.is_mit]
    order = sorted(range(len(plan_mechs)),
                   key=lambda i: (plan_mechs[i].mech.time_s, plan_mechs[i].mech.id))
    for _ in range(TOPUP_MAX_ROUNDS):
        improved = False
        for i in order:
            pm = plan_mechs[i]
            mech = pm.mech
            # Skip mechanics the PF plan owns (the healers are fixed there, so a
            # top-up would break PF authoritativeness and the locked ceiling),
            # hp-sets, and invulned busters (already 0 damage).
            if mech.kind == "hpSet" or pm.invulned or mech.id in pinned_ids:
                continue
            hit_times = _mech_hit_times(mech)
            if not hit_times:
                continue
            for slot, job, a in free:
                if mech.kind == "tankbuster":
                    if (a.target in (Target.PARTY, Target.ENEMY)
                            and not (a.target == Target.ENEMY
                                     and slot in ("T1", "T2"))):
                        continue
                elif a.target == Target.SINGLE:
                    continue
                if any(x.slot == slot and x.action_id == a.action_id
                       and not x.is_suggestion for x in pm.assignments):
                    continue
                cast_at = _cast_at_for(a, hit_times[0])
                covers = _covered_hits(a, cast_at, hit_times)
                if not covers:
                    continue
                if not ctx.timeline(slot, a).can_add(cast_at):
                    continue
                pool = ctx.pool(slot, a)
                if pool is not None and not pool.can_add(cast_at):
                    continue
                if not _requires_ok(ctx, slot, a, cast_at):
                    continue
                undo = _apply_add(plan_mechs, i, slot, job, a, cast_at,
                                  covers, ctx, all_casts)
                trial = cost()
                if trial < best - 1e-6:
                    best = trial
                    improved = True
                    if best <= 0:
                        return
                else:
                    undo()
        if not improved:
            break


def apply_shield_amp(ctx: "_Ctx", owner_slot: str, owner_job: str,
                     partner: "Assignment", cast_at: float) -> "MitAction | None":
    """After a partner GCD shield is committed, a same-healer `shield_mult` RIDER
    off cooldown (Zoe/Recitation/Seraphism) folds into it, multiplying the barrier
    — the amp rides the shield it powers, never a standalone value slot, so nothing
    double-counts. Returns the amp that fired, else None."""
    for amp in actions_for_job(owner_job):
        if amp.shield_mult <= 0 or partner.name not in amp.amp_partner:
            continue
        if amp.shield_mult_windowed:
            continue    # a window amps EVERY host in its duration, not the
            # first one committed — placed globally by _apply_windowed_shield_amps.
        tl = ctx.timeline(owner_slot, amp)
        if not tl.can_add(cast_at):
            continue
        tl.add(cast_at)
        partner.shield_amount *= (1.0 + amp.shield_mult)
        return amp
    return None


def _amp_rider_marker(slot: str, job: str, amp: MitAction, cast_at: float,
                      covers: list[float]) -> Assignment:
    """The visible 'rider fired' suggestion chip next to an amped shield
    (Zoe/Recitation). Display-only: zero value slots — the amp already rode
    the partner's barrier. Shared by the greedy and the pinned pre-pass so
    riders render identically in auto, PF and user plans."""
    return Assignment(
        slot=slot, job=job, action_id=amp.action_id, name=amp.name,
        cast_at_s=cast_at, duration_s=amp.duration_s, target="party",
        mit_pct=0.0, shield_amount=0.0, heal_amount=0.0, hot_hps=0.0,
        is_gcd=False, cast_time_s=0.0, is_suggestion=True,
        covers=list(covers))


def _apply_windowed_shield_amps(plan_mechs: list["PlanMechanic"],
                                duo_slots: list[tuple[str, str]],
                                ctx: "_Ctx") -> None:
    """A WINDOW shield rider (SCH Seraphism) transforms EVERY host GCD shield
    cast inside its duration — Adloquium→Manifestation and Concitation→Accession
    both stay transformed for the full 20s — so it cannot ride just the first
    host the greedy happens to commit.

    Placement is a global pass over the finished shield schedule, which also
    makes it order-independent: the greedy commits by severity, so "first
    committed" is not "earliest in the fight". The optimal window start is
    always some host's cast time (sliding a window later can only drop hosts,
    never gain one), so scanning host cast times is exhaustive rather than a
    heuristic. Runs after the greedy places GCD shields and before the sweep
    prices the residual, so the amplified barriers are what the sweep sees."""
    for slot, job in duo_slots:
        for amp in actions_for_job(job):
            if amp.shield_mult <= 0 or not amp.shield_mult_windowed:
                continue
            hosts = [a for pm in plan_mechs for a in pm.assignments
                     if a.slot == slot and not a.is_carryover
                     and a.shield_amount > 0 and a.name in amp.amp_partner]
            if not hosts:
                continue
            tl = ctx.timeline(slot, amp)
            amped: set[int] = set()
            while True:
                best: tuple[float, float, list] | None = None
                for anchor in sorted({h.cast_at_s for h in hosts}):
                    if not tl.can_add(anchor):
                        continue
                    covered = [h for h in hosts if id(h) not in amped
                               and anchor <= h.cast_at_s <= anchor + amp.duration_s]
                    gain = sum(h.shield_amount for h in covered) * amp.shield_mult
                    # Strict >: ties keep the earliest anchor, so the pass is
                    # deterministic.
                    if gain > 1e-9 and (best is None or gain > best[0] + 1e-9):
                        best = (gain, anchor, covered)
                if best is None:
                    break
                _, anchor, covered = best
                tl.add(anchor)
                for h in covered:
                    h.shield_amount *= (1.0 + amp.shield_mult)
                    amped.add(id(h))


def _amp_window(slot: str, act: MitAction, cast_at: float) -> tuple:
    role_scope = (ROLE_KEYS if (act.heal_mult_scope == "caster"
                                or act.target == Target.PARTY) else ("tank",))
    span = (act.heal_mult_window_s if act.heal_mult_window_s > 0
            else act.duration_s)
    return (slot, act.heal_mult_scope, act.heal_mult, cast_at,
            cast_at + span, role_scope)


def _flat_window(slot: str, act: MitAction, cast_at: float,
                 ctx: "_Ctx") -> tuple:
    """A flat heal-rider window (Plenary's Confession): (slot, bonus HP per
    qualifying host cast, host names, start, end)."""
    return (slot, act.heal_flat_potency * ctx.hp_per_potency(act.job),
            frozenset(act.heal_flat_partners), cast_at,
            cast_at + act.duration_s)


def _build_amp_windows(plan_mechs: list["PlanMechanic"],
                       duo_slots: list[tuple[str, str]], ctx: "_Ctx",
                       need: dict[int, float] | None = None,
                       auto_place: bool = True,
                       base_gcd: dict[int, list["GcdHeal"]] | None = None
                       ) -> tuple[tuple, tuple]:
    """-> (heal% windows, flat-rider windows) the sweep scales healing inside
    (WHM Temperance +heal%, SGE Physis/Krasis heal-received, WHM Plenary's
    Confession rider…). Two sources: (a) amps already placed as assignments
    (Temperance's 10% mit carries the amp; a user plan's pinned Plenary carries
    the rider); (b) with `auto_place`, pure amps (Philosophia/Krasis/Plenary —
    no mit/shield, so the mit greedy never schedules them) placed on the
    mechanics with the greatest costed GCD-heal NEED (from a baseline amp-free
    sweep) — that is where a healing buff actually pays off — cooldown-gated by
    a LOCAL timeline seeded with any placed casts. Every auto placement also
    appends a dimmed SUGGESTED cast on its mechanic, so the button shows up in
    the lanes, the cards and the mit sheet — never an invisible buff. User
    plans pass auto_place=False: only source (a), nothing they didn't place.
    Falls back to severity when no `need` map is supplied.

    FLAT riders are strictly pair-gated, so their auto placement additionally
    requires a payoff: a mechanic qualifies only when the baseline sweep
    (`base_gcd`) actually inserts one of the rider's host heals there — a
    Plenary with no Medica III / Rapture in its window would be a button with
    no effect. `_prune_flat_rider_markers` re-checks against the FINAL sweep,
    so a host heal that later disappears (amp windows / pass-2.5 top-up
    shrinking the bill) takes its suggestion with it."""
    mult_windows: list[tuple] = []
    flat_windows: list[tuple] = []
    placed_casts: dict[tuple[str, int], list[float]] = {}
    for pm in plan_mechs:
        for a in pm.assignments:
            if a.is_carryover:
                continue
            placed_casts.setdefault((a.slot, a.action_id), []).append(a.cast_at_s)
            act = _ACTION_BY_JOB_ID.get((a.job, a.action_id))
            if act is None:
                continue
            if act.heal_mult > 0:
                mult_windows.append(_amp_window(a.slot, act, a.cast_at_s))
            if act.heal_flat_potency > 0:
                flat_windows.append(_flat_window(a.slot, act, a.cast_at_s, ctx))
    if not auto_place:
        return tuple(mult_windows), tuple(flat_windows)
    order = list(range(len(plan_mechs)))
    # No amp on hpSets (nothing to heal through), invulned mechanics (the
    # invuln IS the plan there) or zero-damage rows.
    order = [i for i in order
             if plan_mechs[i].mech.kind != "hpSet"
             and not plan_mechs[i].invulned
             and any(float(plan_mechs[i].mech.unmitigated.get(r) or 0.0) > 0
                     for r in ROLE_KEYS)]
    order.sort(key=lambda i: (
        -(need.get(i, 0.0) if need else 0.0),
        -_severity(plan_mechs[i].mech, ctx.role_hp),
        plan_mechs[i].mech.time_s, plan_mechs[i].mech.id))
    local: dict[tuple[str, int], list[float]] = {}
    for slot, job in duo_slots:
        for act in actions_for_job(job):
            if act.is_mit or act.is_shield:
                continue
            if act.heal_mult <= 0 and act.heal_flat_potency <= 0:
                continue
            flat_only = act.heal_flat_potency > 0 and act.heal_mult <= 0
            for i in order:
                pm = plan_mechs[i]
                hit_times = _mech_hit_times(pm.mech)
                if not hit_times:
                    continue
                if flat_only and not any(
                        g.count > 0 and g.name in act.heal_flat_partners
                        for g in (base_gcd or {}).get(i, ())):
                    continue    # pair-gated rider with nothing to ride here
                cast_at = max(0.0, hit_times[0] - LEAD_MIT_S)
                casts = local.setdefault(
                    (slot, act.action_id),
                    list(placed_casts.get((slot, act.action_id), [])))
                if act.cooldown_s <= 0 or all(
                        abs(cast_at - c) >= act.cooldown_s for c in casts):
                    casts.append(cast_at)
                    if act.heal_mult > 0:
                        mult_windows.append(_amp_window(slot, act, cast_at))
                    if act.heal_flat_potency > 0:
                        flat_windows.append(_flat_window(slot, act, cast_at, ctx))
                    # The visible half of the auto placement: a suggested cast
                    # on the mechanic the window serves.
                    pm.assignments.append(Assignment(
                        slot=slot, job=act.job, action_id=act.action_id,
                        name=act.name, cast_at_s=cast_at,
                        duration_s=act.duration_s,
                        target=("tank" if act.target in (Target.SINGLE,
                                                         Target.SELF)
                                else act.target.value),
                        mit_pct=0.0, shield_amount=0.0, heal_amount=0.0,
                        hot_hps=0.0, is_gcd=False, cast_time_s=0.0,
                        is_suggestion=True,
                        covers=_covered_hits(act, cast_at, hit_times)))
    return tuple(mult_windows), tuple(flat_windows)


def _flat_rider_payoff(a: Assignment, act: MitAction,
                       plan_mechs: list["PlanMechanic"]) -> bool:
    """True when at least one qualifying host heal is credited inside this
    flat-rider cast's window in the FINAL plan (mirrors the sweep's
    flat_bonus coverage test: heals are credited at their mechanic's time)."""
    start, end = a.cast_at_s, a.cast_at_s + act.duration_s
    return any(
        start <= pm.mech.time_s <= end
        and any(g.count > 0 and g.name in act.heal_flat_partners
                for g in pm.gcd_heals)
        for pm in plan_mechs)


def _prune_flat_rider_markers(plan_mechs: list["PlanMechanic"]) -> None:
    """Drop auto-suggested flat-rider casts (Plenary) whose window holds no
    qualifying host heal in the FINAL sweep — the pair never happened, so the
    suggestion would be a button with no payoff. Numerically free: a flat
    window with no host cast inside credited exactly nothing, so removing the
    chip changes no HP, status or price. User-pinned riders (is_suggestion
    False) are plan content and are never touched — exclusive mode warns on
    them instead."""
    for pm in plan_mechs:
        pm.assignments[:] = [
            a for a in pm.assignments
            if not (a.is_suggestion
                    and (act := _ACTION_BY_JOB_ID.get((a.job, a.action_id)))
                    is not None
                    and act.heal_flat_potency > 0 and act.heal_mult <= 0
                    and not _flat_rider_payoff(a, act, plan_mechs))]


def plan(model: DamageModel, shield_healer: str, regen_healer: str,
         tanks: list[str], dps: list[str], pinned=None,
         pinned_exclusive: bool = False) -> Plan:
    """Build the mitigation plan. `pinned` is a premade/user plan (duck-typed
    `PremadePlan`); with `pinned_exclusive=True` (user-authored plans) the
    generator passes are ALL disabled — the plan is exactly the pinned casts,
    with cooldowns/charges/pools as the only hard constraints — while the HP
    sweep, amps, statuses and summary still run. Every exclusive gate is a pure
    skip: `pinned_exclusive=False` is byte-identical to the historic path.

    Exclusive mode also treats GCD healing as plan content: the sweep never
    auto-inserts heals — the user's authored per-gap heals (PinnedEntry.heals)
    are priced instead, so statuses reflect exactly the mitigation AND healing
    the plan commits to (plus the healers' passive oGCD recovery)."""
    _pfx = "Your plan" if pinned_exclusive else "PF plan"
    slots: list[tuple[str, str]] = [
        ("T1", tanks[0]), ("T2", tanks[1]),
        ("H1", shield_healer), ("H2", regen_healer),
        ("D1", dps[0]), ("D2", dps[1]), ("D3", dps[2]), ("D4", dps[3]),
    ]
    ctx = _Ctx(model, slots)
    warnings: list[str] = list(model.warnings)

    # Candidate pool per slot (party-effective, in stable order).
    candidates: list[tuple[str, str, MitAction]] = []   # (slot, job, action)
    for slot, job in slots:
        for a in actions_for_job(job):
            if a.tier in (Tier.PARTY_OTHER, Tier.HEALER_OGCD, Tier.HEALER_GCD):
                if a.is_mit or a.is_shield:
                    candidates.append((slot, job, a))

    plan_mechs = [PlanMechanic(mech=m) for m in model.mechanics]
    order = sorted(range(len(plan_mechs)),
                   key=lambda i: (-_severity(plan_mechs[i].mech, ctx.role_hp),
                                  plan_mechs[i].mech.time_s,
                                  plan_mechs[i].mech.id))
    # Buster ordinal by TIME (tank alternation must follow the fight, not the
    # severity-processing order).
    buster_ordinal: dict[str, int] = {}
    for n, pm in enumerate(sorted(
            (p for p in plan_mechs if p.mech.kind == "tankbuster"),
            key=lambda p: (p.mech.time_s, p.mech.id))):
        buster_ordinal[pm.mech.id] = n

    # A mechanic shortly before an HP-set only needs survival — everyone is
    # reset to 1 HP regardless, so comfort there is wasted mitigation.
    hpset_times = [pm.mech.time_s for pm in plan_mechs
                   if pm.mech.kind == "hpSet"]

    # Premade ("PF") plan: mechanic id -> the (slot, job, action) mits it pins.
    # Empty (byte-identical to the auto-plan) unless a premade plan was passed.
    pinned_by_mech_id, pinned_heals_by_mech_id = _resolve_pinned(
        pinned, plan_mechs, slots, warnings,
        prefix=_pfx, exclusive=pinned_exclusive)

    def _goal_fraction_for(pm: PlanMechanic) -> float:
        base = _goal_fraction(pm.mech)
        if any(0 < t - pm.mech.time_s <= HPSET_RELAX_WINDOW_S
               for t in hpset_times):
            if not any("set to 1 HP right after" in n for n in pm.notes):
                pm.notes.append("The party is set to 1 HP right after — "
                                "plan to survive, not to be comfortable.")
            return max(base, SURVIVAL_ONLY_FRACTION)
        return base

    # Every cast placed anywhere below, for cross-mechanic carryover credit —
    # declared before the invuln pass so an invuln's duration can genuinely
    # blanket a neighboring mechanic's tank hits.
    all_casts: list[tuple[str, str, MitAction, float, bool]] = []

    # ---- invuln-first pass (severity order): a buster whose target tank has
    # an invuln available is planned as the invuln — healer tools and party
    # mitigation stay banked for raid damage. The 240–420s recasts self-limit
    # this to roughly two uses per tank per fight. In exclusive (user-plan)
    # mode the pass is skipped outright — invulns appear only where authored.
    # An invuln is priced as what it is: a 100% self-mit over its REAL
    # duration (covers clamped), so the per-hit scorer handles shared party
    # damage, a train's uncovered tail, and multi-mechanic spans honestly.
    for idx in (() if pinned_exclusive else order):
        pm = plan_mechs[idx]
        mech = pm.mech
        if mech.kind != "tankbuster":
            continue
        if mech.id in pinned_by_mech_id:
            continue   # PF plan owns this mechanic's mitigation
        tank_slot, tank_job = slots[buster_ordinal[mech.id] % 2]
        inv = next((a for a in actions_for_job(tank_job)
                    if a.tier is Tier.INVULN), None)
        if inv is None:
            continue
        hit_times = _mech_hit_times(mech)
        cast_at = max(0.0, hit_times[0] - 1.0)
        if inv.post_hp1:
            # The tank comes out at ~1 HP — demand a quiet window after,
            # unless what follows is an HP-set (everyone is at 1 anyway).
            collides = any(
                p2.mech.kind != "hpSet"
                and 0 < p2.mech.time_s - mech.time_s <= POST_DEBT_WINDOW_S
                and float(p2.mech.unmitigated.get("tank") or 0.0)
                >= POST_DEBT_TANK_FRAC * ctx.role_hp["tank"]
                for p2 in plan_mechs if p2 is not pm)
            if collides:
                continue
        tl = ctx.timeline(tank_slot, inv)
        if not tl.can_add(cast_at):
            continue
        tl.add(cast_at)
        pm.assignments.append(Assignment(
            slot=tank_slot, job=tank_job, action_id=inv.action_id,
            name=inv.name, cast_at_s=cast_at, duration_s=inv.duration_s,
            target="self", mit_pct=1.0, shield_amount=0.0, heal_amount=0.0,
            hot_hps=0.0, is_gcd=False, cast_time_s=0.0, is_suggestion=True,
            covers=_covered_hits(inv, cast_at, hit_times),
        ))
        all_casts.append((tank_slot, tank_job, inv, cast_at, True))
        pm.invulned = True
        pm.invuln_post_hp1 = inv.post_hp1
        pm.notes.append(f"{inv.name} — party tools saved for raid damage.")
        if inv.post_hp1:
            pm.notes.append("Heal the tank back up right after."
                            if inv.name != "Living Dead"
                            else "Heal the tank to FULL within 8s.")

    # Free oGCD mitigation — the party's raid mit AND the healers' raid oGCDs
    # (Neutral Sect, Sacred Soil, Kerachole…) — is ONE value-ranked pass: they
    # cost no GCD, so an optimized plan mixes them per raidwide instead of
    # draining every DPS/tank button before a healer ever presses one. Only the
    # DPS-costing healer GCD shields stay a separate, last-resort tier.
    tier_order = ((Tier.PARTY_OTHER, Tier.HEALER_OGCD), (Tier.HEALER_GCD,))

    # PF pre-pass (time order): reserve the plan's pinned mits BEFORE the
    # severity-order greedy below, so the auto plan for un-pinned mechanics never
    # steals a cooldown the PF plan needs. Reserved casts block their (slot,
    # action) timeline/pool and feed carryover credit; the main loop then only
    # suppresses the greedy HEALER tiers for these mechanics.
    for pm in sorted((p for p in plan_mechs if p.mech.id in pinned_by_mech_id
                      and p.mech.kind != "hpSet"),
                     key=lambda p: (p.mech.time_s, p.mech.id)):
        mech = pm.mech
        hit_times = _mech_hit_times(mech)
        if not hit_times:
            continue
        first_hit = hit_times[0]
        # Prerequisite providers first (Temperance before Divine Caress), so a
        # same-mechanic enabler+dependent pair works regardless of drop order.
        specs = sorted(pinned_by_mech_id[mech.id],
                       key=lambda s: 1 if (s[0] == "job" and s[2].requires)
                       else 0)
        for spec in specs:
            aid = spec[2].action_id if spec[0] == "job" else spec[2]
            if any(x.action_id == aid and not x.is_carryover
                   for x in pm.assignments):
                continue   # this tool is already on the mechanic (no double-cast)
            if spec[0] == "job":                 # a specific healer job's mit
                candidates_sa = [(spec[1], spec[2])]
            else:                                # ("role", role, action_id)
                _, role, action_id = spec
                # Eligible comp slots of the role in slot order — try each until
                # one is cooldown-feasible, so a shared 90s tool (Feint) spreads
                # across the role's jobs. The "swap" is a comp reorder.
                candidates_sa = [
                    (s, _ACTION_BY_JOB_ID[(job, action_id)])
                    for s, job in slots
                    if job in ROLE_JOBS.get(role, ())
                    and (job, action_id) in _ACTION_BY_JOB_ID]
            # A pinned invuln (Hallowed Ground, Holmgang…) gets the invuln
            # treatment on a tank buster — cast just before the hit, priced as
            # a 100% self-mit over its real duration. Anywhere else it has no
            # modeled effect, so it is dropped loudly instead of silently
            # doing nothing. PF files never pin invulns today; this branch
            # serves user plans.
            if candidates_sa and candidates_sa[0][1].tier is Tier.INVULN:
                if mech.kind != "tankbuster":
                    warnings.append(
                        f"{_pfx}: {candidates_sa[0][1].name} on {mech.name} "
                        "has no modeled effect outside a tank buster. Skipped.")
                    continue
                placed = False
                for slot, a in candidates_sa:
                    cast_at = max(0.0, first_hit - 1.0)
                    tl = ctx.timeline(slot, a)
                    if not tl.can_add(cast_at):
                        continue
                    tl.add(cast_at)
                    pm.assignments.append(Assignment(
                        slot=slot, job=a.job, action_id=a.action_id,
                        name=a.name, cast_at_s=cast_at,
                        duration_s=a.duration_s, target="self", mit_pct=1.0,
                        shield_amount=0.0, heal_amount=0.0, hot_hps=0.0,
                        is_gcd=False, cast_time_s=0.0, is_suggestion=False,
                        covers=_covered_hits(a, cast_at, hit_times)))
                    all_casts.append((slot, a.job, a, cast_at, False))
                    pm.invulned = True
                    pm.invuln_post_hp1 = a.post_hp1
                    if a.post_hp1:
                        pm.notes.append("Heal the tank back up right after."
                                        if a.name != "Living Dead"
                                        else "Heal the tank to FULL within 8s.")
                    placed = True
                    break
                if not placed:
                    warnings.append(f"{_pfx}: {candidates_sa[0][1].name} for "
                                    f"{mech.name} is unavailable (cooldown) — "
                                    "skipped.")
                continue
            placed = False
            requires_blocked = False
            for slot, a in candidates_sa:
                cast_at = _cast_at_for(a, first_hit)
                covers = _covered_hits(a, cast_at, hit_times)
                if not covers:
                    continue
                tl = ctx.timeline(slot, a)
                if not tl.can_add(cast_at):
                    continue
                pool = ctx.pool(slot, a)
                if pool is not None and not pool.can_add(cast_at):
                    continue
                if not _requires_ok(ctx, slot, a, cast_at):
                    requires_blocked = True
                    continue
                tl.add(cast_at)
                if pool is not None:
                    pool.add(cast_at)
                pinned_asn = _build_assignment(
                    ctx, slot, a.job, a, mech, cast_at, covers)
                pm.assignments.append(pinned_asn)
                # A pinned party shield (EP II / Concitation) still gets its
                # healer's Zoe/Recitation rider folded in — otherwise PF mode
                # silently drops the +50%/crit the auto plan credits. The
                # marker chip renders the fold, same as the auto plan.
                if a.is_gcd and pinned_asn.shield_amount > 0:
                    amp = apply_shield_amp(ctx, slot, a.job, pinned_asn, cast_at)
                    if amp is not None:
                        pm.assignments.append(_amp_rider_marker(
                            slot, a.job, amp, cast_at, pinned_asn.covers))
                all_casts.append((slot, a.job, a, cast_at, False))
                placed = True
                break
            if not placed and candidates_sa:
                a0 = candidates_sa[0][1]
                if requires_blocked and a0.requires:
                    warnings.append(
                        f"{_pfx}: {a0.name} for {mech.name} needs "
                        f"{a0.requires} within {int(a0.requires_within_s)}s "
                        "before it. Skipped.")
                else:
                    warnings.append(f"{_pfx}: {a0.name} for {mech.name} is "
                                    "unavailable (cooldown) — skipped.")

    # Exclusive (user-plan) mode: the authored per-gap GCD heals become the
    # plan's gcd_heals up front. The sweep initializes from them and — with an
    # empty insert list below — prices them as FIXED heals instead of deciding
    # its own, so statuses reflect exactly the healing the plan commits to.
    # Timing mirrors the sweep's own synthetic pre-casts (before the hit), so
    # the analysis heal-locks derive identical windows.
    if pinned_exclusive:
        # plan_mechs is time-ordered, so resource pools (the lily bucket —
        # which starts EMPTY) are consumed chronologically: authored heals get
        # the same feasibility the mits above got, instead of a free pass.
        for pm in plan_mechs:
            specs = pinned_heals_by_mech_id.get(pm.mech.id)
            if not specs:
                continue
            t = pm.mech.time_s
            ghs: list[GcdHeal] = []
            cast_no = 0
            for slot, job, a, count in specs:
                cast_no += count
                cast_at = max(0.0, t - LEAD_SHIELD_S - cast_no * 2.5)
                kept = count
                pool = ctx.pool(slot, a)
                if pool is not None:
                    kept = 0
                    for _ in range(count):
                        if not pool.can_add(cast_at):
                            break
                        pool.add(cast_at)
                        kept += 1
                    if kept < count:
                        warnings.append(
                            f"{_pfx}: only {kept} of {count} {a.name} fit the "
                            f"{a.resource} gauge before {pm.mech.name}. "
                            "Trimmed.")
                    if kept == 0:
                        continue
                ghs.append(GcdHeal(
                    slot=slot, job=job, action_id=a.action_id, name=a.name,
                    cast_at_s=cast_at,
                    count=kept, cast_time_s=max(a.cast_time_s, 2.5),
                    heal_amount=(ctx.heal_hp(
                        a, "tank" if a.target is Target.SINGLE else "dps")
                        + ctx.regen_total_hp(a)),
                ))
            pm.gcd_heals = ghs

    for idx in order:
        pm = plan_mechs[idx]
        mech = pm.mech
        if mech.kind == "hpSet":
            # Unmitigable by definition — nothing to assign; the HP sweep
            # models the reset itself.
            pm.predicted = {r: 0.0 for r in ROLE_KEYS}
            pm.stop_reason = "hp_set"
            continue
        hit_times = _mech_hit_times(mech)
        first_hit, last_hit = hit_times[0], hit_times[-1]
        goal = {r: _goal_fraction_for(pm) * ctx.role_hp.get(r, 0.0)
                for r in ROLE_KEYS}

        # Inherit earlier casts whose duration blankets this mechanic's hits —
        # their mitigation is real here; their shields count as spent. An
        # invuln carries as the tank-only 100% mit it is, so a 10s Hallowed
        # Ground genuinely covers a neighboring mechanic's tank hits.
        for slot, job, a, cast_at, suggested in all_casts:
            inv_cast = a.tier is Tier.INVULN
            if not a.is_mit and not inv_cast:
                continue
            covers = _covered_hits(a, cast_at, hit_times)
            if not covers:
                continue
            if any(x.slot == slot and x.action_id == a.action_id
                   for x in pm.assignments):
                continue
            pm.assignments.append(Assignment(
                slot=slot, job=job, action_id=a.action_id, name=a.name,
                cast_at_s=cast_at, duration_s=a.duration_s,
                target=("tank" if a.target in (Target.SINGLE, Target.SELF)
                        else a.target.value),
                mit_pct=(1.0 if inv_cast else eff_mit(a, mech.school)),
                shield_amount=0.0,
                heal_amount=0.0, hot_hps=0.0, is_gcd=a.is_gcd,
                cast_time_s=a.cast_time_s, is_suggestion=suggested,
                covers=covers, is_carryover=True,
            ))

        # PF premade plan: the pinned mits were already reserved by the pre-pass
        # (so they win cooldown contention against the auto plan). Here the main
        # loop just suppresses the greedy HEALER tiers for a pinned mechanic — the
        # plan owns the healers, keeping the locked ceiling honest — while party
        # (tank/DPS) mitigation still auto-fills and the HP sweep supplies any
        # residual as GCD heals.
        is_pinned = mech.id in pinned_by_mech_id

        def _met(pred: dict[str, float]) -> bool:
            return all(pred[r] <= goal[r] + 1e-6 for r in ROLE_KEYS
                       if float(mech.unmitigated.get(r) or 0.0) > 0)

        pred = _predicted_for(mech, pm.assignments, ctx)
        pm.stop_reason = "goal_met" if _met(pred) else ""

        # A pinned mechanic's healer mitigation is fixed by the plan — only party
        # mitigation may auto-fill; an un-pinned mechanic uses the full order.
        # Exclusive (user-plan) mode: NO greedy tier runs at all — the plan is
        # exactly what the user placed. An invulned mechanic likewise banks
        # every tool (the invuln-first policy); its residual party damage is
        # priced honestly and falls to the HP sweep.
        active_tiers = (() if pinned_exclusive or pm.invulned
                        else ((Tier.PARTY_OTHER,),) if is_pinned
                        else tier_order)
        for group in active_tiers:
            while (not _met(pred)
                   and len([a for a in pm.assignments if not a.is_suggestion])
                   < MAX_ASSIGN_PER_MECHANIC):
                # Pick the tool that prevents the most (people-weighted). Powerful
                # raid cooldowns are used maximally — not hoarded — with severity
                # order + the cooldown timelines steering the strongest tools onto
                # the biggest mechanics first, and pass 2.5 topping up spare oGCD.
                best_score: float | None = None
                best_pick: tuple | None = None
                for slot, job, a in candidates:
                    if a.tier not in group:
                        continue
                    # One cast of a given tool per mechanic — a second EProg or
                    # Kerachole refreshes the same status, it doesn't stack.
                    if any(x.slot == slot and x.action_id == a.action_id
                           and not x.is_suggestion for x in pm.assignments):
                        continue
                    # Busters are healer/personal/invuln territory — party-wide
                    # mitigation of EVERY tier (the DPS/tank party mit AND the
                    # healers' party oGCDs: Sun Sign, Sacred Soil, Kerachole,
                    # Temperance…) stays banked for raid damage, where it shields
                    # all eight. Only the tanks' own enemy debuff (Reprisal) is
                    # spent on a buster; single-target tank tools are handled
                    # below. Incidental overlap still credits via carryover.
                    if (mech.kind == "tankbuster"
                            and a.target in (Target.PARTY, Target.ENEMY)
                            and not (a.target == Target.ENEMY
                                     and slot in ("T1", "T2"))):
                        continue
                    if mech.kind == "tankbuster" and a.target == Target.SINGLE:
                        target_roles = ("tank",)
                    elif a.target == Target.SINGLE:
                        continue    # single-target tools only planned on busters
                    else:
                        target_roles = ROLE_KEYS
                    cast_at = _cast_at_for(a, first_hit)
                    covers = _covered_hits(a, cast_at, hit_times)
                    if not covers:
                        continue
                    tl = ctx.timeline(slot, a)
                    if not tl.can_add(cast_at):
                        continue
                    pool = ctx.pool(slot, a)
                    if pool is not None and not pool.can_add(cast_at):
                        continue
                    if not _requires_ok(ctx, slot, a, cast_at):
                        continue    # prerequisite (Divine Grace) not up here
                    trial = _build_assignment(ctx, slot, job, a, mech,
                                              cast_at, covers)
                    new_pred = _predicted_for(mech, pm.assignments + [trial], ctx)
                    prevented = sum(
                        (pred[r] - new_pred[r]) * _role_headcount(mech, r)
                        for r in target_roles)
                    if prevented < MIN_MARGINAL_PREVENTED:
                        continue
                    # Mild long-cooldown discount keeps sensible sequencing
                    # (prefer a replaceable 15s mit that also blankets the next
                    # mechanic over a one-shot 90s shield) WITHOUT hoarding — a
                    # genuinely strong raid CD like Neutral Sect still wins on its
                    # value. strict > : ties resolve to the earliest candidate in
                    # the stable slots x ACTIONS order — deterministic.
                    score = prevented / max(1.0, a.cooldown_s / 60.0)
                    if best_score is None or score > best_score:
                        best_score = score
                        best_pick = (slot, job, a, cast_at, trial, new_pred)
                if best_pick is None:
                    break
                slot, job, a, cast_at, best_trial, best_pred = best_pick
                ctx.timeline(slot, a).add(cast_at)
                pool = ctx.pool(slot, a)
                if pool is not None:
                    pool.add(cast_at)
                pm.assignments.append(best_trial)
                if a.is_gcd and best_trial.shield_amount > 0:
                    amp = apply_shield_amp(ctx, slot, job, best_trial, cast_at)
                    if amp is not None:
                        pm.assignments.append(_amp_rider_marker(
                            slot, job, amp, cast_at, best_trial.covers))
                        best_pred = _predicted_for(mech, pm.assignments, ctx)
                all_casts.append((slot, job, a, cast_at, False))
                pred = best_pred
            if _met(pred):
                pm.stop_reason = "goal_met"
                break
        if not pm.stop_reason:
            pm.stop_reason = (
                "user" if pinned_exclusive
                else "pinned" if is_pinned
                else "cap" if len(pm.assignments) >= MAX_ASSIGN_PER_MECHANIC
                else "exhausted")

        # Tank-buster suggestions: personals on the target tank's own timeline.
        # Skipped in exclusive (user-plan) mode — nothing is suggested there —
        # and under an invuln (personals on an invulnerable tank are noise).
        if (mech.kind == "tankbuster" and not pinned_exclusive
                and not pm.invulned):
            tank_slot, tank_job = slots[buster_ordinal[mech.id] % 2]
            added = 0
            for a in actions_for_job(tank_job):
                if a.tier is not Tier.TANK_SUGGESTION or added >= MAX_SUGGEST_PER_BUSTER:
                    continue
                cast_at = max(0.0, first_hit - LEAD_MIT_S)
                covers = _covered_hits(a, cast_at, hit_times)
                tl = ctx.timeline(tank_slot, a)
                if not covers or not tl.can_add(cast_at):
                    continue
                tl.add(cast_at)
                pm.assignments.append(Assignment(
                    slot=tank_slot, job=tank_job, action_id=a.action_id,
                    name=a.name, cast_at_s=cast_at, duration_s=a.duration_s,
                    target="self", mit_pct=eff_mit(a, mech.school),
                    shield_amount=ctx.shield_hp(a, "tank"),
                    heal_amount=ctx.heal_hp(a, "tank"),
                    hot_hps=0.0, is_gcd=False, cast_time_s=0.0,
                    is_suggestion=True, covers=covers,
                ))
                all_casts.append((tank_slot, tank_job, a, cast_at, True))
                added += 1
            pred = _predicted_for(mech, pm.assignments, ctx)
            if pred["tank"] >= LETHAL_FRACTION * ctx.role_hp["tank"]:
                inv = next((a for a in actions_for_job(tank_job)
                            if a.tier is Tier.INVULN), None)
                if inv is not None:
                    cast_at = max(0.0, first_hit - 1.0)
                    tl = ctx.timeline(tank_slot, inv)
                    if tl.can_add(cast_at):
                        tl.add(cast_at)
                        pm.assignments.append(Assignment(
                            slot=tank_slot, job=tank_job,
                            action_id=inv.action_id, name=inv.name,
                            cast_at_s=cast_at, duration_s=inv.duration_s,
                            target="self", mit_pct=0.0, shield_amount=0.0,
                            heal_amount=0.0, hot_hps=0.0, is_gcd=False,
                            cast_time_s=0.0, is_suggestion=True,
                            covers=list(hit_times),
                        ))
                        pm.notes.append(f"{inv.name} suggested — the hit stays "
                                        "lethal under available mitigation.")
                        pred["tank"] = 0.0
            if mech.tank_targets >= 2:
                pm.notes.append("Hits both tanks — mirror the personals on the "
                                "off-tank.")
            pm.notes.append("Swap the suggested tank to match your own "
                            "swap plan.")

        if pm.invulned:
            # The invuln itself is priced per hit by _predicted_for (100%
            # tank mit over its duration) — only the flow-control label here.
            pm.stop_reason = "invulned"
        pm.predicted = pred
        pm.assignments.sort(key=lambda a: (a.cast_at_s, a.slot, a.action_id))

    # ---- pass 3: chronological HP sweep + GCD-heal budgeting ----------------
    duo_slots = [("H1", shield_healer), ("H2", regen_healer)]
    party_hps = 0.0
    tank_extra_hps = 0.0
    for slot, job in duo_slots:
        for a in actions_for_job(job):
            if not a.recovery:
                continue
            # Schedulable mit/shield tools (Kerachole, Sacred Soil, Divine
            # Caress…) credit their healing at placement time in the sweep —
            # keeping them in the always-on budget too would double-count.
            # Only pure recovery tools the greedy never places smear here.
            if a.is_mit or a.is_shield:
                continue
            eff_cd = a.cooldown_s
            if a.resource and eff_cd <= 0:
                eff_cd = RESOURCE_POOLS[a.resource][1]
            if eff_cd <= 0:
                continue
            total = ctx.heal_hp(a, "dps") + ctx.regen_total_hp(a)
            if a.target == Target.PARTY:
                party_hps += total / eff_cd
            elif a.target == Target.SINGLE:
                tank_extra_hps += total / eff_cd
    party_hps *= RECOVERY_EFFICIENCY
    tank_extra_hps *= RECOVERY_EFFICIENCY

    # Insertable AoE GCD heals, cheapest DPS cost first (WHM lilies are free
    # via Misery but bounded by the lily bucket; Medica III is the overflow).
    # Exclusive (user-plan) mode inserts NOTHING: the empty list disables the
    # sweep's insertion walk, so the pre-seeded authored heals are priced as
    # fixed inputs instead.
    aoe_rows = [] if pinned_exclusive else sorted(
        (a for a in actions_for_job(regen_healer)
         if a.tier is Tier.HEALER_GCD and a.target == Target.PARTY
         and a.heal_potency > 0),
        key=lambda a: (a.gcd_cost_potency, a.action_id))

    chrono = sorted(range(len(plan_mechs)),
                    key=lambda i: (plan_mechs[i].mech.time_s,
                                   plan_mechs[i].mech.id))

    # Window shield riders (Seraphism) amp every host shield in their duration —
    # placed globally now that the greedy has committed all the GCD shields, so
    # the sweep below prices the amplified barriers.
    _apply_windowed_shield_amps(
        plan_mechs, [("H1", shield_healer), ("H2", regen_healer)], ctx)

    # Heal-buff windows (+heal% amps, flat riders) the sweep scales healing
    # inside. Place the pure-window amps where healing is actually needed: run
    # a baseline amp-free sweep, price each mechanic's costed GCD-heal bill,
    # and reserve the amps onto the neediest mechanics (each also rendered as
    # a suggested cast). Exclusive mode auto-places nothing — but amps the
    # USER pinned (Temperance's +20%, Plenary's Confession rider) still form
    # their windows, so an authored plan's healing is scaled by the buffs the
    # plan itself contains.
    if pinned_exclusive:
        amp_windows, flat_windows = _build_amp_windows(
            plan_mechs, [("H1", shield_healer), ("H2", regen_healer)], ctx,
            auto_place=False)
    else:
        _base_args = (chrono, ctx, model, party_hps, tank_extra_hps,
                      aoe_rows, regen_healer, (), ())
        _, _base_gcd = _run_sweep(plan_mechs, *_base_args)
        _need = {i: sum(g.count * _gcd_potency(g) for g in _base_gcd.get(i, ()))
                 for i in range(len(plan_mechs))}
        amp_windows, flat_windows = _build_amp_windows(
            plan_mechs, [("H1", shield_healer), ("H2", regen_healer)], ctx,
            _need, base_gcd=_base_gcd)
    sweep_args = (chrono, ctx, model, party_hps, tank_extra_hps,
                  aoe_rows, regen_healer, amp_windows, flat_windows)
    # pass 2.5: spend spare free oGCD mitigation to shrink the costed GCD-heal
    # bill before pass 3 commits it — "exhaust oGCD before dipping into GCD".
    # Exclusive (user-plan) mode adds nothing the user didn't place.
    if not pinned_exclusive:
        _topup_ogcd(plan_mechs, candidates, ctx, all_casts, sweep_args,
                    frozenset(pinned_by_mech_id))
    hp_by_idx, gcd_by_idx = _run_sweep(plan_mechs, *sweep_args)
    for i, pm in enumerate(plan_mechs):
        pm.gcd_heals = gcd_by_idx[i]
        pm.hp_after = hp_by_idx[i]
        # Re-sort: the pass-2.5 top-up appends after the greedy's per-mechanic
        # sort, so fold any added casts back into cast-time order for the lanes.
        pm.assignments.sort(key=lambda a: (a.cast_at_s, a.slot, a.action_id))
    # A pair-gated rider whose window ended up with no host heal in the FINAL
    # sweep is a button with no payoff: drop auto suggestions outright, and in
    # exclusive mode tell the user their pinned rider pairs with nothing.
    _prune_flat_rider_markers(plan_mechs)
    if pinned_exclusive:
        for pm in plan_mechs:
            for a in pm.assignments:
                act = _ACTION_BY_JOB_ID.get((a.job, a.action_id))
                if (act is None or a.is_suggestion or a.is_carryover
                        or act.heal_flat_potency <= 0 or act.heal_mult > 0):
                    continue
                if not _flat_rider_payoff(a, act, plan_mechs):
                    warnings.append(
                        f"{_pfx}: {act.name} on {pm.mech.name} pairs with no "
                        f"{' / '.join(act.heal_flat_partners)} in its window. "
                        "No bonus healing credited.")

    # ---- pass 4: statuses + summary -----------------------------------------
    covered = tight = uncovered = 0
    gcd_heal_count = 0
    gcd_heal_time = 0.0
    gcd_heal_potency = 0.0
    total_unmit = 0.0
    total_pred = 0.0
    for pm in plan_mechs:
        mech = pm.mech
        if mech.kind == "hpSet":
            # Nothing to cover — the reset is the mechanic. The recovery it
            # forces shows up in the NEXT mechanics' entry HP.
            pm.status = "covered"
            covered += 1
            continue
        worst = 1.0
        for r in ROLE_KEYS:
            if float(mech.unmitigated.get(r) or 0.0) <= 0:
                continue
            if r == "tank" and pm.invulned and pm.invuln_post_hp1:
                # Holmgang/Living Dead/Superbolide leave the tank at ~1 HP BY
                # DESIGN — that clamp is the plan, not a failure. (Hallowed
                # Ground keeps real HP, so its tank number stays counted.)
                continue
            hp_frac = pm.hp_after.get(r, 0.0) / max(1.0, ctx.role_hp[r])
            worst = min(worst, hp_frac)
        # STRICT invuln detection: only a suggested cast whose library row is
        # actually an invuln forces "covered". The zero-value pattern alone
        # also matches the Zoe/Recitation rider markers and regen-only
        # suggestions (Aurora), which must never mask a low-HP mechanic.
        has_invuln = any(
            a.is_suggestion
            and (act := _ACTION_BY_JOB_ID.get((a.job, a.action_id))) is not None
            and act.tier is Tier.INVULN
            for a in pm.assignments)
        if worst >= 0.25 or has_invuln:
            pm.status = "covered"
            covered += 1
        elif worst >= 0.05:
            pm.status = "tight"
            tight += 1
        else:
            pm.status = "uncovered"
            uncovered += 1
            reason = {"cap": "assignment cap reached",
                      "exhausted": "every applicable cooldown already spent",
                      "goal_met": "recovery debt, not mitigation",
                      "pinned": "PF plan mitigation set — heal the residual",
                      "user": "your plan leaves a gap here",
                      "invulned": "the invuln covers the tank; the party "
                                  "still takes this",
                      }.get(pm.stop_reason, "insufficient tools")
            pm.notes.append(f"Uncovered: {reason}.")
        for a in pm.assignments:
            if a.is_gcd and not a.is_suggestion and not a.is_carryover:
                gcd_heal_count += 1
                gcd_heal_time += max(a.cast_time_s, 2.5)
                gcd_heal_potency += _ACTION_BY_JOB_ID[
                    (a.job, a.action_id)].gcd_cost_potency
        for gh in pm.gcd_heals:
            gcd_heal_count += gh.count
            gcd_heal_time += gh.count * gh.cast_time_s
            row = _ACTION_BY_JOB_ID.get((gh.job, gh.action_id))
            gcd_heal_potency += gh.count * (
                row.gcd_cost_potency if row is not None
                else FILLER_GCD_POTENCY.get(gh.job, 300.0))
        for r in ROLE_KEYS:
            n = _ROLE_COUNTS[r]
            total_unmit += float(mech.unmitigated.get(r) or 0.0) * n
            total_pred += pm.predicted.get(r, 0.0) * n

    kinds = [pm.mech.kind for pm in plan_mechs]
    summary = {
        "mechanic_count": len(plan_mechs),
        "raidwide_count": kinds.count("raidwide"),
        "tankbuster_count": kinds.count("tankbuster"),
        "bleed_count": kinds.count("bleed"),
        "multi_hit_count": kinds.count("multiHit"),
        "covered_count": covered,
        "tight_count": tight,
        "uncovered_count": uncovered,
        "gcd_heal_count": gcd_heal_count,
        "gcd_heal_time_s": round(gcd_heal_time, 1),
        "gcd_heal_potency_lost": round(gcd_heal_potency, 0),
        "total_unmitigated": round(total_unmit, 0),
        "total_predicted": round(total_pred, 0),
    }

    _validate(ctx)
    return Plan(slots=slots, mechanics=plan_mechs, summary=summary,
                warnings=warnings)


def _validate(ctx: _Ctx) -> None:
    """Every timeline and pool must be self-consistent (test invariant)."""
    for (slot, aid), tl in ctx.timelines.items():
        if not tl._ok(tl.casts):
            raise AssertionError(f"infeasible timeline {slot}/{aid}: {tl.casts}")
    for (slot, res), pool in ctx.pools.items():
        if not pool._ok(pool.casts):
            raise AssertionError(f"infeasible pool {slot}/{res}: {pool.casts}")
