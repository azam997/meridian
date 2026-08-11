"""Samurai data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

SAM was the analyzer's data-only smoke test; this rewrite gives it a real
idealized simulator (its sixth). Single source of truth for SAM numbers:
potencies, the recast-gated cooldowns, the Kenki economy, the Sen / Iaijutsu
chain, and the maintained self-buffs.

Three things make SAM structurally distinct from the DPS sims already shipped:

  * **Fugetsu is a MAINTAINED self-buff** (a 13% personal-damage amp from Jinpu/
    Gekko, kept ~100% uptime) — the SAM analog of WAR's Surging Tempest / RPR's
    Death's Design. Modeled as a `coverage_intervals` overlay (scoring.py): the
    idealized side assumes full coverage; the delivered side is scaled by the
    *measured* coverage from the player's own damage events (jobs/samurai/buffs.py).
    Its sibling **Fuka** (a 13% haste buff, also kept ~100%) is baked into the GCD
    base instead (a dropped Fuka shows up as fewer delivered casts).
  * **Tengentsu Kenki.** The defensive Tengentsu grants +10 Kenki when it blocks a
    hit. A live top-10 probe (scripts/probe_samurai_tengentsu.py) found EVERY top
    SAM does this (0/30 pulls with zero procs, avg ~16.8 procs ≈ 168 Kenki ≈ 1.1%
    of fight potency) — but *where* a survivable hit lands is encounter-specific
    and not in a player's own log, so it can't be simmed from first principles.
    Instead the player's measured proc count rides in as the idealized ceiling's
    `sim_context` (the RDM proc-budget pattern): the ceiling spends the *same*
    Kenki the player got, so it's symmetric (preserves the <=100% guard) without
    needing the boss damage timeline. See scoring.py.
  * **Guaranteed critical hits.** Midare / Tendo Setsugekka (+ their Kaeshi) and
    Ogi / Kaeshi Namikiri always land a critical hit (crit only, NOT direct hit —
    verified on the console wiki). Priced with a flat crit multiplier in
    scoring.py (the crit-only analog of WAR's guaranteed crit-DH), symmetric on
    delivered + idealized.

⚠️ ACTION IDS were probed from a real top-SAM pull's `masterData.abilities`
(scripts/probe_samurai_ids.py, report c6VWJQCFzRgt3pPX) — the scaffold's were
wrong (id collisions). POTENCIES are DT 7.x level-100 values cross-checked on
ffxiv.consolegameswiki.com. Re-verify the crit multiplier + tincture per gear
tier via scripts/calibrate_crit_dh.py / calibrate_tincture.py.
"""
from __future__ import annotations

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100; probed from masterData.abilities) ------

# Main combo (single target). Gyofu is the Lv92 upgrade of Hakaze.
GYOFU              = 36963     # combo starter; +5 Kenki
JINPU              = 7478      # 2nd; +5 Kenki; grants Fugetsu (+13% damage)
GEKKO              = 7481      # ender; +10 Kenki; 1 Getsu Sen; rear positional
SHIFU              = 7479      # 2nd; +5 Kenki; grants Fuka (+13% haste)
KASHA              = 7482      # ender; +10 Kenki; 1 Ka Sen; flank positional
YUKIKAZE           = 7480      # ender; +15 Kenki; 1 Setsu Sen

# Iaijutsu (Sen-consuming GCDs)
HIGANBANA          = 7489      # 1 Sen; initial hit + 60s DoT (scored separately)
MIDARE_SETSUGEKKA  = 7487      # 3 Sen; guaranteed crit
TENDO_SETSUGEKKA   = 36966     # 3 Sen; guaranteed crit; enhanced (needs Tendo)
# Tsubame-Gaeshi follow-ups (replay the last Iaijutsu)
KAESHI_SETSUGEKKA      = 16486   # after Midare; guaranteed crit
TENDO_KAESHI_SETSUGEKKA = 36968  # after Tendo Setsugekka; guaranteed crit

# Ikishoten line
IKISHOTEN          = 16482     # 120s; +50 Kenki; grants Ogi Namikiri Ready + Zanshin Ready
OGI_NAMIKIRI       = 25781     # GCD; guaranteed crit (via Ogi Namikiri Ready)
KAESHI_NAMIKIRI    = 25782     # GCD; guaranteed crit (follow-up to Ogi)
ZANSHIN            = 36964     # oGCD; 50 Kenki (via Zanshin Ready)

# Kenki / Meditation finishers
MEIKYO_SHISUI      = 7499      # 55s / 2 charges; 3 instant combo enders + grants Tendo
HISSATSU_SHINTEN   = 7490      # oGCD; 25 Kenki spender
HISSATSU_SENEI     = 16481     # oGCD; 25 Kenki; 60s recast; burst spender
SHOHA              = 16487     # oGCD; spends 3 Meditation
# Downtime resource bank (channel; gains Kenki + Meditation per tick)
MEDITATE           = 7497      # oGCD channel — downtime-only (top players Meditate
                               # through phase gaps; 10 Kenki + 1 Meditation / 3s tick)
MEDITATE_KENKI_PER_TICK     = 10
MEDITATE_MEDITATION_PER_TICK = 1
MEDITATE_TICK_S             = 3.0
MEDITATE_MAX_TICKS          = 5    # 15s full channel
# Movement / ranged filler (not part of the uptime rotation)
ENPI               = 7486      # ranged GCD; +10 Kenki
HISSATSU_GYOTEN    = 7492      # oGCD gap-closer (movement; not scored)
HISSATSU_YATEN     = 7493      # oGCD backstep; grants Enhanced Enpi (movement)

# --- AoE abilities (cast only in multi-target windows; gated on N>=2) --------
# The AoE combo is 2-step (Fuko -> Mangetsu/Oka) and only ever builds Getsu + Ka,
# so the AoE Iaijutsu is the 2-Sen Tenka/Tendo Goken (never the 3-Sen Midare).
# Mangetsu/Oka grant Fugetsu/Fuka exactly like Jinpu/Shifu, so the AoE rotation
# keeps the Fugetsu coverage overlay valid. All full-to-all (no falloff). NOT
# guaranteed crits (only the Setsugekka / Namikiri families crit — verified on the
# console wiki, 2026-06-22). ⚠️ IDs are the canonical XIVAPI action ids; re-verify
# against a live AoE pull via scripts/probe_samurai_ids.py (same caveat as the ST set).
FUKO                = 25780    # AoE combo starter; +10 Kenki; no Sen, no buff
MANGETSU            =  7484    # AoE ender (after Fuko / Meikyo); +10 Kenki; Getsu; Fugetsu
OKA                 =  7485    # AoE ender (after Fuko / Meikyo); +10 Kenki; Ka; Fuka
TENKA_GOKEN         =  7488    # 2-Sen AoE Iaijutsu
TENDO_GOKEN         = 36965    # 2-Sen AoE Iaijutsu (Tendo-enhanced, Lv100); XIVAPI-verified
KAESHI_GOKEN        = 16485    # Tsubame replay of Tenka Goken
TENDO_KAESHI_GOKEN  = 36967    # Tsubame replay of Tendo Goken; XIVAPI-verified (36969 was Sacrificium, a RPR action)

# Defensive (NO damage value; excluded from the DPS diff via isDefensive)
TENGENTSU          = 36962     # 15s; the +10-Kenki-on-hit defensive (Lv82 Third Eye upgrade)
THIRD_EYE          = 7498      # pre-Lv82 Tengentsu (kept for id completeness)

DEFENSIVE_IDS: frozenset[int] = frozenset({TENGENTSU, THIRD_EYE})

# --- Status (buff) IDs ------------------------------------------------------
FUGETSU_STATUS_ID            = 1001298   # +13% damage (the coverage overlay)
FUGETSU_MULT: float          = 1.13
FUKA_STATUS_ID               = 1001299   # +13% haste (baked into the GCD base)
TENGENTSU_STATUS_ID          = 1003853   # the 4s pre-hit defensive window
TENGENTSU_FORESIGHT_STATUS_ID = 1003854  # applied on a successful block -> +10 Kenki
TENDO_STATUS_ID              = 1003856   # enhanced-Iaijutsu state (from Meikyo)
TENGENTSU_KENKI_PER_PROC     = 10

# --- Potencies --------------------------------------------------------------
# action_id -> base potency (no buffs / crit modeling). Combo abilities carry
# their COMBO'd value; positional GCDs (Gekko/Kasha) carry the positional-hit
# value (the sim always lands positionals). Guaranteed-crit GCDs carry their raw
# potency — the crit multiplier is applied in scoring. Higanbana is the initial
# hit only; its DoT is scored separately (see scoring._higanbana_dot_potency).

POTENCIES: dict[int, int] = {
    GYOFU:                    240,
    JINPU:                    300,   # combo'd
    GEKKO:                    420,   # combo'd + rear positional
    SHIFU:                    300,   # combo'd
    KASHA:                    420,   # combo'd + flank positional
    YUKIKAZE:                 340,   # combo'd
    HIGANBANA:                200,   # initial hit; +50p/3s DoT folded in scoring
    MIDARE_SETSUGEKKA:        680,   # guaranteed crit
    TENDO_SETSUGEKKA:        1100,   # guaranteed crit
    KAESHI_SETSUGEKKA:        680,   # guaranteed crit
    TENDO_KAESHI_SETSUGEKKA: 1100,   # guaranteed crit
    OGI_NAMIKIRI:            1000,   # guaranteed crit
    KAESHI_NAMIKIRI:        1000,   # guaranteed crit
    HISSATSU_SHINTEN:         250,
    HISSATSU_SENEI:           800,
    ZANSHIN:                  940,
    SHOHA:                    640,
    ENPI:                     100,
    # AoE GCDs — PRIMARY (first-target) potency. The per-extra-target value lives
    # in AOE_POTENCIES (full-to-all -> secondary == primary). Not guaranteed crits.
    # Mangetsu/Oka carry their COMBO'd potency (120, after Fuko / a Meikyo ender) —
    # the same convention as the ST enders (Jinpu 300, Gekko 420); they only ever
    # land combo'd in the AoE rotation. Fuko is the combo STARTER (100, no bonus).
    FUKO:                     100,
    MANGETSU:                 120,
    OKA:                      120,
    TENKA_GOKEN:              300,
    TENDO_GOKEN:              410,
    KAESHI_GOKEN:             300,
    TENDO_KAESHI_GOKEN:       410,
}

# Per-extra-target potency for the dedicated AoE buttons (full-to-all: each extra
# target adds the same potency as the first, so secondary == primary). Read by
# aoe_potency.potency_for; absent abilities stay single-target. Symmetric on the
# delivered side + the AoE-aware ceiling.
AOE_POTENCIES: dict[int, int] = {
    FUKO:               100,
    MANGETSU:           120,
    OKA:                120,
    TENKA_GOKEN:        300,
    TENDO_GOKEN:        410,
    KAESHI_GOKEN:       300,
    TENDO_KAESHI_GOKEN: 410,
}

# Weaponskills that land a GUARANTEED CRITICAL HIT (crit only, no direct hit —
# verified per-ability on the console wiki). Priced x GUARANTEED_CRIT_MULT in
# scoring, symmetric on delivered + idealized.
ALWAYS_CRIT_IDS: frozenset[int] = frozenset({
    MIDARE_SETSUGEKKA, TENDO_SETSUGEKKA, KAESHI_SETSUGEKKA,
    TENDO_KAESHI_SETSUGEKKA, OGI_NAMIKIRI, KAESHI_NAMIKIRI,
})

# Crit-only multiplier at current gear (= the tier crit_mult; WAR's guaranteed
# crit-DH 2.03 / the fixed DH 1.25 = 1.62). ⚠️ recompute per gear tier via
# scripts/calibrate_crit_dh.py (it prints crit_mult); crit scales slowly.
GUARANTEED_CRIT_MULT: float = 1.62

# Higanbana DoT (snapshots buffs at cast; scored by time-to-next-cast, capped at
# the 60s duration, so an early refresh credits less — symmetric overcap-safe).
HIGANBANA_DOT_TICK_P: int = 50
HIGANBANA_DOT_TICK_S: float = 3.0
HIGANBANA_DOT_DURATION_S: float = 60.0

# oGCD set — kept job-local (not read from XIVAPI) so the sim's weave logic and
# scoring's GCD/oGCD handling stay hermetic under the test stub. Everything else
# in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    HISSATSU_SHINTEN, HISSATSU_SENEI, ZANSHIN, SHOHA, IKISHOTEN, MEIKYO_SHISUI,
    TENGENTSU, HISSATSU_GYOTEN, HISSATSU_YATEN, MEDITATE,
})

# --- Kenki gauge (0-100) ----------------------------------------------------
# Generated by weaponskills + Ikishoten (+ Tengentsu blocks, threaded in as
# sim_context — NOT a generator here). Spent by Shinten/Senei (25) and Zanshin
# (50). Overcapping is wasted potency.

KENKI_GENERATORS: dict[int, int] = {
    GYOFU: 5, JINPU: 5, SHIFU: 5, GEKKO: 10, KASHA: 10, YUKIKAZE: 15,
    ENPI: 10, IKISHOTEN: 50,
    # AoE combo (Fuko / Mangetsu / Oka each +10 Kenki).
    FUKO: 10, MANGETSU: 10, OKA: 10,
}
KENKI_SPENDERS: dict[int, int] = {
    HISSATSU_SHINTEN: 25, HISSATSU_SENEI: 25, ZANSHIN: 50,
}
KENKI_CAP = 100
# Fungible marginal Kenki = one Shinten (250p / 25). The premium spends (Senei /
# Zanshin) are recast/proc-limited, so a raw Kenki unit is worth the Shinten floor.
KENKI_VALUE_P_PER_UNIT: float = 10.0

# --- Cooldowns (recast_s, max_charges) -------------------------------------
# Only RECAST-gated actions live here. The Sen-/Meditation-/proc-gated buttons
# (Iaijutsu, Shoha, Ogi Namikiri, Zanshin) are state-gated, so listing them would
# read as false drift — they're modeled as state flags in the simulator.

COOLDOWNS: dict[int, tuple[float, int]] = {
    MEIKYO_SHISUI:  (55.0, 2),
    IKISHOTEN:     (120.0, 1),
    HISSATSU_SENEI: (60.0, 1),
}

COOLDOWN_VALUE_P: dict[int, int] = {
    MEIKYO_SHISUI: 1300,   # ~2 Tendo Setsugekka shift (priced via enablers)
    IKISHOTEN:     1900,   # Ogi + Kaeshi Namikiri + Zanshin (priced via enablers)
    HISSATSU_SENEI: 800,
}

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) --------
# ⚠️ approximate DT 7.x ST opener (Fugetsu-first). Refine against a current guide.
CANONICAL_OPENER: tuple[int, ...] = (
    GYOFU, JINPU, GEKKO, SHIFU, KASHA, YUKIKAZE,
    MIDARE_SETSUGEKKA, KAESHI_SETSUGEKKA,
    OGI_NAMIKIRI, KAESHI_NAMIKIRI, GYOFU, JINPU,
)

# --- Detection exclusions ---------------------------------------------------
CLIP_EXCLUSIONS: frozenset[int] = frozenset()   # no reduced-GCD window
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()  # COOLDOWNS lists only recast-gated

# --- Burst-alignment abilities (AlignmentAspect watches these) -------------
BURST_ABILITIES: frozenset[int] = frozenset({
    IKISHOTEN, MEIKYO_SHISUI, OGI_NAMIKIRI, TENDO_SETSUGEKKA,
    MIDARE_SETSUGEKKA, HISSATSU_SENEI, ZANSHIN, SHOHA,
})

# Enablers whose value is throughput/burst, not standalone potency — priced by
# the sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (IKISHOTEN, MEIKYO_SHISUI)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Samurai",
    # Fugetsu coverage reads the player's DamageDone every pull — bundle it.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="kenki",
            generators=KENKI_GENERATORS,
            spenders=KENKI_SPENDERS,
            cap=KENKI_CAP,
            value_p_per_unit=KENKI_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),         # SAM has no cross-cooldown reductions
    charge_sharing={},    # no shared-charge pools
    raid_buffs={},        # party buffs modeled via buff_windows (job-agnostic)
    role_policy=MELEE_DPS,
    # A dropped GCD backfills with a combo ender (~420).
    filler_gcd_potency=420,
    # Tincture: melee-DPS Strength (party-comp-inclusive, from xivgear); same tier
    # stat/slope as Reaper. ⚠️ refine per tier via scripts/calibrate_tincture.py.
    tincture_main_stat=6841,
    tincture_role_coeff=237,
)
