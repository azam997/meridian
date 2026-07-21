"""Monk data tables (Dawntrail 7.2+, level 100) + the `JOB_DATA` JobData instance.

Single source of truth for MNK numbers: potencies, the recast-gated cooldowns, the
three Fury gauges, the form/Blitz system constants, and the windowed self-buff
(Riddle of Fire).

What makes MNK structurally distinct from the melee sims already shipped:

  * **The form cycle** — every melee GCD both requires and grants a form
    (opo-opo -> raptor -> coeurl -> opo-opo), and within each form the pick is a
    generator (grants that form's Fury) or a spender (consumes Fury for +200/+150
    potency). Leaping Opo is a GUARANTEED CRIT when opo-eligible (the form bonus)
    — the SAM Setsugekka pattern.
  * **Perfect Balance -> Masterful Blitz** — PB (40s x2 charges) makes the next 3
    weaponskills form-free, each granting a Beast Chakra of its action's form; the
    composition resolves the Blitz: 3 same -> Elixir Burst (Lunar Nadi), 3 distinct
    -> Rising Phoenix (Solar), 2+1 -> Celestial Revolution (the mistake), both Nadi
    lit -> Phantom Rush (1500p). The lunar-vs-solar commitment is the job's GCD
    fork (the SAM/DRG beam pattern).
  * **Riddle of Fire** — a +15% self window (measured 20.7s in-game / 60s) derived
    from the player's own casts and folded into the incremental beam score (the
    GNB No-Mercy pattern, NOT a coverage overlay). Brotherhood's +5% is the PARTY
    buff and lives in the shared raid-buff catalog (jobs/_core/raid_buffs.py).
  * **Chakra is a BUDGET, not a gauge** (the DNC pattern): generation is crit-RNG
    (Deep Meditation II) + party-fed (Meditative Brotherhood 20%/party GCD), both
    invisible to the cast stream — so the ceiling spends the player's MEASURED
    The Forbidden Chakra count via `sim_context` and below-average luck never
    costs efficiency.
  * **Downtime** — Monks pump chakra with Forbidden Meditation (1s GCD, no target
    needed) and re-arm Formless Fist with Form Shift during downtime; both are
    modeled in `on_downtime_window` (the NIN mudra-priming pattern).

⚠️ ACTION + STATUS IDS — VERIFIED 2026-07-01 against live top-Monk M11S/M12S-P1
logs' masterData.abilities (scripts/probe_monk_ids.py). POTENCIES are DT 7.2
level-100 values verified on ffxiv.consolegameswiki.com per-action pages (cross-
checked vs the official job guide) and against the logs' damage `bonusPercent`
bytes: Demolish rear-hit bp=14 == 60/420, Pouncing Coeurl flank-hit bp=11 ==
60/520 under Coeurl's Fury — the Fury bonus raises base AND total (the NIN
Kazematoi byte convention), and the wiki splits reproduce the wire bytes exactly.
Leaping Opo measured 100% crit across every probed player (the form bonus).
Positional abilities carry the positional-HIT value (assume-always-hit, the RPR
convention); Fury bonuses are state-derived in scoring/simulator.
"""
from __future__ import annotations

from dataclasses import replace

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) — VERIFIED from live logs ---------------

# Form GCDs (generator / spender per form; every one requires + grants a form)
DRAGON_KICK     = 74      # opo generator: grants 1 Opo-opo's Fury (form-gated)
LEAPING_OPO     = 36945   # opo spender: +200 under Fury; GUARANTEED CRIT in form
TWIN_SNAKES     = 61      # raptor generator: grants 1 Raptor's Fury
RISING_RAPTOR   = 36946   # raptor spender: +200 under Fury
DEMOLISH        = 66      # coeurl generator: grants 2 Coeurl's Fury; REAR positional
POUNCING_COEURL = 36947   # coeurl spender: +150 under Fury; FLANK positional

# Special GCD weaponskills
SIX_SIDED_STAR  = 16476   # 780p (+80/chakra unmodeled), 4s recast, closes all chakra
WINDS_REPLY     = 36949   # 1040p line AoE; needs Wind's Rumination (Riddle of Wind)
FIRES_REPLY     = 36950   # 1400p AoE; needs Fire's Rumination (RoF); grants Formless
FORM_SHIFT      = 4262    # 0p; grants Formless Fist (30s) — pre-pull / downtime
FORBIDDEN_MEDITATION = 36942  # 0p, 1s GCD-linked recast; +1 chakra (downtime pump)

# Masterful Blitz resolutions (the button logs as the resolved blitz)
ELIXIR_BURST    = 36948   # 3 same Beast Chakra -> 900p, opens the Lunar Nadi
RISING_PHOENIX  = 25768   # 3 distinct -> 900p, opens the Solar Nadi
CELESTIAL_REVOLUTION = 25765  # 2+1 mix -> 600p (the mistake; top logs: zero casts)
PHANTOM_RUSH    = 25769   # both Nadi lit -> 1500p, consumes both

# AoE GCD weaponskills (dedicated AoE — the ST sim never casts these; kept for
# delivered-side credit + timeline naming on multi-target pulls)
SHADOW_OF_THE_DESTROYER = 25767  # opo AoE, 120p, guaranteed crit in form
FOUR_POINT_FURY = 16473          # raptor AoE, 140p
ROCKBREAKER     = 70             # coeurl AoE, 150p

# oGCDs
THE_FORBIDDEN_CHAKRA = 3547   # 400p; consumes 5 chakra (gauge-gated, 1s recast)
ENLIGHTENMENT   = 16474       # 160p line AoE; consumes 5 chakra
PERFECT_BALANCE = 69          # 40s x2 charges; 3 form-free GCDs -> Beast Chakra
RIDDLE_OF_FIRE  = 7395        # 60s; +15% self-damage 20.7s + Fire's Rumination
RIDDLE_OF_WIND  = 25766       # 90s; auto-attack haste (unmodeled) + Wind's Rumination
BROTHERHOOD     = 7396        # 120s; the PARTY +5% (shared catalog) + chakra feed

# Defensive / movement / utility (no DPS value; sim never casts)
MANTRA          = 65      # 90s; party heal potency buff
RIDDLE_OF_EARTH = 7394    # 120s; 20% mitigation -> Earth's Reply
EARTHS_REPLY    = 36944   # the Riddle of Earth follow-up heal
THUNDERCLAP     = 25762   # 30s x3 charges; gap closer

DEFENSIVE_IDS: frozenset[int] = frozenset({
    MANTRA, RIDDLE_OF_EARTH, EARTHS_REPLY, THUNDERCLAP,
})

# --- Status (buff) IDs — verified from live player-aura streams --------------
OPO_FORM_STATUS_ID       = 1000107
RAPTOR_FORM_STATUS_ID    = 1000108
COEURL_FORM_STATUS_ID    = 1000109
PERFECT_BALANCE_STATUS_ID = 1000110
FORMLESS_FIST_STATUS_ID  = 1002513
RIDDLE_OF_FIRE_STATUS_ID = 1001181
RIDDLE_OF_WIND_STATUS_ID = 1002687
FIRES_RUMINATION_STATUS_ID = 1003843
WINDS_RUMINATION_STATUS_ID = 1003842
BROTHERHOOD_STATUS_ID    = 1001185   # the party window (shared catalog watches it)
MEDITATIVE_BROTHERHOOD_STATUS_ID = 1001182

# --- Self-buff / mechanic constants ------------------------------------------
# Riddle of Fire: tooltip 20s, measured 20.71-20.80s in-game across 6 top parses
# (the GNB in-game-expiry lesson) — use the measured low end, symmetric on both
# sides so a boundary GCD is never credited on one side only.
RIDDLE_OF_FIRE_MULT: float        = 1.15
RIDDLE_OF_FIRE_DURATION_S: float  = 20.7
FIRES_RUMINATION_DURATION_S: float = 20.0
WINDS_RUMINATION_DURATION_S: float = 15.0
FORMLESS_DURATION_S: float        = 30.0
FORM_DURATION_S: float            = 30.0
PB_STACK_DURATION_S: float        = 20.0
PB_STACKS: int                    = 3

# Fury potency bonuses (wiki-verified: 220->420 / 260->460 opo, 300->500 /
# 340->540 raptor, 270->420 / 310->460 coeurl — +200 / +200 / +150).
OPO_FURY_BONUS_P: int    = 200
RAPTOR_FURY_BONUS_P: int = 200
COEURL_FURY_BONUS_P: int = 150

# Leaping Opo's guaranteed critical hit (the opo form bonus — measured 100% crit
# on every probed live parse). Same empirical crit-only multiplier SAM/DRG use
# (scripts/calibrate_crit_dh.py).
GUARANTEED_CRIT_MULT: float = 1.62
# Guaranteed-crit ids, gated on opo-eligibility in the scoring pass.
ALWAYS_CRIT_IDS: frozenset[int] = frozenset({LEAPING_OPO, SHADOW_OF_THE_DESTROYER})

# The Forbidden Chakra: 5 chakra -> 400p => 80 p/chakra.
TFC_CHAKRA_COST: int = 5

# --- Potencies ----------------------------------------------------------------
# ability_id -> base potency. Positionals carry the positional-HIT value; the
# Fury bonuses (+200/+200/+150) and the Leaping Opo crit are STATE-DERIVED in
# scoring/simulator (symmetric on both sides). Six-Sided Star's +80/chakra is
# intentionally unmodeled (flat 780 both sides — the chakra budget can't see
# per-cast open chakra; symmetric, so the ratio holds).
POTENCIES: dict[int, int] = {
    # Form GCDs
    DRAGON_KICK:     320,
    LEAPING_OPO:     260,   # +200 under Opo-opo's Fury (state-derived); crit x1.62
    TWIN_SNAKES:     420,
    RISING_RAPTOR:   340,   # +200 under Raptor's Fury
    DEMOLISH:        420,   # rear-hit value (miss 360)
    POUNCING_COEURL: 370,   # flank-hit value (miss 310); +150 under Coeurl's Fury
    # Special GCDs
    SIX_SIDED_STAR:  780,
    WINDS_REPLY:    1040,
    FIRES_REPLY:    1400,
    FORM_SHIFT:        0,
    FORBIDDEN_MEDITATION: 0,
    # Blitzes
    ELIXIR_BURST:    900,
    RISING_PHOENIX:  900,
    CELESTIAL_REVOLUTION: 600,
    PHANTOM_RUSH:   1500,
    # Dedicated AoE (delivered credit + naming only; ST sim never casts)
    SHADOW_OF_THE_DESTROYER: 120,
    FOUR_POINT_FURY: 140,
    ROCKBREAKER:     150,
    # oGCDs
    THE_FORBIDDEN_CHAKRA: 400,
    ENLIGHTENMENT:   160,
    PERFECT_BALANCE:   0,
    RIDDLE_OF_FIRE:    0,
    RIDDLE_OF_WIND:    0,
    BROTHERHOOD:       0,
}

# Non-positional ("missed") potency for the positional GCDs, cross-checked against
# the live bonusPercent bytes (Demolish hit bp=14 == 60/420; Pouncing Coeurl hit
# bp=11 == 60/520 with Fury — the byte's base includes the Fury bonus, so the
# positional delta is a clean 60 on both). Idealized always uses the hit value.
POSITIONAL_MISS_POTENCY: dict[int, int] = {
    DEMOLISH:        360,
    POUNCING_COEURL: 310,
}
POSITIONAL_IDS: frozenset[int] = frozenset(POSITIONAL_MISS_POTENCY)
# A positional HIT pushes the bonus byte to 11 (PC) / 14 (Demolish); a miss carries
# no byte (no combo bonus exists to keep it present, unlike DRG). Threshold between.
POSITIONAL_HIT_MIN_BP: int = 6

# oGCD set — kept job-local so the sim's weave logic and scoring's GCD/oGCD split
# stay hermetic under the test stub. Everything else in POTENCIES is a GCD.
# (Forbidden Meditation is typed "Ability" in XIVAPI but triggers the GCD — it is
# a GCD here, the §2 is_ogcd correction.)
OGCD_IDS: frozenset[int] = frozenset({
    THE_FORBIDDEN_CHAKRA, ENLIGHTENMENT, PERFECT_BALANCE, RIDDLE_OF_FIRE,
    RIDDLE_OF_WIND, BROTHERHOOD, MANTRA, RIDDLE_OF_EARTH, EARTHS_REPLY,
    THUNDERCLAP,
})

# Form families (shared by the sim picker and the scoring forward pass).
OPO_GCD_IDS: frozenset[int]    = frozenset({DRAGON_KICK, LEAPING_OPO,
                                            SHADOW_OF_THE_DESTROYER})
RAPTOR_GCD_IDS: frozenset[int] = frozenset({TWIN_SNAKES, RISING_RAPTOR,
                                            FOUR_POINT_FURY})
COEURL_GCD_IDS: frozenset[int] = frozenset({DEMOLISH, POUNCING_COEURL,
                                            ROCKBREAKER})
FORM_GCD_IDS: frozenset[int] = OPO_GCD_IDS | RAPTOR_GCD_IDS | COEURL_GCD_IDS
BLITZ_IDS: frozenset[int] = frozenset({
    ELIXIR_BURST, RISING_PHOENIX, CELESTIAL_REVOLUTION, PHANTOM_RUSH,
})
# Blitzes + Fire's Reply + Form Shift grant Formless Fist (verified live: every
# blitz's Formless was consumed by the following opo GCD).
FORMLESS_GRANT_IDS: frozenset[int] = BLITZ_IDS | frozenset({FIRES_REPLY, FORM_SHIFT})

# --- Cooldowns + charges ------------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only genuinely RECAST-gated actions.
# The Forbidden Chakra is chakra-gated (budget), Masterful Blitz is Beast-Chakra-
# gated, the replies are Rumination-gated — none belong here (gotcha #2).
COOLDOWNS: dict[int, tuple[float, int]] = {
    PERFECT_BALANCE: (40.0, 2),
    RIDDLE_OF_FIRE:  (60.0, 1),
    RIDDLE_OF_WIND:  (90.0, 1),
    BROTHERHOOD:    (120.0, 1),
}

# Per-cast value for the cooldown-drift detector (lost potential if skipped).
# Enabler improvements are priced by the sim's marginal values, not these.
COOLDOWN_VALUE_P: dict[int, int] = {
    PERFECT_BALANCE: 1100,   # a 900 blitz + the form-free Fury-optimal fillers
    RIDDLE_OF_FIRE:  1500,   # +15% over ~20.7s + the 1400 Fire's Reply enable
    RIDDLE_OF_WIND:  1040,   # the Wind's Reply enable (auto haste unmodeled)
    BROTHERHOOD:      700,   # the chakra feed (party +5% lives in the shared catalog)
}

# --- Fury gauges (the visible resource economy) --------------------------------
# Chakra is deliberately NOT a gauge: its generators (weaponskill crits, party
# GCDs under Meditative Brotherhood) are invisible to the cast stream, so an
# overcap detector would only invent waste. The three Fury stacks ARE fully
# cast-derived: a generator cast at cap is a real wasted grant (+200/+150 the
# next spender won't get).
OPO_FURY_GENERATORS: dict[int, int]    = {DRAGON_KICK: 1}
OPO_FURY_SPENDERS: dict[int, int]      = {LEAPING_OPO: 1}
RAPTOR_FURY_GENERATORS: dict[int, int] = {TWIN_SNAKES: 1}
RAPTOR_FURY_SPENDERS: dict[int, int]   = {RISING_RAPTOR: 1}
COEURL_FURY_GENERATORS: dict[int, int] = {DEMOLISH: 2}
COEURL_FURY_SPENDERS: dict[int, int]   = {POUNCING_COEURL: 1}

# --- Per-ability GCD recast (multiple of the standard hasted ~2.0s GCD) --------
# MNK's Greased Lightning trait puts the standard weaponskill at 2.0s; Six-Sided
# Star runs its own 4s slot (2.0x) and Forbidden Meditation a 1s GCD-linked
# recast (0.5x). The idle/clip detector scales the run's standard effective GCD
# by these so an SSS gap isn't read as idle and a Meditation chain isn't read as
# clipping.
GCD_RECAST_MULT: dict[int, float] = {
    SIX_SIDED_STAR: 2.0,
    FORBIDDEN_MEDITATION: 0.5,
}
CLIP_EXCLUSIONS: frozenset[int] = frozenset({FORBIDDEN_MEDITATION})

# --- Burst-alignment abilities (AlignmentAspect watches these) -----------------
BURST_ABILITIES: frozenset[int] = frozenset({
    RIDDLE_OF_FIRE, BROTHERHOOD, PERFECT_BALANCE, RIDDLE_OF_WIND,
})

# Enablers whose value is throughput, not standalone table potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (
    RIDDLE_OF_FIRE, BROTHERHOOD, PERFECT_BALANCE, RIDDLE_OF_WIND,
)

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) -----------
# MEASURED consensus from live top parses (M11S/M12S-P1, 2026-07-01): pre-pull
# Form Shift + Meditation x5 -> Dragon Kick (Formless) with Perfect Balance
# weaved -> the double-lunar PB window (LO/DK/LO -> Elixir Burst) under
# Brotherhood + Riddle of Fire -> Wind's / Fire's Reply -> the second PB.
CANONICAL_OPENER: tuple[int, ...] = (
    DRAGON_KICK,
    LEAPING_OPO,
    DRAGON_KICK,
    LEAPING_OPO,
    ELIXIR_BURST,
    DRAGON_KICK,
    WINDS_REPLY,
    FIRES_REPLY,
    LEAPING_OPO,
    DRAGON_KICK,
    LEAPING_OPO,
    DRAGON_KICK,
)


# --- JOB_DATA bundle ------------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Monk",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="opo_fury",
            generators=OPO_FURY_GENERATORS,
            spenders=OPO_FURY_SPENDERS,
            cap=1,
            value_p_per_unit=float(OPO_FURY_BONUS_P),
        ),
        GaugeModel(
            name="raptor_fury",
            generators=RAPTOR_FURY_GENERATORS,
            spenders=RAPTOR_FURY_SPENDERS,
            cap=1,
            value_p_per_unit=float(RAPTOR_FURY_BONUS_P),
        ),
        GaugeModel(
            name="coeurl_fury",
            generators=COEURL_FURY_GENERATORS,
            spenders=COEURL_FURY_SPENDERS,
            cap=2,
            value_p_per_unit=float(COEURL_FURY_BONUS_P),
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=frozenset(),
    gcd_recast_mult=GCD_RECAST_MULT,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),
    charge_sharing={},
    # Free-splash: ST-rotation abilities that incidentally cleave with a -35%
    # falloff (wiki-verified). The ST sim already casts them, so window-gated
    # splash credit is symmetric on delivered + ceiling (the >100% guard holds).
    # The dedicated AoE line (Rockbreaker / Four-point Fury / SotD / Enlightenment)
    # is not modeled in v1, so those windows stay disclaimed (DRG/GNB/NIN precedent).
    splash_potencies={
        WINDS_REPLY:    676,   # 1040 x 0.65
        FIRES_REPLY:    910,   # 1400 x 0.65
        ELIXIR_BURST:   585,   # 900 x 0.65
        RISING_PHOENIX: 585,
        PHANTOM_RUSH:   975,   # 1500 x 0.65
    },
    raid_buffs={},          # Brotherhood modeled via the job-agnostic buff catalog
    # Melee-DPS Tier-B tuning, but a FAST measured engage: every probed top Monk's
    # first GCD lands at 0.58-0.71s (Thunderclap-assisted run-in; there is no
    # pre-pull channel to hide behind, unlike RPR Harpe) — the fast end protects
    # the <=100% guard (a 1.0s start would begin the ceiling a third of a GCD
    # late and let tight parses edge over).
    role_policy=replace(MELEE_DPS, engage_delay_s=0.5),
    # A dropped high-potency GCD backfills with a form GCD (~320-540).
    filler_gcd_potency=420,
    # Clipped-instant pre-pull cast: Form Shift during the countdown leaves only
    # its surviving Formless Fist buff (the MCH Reassemble pattern); the analyzer
    # reconstructs the cast in the Timeline's pre-pull zone.
    prepull_buff_ids={FORM_SHIFT: FORMLESS_FIST_STATUS_ID},
    # The positional detector reads the player's DamageDone stream EVERY pull —
    # fold it into the per-pull prefetch bundle (the RPR/DRG pattern) instead of
    # paying a separate round trip.
    prebundle_damage_done=True,
    # Tincture: melee-DPS Strength (party-comp-inclusive, from xivgear) — live top
    # parses use Grade 4 Gemdraughts of Strength. Same value RPR/SAM/DRG use.
    tincture_main_stat=6841,
    # No ranged_filler_id: Monk bridges forced disconnects with Thunderclap +
    # Six-Sided Star (a real 780p GCD, credited on delivered), so disengages are
    # near-seamless rather than an RPR-Harpe genuine loss. If M11S calibration
    # shows structural looseness on disconnect-heavy fights, the SSS-consensus
    # window pattern is the lever — lenient-only, never strict.
)
