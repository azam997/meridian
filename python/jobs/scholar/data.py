"""Scholar data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for SCH numbers: potencies, cast times, cooldowns, and the
opener. SCH is the analyzer's third **healer** (after WHM + AST) and models on the
damage side almost exactly like AST, with one addition — a real offensive gauge:

  * **Filler** — hardcast Broil IV (1.5s cast < 2.5s recast, so the slot is always
    recast-bound) with a Biolysis DoT kept up on a ~30s cadence. Art of War is the
    AoE-only filler; Ruin II is an instant movement filler the sim never picks
    (the ceiling assumes stationary Broil IV). No Misery-analog damage GCD.
  * **oGCD economy** — Chain Stratagem (the party raid buff, also unlocking one
    Baneful Impaction), Baneful Impaction (a short DoT oGCD, folded to one cast),
    and the **Aetherflow gauge** (3 stacks / 60s) spent by Energy Drain.

Three facts shape the model (see jobs/scholar/__init__.py):
  * **Chain Stratagem is a party buff modeled in raid_buffs.py** (BuffProvider,
    1.064/20s, on_enemy) — it flows through `buff_intervals` on both delivered and
    ceiling, so it sits at potency 0 here (realism/cast-diff only) and is NEVER
    re-derived as a self-buff (that would double-count). The AST Divination pattern.
  * **The fairy (Eos/Selene) is HEAL-ONLY** — no pet DPS. Its ids live in
    `DEFENSIVE_IDS`; the model folds NO pet damage (aux is always 0), the opposite
    of SMN/DRK. The pet's stream is simply ignored by the cast-driven scorer.
  * **SCH has NO GCD-recast haste** (Seraphism/Dissipation don't shorten the damage
    GCD recast), so the GCD is flat 2.5s (SpS-scaled) → `demonstrated_cadence_anchor`
    is valid (scoring.py), like AST, the opposite of WHM.

**Aetherflow is modeled as a SimState int, NOT a GaugeModel** (so `gauges=()`, like
AST): Aetherflow is ALSO spent on oGCD heals (Indomitability/Excogitation/Sacred
Soil) the damage table can't see, so an Overcap card would mis-flag a healing SCH
as "wasting" the gauge. The ceiling models all-Aetherflow→Energy-Drain (the honest
damage max); the real heal-diversion tax is a documented known lever, not tuned.
MP is deliberately NOT modeled (only gauges that bind the offensive rotation).

⚠️ CALIBRATION: potencies below are best-known and MUST be verified against a live
pull (scripts/probe_scholar_ids.py) before the ceiling is trusted — the RDM/DRG
id-caveat doctrine. Ability IDs are XIVAPI-verified (name + icon + oGCD category).
Confirmed from python/mitplan/library.py: Concitation id 37013, filler 320 (Broil
IV); from tests/test_buff_windows.py: Chain Stratagem cast id 7436.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, JobData


PATCH_VERSION = "7.2"

# --- Ability IDs (XIVAPI-verified names/icons/category; ⚠️ = probe potency) ---

# Damage rotation
BROIL_IV       = 25865   # 320p, 1.5s cast — the filler nuke (320 from mitplan)
BIOLYSIS       = 16540   # DoT (initial + tick/3s over ~30s), instant, refreshable
ART_OF_WAR     = 25866   # Art of War II (level-82 upgrade of 16539) — AoE nuke GCD, MT only
RUIN_II        = 17870   # instant filler for movement (sim never picks it; ST assumes Broil IV)
CHAIN_STRATAGEM   = 7436  # 120s party-buff oGCD; 0 self-potency; unlocks one Baneful Impaction
BANEFUL_IMPACTION = 37012 # short DoT oGCD, gated by Chain Stratagem (state flag); folded to one cast
ENERGY_DRAIN      = 167   # damage oGCD; spends 1 Aetherflow stack (NOT the SMN id 16508!)
AETHERFLOW        = 166   # 60s resource oGCD; refills 3 Aetherflow stacks; 0 self-potency

# --- Status ids (reference; the sim is cast-driven and never fetches these) --
CHAIN_STRATAGEM_STATUS_ID = 1221   # ⚠️ unverified — unused by the pipeline
IMPACT_STATUS_ID          = 3883   # ⚠️ the Baneful-Impaction-enabling stack
BIOLYSIS_STATUS_ID        = 3089   # ⚠️

# --- Rotation constants ------------------------------------------------------
BROIL_IV_CAST_S: float = 1.5
SCH_GCD_S: float = 2.5
BIOLYSIS_DOT_DURATION_S: float = 30.0
BIOLYSIS_DOT_TICK_S: float = 3.0
BIOLYSIS_DOT_TICK_P: int = 85        # wiki-verified (per-tick, 30s duration)
BIOLYSIS_INITIAL_P: int = 0          # wiki-verified (pure DoT, no initial hit)

# Aetherflow gauge (modeled as a SimState int, not a GaugeModel — see docstring).
AETHERFLOW_STACKS: int = 3
AETHERFLOW_CD_S: float = 60.0

# Baneful Impaction: a fixed ~15s DoT fired once per Chain Stratagem, not
# refreshable/clippable → its ticks are folded into one cast potency (the PLD
# Circle of Scorn pattern), scored at the cast-time multiplier.
BANEFUL_TOTAL_P: int = 700           # wiki-verified (140/tick x 5 ticks over 15s)

# --- Healing / mitigation kit ability ids (non-rotational) -------------------
# GCD heals (also the costed-heal currency) ...
ADLOQUIUM          = 185     # is_gcd shield heal (2.0s cast)
SUCCOR             = 186     # is_gcd AoE shield heal (2.0s cast)
CONCITATION        = 37013   # is_gcd AoE shield heal (Succor upgrade); the LOCKED heal (mitplan)
PHYSICK            = 190     # is_gcd single-target heal
# ... and the oGCD heal/mit + fairy kit
LUSTRATE           = 189     # Aetherflow oGCD heal
INDOMITABILITY     = 3583    # Aetherflow oGCD AoE heal
EXCOGITATION       = 7434    # Aetherflow oGCD heal (from mitplan)
SACRED_SOIL        = 188     # Aetherflow oGCD ground mit (from mitplan)
EXPEDIENT          = 25868   # oGCD raidwide mit (from mitplan)
PROTRACTION        = 25867   # oGCD single-target max-HP buffer (from mitplan)
RECITATION         = 16542   # oGCD (guarantees crit/free Aetherflow on next heal)
DISSIPATION        = 3587    # oGCD Aetherflow refill (sacrifices the fairy) — heal-side
EMERGENCY_TACTICS  = 3586    # oGCD (converts shields to heals)
DEPLOYMENT_TACTICS = 3585    # oGCD (spreads a shield; mitplan HEALER_GCD row)
WHISPERING_DAWN    = 16537   # fairy regen oGCD (from mitplan)
FEY_ILLUMINATION   = 16538   # fairy mit oGCD (from mitplan)
FEY_BLESSING       = 16543   # fairy heal oGCD
CONSOLATION        = 16546   # Seraph heal+barrier oGCD
SUMMON_SERAPH      = 16545   # fairy oGCD (from mitplan)
SERAPHISM          = 37014   # oGCD healer stance (from mitplan)
SUMMON_EOS         = 17215   # fairy summon GCD (heal-only pet)
SUMMON_SELENE      = 17216   # alternate fairy summon GCD
AETHERPACT         = 7423    # oGCD fairy-tether channel

# --- Cast times (s) — feeds the HardcastGCD timing preset -------------------
# Absent ids are instant (Biolysis / Art of War / Ruin II / all oGCDs). Broil IV's
# 1.5s cast is shorter than the 2.5s recast, so the slot is always recast-bound;
# the cast only costs a weave slot. Concitation (2.0s cast) is only ever cast by
# the sim as a mit-plan LOCKED heal — the unlocked rotation never fires it, so
# adding it here leaves every unlocked run byte-identical (the AST/WHM pattern).
CAST_TIMES: dict[int, float] = {
    BROIL_IV:    BROIL_IV_CAST_S,
    CONCITATION: 2.0,   # locked heal GCD (mit-plan integration)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) -----------
# ability_id -> per-extra-target potency. Art of War is full-to-all. Baneful
# Impaction already cleaves and lives in SPLASH_POTENCIES. ⚠️ verify-live.
AOE_POTENCIES: dict[int, int] = {
    ART_OF_WAR: 180,   # wiki-verified (Art of War II)
}

# --- Potencies ---------------------------------------------------------------
# ability_id -> base potency. BIOLYSIS carries only its (probably 0) initial here —
# the DoT is scored per cast by time-to-next-application (scoring.py, the AST
# Combust / WHM Dia pattern) so an early refresh credits less. BANEFUL_IMPACTION
# folds its whole 15s DoT into one cast potency (not refreshable). CHAIN_STRATAGEM
# and AETHERFLOW carry 0 (their value is external — the party buff via
# raid_buffs.py's buff_intervals; Aetherflow is a resource refill). ⚠️ probe values.
POTENCIES: dict[int, int] = {
    BROIL_IV:          320,   # wiki-verified (filler, 1.5s cast)
    BIOLYSIS:          BIOLYSIS_INITIAL_P,
    ART_OF_WAR:        180,   # wiki-verified (Art of War II, AoE)
    RUIN_II:           220,   # wiki-verified (instant movement filler)
    ENERGY_DRAIN:      100,   # wiki-verified
    BANEFUL_IMPACTION: BANEFUL_TOTAL_P,
    CHAIN_STRATAGEM:     0,   # party buff (external buff_intervals); realism only
    AETHERFLOW:          0,   # resource refill; realism only
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under the
# test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    CHAIN_STRATAGEM, BANEFUL_IMPACTION, ENERGY_DRAIN, AETHERFLOW,
})

# --- Cooldowns + charges -----------------------------------------------------
# The genuinely RECAST-gated DPS oGCDs. Baneful Impaction is Chain-Stratagem-gated
# (state flag) and Energy Drain is Aetherflow-gated (gauge), so NEITHER is listed
# (would read as false drift, gotcha #2). Aetherflow (the refill ability) IS a 60s
# recast; Chain Stratagem is the 120s burst anchor.
COOLDOWNS: dict[int, tuple[float, int]] = {
    CHAIN_STRATAGEM: (120.0, 1),
    AETHERFLOW:       (60.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    CHAIN_STRATAGEM: 700,   # ~the Baneful Impaction it enables (+ its party value, external)
    AETHERFLOW:      300,   # ~3 Energy Drains it fuels
}

# --- Canonical opener --------------------------------------------------------
# First ~12 in-fight GCDs (the pre-pull Broil IV channel is separate). Nearly pure
# filler: precast Broil IV lands at t≈0, Biolysis 2nd, then Broil IV fillers with
# Chain Stratagem / Baneful Impaction / Aetherflow / Energy Drain weaving as oGCDs.
# OpenerAspect is a zero-priced diagnostic. ⚠️ refine to the measured M11S
# top-parse consensus during calibration.
CANONICAL_OPENER: tuple[int, ...] = (
    BROIL_IV,
    BIOLYSIS,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
    BROIL_IV,
)

# --- Non-rotational ids ------------------------------------------------------
# The healing/mitigation kit + the heal-only fairy (Eos/Selene) commands — real
# casts with no SCH-self DPS value the simulator never fires. Excluded from the DPS
# timeline + cast-diff (isDefensive on the wire), rendered on the Defensives lane.
# NOT here: Broil IV / Biolysis / Art of War / Ruin II (damage GCDs); Chain
# Stratagem / Baneful Impaction / Energy Drain / Aetherflow (damage + gauge oGCDs).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    ADLOQUIUM, SUCCOR, CONCITATION, PHYSICK, DEPLOYMENT_TACTICS,
    LUSTRATE, INDOMITABILITY, EXCOGITATION, SACRED_SOIL, EXPEDIENT, PROTRACTION,
    RECITATION, DISSIPATION, EMERGENCY_TACTICS,
    WHISPERING_DAWN, FEY_ILLUMINATION, FEY_BLESSING, CONSOLATION, SUMMON_SERAPH,
    SERAPHISM, SUMMON_EOS, SUMMON_SELENE, AETHERPACT,
})

# --- Costed heal GCDs ---------------------------------------------------------
# Hardcast/GCD heals that displace a damage GCD (a Broil IV) when cast during
# uptime — the currency of the mit-plan lock accounting and the "extra healing GCDs
# beyond the plan" improvement card. Deployment Tactics is an oGCD (instant), so it
# does NOT displace a GCD and stays out of this set.
COSTED_HEAL_GCD_IDS: frozenset[int] = frozenset({
    ADLOQUIUM, SUCCOR, CONCITATION, PHYSICK,
})

# --- Burst-alignment abilities ----------------------------------------------
BURST_ABILITIES: frozenset[int] = frozenset({
    CHAIN_STRATAGEM, BANEFUL_IMPACTION, ENERGY_DRAIN,
})

# Enablers whose value is what they unlock, not standalone table potency — Chain
# Stratagem unlocks Baneful Impaction (its party-buff value is external). Priced by
# the sim's marginal contribution.
ENABLER_IDS: tuple[int, ...] = (CHAIN_STRATAGEM,)

# Interchangeable filler GCDs whose under-count vs the ideal is the diffuse "healed
# with a GCD where the ideal casts Broil IV" loss — THE healer story, structurally
# invisible to the cooldown missed-cast diff.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({BROIL_IV})

# --- Multi-target ------------------------------------------------------------
# Free-splash abilities the ST sim already casts: secondary-target potency (falloff
# baked in). Credited symmetrically on delivered + ceiling inside confirmed
# multi-target windows. Baneful Impaction is a full-to-all AoE DoT. Art of War is
# an AoE-investment button the ST sim does NOT cast, so it stays in AOE_POTENCIES.
# ⚠️ probe falloffs.
SPLASH_POTENCIES: dict[int, int] = {
    BANEFUL_IMPACTION: BANEFUL_TOTAL_P,   # ⚠️ full-to-all AoE DoT
}


# --- JOB_DATA bundle ---------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Scholar",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    # No JobData GaugeModel: Aetherflow is modeled as a SimState int in the
    # simulator (see the module docstring) because it is also spent on oGCD heals
    # the damage table can't see, so a GaugeModel/Overcap card would mis-flag a
    # healing SCH. MP never binds the optimized line.
    gauges=(),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    burst_abilities=BURST_ABILITIES,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    role_policy=CASTER_HEALER,
    filler_gcd_potency=320,
    # Tincture of Mind. ⚠️ Placeholder: effective BiS Mind incl. party bonus +
    # food, mirrored from the WHM/AST/RDM caster value; refine per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat=6838,
)
