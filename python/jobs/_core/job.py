"""JobData + Job + registry.

`JobData` is the per-job data bundle (potencies, cooldowns, gauge models,
opener, etc.) that the shared aspects in `jobs._aspects/` consume. `Job`
binds a job name to its data, the ordered list of Aspects to run, and an
optional idealized simulator.

Per-job packages (e.g. `jobs.machinist`) build a Job on import and call
`register(job)`. The registry is the single source of truth — there's no
hard-coded `if job == "Machinist"` branch anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol

from .aspect import Aspect


# --- Role policy -----------------------------------------------------------

Role = Literal["physical_ranged", "melee_tank", "melee_dps", "caster_healer"]


@dataclass(frozen=True)
class RolePolicy:
    """Role-specific tuning for the downtime detection pipeline.

    Different roles have different baselines for "what counts as a
    suspicious gap":
      * Physical Ranged (MCH, BRD, DNC) — every weaponskill is instant
        and mobile-friendly; a top player's gap > ~1.5 GCDs is almost
        always forced. 60% ref agreement is plenty.
      * Melee / Tank — instant casts too, but positional drops and
        mitigation oGCD priorities create real solo gaps that aren't
        downtime. Slightly stricter agreement threshold.
      * Caster / Healer — hardcasts and intentional phase transitions
        (e.g. BLM Astral/Umbral swap) create legitimate idle. Demand
        much stronger consensus, a larger gap floor, a deeper ref pool
        before producing windows at all, and a more permissive Tier-C
        fallback threshold so a hardcast slidecast pause doesn't read
        as forced downtime when Tier-A targetability data is missing.
    """
    role: Role
    # Tier-B (consensus) tuning.
    idle_floor_mult: float          # gap > eff_gcd × this counts as "idle"
    consensus_pct: float            # fraction of refs idle to call Tier B
    consensus_tick_s: float = 0.5
    min_ref_count: int = 4          # ref pool must reach this for Tier B
    # High-confidence sub-tier. A Tier-B window at `consensus_pct` is only
    # "suspected" forced downtime (used for the lenient ceiling + the display
    # band). Where the per-tick agreement is at least this near-unanimous, the
    # stretch is treated as GENUINELY forced: the idealized rotation skips it and
    # it is never scored against the player (no "cast during downtime" nudge).
    # Set high (0.90) so a single ref who caught a lucky server tick at a window
    # edge can't spoil the core's confidence, while two-plus players still casting
    # keeps that slice castable — the effective downtime start/end flexes to where
    # the pool truly agrees. Sub-`consensus_pct`..`consensus_high_pct` stays
    # "ambiguous": shown, but the ideal casts there so the improvement stands.
    consensus_high_pct: float = 0.90
    # Tier-C (legacy heuristic) fallback when Tier A is unavailable.
    fallback_gap_threshold_s: float = 8.0
    # Engage delay: seconds before a melee's first in-fight GCD lands — they cross
    # distance to the boss (reducible with a dash) rather than acting at t=0. The
    # idealized sim starts the in-fight loop here so a t=0 melee ceiling isn't
    # treated as reachable. Sampled at the FAST end from real pulls so a quick
    # dasher rarely beats it (protecting the <=100% guard). 0 for ranged/casters,
    # who act at t=0 from range. See scripts/calibrate_engage_delay.py.
    engage_delay_s: float = 0.0


PHYSICAL_RANGED = RolePolicy(
    role="physical_ranged",
    idle_floor_mult=1.5,
    consensus_pct=0.60,
    min_ref_count=4,
    fallback_gap_threshold_s=8.0,
)
MELEE_TANK = RolePolicy(
    role="melee_tank",
    idle_floor_mult=1.5,
    consensus_pct=0.65,
    min_ref_count=4,
    fallback_gap_threshold_s=8.0,
    # A tank is already in melee on the boss at the pull (it holds aggro), so
    # there's no run-in — the idealized rotation starts at t=0 like real tanks do.
    # Calibrated against PLD top-10 pulls (scripts: their first GCD lands at ~0.0;
    # a 1.0s delay started the ceiling a GCD late and let top parses edge over
    # 100%). 0 here protects the <=100% guard by not under-running the ceiling.
    engage_delay_s=0.0,
)
# Melee DPS keep a tighter rotation than tanks (no mit-GCD / tank-swap gaps) but
# unlike physical ranged they pay a real, fight-specific uptime tax: short (~1
# GCD) forced repositioning around the boss for positionals and mechanics. The
# 1.5× idle floor the other roles use steps right over those ~2.5-3.75s gaps, so
# the consensus detector credits them nothing and a melee player gets penalised
# for downtime they can't avoid. A 1-GCD floor (1.0×) catches them, paired with
# a high 0.75 consensus so ONLY a gap most of the top-parse pool shares (i.e.
# genuinely forced by the fight, not one player's choice) is subtracted — never
# the delivered side. Validated against M9S/M10S/M12S top-10 Reaper pools: it
# recovers ~1pp of forced downtime where it exists (M9S/M10S) and ~0 where it
# doesn't (M12S), and never over-pardons (a 0.75× floor was rejected — it starts
# crediting normal oGCD weave gaps and blows lenient efficiency past 100%).
MELEE_DPS = RolePolicy(
    role="melee_dps",
    idle_floor_mult=1.0,
    consensus_pct=0.75,
    min_ref_count=4,
    fallback_gap_threshold_s=8.0,
    # Fast-end run-in (scripts/calibrate_engage_delay.py). The probe found ~39/40
    # top RPRs PRE-CHANNEL Harpe, so the bare melee run-in is almost never exposed
    # in real data — 1.0s is the fast-end estimate (melee reaching the boss with a
    # dash). The RPR sweep then chooses pre-Harpe vs straight-into-melee per
    # duration on the math, exactly as intended.
    engage_delay_s=1.0,
)
CASTER_HEALER = RolePolicy(
    role="caster_healer",
    idle_floor_mult=2.5,
    consensus_pct=0.80,
    min_ref_count=5,
    fallback_gap_threshold_s=12.0,
)


# --- Cross-cutting per-job rules --------------------------------------------

@dataclass(frozen=True)
class GaugeModel:
    """A FFXIV resource gauge (e.g. MCH Heat or Battery, SAM Kenki, BRD Soul
    Voice). Generators add to it; spenders subtract. Anything generated past the
    live cap is wasted potency (priced at `value_p_per_unit`)."""
    name: str
    generators: dict[int, int]                  # ability_id -> +N units
    spenders: dict[int, int | str]              # ability_id -> -N or "all"
    cap: int
    value_p_per_unit: float                     # potency lost per unit overcapped
    # Optional: abilities that TEMPORARILY raise the cap (GNB Bloodfest: cartridge
    # cap 3 -> 6 for 30s). ability_id -> (raised_cap, duration_s). While a boost is
    # active the overcap detector measures against the raised cap; when it lapses,
    # units above the BASE cap are lost (and priced). Empty (default) -> static cap,
    # byte-identical for every gauge without one.
    cap_boosts: dict[int, tuple[int, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class RaidBuff:
    """One of the standard 20-30s party damage buffs (Trick Attack, Battle
    Litany, etc.). Used by alignment detection."""
    status_id: int
    name: str
    duration_s: float
    dmg_multiplier: float


@dataclass(frozen=True)
class CDRRule:
    """Casting `source` reduces remaining cooldown on each member of
    `targets` by `reduction_s` seconds. Example: MCH's Blazing Shot reduces
    Double Check / Checkmate by 15s per cast.

    `targets` is a frozenset so CDRRule instances stay hashable."""
    source: int
    targets: frozenset[int]
    reduction_s: float


class IdealizedSimulator(Protocol):
    """Optional per-job rotation simulator. Returns a theoretical-best
    potency for a given duration + downtime windows. Drives the dashboard
    efficiency KPI when present; jobs without one show '—' for efficiency."""

    def simulate(self, duration_s: float,
                 downtime: tuple[tuple[float, float], ...]) -> "SimResult": ...


@dataclass(frozen=True)
class SimResult:
    delivered_potency: float
    timeline: tuple[tuple[float, int], ...]    # (t_s, ability_id), frozen


# --- The data bundle --------------------------------------------------------

@dataclass(frozen=True)
class JobData:
    """Per-job data bundle. Anything an `_aspects/` module needs to know
    about a specific job lives here — not as module constants in some
    per-job file the aspect would have to import directly.

    Defaults are empty so jobs can be added incrementally (a job with just
    `potencies` and `cooldowns` populated still produces meaningful drift
    findings)."""
    job_name: str                               # canonical FFLogs spec name
    patch_version: str

    # Core damage / cooldown tables.
    potencies: dict[int, int] = field(default_factory=dict)
    cooldowns: dict[int, tuple[float, int]] = field(default_factory=dict)   # id -> (recast_s, max_charges)
    cooldown_value_p: dict[int, int] = field(default_factory=dict)          # per-cast lost value if skipped

    # Resource gauges (heat, battery, kenki, …). Each becomes one overcap detector pass.
    gauges: tuple[GaugeModel, ...] = ()

    # Opener: first ~12 in-fight GCDs in canonical order. Empty = no opener check.
    canonical_opener: tuple[int, ...] = ()

    # Defensive / utility oGCDs that carry no DPS value and the simulator never
    # fires (e.g. MCH Tactician / Dismantle). Excluded from the DPS timeline +
    # cast-diff: the sidecar unions these with the shared role actions
    # (jobs._core.role_actions.ROLE_ACTION_IDS) and tags each `abilityMeta` entry
    # with `isDefensive`, so the frontend filters off one flag instead of a
    # hand-maintained per-job id list. Mirrors the picker's ignored-action set.
    defensive_ids: frozenset[int] = frozenset()

    # Ability IDs to skip from GCD clip detection (e.g. MCH Blazing Shot at 1.5s).
    clip_exclusions: frozenset[int] = frozenset()

    # Per-ability GCD recast as a MULTIPLE of the job's standard 2.5s GCD, for the
    # jobs whose kit mixes GCD speeds (Viper: 2.0s Generations -> 0.8, 3.0s Coils /
    # Vicewinder / Ouroboros -> 1.2, 3.5s Uncoiled Fury -> 1.4). The idle/clip
    # detector and the Tier-B idle estimator multiply the run's standard effective
    # GCD by this for each ability, so a longer GCD's natural gap isn't misread as
    # idle and a faster one's isn't misread as clipping; the standard-GCD estimate
    # itself is taken over `== 1.0` abilities only. Empty (default) ⇒ every ability
    # is the standard GCD — byte-identical for the uniform-GCD jobs.
    gcd_recast_mult: dict[int, float] = field(default_factory=dict)

    # Cast-id -> window duration (s) that excludes the affected stretch from
    # clip detection. Used for sub-rotations that intentionally break global
    # GCD pacing (e.g. MCH Hypercharge spawns an 8s window of 1.5s recast
    # Blazing Shots that would otherwise read as clipping). Each in-fight
    # cast of `source_id` excludes [t, t + window_s] from clip pairs.
    clip_skip_windows: dict[int, float] = field(default_factory=dict)

    # Ability IDs to skip from cooldown-drift detection because the binding
    # constraint is gauge/resource availability, not pilot drift (e.g. MCH
    # Hypercharge — gated by heat, real waste signal is Overcap not Drift).
    drift_exclusions: frozenset[int] = frozenset()

    # Clipped-instant pre-pull casts: `ability_id -> buff_status_id`. An INSTANT
    # ability pressed during the countdown (e.g. MCH Reassemble) executes before
    # the report's fight boundary, so FFLogs drops the cast event entirely — but
    # the buff it applies survives into the fight. When that buff is seen
    # *pre-applied* (its first event is a remove/refresh with no apply), the
    # player precast the ability, so the analyzer reconstructs the cast in the
    # Timeline's pre-pull zone. (Hardcast precasts — RDM Verthunder III, RPR
    # Harpe — don't need this: their cast/completion lands in-fight and is logged.)
    prepull_buff_ids: dict[int, int] = field(default_factory=dict)

    # RNG-gated abilities (e.g. RDM Verfire / Verstone, unlocked only by a random
    # proc). The idealized sim casts them on a modeled cadence, but the player
    # can't summon a proc on demand — so a sim/player mismatch on these is NOT a
    # "missed cast" the player could have made. Excluded from the missed-cast
    # improvement diff so they never surface as "should have cast here" clutter.
    rng_proc_ids: frozenset[int] = frozenset()

    # High-potency tool IDs the alignment detector watches — these are the
    # abilities worth shifting into raid-buff windows. For MCH: Drill /
    # Air Anchor / Chain Saw. For SAM: Iaijutsu chain. Empty = no alignment.
    burst_abilities: frozenset[int] = frozenset()

    # Cross-cooldown effects (e.g. Blazing Shot reduces DC/CM).
    cdr_rules: tuple[CDRRule, ...] = ()

    # Shared-charge pools (e.g. Bioblaster consumes Drill charges).
    # Map: consumer_id -> source_id whose charges/cooldown it shares.
    charge_sharing: dict[int, int] = field(default_factory=dict)

    # Splash / AoE secondary-target potency: ability_id -> potency dealt to
    # EACH additional target beyond the primary (falloff already baked in — a
    # "-20% for others" line stores the reduced secondary value, not the
    # primary). Two classes share this map:
    #   * Free-splash: ST-rotation abilities that incidentally cleave (e.g. RPR
    #     Communio / Plentiful Harvest, MCH Chain Saw / Full Metal Field). The
    #     idealized ST sim already casts them, so crediting their splash on both
    #     delivered and the ceiling is symmetric (the >100% guard holds).
    #   * AoE-investment: dedicated AoE buttons the ST sim does NOT cast (e.g.
    #     RPR Spinning Scythe / Guillotine). Their delivered splash is only
    #     matched on the ceiling once the sim learns the duration-gated AoE
    #     pick (Phase 6) — until then their windows stay disclaimed.
    # Empty (the default) = pure single target; every number stays byte-
    # identical to the single-target model.
    splash_potencies: dict[int, int] = field(default_factory=dict)

    # AoE buttons the AoE-aware sim casts in multi-target windows (the ones the
    # single-target rotation does NOT use — MCH Scattergun / Auto Crossbow /
    # Bioblaster, RPR Nightmare Scythe / Guillotine, BLM Flare / Flare Star, …).
    # Same convention as `splash_potencies`: ability_id -> per-extra-target
    # potency (falloff baked in; a full-to-all AoE stores `secondary == primary`,
    # i.e. == POTENCIES[id]). The PRIMARY potency always lives in `potencies`.
    # Kept distinct from `splash_potencies` so the legacy free-splash sidecar
    # overlay (which credits only ST-rotation casts) stays uncontaminated.
    # `jobs._core.sim.aoe_potency.potency_for` reads both. Empty = byte-identical.
    aoe_potencies: dict[int, int] = field(default_factory=dict)

    # Per-ability target cap for splash/AoE scoring (most FFXIV AoE caps at
    # `aoe_potency.DEFAULT_AOE_CAP` = 8; only list the exceptions). Rarely binds
    # at these fights' target counts but keeps the ceiling a true upper bound.
    aoe_target_caps: dict[int, int] = field(default_factory=dict)

    # AoE reach overrides in yalms for the ADVISORY cleave-geometry verdicts
    # (jobs._core.cleave_geometry) — only abilities whose reach materially
    # exceeds the standard 5y target-centered splash circle (long lines / wide
    # cones, stored as circles of their length: generous, which only biases the
    # advisory toward "reachable", i.e. away from auto-denying). Never touches
    # any potency/ceiling math. Empty = every splash/AoE ability at the 5y
    # default.
    aoe_radii_yalm: dict[int, float] = field(default_factory=dict)

    # Standard party damage buffs the alignment detector watches for.
    raid_buffs: dict[int, RaidBuff] = field(default_factory=dict)

    # Role policy — drives Tier-B (consensus) downtime tuning. Default is
    # physical_ranged because that's the role with the cleanest signal
    # and the most conservative thresholds; individual jobs override.
    role_policy: RolePolicy = PHYSICAL_RANGED

    # Representative potency of the GCD that backfills a slot when a
    # high-potency tool is missed. Because GCD slots are always filled by
    # *something*, a dropped 660p tool only costs its potency above this
    # filler (fungibility). 0 = treat a miss as full potency (jobs that
    # haven't characterized their filler). MCH ≈ a Heated combo hit.
    filler_gcd_potency: int = 0

    # High-value, interchangeable *filler* GCDs whose under-count vs the idealized
    # rotation is genuine throughput loss the cooldown missed-cast diff can't see
    # (it only tracks `cooldowns`). For RDM: the Dualcasted 440s + the enchanted
    # combo/finisher chain (casting Jolt III where the ideal runs a 440, or fewer
    # combos). The improvements panel diffs these against the ideal to surface a
    # "Filler quality" card instead of an opaque residual. Empty (default) = no
    # such card — MCH/RPR/SAM stay byte-identical, their gap is already pinned by
    # idle/clip + drift + overcap + missed cooldowns.
    filler_quality_gcds: frozenset[int] = frozenset()

    # Tincture (damage potion) modeling. `tincture_main_stat` is the job's
    # effective (post-food/party) BiS main stat — the `base` in the tincture
    # multiplier `f(base+Δ)/f(base)` (jobs._core.tincture). FFLogs doesn't expose
    # per-player gear on the client API, so this per-job constant is the source;
    # both delivered and idealized use the same M, so its effect on rank is
    # self-correcting. `None` ⇒ no tincture modeling (the scoring path stays
    # byte-identical — SAM and any data-only job). `tincture_role_coeff` is the
    # level-100 main-attribute slope (non-tank 237, tank ~190); the ratio is
    # nearly insensitive to it. Refine the main stat per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat: int | None = None
    tincture_role_coeff: int = 237

    # The job's ranged-filler GCD (e.g. RPR Harpe) — the button a melee bridges
    # a forced disconnect with instead of going idle. Enables the consensus
    # ranged-filler windows (downtime_sources.ranged_filler_windows_from_refs):
    # where >= consensus_pct of refs cast it at the same fight time, the LENIENT
    # ceiling swaps its melee GCDs for the filler there (Tier-B's sibling —
    # never the strict ceiling, never the delivered side). `None` (default) ⇒
    # the detection never runs and the job stays byte-identical.
    ranged_filler_id: int | None = None

    # True for jobs whose aspects read the PLAYER's own DamageDone stream on
    # EVERY pull (buff/debuff-coverage reconstruction from each hit's `buffs`
    # snapshot — RPR Death's Design, SAM Fugetsu, WAR Surging Tempest — and MCH's
    # Queen/multi-target scan). When set, `_prime_pull_bundle` folds that stream
    # into the single per-pull bundle so the coverage fetch is a cache hit
    # instead of a separate round trip. Jobs that read DamageDone only on
    # multi-target pulls leave this False — the on-demand fetch there is already
    # gated to those pulls, so bundling it would just waste bandwidth on the
    # single-target majority. Default False (byte-identical).
    prebundle_damage_done: bool = False


# --- Job + registry ---------------------------------------------------------

@dataclass(frozen=True)
class Job:
    """One registered job. `aspects` runs in the listed order during
    `analyze_pull`. `simulator` is None for jobs that don't have one yet —
    `_aspects` modules that depend on it should degrade gracefully."""
    name: str
    data: JobData
    aspects: tuple[Aspect, ...]
    simulator: Optional[IdealizedSimulator] = None
    # Optional: extra per-pull event streams to fold into the prefetch bundle
    # (e.g. MCH Queen pet DamageDone), beyond the generic casts / targetability /
    # buffs streams. `(report, fight, actor) -> list[BundleStream]`. Lets
    # `analyze_pull` warm a job's data-dependent streams in the same round trip
    # without the core knowing what they are.
    bundle_extra_streams: Optional[Callable[..., list]] = None
    # Optional: job-specific priced Potential-Improvement contributors, read by
    # the sidecar's `_build_improvements`. Signature:
    # `(you: ModuleResult, idealized: list[(t, id)], enabler_values: dict[int,
    # float], death_windows: list[(s, e)]) -> list[Improvement]`. Lets a job add
    # cards for losses the generic missed-cast diff can't see (e.g. RPR Death's
    # Design downtime / positional misses) without the sidecar importing the job.
    improvement_contributors: Optional[Callable[..., list]] = None


_REGISTRY: dict[str, Job] = {}


def register(job: Job) -> None:
    """Add a Job to the global registry. Per-job packages call this at
    import time. Overwrites silently on duplicate name — last import wins."""
    _REGISTRY[job.name] = job


def get_job(name: str) -> Job:
    """Look up a registered Job by canonical name. Raises KeyError if the
    job exists in `ALL_JOBS` but hasn't been registered (i.e. has no
    analyzer support yet)."""
    if name not in _REGISTRY:
        raise KeyError(
            f"job {name!r} is not registered. Available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def registered_jobs() -> list[str]:
    return sorted(_REGISTRY)
