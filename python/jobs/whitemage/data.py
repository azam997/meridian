"""White Mage data tables (Dawntrail 7.4, level 100) + the `JOB_DATA` JobData.

Single source of truth for WHM numbers: potencies, cast times, cooldowns, the
Lily gauge constants, and the opener. Potencies / cast times / recasts are
cross-checked against ffxiv.consolegameswiki.com per-ability pages (patch 7.4
values: Glare III 350, Dia 85 + 85/tick, Afflatus Misery 1,400) and every
action id below was verified against XIVAPI (scripts/probe_whm_ids.py — 33/33
name matches).

WHM is the analyzer's first **healer**. The damage kit is tiny — two filler
spells (Glare III hardcast, Dia DoT), one oGCD (Assize), one haste/proc window
(Presence of Mind → Glare IV ×3) — but the interesting part is the
**dual-purpose Lily economy**:

  * One Healing Lily accrues every 20 s in combat (cap 3). Spending one casts
    Afflatus Solace / Rapture — an INSTANT heal GCD that deals no damage but
    nourishes the Blood Lily; three nourishes bloom Afflatus Misery (1,400).
  * Misery (1,400) vs the four Glare III (4 × 350 = 1,400) it displaces is
    exactly potency-NEUTRAL by design. Its real value: lily heals are
    instant + party-targeted, so a good WHM spends them during downtime /
    forced movement (zero damage GCDs displaced) and banks Misery for raid-buff
    windows — both modeled in the simulator, which is what makes the lily
    line strictly positive for the ceiling instead of cosmetic.

MP is deliberately NOT modeled (gotcha: model only gauges that bind the
offensive rotation): lily heals are free and Assize/Lucid refund MP, so MP
never binds the optimized line.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, JobData


PATCH_VERSION = "7.4"

# --- Ability IDs (verified vs XIVAPI; see scripts/probe_whm_ids.py) ---------

# Damage rotation
GLARE_III   = 25859   # 350p, 1.5 s cast — the filler spell
GLARE_IV    = 37009   # 640p, instant; needs a Sacred Sight stack (from PoM)
DIA         = 16532   # 85p initial + 85p/3s DoT for 30 s, instant
ASSIZE      = 3571    # 400p AoE oGCD, 40 s; also heals 400 + restores 5% MP
PRESENCE_OF_MIND = 136  # oGCD 120 s: 20% cast/recast haste 15 s + 3 Sacred Sight
AFFLATUS_MISERY  = 16535  # 1,400p instant GCD; consumes the bloomed Blood Lily
HOLY_III    = 25860   # 150p AoE GCD (multi-target only; the ST sim never casts it)

# Lily heals (dual-purpose: 0 damage, nourish the Blood Lily toward Misery)
AFFLATUS_SOLACE  = 16531  # instant ST heal GCD, consumes 1 lily
AFFLATUS_RAPTURE = 16534  # instant AoE heal GCD, consumes 1 lily

# Healing / mitigation kit (non-rotational — excluded from the DPS diff)
CURE        = 120
MEDICA      = 124
RAISE       = 125
CURE_III    = 131
MEDICA_II   = 133
CURE_II     = 135
REGEN       = 137
BENEDICTION = 140
ASYLUM      = 3569
TETRAGRAMMATON = 3570
THIN_AIR    = 7430
DIVINE_BENISON = 7432
PLENARY_INDULGENCE = 7433
TEMPERANCE  = 16536
AQUAVEIL    = 25861
LITURGY_OF_THE_BELL = 25862
AETHERIAL_SHIFT = 37008
MEDICA_III  = 37010
DIVINE_CARESS = 37011

# --- Status ids (reference; the sim is cast-driven and never fetches these) --
PRESENCE_OF_MIND_STATUS_ID = 157
SACRED_SIGHT_STATUS_ID = 3879     # ⚠️ unverified — unused by the pipeline
DIA_STATUS_ID = 1871

# --- Rotation constants ------------------------------------------------------
GLARE_CAST_S: float = 1.5
POM_HASTE: float = 0.80           # 20% cast + recast reduction
POM_DURATION_S: float = 15.0
SACRED_SIGHT_STACKS: int = 3
SACRED_SIGHT_DURATION_S: float = 30.0
LILY_INTERVAL_S: float = 20.0     # one Healing Lily per 20 s in combat
LILY_CAP: int = 3
BLOOD_LILY_CAP: int = 3           # nourishes to bloom Afflatus Misery
DIA_DOT_DURATION_S: float = 30.0
DIA_DOT_TICK_S: float = 3.0
DIA_DOT_TICK_P: int = 85

# --- Cast times (s) — feeds the HardcastGCD timing preset -------------------
# Absent ids are instant. Glare III's 1.5 s cast is shorter than the 2.5 s
# recast, so the slot is always recast-bound; the cast time only matters for
# the weave budget (a hardcast leaves one weave, an instant leaves two).
# Medica III (2.0 s cast, still recast-bound) is only ever cast by the sim as
# a mit-plan LOCKED heal — the unlocked rotation never fires it, so adding it
# here leaves every unlocked run byte-identical.
CAST_TIMES: dict[int, float] = {
    GLARE_III: GLARE_CAST_S,
    HOLY_III:  GLARE_CAST_S,   # AoE filler — same 1.5s cast / recast-bound slot
    MEDICA_III: 2.0,           # locked heal GCD (mit-plan integration)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) -----------
# ability_id -> per-extra-target potency. Holy III is full-to-all (secondary ==
# primary). Glare IV / Assize / Misery already cleave and live in
# SPLASH_POTENCIES (free-splash the ST sim casts). ⚠️ verify-live (Phase 5).
AOE_POTENCIES: dict[int, int] = {
    HOLY_III: 150,
}

# --- Potencies ---------------------------------------------------------------
# ability_id -> base potency. DIA carries only its initial hit here — the DoT
# is scored per cast by time-to-next-application (jobs/whitemage/scoring.py,
# the SAM Higanbana pattern) so an early refresh credits less, never double-
# counts. The lily heals are rotational 0-potency GCDs (they must appear on
# the sim timeline so the lily->Misery economy is visible + diffable).
POTENCIES: dict[int, int] = {
    GLARE_III:        350,
    GLARE_IV:         640,
    DIA:               85,
    ASSIZE:           400,
    AFFLATUS_MISERY: 1400,
    HOLY_III:         150,
    AFFLATUS_SOLACE:    0,
    AFFLATUS_RAPTURE:   0,
    PRESENCE_OF_MIND:   0,
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({ASSIZE, PRESENCE_OF_MIND})

# --- Cooldowns + charges -----------------------------------------------------
# Only the genuinely RECAST-gated DPS oGCDs. Glare IV is Sacred-Sight-gated and
# Misery is gauge-gated — listing either would read as false drift.
COOLDOWNS: dict[int, tuple[float, int]] = {
    ASSIZE:           (40.0, 1),
    PRESENCE_OF_MIND: (120.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    ASSIZE:           400,
    # 3 Glare IV upgrades (3 x 290 over the Glare III they displace) + ~1.5
    # extra GCDs from the 15 s haste window (~525p). Rough; the priced
    # improvement card uses the sim-derived enabler_net_values instead.
    PRESENCE_OF_MIND: 1400,
}

# --- Canonical opener --------------------------------------------------------
# First ~12 in-fight GCDs (the pre-pull Glare III channel is separate, not part
# of this in-fight sequence). Measured from the M11S top-parse consensus
# (scripts/probe_whm_entry.py): precast Glare lands at t≈0, Dia 2nd, fillers
# until PoM weaves in ~GCD6, then the 3 Sacred Sight Glare IVs spread across the
# 15 s haste window (interleaved with filler), then the first Misery once the
# Blood Lily blooms (~GCD12). OpenerAspect is a zero-priced diagnostic.
CANONICAL_OPENER: tuple[int, ...] = (
    GLARE_III,
    DIA,
    GLARE_III,
    GLARE_III,
    GLARE_III,
    GLARE_III,        # PoM weaved here -> 3 Sacred Sight stacks
    GLARE_IV,
    GLARE_IV,
    GLARE_III,
    GLARE_III,
    GLARE_IV,
    AFFLATUS_MISERY,
)

# --- Non-rotational ids ------------------------------------------------------
# The healing/mitigation kit: real casts with no DPS value the simulator never
# fires. Excluded from the DPS timeline + cast-diff (isDefensive on the wire).
# NOT here: the lily heals + Misery (rotational — the lily economy), Assize
# (damage oGCD), Holy III (damage AoE).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    CURE, MEDICA, RAISE, CURE_III, MEDICA_II, CURE_II, REGEN, BENEDICTION,
    ASYLUM, TETRAGRAMMATON, THIN_AIR, DIVINE_BENISON, PLENARY_INDULGENCE,
    TEMPERANCE, AQUAVEIL, LITURGY_OF_THE_BELL, AETHERIAL_SHIFT, MEDICA_III,
    DIVINE_CARESS,
})

# --- Costed heal GCDs ---------------------------------------------------------
# Hardcast/GCD heals that displace a damage GCD (a Glare III) when cast during
# uptime — the currency of the mit-plan lock accounting and the "extra healing
# GCDs beyond the plan" improvement card. NOT here: Solace/Rapture (free lily
# spends, potency-neutral via Misery) and Raise (death recovery, priced by the
# Death card's context, not healing triage).
COSTED_HEAL_GCD_IDS: frozenset[int] = frozenset({
    CURE, CURE_II, CURE_III, MEDICA, MEDICA_II, MEDICA_III, REGEN,
})

# --- Burst-alignment abilities ----------------------------------------------
# Worth shifting into raid-buff windows: the banked Misery, the Glare IV
# stacks, and PoM itself (the 2-minute anchor).
BURST_ABILITIES: frozenset[int] = frozenset({
    AFFLATUS_MISERY, GLARE_IV, PRESENCE_OF_MIND,
})

# Enablers whose value is throughput (haste window + Sacred Sight stacks), not
# standalone table potency — priced by the sim's marginal contribution.
ENABLER_IDS: tuple[int, ...] = (PRESENCE_OF_MIND,)

# Interchangeable filler GCDs whose under-count vs the ideal is the diffuse
# "healed with a GCD where the ideal casts Glare III" loss — THE healer story,
# structurally invisible to the cooldown missed-cast diff. Drives the "Filler
# quality" Improvement card.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({GLARE_III})

# --- Multi-target ------------------------------------------------------------
# Free-splash abilities the ST sim already casts: secondary-target potency
# (falloff baked in). Credited symmetrically on delivered + ceiling inside
# confirmed multi-target windows. Holy III is an AoE-investment button the ST
# sim does NOT cast, so it stays out (its windows remain disclaimed).
SPLASH_POTENCIES: dict[int, int] = {
    GLARE_IV:         384,   # "40% less for all remaining enemies"
    ASSIZE:           400,   # full potency to all nearby enemies
    AFFLATUS_MISERY:  700,   # "50% less for all remaining enemies"
}


# --- JOB_DATA bundle ---------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="White Mage",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    # The Lily gauge is TIME-generated (1 / 20 s), not cast-generated, so it
    # doesn't fit the cast-economy GaugeModel (which drives the overcap pass).
    # The simulator models the lily timer itself; lily overcap on the delivered
    # side is potency-neutral (Misery == 4 Glare) so there is no overcap card.
    gauges=(),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    burst_abilities=BURST_ABILITIES,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    role_policy=CASTER_HEALER,
    filler_gcd_potency=350,
    # Tincture of Mind. ⚠️ Placeholder: effective BiS Mind incl. party bonus +
    # food, mirrored from the RDM caster value; refine per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat=6838,
)
