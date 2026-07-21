"""Astrologian data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for AST numbers: potencies, cast times, cooldowns, and
the opener. AST is the analyzer's second **healer** (after WHM) and the simplest
to model on the damage side:

  * **Filler** — Fall Malefic (hardcast 1.5s < 2.5s recast, so the slot is
    always recast-bound) with a Combust III DoT kept up on a ~30s cadence. There
    is NO banking gauge and NO Misery-analog damage GCD — the GCD line is nearly
    pure filler.
  * **oGCD economy** — the interesting part: Divination (the party raid buff,
    also unlocking one Oracle), Oracle (the burst oGCD), Earthly Star (a placed
    60s oGCD whose Stellar Explosion is the damage), Lord of Crowns (drawn via
    Minor Arcana). None of these gate the GCD line.

Two facts shape the model (see jobs/astrologian/__init__.py):
  * **Divination is a party buff modeled in raid_buffs.py** (BuffProvider,
    1.06/20s) — it flows through `buff_intervals` on both delivered and ceiling,
    so it sits at potency 0 here (realism/cast-diff only) and is NEVER re-derived
    as a self-buff (that would double-count). AST's damage cards (The Balance,
    etc.) are single-target and given to DPS, not self, so they are out of scope.
  * **Lightspeed reduces CAST time only, not the GCD recast** — so AST has NO
    modeled sub-GCD haste window; the GCD is flat 2.5s (SpS-scaled). That makes
    `demonstrated_cadence_anchor` valid (see scoring.py), the opposite of WHM
    (whose Presence of Mind is a real recast haste).

MP is deliberately NOT modeled (only gauges that bind the offensive rotation).

⚠️ CALIBRATION: every id/potency below is best-known and MUST be verified against
a live pull (scripts/probe_astrologian_ids.py) before the ceiling is trusted —
the RDM/DRG id-caveat doctrine. Confirmed from python/mitplan/library.py:
Fall Malefic potency 270 and Helios Conjunction id 37030.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, JobData


PATCH_VERSION = "7.2"

# --- Ability IDs (⚠️ probe every one; only the two noted are pre-verified) ----

# Damage rotation
FALL_MALEFIC   = 25871   # 270p, 1.5s cast — the filler nuke (270 VERIFIED)
COMBUST_III    = 16554   # DoT (initial + tick/3s over ~30s), instant, refreshable
GRAVITY_II     = 25872   # AoE nuke GCD (multi-target only; ST sim never casts it)
DIVINATION     = 16552   # 120s party-buff oGCD; 0 self-potency; unlocks one Oracle
ORACLE         = 37029   # burst damage oGCD; gated by Divination's Divining (state flag)
EARTHLY_STAR   = 7439    # 60s placed oGCD; the Stellar Explosion is the damage
LORD_OF_CROWNS = 7444    # AoE damage oGCD; gated by a drawn Lord (state flag)
MINOR_ARCANA   = 37022   # 60s card-draw oGCD; 0 potency (draws Lord/Lady). Live top
                         # parses cast Lord of Crowns directly on a ~120s cadence and
                         # rarely press this, so the SIM does not use it (see simulator).
STELLAR_DETONATION = 8324  # manual Earthly Star detonate (0 self-cast potency; the
                           # damage is credited on the EARTHLY_STAR place cast)

# Card-draw / ally-buff / heal-card oGCDs — NO self-damage; realism only
ASTRAL_DRAW    = 37017   # 60s card oGCD (toggles with Umbral Draw)
UMBRAL_DRAW    = 37018   # 60s card oGCD
PLAY_I         = 37019
PLAY_II        = 37020
PLAY_III       = 37021
THE_BALANCE    = 37023   # damage card played on a DPS (single-target buff; not self)
THE_EWER       = 37028   # regen card
LADY_OF_CROWNS = 7445    # heal payoff of Minor Arcana (Lady) — no damage
MICROCOSMOS    = 25875   # Macrocosmos detonate (heal); no self-damage
LIGHTSPEED     = 3606    # cast-time reduction only; NO recast haste -> not modeled

# --- Status ids (reference; the sim is cast-driven and never fetches these) --
DIVINATION_STATUS_ID = 1878   # ⚠️ unverified — unused by the pipeline
DIVINING_STATUS_ID   = 3893   # ⚠️ the Oracle-enabling stack
COMBUST_III_STATUS_ID = 1881  # ⚠️

# --- Rotation constants ------------------------------------------------------
FALL_MALEFIC_CAST_S: float = 1.5
AST_GCD_S: float = 2.5
COMBUST_DOT_DURATION_S: float = 30.0
COMBUST_DOT_TICK_S: float = 3.0
COMBUST_DOT_TICK_P: int = 70        # wiki-verified (per-tick, 30s duration)
COMBUST_INITIAL_P: int = 0          # wiki-verified (pure DoT, no initial hit)

# --- Healing / mitigation kit ability ids (non-rotational) -------------------
# ⚠️ ids below the noted-verified line are best-known long-standing ids.
BENEFIC            = 3594
BENEFIC_II         = 3610
ASPECTED_BENEFIC   = 3595
HELIOS             = 3600
ASPECTED_HELIOS    = 3601
HELIOS_CONJUNCTION = 37030   # is_gcd heal, cast 1.5s, gcd_cost 270 (VERIFIED via mitplan)
ESSENTIAL_DIGNITY  = 3614
CELESTIAL_INTERSECTION = 16556
CELESTIAL_OPPOSITION   = 16553
COLLECTIVE_UNCONSCIOUS  = 3613
EXALTATION         = 25873
MACROCOSMOS        = 25874   # (Microcosmos detonate shares the button/id)
NEUTRAL_SECT       = 16559
SUN_SIGN           = 37031
SYNASTRY           = 3612
HOROSCOPE          = 16557
HOROSCOPE_HELIOS   = 16558
THE_BOLE           = 37027
THE_SPIRE          = 37025
THE_ARROW          = 37024
THE_SPEAR          = 37026

# --- Cast times (s) — feeds the HardcastGCD timing preset -------------------
# Absent ids are instant. Fall Malefic's 1.5s cast is shorter than the 2.5s
# recast, so the slot is always recast-bound; the cast only costs a weave slot.
# Helios Conjunction (1.5s cast, still recast-bound) is only ever cast by the sim
# as a mit-plan LOCKED heal — the unlocked rotation never fires it, so adding it
# here leaves every unlocked run byte-identical (the WHM Medica III pattern).
CAST_TIMES: dict[int, float] = {
    FALL_MALEFIC:       FALL_MALEFIC_CAST_S,
    GRAVITY_II:         FALL_MALEFIC_CAST_S,   # AoE filler — same 1.5s cast slot
    HELIOS_CONJUNCTION: 1.5,                   # locked heal GCD (mit-plan integration)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) -----------
# ability_id -> per-extra-target potency. Gravity II is full-to-all. Oracle /
# Lord of Crowns / Earthly Star already cleave and live in SPLASH_POTENCIES.
# ⚠️ verify-live (Phase 5).
AOE_POTENCIES: dict[int, int] = {
    GRAVITY_II: 140,   # wiki-verified (7.2)
}

# --- Potencies ---------------------------------------------------------------
# ability_id -> base potency. COMBUST_III carries only its (probably 0) initial
# here — the DoT is scored per cast by time-to-next-application (scoring.py, the
# WHM Dia / SAM Higanbana pattern) so an early refresh credits less. Divination
# and the card-draw oGCDs carry 0 (their party/ally value is external — Divination
# via raid_buffs.py's buff_intervals; the damage cards go to DPS, not self).
# EARTHLY_STAR is scored at the Stellar Explosion potency (the sim casts it as one
# 60s damage oGCD; v1 collapses the place+detonate two-step). ⚠️ probe all values.
POTENCIES: dict[int, int] = {
    FALL_MALEFIC:    270,   # wiki-verified
    COMBUST_III:     COMBUST_INITIAL_P,
    GRAVITY_II:      140,   # wiki-verified (7.2)
    ORACLE:          860,   # wiki-verified (860 first target)
    EARTHLY_STAR:    310,   # wiki-verified — full-grown Stellar Explosion (was 540)
    LORD_OF_CROWNS:  400,   # wiki-verified (400 AoE, was 250)
    DIVINATION:        0,   # party buff (external buff_intervals); realism only
    MINOR_ARCANA:      0,
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    DIVINATION, ORACLE, EARTHLY_STAR, LORD_OF_CROWNS, MINOR_ARCANA,
})

# --- Cooldowns + charges -----------------------------------------------------
# The genuinely RECAST-gated DPS oGCDs. Oracle is Divining-gated (state flag),
# so it is NOT listed (would read as false drift, gotcha #2). Lord of Crowns is a
# drawn card; live top parses cast it on a ~120s effective cadence, so it is
# modeled as a direct 120s recast (the Minor Arcana 60s-draw model over-produced
# it — a verified live-count fix, gotcha #1).
COOLDOWNS: dict[int, tuple[float, int]] = {
    DIVINATION:     (120.0, 1),
    EARTHLY_STAR:    (60.0, 1),
    LORD_OF_CROWNS: (120.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    DIVINATION:    860,    # ~the Oracle it enables (+ its party value, external)
    EARTHLY_STAR:  310,
    LORD_OF_CROWNS: 400,
}

# --- Canonical opener --------------------------------------------------------
# First ~12 in-fight GCDs (the pre-pull Fall Malefic channel is separate). Nearly
# pure filler: precast Fall Malefic lands at t≈0, Combust 2nd, then Fall Malefic
# fillers with Divination/Oracle/Lord/Star/Minor Arcana weaving as oGCDs.
# OpenerAspect is a zero-priced diagnostic. ⚠️ refine to the measured M11S
# top-parse consensus during calibration.
CANONICAL_OPENER: tuple[int, ...] = (
    FALL_MALEFIC,
    COMBUST_III,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
    FALL_MALEFIC,
)

# --- Non-rotational ids ------------------------------------------------------
# The healing/mitigation kit + the card-draw / ally-buff / heal-card oGCDs
# (Astral/Umbral Draw, Play I-III, Lady of Crowns) — real casts with no AST-self
# DPS value the simulator never fires. Excluded from the DPS timeline + cast-diff
# (isDefensive on the wire). NOT here: Combust/Fall Malefic/Gravity (damage GCDs),
# the damage oGCDs (Divination/Oracle/Earthly Star/Lord of Crowns/Minor Arcana).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    BENEFIC, BENEFIC_II, ASPECTED_BENEFIC, HELIOS, ASPECTED_HELIOS,
    HELIOS_CONJUNCTION, ESSENTIAL_DIGNITY, CELESTIAL_INTERSECTION,
    CELESTIAL_OPPOSITION, COLLECTIVE_UNCONSCIOUS, EXALTATION, MACROCOSMOS,
    MICROCOSMOS, NEUTRAL_SECT, SUN_SIGN, SYNASTRY, HOROSCOPE, HOROSCOPE_HELIOS,
    THE_BOLE, THE_SPIRE, THE_ARROW, THE_SPEAR, THE_BALANCE, THE_EWER,
    ASTRAL_DRAW, UMBRAL_DRAW, PLAY_I, PLAY_II, PLAY_III, MINOR_ARCANA,
    LADY_OF_CROWNS, STELLAR_DETONATION, LIGHTSPEED,
})

# --- Costed heal GCDs ---------------------------------------------------------
# Hardcast/GCD heals that displace a damage GCD (a Fall Malefic) when cast during
# uptime — the currency of the mit-plan lock accounting and the "extra healing
# GCDs beyond the plan" improvement card. (AST has no free instant-heal GCD like
# WHM's lily spends, so all its GCD heals are costed.)
COSTED_HEAL_GCD_IDS: frozenset[int] = frozenset({
    BENEFIC, BENEFIC_II, ASPECTED_BENEFIC, HELIOS, ASPECTED_HELIOS,
    HELIOS_CONJUNCTION,
})

# --- Burst-alignment abilities ----------------------------------------------
BURST_ABILITIES: frozenset[int] = frozenset({
    DIVINATION, ORACLE, LORD_OF_CROWNS, EARTHLY_STAR,
})

# Enablers whose value is what they unlock, not standalone table potency —
# Divination unlocks the Oracle (its party-buff value is external). Priced by the
# sim's marginal contribution.
ENABLER_IDS: tuple[int, ...] = (DIVINATION,)

# Interchangeable filler GCDs whose under-count vs the ideal is the diffuse
# "healed with a GCD where the ideal casts Fall Malefic" loss — THE healer story,
# structurally invisible to the cooldown missed-cast diff.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({FALL_MALEFIC})

# --- Multi-target ------------------------------------------------------------
# Free-splash abilities the ST sim already casts: secondary-target potency
# (falloff baked in). Credited symmetrically on delivered + ceiling inside
# confirmed multi-target windows. Gravity II is an AoE-investment button the ST
# sim does NOT cast, so it stays in AOE_POTENCIES. ⚠️ probe falloffs.
SPLASH_POTENCIES: dict[int, int] = {
    ORACLE:         516,   # ⚠️ probe (~40% falloff)
    LORD_OF_CROWNS: 250,   # ⚠️ full-to-all AoE
    EARTHLY_STAR:   540,   # ⚠️ full-to-all AoE
}


# --- JOB_DATA bundle ---------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Astrologian",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    # No offensive gauge: cards are not a numeric banking gauge, and MP never
    # binds the optimized line. The card/Oracle/Lord economy is modeled as state
    # flags in the simulator, not via the cast-economy GaugeModel.
    gauges=(),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    burst_abilities=BURST_ABILITIES,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    role_policy=CASTER_HEALER,
    filler_gcd_potency=270,
    # Tincture of Mind. ⚠️ Placeholder: effective BiS Mind incl. party bonus +
    # food, mirrored from the WHM/RDM caster value; refine per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat=6838,
)
