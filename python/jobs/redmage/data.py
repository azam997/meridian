"""Red Mage data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for RDM numbers: potencies, cast times, cooldowns, the
White/Black mana gauges, and the opener. Potencies / cast times / recasts are
cross-checked against ffxiv.consolegameswiki.com/wiki/Red_Mage (the level-100
PvE actions page).

RDM is the analyzer's first **caster**, which brings two firsts:

  * **Dualcast** — casting a spell with a cast time grants Dualcast, making the
    *next* spell instant. So the filler alternates a 2 s-cast enabler (Jolt III /
    Verfire / Verstone) with a Dualcasted 5 s spell (Verthunder III / Veraero III)
    cast instantly. Modeled in the simulator (Phase 2) via the `HardcastGCD`
    timing preset + a `dualcast` state flag; the cast times below feed it.
  * **RNG procs** — Verthunder III / Veraero III have a 50% chance to grant
    Verfire Ready / Verstone Ready, which UNLOCK the 2 s-cast instawin-enablers
    Verfire / Verstone (380p, vs Jolt III's 360p — only ~20p above filler, so a
    proc is filler-tier; the real loss is *wasting* a proc). The proc status ids
    below drive the Phase 3 ProcsAspect.

⚠️ ACTION IDS — these are the game/FFLogs action ids to the best of current
knowledge (Stormblood base 75xx, Endwalker 16xxx / 258xx, Dawntrail 370xx). They
map the FFLogs cast stream onto this table, so a wrong id silently drops an
ability from scoring/findings. Verify the full set against a real RDM log's
`masterData.abilities` on the first live run. The synthetic test-suite is
ID-agnostic (it builds fixtures from whatever this file declares), so tests pass
regardless of id accuracy; the live run is the authority.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) ---------------------------------------

# Filler spells (the Dualcast loop)
JOLT_III           = 37004   # 2 s cast, +2/+2 mana, grants Dualcast; -> Scorch in combo
VERTHUNDER_III     = 25855   # 5 s cast, +6 black, 50% Verfire Ready
VERAERO_III        = 25856   # 5 s cast, +6 white, 50% Verstone Ready
VERFIRE            = 7510    # 2 s cast, +5 black; requires Verfire Ready
VERSTONE           = 7511    # 2 s cast, +5 white; requires Verstone Ready
# AoE filler
VERTHUNDER_II      = 16524   # 2 s cast, +7 black; -> Verflare at 3 stacks
VERAERO_II         = 16525   # 2 s cast, +7 white; -> Verholy at 3 stacks
IMPACT             = 16526   # 5 s cast AoE, +3/+3; -> Grand Impact w/ proc
GRAND_IMPACT       = 37006   # instant, +3/+3; requires Grand Impact Ready (Acceleration)

# Melee combo (enchanted — spends White + Black mana, builds Mana Stacks)
ENCHANTED_RIPOSTE      = 7527   # 1.5 s recast, -20/-20, +1 stack
ENCHANTED_ZWERCHHAU    = 7528   # 1.5 s recast, -15/-15, +1 stack
ENCHANTED_REDOUBLEMENT = 7529   # 2.2 s recast, -15/-15, +1 stack
# Magicked Swordplay variants (Manafication grants 3 FREE enchanted GCDs that
# cost no mana — distinct action ids, same potency). Live-probe confirmed on a
# real RDM log. The sim emits the base ids for its combos; these exist so the
# delivered side scores the player's free-combo casts (else ~18 casts read 0p).
ENCHANTED_RIPOSTE_M      = 45960
ENCHANTED_ZWERCHHAU_M    = 45961
ENCHANTED_REDOUBLEMENT_M = 45962
ENCHANTED_REPRISE      = 16528  # 2.5 s recast, -5/-5 (ranged gap-filler)
# AoE melee combo (Enchanted Moulinet I/II/III) is out of scope for the
# single-target v1 sim; the DT II/III ids were unverifiable (live-probe showed
# 37008/37009 belong to other jobs), so they're omitted until AoE is modeled.
# Finishers (instant; the burst payoff)
VERFLARE           = 7525    # instant, costs 3 stacks, +11 black, 20%/100% Verfire Ready
VERHOLY            = 7526    # instant, costs 3 stacks, +11 white, 20%/100% Verstone Ready
SCORCH             = 16530   # instant, +4/+4; after Verflare/Verholy
RESOLUTION         = 25858   # instant, +4/+4; after Scorch

# oGCDs
FLECHE             = 7517    # 25 s
CONTRE_SIXTE       = 7519    # 35 s AoE
ACCELERATION       = 7518    # 55 s, 2 charges; next VT3/VA3/Impact instant + Grand Impact Ready
MANAFICATION       = 7521    # 110 s; 3 Magicked Swordplay stacks + Prefulgence Ready
EMBOLDEN           = 7520    # 120 s; +10% self / +5% party magic dmg + Thorned Flourish
CORPS_A_CORPS      = 7506    # 35 s, 2 charges (gap closer)
ENGAGEMENT         = 16527   # 35 s, 2 charges (shares recast with Displacement)
DISPLACEMENT       = 7515    # 35 s, 2 charges (shares recast with Engagement)
VICE_OF_THORNS     = 37005   # via Thorned Flourish (Embolden)
PREFULGENCE        = 37007   # via Prefulgence Ready (Manafication)
MAGICK_BARRIER     = 25857   # 120 s, defensive (no DPS)
SWIFTCAST          = 7561    # role action: next spell instant (10s). DPS effect
                             # (a free instant 440) is modeled by the simulator;
                             # see SWIFTCAST_RECAST_S. ⚠️ verify id live.
# Vercure — 2 s-cast heal (350 CURE potency, no DPS, so NOT in POTENCIES). Its
# only rotational use: cast during downtime to bank a Dualcast, so the first GCD
# out of downtime is an instant 440 instead of a 2 s hardcast enabler. Modeled in
# the simulator's on_downtime_window. ⚠️ verify id live.
VERCURE            = 7503
VERCURE_CAST_S: float = 2.0

# --- Proc / mechanic status ids (Phase 3 ProcsAspect; ⚠️ verify live) -------
DUALCAST_STATUS_ID        = 1249
VERFIRE_READY_STATUS_ID   = 1234
VERSTONE_READY_STATUS_ID  = 1235
GRAND_IMPACT_READY_STATUS_ID = 3215
MAGICKED_SWORDPLAY_STATUS_ID = 3875
PROC_DURATION_S: float = 30.0

# --- Cast times (s) — feeds the HardcastGCD timing preset (Phase 2) ---------
# Absent ids are instant. The 5 s spells are virtually always cast under
# Dualcast (instant); the cast time only bites on a forced hardcast.
CAST_TIMES: dict[int, float] = {
    JOLT_III:       2.0,
    VERFIRE:        2.0,
    VERSTONE:       2.0,
    VERTHUNDER_II:  2.0,
    VERAERO_II:     2.0,
    VERTHUNDER_III: 5.0,
    VERAERO_III:    5.0,
    IMPACT:         5.0,
}

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / crit modeling). Combo'd value for the
# combo finishers. AoE values are the primary (full) hit; falloff lives in
# SPLASH_POTENCIES (unused until the sim models AoE).

POTENCIES: dict[int, int] = {
    # Filler
    JOLT_III:       360,
    VERTHUNDER_III: 440,
    VERAERO_III:    440,
    VERFIRE:        380,
    VERSTONE:       380,
    VERTHUNDER_II:  140,
    VERAERO_II:     140,
    IMPACT:         210,
    GRAND_IMPACT:   600,
    # Melee combo
    ENCHANTED_RIPOSTE:      340,
    ENCHANTED_ZWERCHHAU:    380,   # combo'd
    ENCHANTED_REDOUBLEMENT: 560,   # combo'd
    ENCHANTED_RIPOSTE_M:      340,  # Magicked Swordplay (free) — same potency
    ENCHANTED_ZWERCHHAU_M:    380,
    ENCHANTED_REDOUBLEMENT_M: 560,
    ENCHANTED_REPRISE:      420,
    # Finishers
    VERFLARE:       650,
    VERHOLY:        650,
    SCORCH:         750,
    RESOLUTION:     850,
    # oGCDs
    FLECHE:         480,
    CONTRE_SIXTE:   420,
    CORPS_A_CORPS:  130,
    ENGAGEMENT:     180,
    DISPLACEMENT:   180,
    VICE_OF_THORNS: 950,
    PREFULGENCE:   1200,
    # Buttons with no direct potency
    ACCELERATION:   0,
    MANAFICATION:   0,
    EMBOLDEN:       0,
    MAGICK_BARRIER: 0,
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    FLECHE, CONTRE_SIXTE, ACCELERATION, MANAFICATION, EMBOLDEN,
    CORPS_A_CORPS, ENGAGEMENT, DISPLACEMENT, VICE_OF_THORNS, PREFULGENCE,
    MAGICK_BARRIER, SWIFTCAST,
})

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only the genuinely RECAST-gated
# DPS oGCDs live here; the melee combo and finishers are GAUGE/combo-gated, and
# the gap-closers (Corps-a-corps / Engagement / Displacement) are mobility tools
# held for movement, not DPS — listing any of them would read as false drift.

COOLDOWNS: dict[int, tuple[float, int]] = {
    FLECHE:         (25.0, 1),
    CONTRE_SIXTE:   (35.0, 1),
    ACCELERATION:   (55.0, 2),
    MANAFICATION:   (110.0, 1),
    EMBOLDEN:       (120.0, 1),
    # Gap-closers — top RDMs weave these for damage in single target (live-probe:
    # ~15 each / pull). The sim fires them as low-priority weave fillers; they're
    # in drift_exclusions so HOLDING one for movement isn't flagged as a mistake.
    ENGAGEMENT:     (35.0, 2),
    CORPS_A_CORPS:  (35.0, 2),
}

# Swiftcast recast (s) — Enhanced Swiftcast (lv94) drops it to 40s, 1 charge.
# Deliberately NOT in COOLDOWNS: the idealized sim fires it on cooldown for a
# free instant (max DPS ceiling), but a player who holds Swiftcast for a movement
# mechanic should never be flagged for drift / a missed cast, so it stays out of
# the drift + missed-cast diffs.
SWIFTCAST_RECAST_S: float = 40.0

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    FLECHE:         480,
    CONTRE_SIXTE:   420,
    ACCELERATION:   250,   # enables a Grand Impact (600) + guaranteed proc/instant
    MANAFICATION:  2000,   # 3 free enchanted GCDs + finisher chain + Prefulgence
    EMBOLDEN:      1000,   # raid buff + Vice of Thorns (rough; refine post-validation)
    ENGAGEMENT:     180,
    CORPS_A_CORPS:  130,
}

# --- White / Black mana gauges ----------------------------------------------
# Two symmetric 0–100 gauges. Built by the elemental spells (Thunder/Fire ->
# Black; Aero/Stone -> White; Jolt/Impact/Scorch/Resolution build both). Spent
# 50 of each by the enchanted melee combo (Riposte 20, Zwerchhau 15,
# Redoublement 15). Overcapping past 100 wastes a spell's generation.

BLACK_MANA_GENERATORS: dict[int, int] = {
    JOLT_III:        2,
    VERTHUNDER_III:  6,
    VERTHUNDER_II:   7,
    VERFIRE:         5,
    IMPACT:          3,
    GRAND_IMPACT:    3,
    VERFLARE:       11,
    SCORCH:          4,
    RESOLUTION:      4,
}
WHITE_MANA_GENERATORS: dict[int, int] = {
    JOLT_III:        2,
    VERAERO_III:     6,
    VERAERO_II:      7,
    VERSTONE:        5,
    IMPACT:          3,
    GRAND_IMPACT:    3,
    VERHOLY:        11,
    SCORCH:          4,
    RESOLUTION:      4,
}
_MANA_SPENDERS: dict[int, int] = {
    ENCHANTED_RIPOSTE:      20,
    ENCHANTED_ZWERCHHAU:    15,
    ENCHANTED_REDOUBLEMENT: 15,
    ENCHANTED_REPRISE:       5,
}
# Both gauges spend identically (the enchanted combo drains White and Black
# together), so the spender table is shared.
BLACK_MANA_SPENDERS: dict[int, int] = dict(_MANA_SPENDERS)
WHITE_MANA_SPENDERS: dict[int, int] = dict(_MANA_SPENDERS)
MANA_CAP = 100
# 50 of each mana -> one enchanted melee->finisher chain (~340+380+560 +
# 650+750+850). Overcapping one unit forfeits ~that fraction of a future chain.
# Estimate; refine against top parses once the sim is validated.
MANA_VALUE_P_PER_UNIT: float = 4.0

# --- Canonical opener -------------------------------------------------------
# First ~12 in-fight GCDs in expected order. ⚠️ Placeholder ordering — refine
# against a current RDM opener guide. OpenerAspect is a zero-priced diagnostic,
# so an approximate sequence is acceptable until the live pass.
CANONICAL_OPENER: tuple[int, ...] = (
    VERAERO_III,             # pre-built mana / Dualcast loop
    VERTHUNDER_III,
    JOLT_III,
    VERTHUNDER_III,
    ENCHANTED_RIPOSTE,       # melee combo at 50/50 mana
    ENCHANTED_ZWERCHHAU,
    ENCHANTED_REDOUBLEMENT,
    VERHOLY,                 # finisher
    SCORCH,
    RESOLUTION,
    GRAND_IMPACT,            # Acceleration-granted instant
    VERFIRE,                 # proc spend
)

# --- Clip-detection exclusions ---------------------------------------------
# The enchanted melee combo runs at 1.5 s / 2.2 s recast — intentionally faster
# than the 2.5 s global, so consecutive enchanted weaponskills are NOT clips
# (the RPR Enshroud-Reaping analog).
CLIP_EXCLUSIONS: frozenset[int] = frozenset({
    ENCHANTED_RIPOSTE, ENCHANTED_ZWERCHHAU, ENCHANTED_REDOUBLEMENT,
    ENCHANTED_RIPOSTE_M, ENCHANTED_ZWERCHHAU_M, ENCHANTED_REDOUBLEMENT_M,
})

# --- Drift-detection exclusions --------------------------------------------
# The gap-closers are in COOLDOWNS so the sim can fire them for damage, but they
# double as movement tools — holding one isn't drift, so suppress those findings.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset({ENGAGEMENT, CORPS_A_CORPS})

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these): the
# 2-minute melee burst + its enablers.
BURST_ABILITIES: frozenset[int] = frozenset({
    MANAFICATION, EMBOLDEN, VERFLARE, VERHOLY, SCORCH, RESOLUTION,
    VICE_OF_THORNS, PREFULGENCE,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (Phase 2 scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (MANAFICATION, EMBOLDEN, ACCELERATION)

# RNG-gated proc spells — unlocked only by a random Verfire/Verstone Ready proc,
# so the player can't cast them on demand. The missed-cast diff excludes these.
RNG_PROC_IDS: frozenset[int] = frozenset({VERFIRE, VERSTONE})

# Interchangeable high-value filler GCDs whose under-count vs the ideal is the
# diffuse "cast Jolt III (360) where the ideal Dualcasts a 440" loss — the
# dominant residual the cooldown missed-cast diff structurally can't see. Kept to
# the Dualcasted 440s on purpose: combo/finisher shortfall is already attributed
# to the Manafication missed-enabler + mana-overcap cards, so pricing it here too
# would double-count (the whole reconcile model exists to avoid that). VT3/VA3 are
# a symmetric pair (build black/white evenly) and carry no Magicked-Swordplay id
# alias, so a per-id diff is clean. Drives the "Filler quality" Improvement card.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({VERTHUNDER_III, VERAERO_III})

# Defensive / utility oGCDs the simulator never fires (excluded from the DPS
# timeline + cast-diff).
DEFENSIVE_IDS: frozenset[int] = frozenset({MAGICK_BARRIER})


# --- JOB_DATA bundle --------------------------------------------------------

# --- AoE potencies (the cleaving burst the ST rotation already casts) -------
# ability_id -> per-extra-target potency. RDM's filler/melee combo has dedicated
# AoE buttons (Veraero/Verthunder II, Enchanted Moulinet) deferred for now; what
# the ST rotation ALREADY casts and cleaves is the finisher chain + Contre Sixte,
# wiki-verified at 55% falloff (x0.45) — Contre Sixte is full-to-all. These scale
# on delivered + ceiling via `aoe_potency.potency_for`.
AOE_POTENCIES: dict[int, int] = {
    GRAND_IMPACT: 270,   # 600 x 0.45
    VERFLARE:     292,   # 650 x 0.45
    VERHOLY:      292,   # 650 x 0.45
    SCORCH:       337,   # 750 x 0.45
    RESOLUTION:   382,   # 850 x 0.45
    CONTRE_SIXTE: 420,   # full-to-all (no falloff)
}


JOB_DATA: JobData = JobData(
    job_name="Red Mage",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="white_mana",
            generators=WHITE_MANA_GENERATORS,
            spenders=WHITE_MANA_SPENDERS,
            cap=MANA_CAP,
            value_p_per_unit=MANA_VALUE_P_PER_UNIT,
        ),
        GaugeModel(
            name="black_mana",
            generators=BLACK_MANA_GENERATORS,
            spenders=BLACK_MANA_SPENDERS,
            cap=MANA_CAP,
            value_p_per_unit=MANA_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    rng_proc_ids=RNG_PROC_IDS,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),                       # no cross-cooldown reductions
    charge_sharing={},                  # gap-closers aren't drift-tracked (mobility)
    raid_buffs={},                      # Embolden modeled via buff_windows (job-agnostic)
    role_policy=CASTER_HEALER,
    aoe_potencies=AOE_POTENCIES,

    # A dropped tool backfills with a filler spell (~Jolt III / Verfire 360-380);
    # price a miss at opportunity cost above that.
    filler_gcd_potency=380,
    # Tincture: effective BiS Intelligence from xivgear (incl. party-comp bonus +
    # food — the in-raid value). Base for f(base+Δ)/f(base), Δ541 ⇒ ≈ +8.21%.
    tincture_main_stat=6838,
)
