"""Ninja data tables (Dawntrail 7.2+, level 100) + the `JOB_DATA` JobData instance.

Single source of truth for NIN numbers: potencies, the recast-gated cooldowns, the
two gauges (Ninki, Kazematoi), the mudra/ninjutsu system constants, and the
windowed self-buff (Kunai's Bane).

What makes NIN structurally distinct from the melee sims already shipped:

  * **The mudra system** — Ten/Chi/Jin are 0.5s-recast GCDs that build to a 1.5s
    ninjutsu (Raiton / Suiton / Hyosho Ranryu), consuming ONE shared charge per
    ninjutsu (2 charges / 20s regen; Kassatsu's ninjutsu is free). The whole kit
    mixes GCD speeds — the hasted 2.12s weaponskills (Huton: the permanent 15%
    trait), fixed 0.5s mudras, fixed 1.5s ninjutsu, ~1.0s Ten Chi Jin steps — so
    per-ability `gcd_recast_mult` + a per-ability `gcd_duration` (the Viper
    pattern) are load-bearing, and mudra charges regenerating through downtime
    (plus edge-of-window mudra pre-casting) is the job's signature downtime
    optimization.
  * **Kunai's Bane** — a NINJA-ONLY +10% damage window (15s / 60s; the wiki
    tooltip is explicit: "Increases damage YOU deal target by 10%") derived from
    the player's own KB casts and folded into the incremental beam score (the
    DRG/GNB windowed-self-buff pattern, NOT a coverage overlay). Dokumori's +5%
    is the PARTY buff and lives in the shared raid-buff catalog
    (jobs/_core/raid_buffs.py) — modeling it here would double-count.
  * **Ninki economy** — built by weaponskills/Dokumori/Meisui (and the Bunshin
    shadow's +5 per mirrored hit — invisible to the cast stream, see the gauge
    note), spent 50 at a time on Bhavacakra / Zesho Meppo / Bunshin.
  * **The 120s cycle** — Dokumori grants Higi (30s; enables Zesho Meppo), Ten Chi
    Jin fires the charge-free Fuma->Raiton->Suiton ladder and arms Tenri Jindo,
    TCJ's Suiton feeds Meisui (+50 Ninki, next spender +150).

⚠️ ACTION + STATUS IDS — VERIFIED 2026-07-01 against live top-Ninja M11S/M12S-P1
logs' masterData.abilities (scripts/probe_ninja_ids.py). POTENCIES are DT 7.2+
level-100 values verified on ffxiv.consolegameswiki.com per-action pages and
cross-checked against the logs' damage `bonusPercent` bytes (Armor Crush bp=52 ==
(500-240)/500, Aeolian bp=42 == (560-320)/560 under Kazematoi — the wiki splits
reproduce the wire bytes exactly). Positional abilities carry the positional-HIT
value (assume-always-hit, the RPR convention).
"""
from __future__ import annotations

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) — VERIFIED from live logs ---------------

# Melee combo
SPINNING_EDGE   = 2240    # starter, +5 Ninki
GUST_SLASH      = 2242    # 2nd, +5 Ninki
AEOLIAN_EDGE    = 2255    # finisher, REAR positional, +15 Ninki; +100 under Kazematoi
ARMOR_CRUSH     = 3563    # finisher, FLANK positional, +15 Ninki; grants 2 Kazematoi

# Non-combo GCD weaponskills
THROWING_DAGGER = 2247    # 20y ranged filler, +5 Ninki (does not break combo)
FLEETING_RAIJU  = 25778   # 700p, consumes 1 Raiju Ready, +5 Ninki
FORKED_RAIJU    = 25777   # 700p gap-closer variant (used at disconnects)
PHANTOM_KAMAITACHI = 25774  # 700p shadow (pet) GCD; requires PK Ready (Bunshin); +10 Ninki
PHANTOM_KAMAITACHI_PET = 25775  # the pet DAMAGE id (cast id above; metadata only)

# AoE GCD weaponskills (dedicated AoE — the ST sim never casts these)
DEATH_BLOSSOM   = 2254    # AoE starter, +5 Ninki
HAKKE_MUJINSATSU = 16488  # AoE 2nd (combo), +5 Ninki

# Mudras — 0.5s GCDs, 0 potency. TWO id families: the charge-consuming id logs on
# the FIRST mudra of a paid sequence; the free ids log for in-sequence mudras and
# for sequences opened under Kassatsu (verified live: Hyosho under Kassatsu opens
# with 18805/18806).
TEN             = 2259    # first-mudra (charged) id
CHI             = 2261
JIN             = 2263
TEN_FREE        = 18805   # in-sequence / Kassatsu-opened ids
CHI_FREE        = 18806
JIN_FREE        = 18807

# Ninjutsu — 1.5s-recast GCDs.
FUMA_SHURIKEN   = 2265    # any 1 mudra, 500p
RAITON          = 2267    # Ten->Chi, 740p, grants 1 Raiju Ready (max 3)
SUITON          = 2271    # Ten->Chi->Jin, 580p, grants Shadow Walker (20s)
HYOSHO_RANRYU   = 16492   # Kassatsu-only, Ten->Jin, 1300p (x1.30 under Kassatsu)
KATON           = 2266    # AoE 350p
GOKA_MEKKYAKU   = 16491   # Kassatsu-only AoE, 850p
HYOTON          = 2268    # 350p + bind (utility)
HUTON_NINJUTSU  = 2269    # AoE 240p + Shadow Walker (the 7.0-reworked ninjutsu)
DOTON           = 2270    # ground AoE DoT

# Ten Chi Jin step ids (charge-free; ~1.0s between steps, the closer at 1.5s)
TCJ_FUMA        = 18873   # 500p
TCJ_RAITON      = 18877   # 740p, grants Raiju Ready
TCJ_SUITON      = 18881   # 580p, grants Shadow Walker

# oGCDs
KUNAIS_BANE     = 36958   # 60s; 700p + the +10% ninja-only window (needs Shadow Walker)
DOKUMORI        = 36957   # 120s; 400p + 40 Ninki + Higi (30s); the PARTY +5% debuff
KASSATSU        = 2264    # 60s; next ninjutsu free + 30% damage (-> Hyosho Ranryu)
TEN_CHI_JIN     = 7403    # 120s; the 3-step charge-free ladder + Tenri Jindo Ready
TENRI_JINDO     = 36961   # 1100p; requires Tenri Jindo Ready (30s)
MEISUI          = 16489   # 120s; dispels Shadow Walker -> +50 Ninki, next spender +150
BHAVACAKRA      = 7402    # 400p, 50 Ninki (550 under Meisui)
ZESHO_MEPPO     = 36960   # 700p, 50 Ninki, requires Higi (850 under Meisui)
BUNSHIN         = 16493   # 90s, 50 Ninki; 5 shadow mirrors (160p each) + PK Ready
DREAM_WITHIN_A_DREAM = 3566  # 60s; 3x180 = 540p
HELLFROG_MEDIUM = 7401    # AoE 250p, 50 Ninki
DEATHFROG_MEDIUM = 36959  # AoE 400p, 50 Ninki, requires Higi
HOLLOW_NOZUCHI  = 25776   # 70p auto-proc under own Doton (AoE; metadata only)

# Defensive / movement / utility (no DPS value; sim never casts)
SHADE_SHIFT     = 2241    # 120s; 20% max-HP shield
SHUKUCHI        = 2262    # 60s / 2 charges; teleport
HIDE            = 2245    # out-of-combat; restores mudra charges

DEFENSIVE_IDS: frozenset[int] = frozenset({SHADE_SHIFT, SHUKUCHI, HIDE})

# --- Status (buff) IDs — verified from live player-aura streams --------------
MUDRA_STATUS_ID          = 1000496   # the 6s mudra-sequence window
KASSATSU_STATUS_ID       = 1000497
SHADOW_WALKER_STATUS_ID  = 1003848   # from Suiton / Huton / TCJ Suiton (20s)
RAIJU_READY_STATUS_ID    = 1002690   # from Raiton (30s, max 3 stacks)
BUNSHIN_STATUS_ID        = 1001954   # 5 stacks / 30s
PK_READY_STATUS_ID       = 1002723   # Phantom Kamaitachi Ready (45s)
HIGI_STATUS_ID           = 1003850   # from Dokumori (30s) -> Zesho / Deathfrog
MEISUI_STATUS_ID         = 1002689   # next Bhavacakra/Zesho +150 (30s)
TCJ_STATUS_ID            = 1001186   # the 6s Ten Chi Jin window
TENRI_READY_STATUS_ID    = 1003851   # Tenri Jindo Ready (30s)

# --- Self-buff / mechanic constants ------------------------------------------
KUNAIS_BANE_MULT: float       = 1.10
KUNAIS_BANE_DURATION_S: float = 15.0
KASSATSU_MULT: float          = 1.30   # on the ninjutsu that consumes it
MEISUI_BONUS_P: int           = 150    # Bhavacakra 400->550 / Zesho Meppo 700->850
BUNSHIN_STACKS: int           = 5
BUNSHIN_MIRROR_P: int         = 160    # shadow hit per mirrored ST weaponskill
RAIJU_READY_MAX: int          = 3
SHADOW_WALKER_DURATION_S: float = 20.0
HIGI_DURATION_S: float        = 30.0
MEISUI_DURATION_S: float      = 30.0

# --- Potencies ----------------------------------------------------------------
# ability_id -> base potency (no crit modeling; combo abilities carry the COMBO'd
# value, positionals the positional-HIT value — assume-always-hit). Aeolian Edge
# carries the NO-Kazematoi value (460); the +100 Kazematoi bonus is state-derived
# in scoring/simulator (symmetric on both sides). The Meisui +150 on Bhavacakra /
# Zesho Meppo and the Kassatsu x1.30 are likewise state-derived.
POTENCIES: dict[int, int] = {
    # Melee combo (combo'd + positional-hit values)
    SPINNING_EDGE:   300,
    GUST_SLASH:      400,
    AEOLIAN_EDGE:    460,   # combo + rear; +100 under Kazematoi (state-derived)
    ARMOR_CRUSH:     500,   # combo + flank
    # Non-combo GCDs
    THROWING_DAGGER: 200,
    FLEETING_RAIJU:  700,
    FORKED_RAIJU:    700,
    PHANTOM_KAMAITACHI: 700,   # shadow-dealt
    # Ninjutsu
    FUMA_SHURIKEN:   500,
    RAITON:          740,
    SUITON:          580,
    HYOSHO_RANRYU:  1300,   # x1.30 under Kassatsu (state-derived)
    KATON:           350,
    GOKA_MEKKYAKU:   850,
    HYOTON:          350,
    HUTON_NINJUTSU:  240,
    # TCJ steps (same potencies as the normal actions)
    TCJ_FUMA:        500,
    TCJ_RAITON:      740,
    TCJ_SUITON:      580,
    # Mudras (no damage)
    TEN: 0, CHI: 0, JIN: 0, TEN_FREE: 0, CHI_FREE: 0, JIN_FREE: 0,
    # oGCDs
    KUNAIS_BANE:     700,
    DOKUMORI:        400,
    KASSATSU:          0,
    TEN_CHI_JIN:       0,
    TENRI_JINDO:    1100,
    MEISUI:            0,
    BHAVACAKRA:      400,   # +150 under Meisui (state-derived)
    ZESHO_MEPPO:     700,   # +150 under Meisui (state-derived)
    BUNSHIN:           0,   # value = the 5 mirrors + PK Ready (enabler-priced)
    DREAM_WITHIN_A_DREAM: 540,   # 3 x 180
    HELLFROG_MEDIUM: 250,
    DEATHFROG_MEDIUM: 400,
    # AoE GCDs (dedicated AoE — ST sim never casts; delivered timeline naming)
    DEATH_BLOSSOM:   100,
    HAKKE_MUJINSATSU: 120,
    DOTON:            80,
    HOLLOW_NOZUCHI:   70,
}

# --- AoE potencies (dedicated AoE buttons the ST sim never casts) -------------
# ability_id -> per-extra-target potency. NIN's whole AoE line is FULL-TO-ALL
# (no "reduced for remaining" clause on any of these wiki pages): secondary ==
# primary (== POTENCIES[id]). Ids live-verified via get_metadata 2026-07-16
# (all nine names match — the WAR-Decimate wrong-id trap checked). The AoE-aware
# sim swaps these in at the audited crossovers (see simulator._KATON_MIN_TARGETS
# etc.); Hakke Mujinsatsu carries the COMBO'd value like the ST combo entries.
#
# Deliberately EXCLUDED (documented, not forgotten):
#   * Doton (2270) + Hollow Nozuchi (25776) — ground-target DoT placement /
#     uptime modeling is out of scope (the ceiling would need position-aware
#     puddle uptime); the delivered side still credits any real casts via
#     POTENCIES.
#   * Huton (2269, 240p AoE + Shadow Walker) — a Suiton alternative worth ~140p
#     per 60s cycle at N=3; deferred until a fight makes it material.
#   * Phantom Kamaitachi splash (700p, 50% falloff) — the DAMAGE is dealt by
#     the Bunshin shadow (pet id 25775), invisible to the player's DamageDone
#     stream, so the delivered side can never measure its cleave; crediting it
#     ceiling-only would be asymmetric against the player (the MCH-Queen
#     precedent — and the 2026-07-16 Queen probe showed pet cleave doesn't
#     happen in practice anyway).
AOE_POTENCIES: dict[int, int] = {
    KATON:            350,   # mudra AoE finisher (full-to-all)
    GOKA_MEKKYAKU:    850,   # Kassatsu AoE (full-to-all, x1.30 state-derived)
    DEATH_BLOSSOM:    100,   # AoE combo starter
    HAKKE_MUJINSATSU: 120,   # AoE combo finisher (combo'd value)
    HELLFROG_MEDIUM:  250,   # Ninki AoE spender
    DEATHFROG_MEDIUM: 400,   # Ninki AoE spender under Higi
}

# The Kazematoi bonus on Aeolian Edge (all its potencies +100 while stacked).
AEOLIAN_KAZEMATOI_BONUS_P: int = 100

# Non-positional ("missed") potency for the positional finishers, cross-checked
# against the live bonusPercent bytes (AE miss bp=36 == (500-320)/500 with
# Kazematoi; AC miss bp=45 == (440-240)/440). The delta vs POTENCIES is what a
# missed positional costs; idealized always uses the hit value.
POSITIONAL_MISS_POTENCY: dict[int, int] = {
    AEOLIAN_EDGE: 400,   # combo, no rear (Kazematoi bonus separate)
    ARMOR_CRUSH:  440,   # combo, no flank
}
POSITIONAL_IDS: frozenset[int] = frozenset(POSITIONAL_MISS_POTENCY)

# oGCD set — kept job-local so the sim's weave logic and scoring's GCD/oGCD split
# stay hermetic under the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    KUNAIS_BANE, DOKUMORI, KASSATSU, TEN_CHI_JIN, TENRI_JINDO, MEISUI,
    BHAVACAKRA, ZESHO_MEPPO, BUNSHIN, DREAM_WITHIN_A_DREAM,
    HELLFROG_MEDIUM, DEATHFROG_MEDIUM, HOLLOW_NOZUCHI,
    SHADE_SHIFT, SHUKUCHI, HIDE,
})

# All ninjutsu finishers (the casts that consume a Kassatsu / mudra sequence).
NINJUTSU_IDS: frozenset[int] = frozenset({
    FUMA_SHURIKEN, RAITON, SUITON, HYOSHO_RANRYU, KATON, GOKA_MEKKYAKU,
    HYOTON, HUTON_NINJUTSU, DOTON,
})
MUDRA_IDS: frozenset[int] = frozenset({TEN, CHI, JIN, TEN_FREE, CHI_FREE, JIN_FREE})
TCJ_STEP_IDS: frozenset[int] = frozenset({TCJ_FUMA, TCJ_RAITON, TCJ_SUITON})

# GCD weaponskills the Bunshin shadow mirrors (physical weaponskills only — NOT
# mudras, ninjutsu or TCJ steps).
BUNSHIN_MIRRORED_IDS: frozenset[int] = frozenset({
    SPINNING_EDGE, GUST_SLASH, AEOLIAN_EDGE, ARMOR_CRUSH, THROWING_DAGGER,
    FLEETING_RAIJU, FORKED_RAIJU, DEATH_BLOSSOM, HAKKE_MUJINSATSU,
})

# --- Cooldowns + charges ------------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only genuinely RECAST-gated actions.
# TEN carries the SHARED mudra-charge pool (2 charges / 20s; one charge per paid
# ninjutsu — spent manually in the simulator, regenerated by the engine's generic
# multi-charge regen, including through downtime, which is the point). Bhavacakra /
# Zesho / Bunshin are Ninki-gated (gauge below); Tenri / Hyosho are state-gated.
COOLDOWNS: dict[int, tuple[float, int]] = {
    TEN:                  (20.0, 2),
    KUNAIS_BANE:          (60.0, 1),
    KASSATSU:             (60.0, 1),
    DREAM_WITHIN_A_DREAM: (60.0, 1),
    BUNSHIN:              (90.0, 1),
    DOKUMORI:            (120.0, 1),
    TEN_CHI_JIN:         (120.0, 1),
    MEISUI:              (120.0, 1),
}

# Per-cast value for the cooldown-drift detector (lost potential if skipped).
# Enabler improvements are priced by the sim's marginal values, not these.
COOLDOWN_VALUE_P: dict[int, int] = {
    KUNAIS_BANE:          1500,   # 700 direct + ~10% of a burst window
    KASSATSU:              950,   # Hyosho x1.3 (1690) over a Raiton (740)
    DREAM_WITHIN_A_DREAM:  540,
    BUNSHIN:              1400,   # 5 x 160 mirrors + the 700 Phantom Kamaitachi
    DOKUMORI:              800,   # 400 direct + 40 Ninki + the Higi Zesho uplift
    TEN_CHI_JIN:          2500,   # 1820 ladder + Tenri Jindo 1100 in ~3.5s of GCDs
    MEISUI:                550,   # +50 Ninki (~a Bhavacakra) + the +150 spender
}

# Mudra-charge pacing is economy-driven (banked for Suiton-before-Kunai's-Bane and
# for downtime edges), not pilot drift.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset({TEN})

# --- Ninki gauge (0-100) ------------------------------------------------------
# Wiki-verbatim per-action grants. NOTE the Bunshin shadow also grants +5 per
# mirrored hit (5 x 5 per Bunshin) — pet hits are invisible to the cast stream, so
# the overcap detector under-counts generation slightly (it can only MISS overcap,
# never invent it — the conservative direction).
NINKI_GENERATORS: dict[int, int] = {
    SPINNING_EDGE: 5, GUST_SLASH: 5, AEOLIAN_EDGE: 15, ARMOR_CRUSH: 15,
    THROWING_DAGGER: 5, FLEETING_RAIJU: 5, FORKED_RAIJU: 5,
    PHANTOM_KAMAITACHI: 10, DEATH_BLOSSOM: 5, HAKKE_MUJINSATSU: 5,
    DOKUMORI: 40, MEISUI: 50,
}
NINKI_SPENDERS: dict[int, int] = {
    BHAVACAKRA: 50, ZESHO_MEPPO: 50, BUNSHIN: 50,
    HELLFROG_MEDIUM: 50, DEATHFROG_MEDIUM: 50,
}
NINKI_CAP = 100
# 50 Ninki -> a 400p Bhavacakra => ~8 p/unit.
NINKI_VALUE_P_PER_UNIT: float = 8.0

# --- Kazematoi gauge (0-5) ----------------------------------------------------
# Armor Crush (combo) grants 2; Aeolian Edge consumes 1 for +100 potency.
KAZEMATOI_GENERATORS: dict[int, int] = {ARMOR_CRUSH: 2}
KAZEMATOI_SPENDERS: dict[int, int] = {AEOLIAN_EDGE: 1}
KAZEMATOI_CAP = 5
KAZEMATOI_VALUE_P_PER_UNIT: float = 100.0

# --- Per-ability GCD recast (multiple of the standard hasted ~2.12s GCD) -------
# NIN mixes GCD speeds: fixed 0.5s mudras, fixed 1.5s ninjutsu, ~1.0s TCJ steps,
# and the Huton-hasted 2.12s weaponskills (the standard). The idle/clip detector
# scales the run's standard effective GCD by these so a ninjutsu's 1.5s gap isn't
# read as clipping and a mudra chain isn't read as idle. Ratios vs 2.125.
GCD_RECAST_MULT: dict[int, float] = {
    TEN: 0.235, CHI: 0.235, JIN: 0.235,
    TEN_FREE: 0.235, CHI_FREE: 0.235, JIN_FREE: 0.235,
    FUMA_SHURIKEN: 0.71, RAITON: 0.71, SUITON: 0.71, HYOSHO_RANRYU: 0.71,
    KATON: 0.71, GOKA_MEKKYAKU: 0.71, HYOTON: 0.71, HUTON_NINJUTSU: 0.71,
    DOTON: 0.71,
    TCJ_FUMA: 0.47, TCJ_RAITON: 0.47, TCJ_SUITON: 0.71,
}

# The reduced-recast mudra/ninjutsu/TCJ GCDs are paced by GCD_RECAST_MULT above;
# keep them out of raw clip pairing too (VPR Reawaken convention).
CLIP_EXCLUSIONS: frozenset[int] = frozenset(
    MUDRA_IDS | NINJUTSU_IDS | TCJ_STEP_IDS)

# --- Burst-alignment abilities (AlignmentAspect watches these) -----------------
BURST_ABILITIES: frozenset[int] = frozenset({
    KUNAIS_BANE, DOKUMORI, TEN_CHI_JIN, KASSATSU, BUNSHIN,
})

# Enablers whose value is throughput, not standalone table potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (
    KUNAIS_BANE, DOKUMORI, TEN_CHI_JIN, KASSATSU, BUNSHIN, MEISUI,
)

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) -----------
# MEASURED consensus from live top parses (M11S/M12S-P1, 2026-07-01): pre-pull
# mudras -> Suiton at ~0.5s (Kassatsu weaved) -> the melee combo with Dokumori /
# Bunshin -> Phantom Kamaitachi -> Kunai's Bane weave -> the Kassatsu Hyosho ->
# Raiton -> the TCJ ladder (Meisui / Tenri / Zesho weaves) -> Raiju chain.
CANONICAL_OPENER: tuple[int, ...] = (
    SUITON,
    SPINNING_EDGE,
    GUST_SLASH,
    PHANTOM_KAMAITACHI,
    TEN_FREE,
    JIN_FREE,
    HYOSHO_RANRYU,
    TEN,
    CHI_FREE,
    RAITON,
    TCJ_FUMA,
    TCJ_RAITON,
)


# --- JOB_DATA bundle ------------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Ninja",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="ninki",
            generators=NINKI_GENERATORS,
            spenders=NINKI_SPENDERS,
            cap=NINKI_CAP,
            value_p_per_unit=NINKI_VALUE_P_PER_UNIT,
        ),
        GaugeModel(
            name="kazematoi",
            generators=KAZEMATOI_GENERATORS,
            spenders=KAZEMATOI_SPENDERS,
            cap=KAZEMATOI_CAP,
            value_p_per_unit=KAZEMATOI_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    gcd_recast_mult=GCD_RECAST_MULT,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),
    charge_sharing={},
    # No free-splash cleave in the ST rotation (Phantom Kamaitachi's shadow
    # cleave is pet-dealt and unmeasurable — see AOE_POTENCIES exclusions); the
    # dedicated AoE line rides `aoe_potencies` + the sim's audited swaps.
    splash_potencies={},
    aoe_potencies=AOE_POTENCIES,
    raid_buffs={},          # Dokumori modeled via the job-agnostic buff_windows pass
    role_policy=MELEE_DPS,
    # A dropped high-potency GCD backfills with a combo hit (~300-500).
    filler_gcd_potency=400,
    # Tincture: effective BiS Dexterity (party-comp + food inclusive) — live top
    # parses use Gemdraughts of Dexterity. Same DEX value MCH/DNC/VPR use.
    tincture_main_stat=6838,
    # No ranged_filler_id: NIN bridges forced disconnects with rotational ranged
    # tools (ninjutsu at 20-25y, Forked Raiju, Throwing Dagger), so disengages are
    # near-seamless (the VPR/PLD reasoning) rather than an RPR-Harpe genuine loss.
)
