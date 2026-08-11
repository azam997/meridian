"""Pictomancer data tables (Dawntrail, level 100, patch 7.5) + `JOB_DATA`.

Single source of truth for PCT numbers: potencies, cast/recast tables, the
palette / paint economy, the canvas -> muse -> portrait ladder, Hammer Time, the
Starry Muse burst (Hyperphantasia / Inspiration / Starstruck / Rainbow Bright),
and the opener. Every id below is PROBE-VERIFIED against real top-pull cast
streams (scripts/probe_pictomancer_ids.py, 2026-07-03: M11S x3, M9S x2,
M12S-P1 x2, M12S-P2 x2) and every potency is verified by the multiplier-
deconvolved damage probe (scripts/probe_pictomancer_potency.py — amount /
FFLogs-multiplier per clean hit; all 16 scoreable abilities land within +/-3.4%
of the wiki 7.5 values, so the 7.2-rebalance numbers are confirmed current).

PCT is the analyzer's **third caster** (after RDM / BLM) and its first job whose
optimization axis is a **downtime-painting economy**:

  * **Motifs** (Creature / Weapon=Hammer / Landscape=Starry Sky) are 0-potency
    GCDs — 3s cast / 4s recast in combat, instant out of combat — that load a
    canvas a Muse oGCD then consumes. Top parses pre-paint all three before the
    pull and re-paint during downtime windows (probe: M12S-P1's ~6.6s gap is
    filled with motif hardcasts); the repaint economy closes exactly 1:1 with
    muse casts (+1 pre-paint each).
  * **Fully deterministic** — no RNG procs at all, so the ceiling needs no
    proc-budget `sim_context`; resources are a closed economy of palette
    (Water +25 -> Subtractive Palette 50), white/black paint, portraits and
    Hammer Time stacks. No pet: Muses/portraits log as the player's own casts.
  * **Starry Muse** is the party raid buff (+5%/20s — modeled by the shared
    `raid_buffs.PROVIDER_BUFFS` catalog, NEVER in job scoring). Its SELF effects
    live here: Hyperphantasia x5 -> **Inspiration** (-25% cast AND recast on
    damaging spells while stacks remain — probe: hasted CMY begincast gaps
    2.489s == 3.3 x 0.75, hasted CMY cast 1.20+0.5s snapshot == 2.3 x 0.75),
    Starstruck (Star Prism), Subtractive Spectrum (a free Subtractive Palette),
    and Rainbow Bright (instant Rainbow Drip) once the 5th stack is consumed.
  * **Hammer combo** (Stamp/Brush/Polishing) is guaranteed crit + direct hit —
    probe: 313/313 damage events crit AND DH across 9 pulls. Scored with the
    tier-measured crit+DH multiplier (below), symmetric on both sides.

A scoring-basis note (the crit-neutrality analog): white-paint OVERCAP is
deliberately not a modeled loss. A Holy in White (570) displaces a marginal
aetherhue-chain GCD worth ~680 amortized (the chain backloads its value into the
CMY + Comet cycle), so optimal play banks/wastes paint outside movement and the
fight tail — top parses waste 11-18 paint per pull. The white gauge is therefore
NOT a `GaugeModel` (an overcap finding would be flat wrong); paint legality is
simulator state only.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, GaugeModel, JobData


PATCH_VERSION = "7.5"

# --- Ability IDs (DT 7.5, level 100; probe-verified from live logs) ----------

# RGB aetherhue cycle (1.5s cast / 2.5s recast; the chain forces R->G->B).
FIRE_IN_RED        = 34650   # 490p, grants Aetherhues
AERO_IN_GREEN      = 34651   # 530p, needs Aetherhues
WATER_IN_BLUE      = 34652   # 570p, needs Aetherhues II; +25 palette, +1 white paint
# CMY subtractive cycle (2.3s cast / 3.3s recast; needs a Subtractive stack).
BLIZZARD_IN_CYAN   = 34653   # 860p
STONE_IN_YELLOW    = 34654   # 900p
THUNDER_IN_MAGENTA = 34655   # 940p, +1 white paint
# AoE "II" versions (same casts/recasts/chain; full potency to all targets).
# 34659/34661 probe-confirmed (M9S adds); 34656-58/34660 are the contiguous
# block XIVAPI-verified by the bundled-metadata capture (names match).
FIRE_II_IN_RED        = 34656   # 180p
AERO_II_IN_GREEN      = 34657   # 200p
WATER_II_IN_BLUE      = 34658   # 220p, +25 palette, +1 white paint
BLIZZARD_II_IN_CYAN   = 34659   # 360p
STONE_II_IN_YELLOW    = 34660   # 380p
THUNDER_II_IN_MAGENTA = 34661   # 400p, +1 white paint
# Paint spenders (instant).
HOLY_IN_WHITE      = 34662   # 570p, 2.5s recast; spends 1 white paint
COMET_IN_BLACK     = 34663   # 940p, 3.3s recast; spends the black paint
# Motifs (GCD, 0 potency, self-target; 3s cast / 4s recast in combat, instant
# out of combat). The creature motif cycles Pom -> Wing -> Claw -> Maw (an id
# FAMILY — each stage logs its own id).
POM_MOTIF          = 34664
WING_MOTIF         = 34665
CLAW_MOTIF         = 34666
MAW_MOTIF          = 34667
HAMMER_MOTIF       = 34668
STARRY_SKY_MOTIF   = 34669
# Muses (oGCD). Living Muse is a 3-charge/40s pool shared across the four
# creature variants (keyed on POM_MUSE); Striking Muse is 2 charges/60s
# (in-combat only); Starry Muse is the 120s raid buff.
POM_MUSE           = 34670   # 800p (all four variants; probe-verified)
WINGED_MUSE        = 34671   # 800p; completes the Moogle portrait (with Pom)
CLAWED_MUSE        = 34672   # 800p
FANGED_MUSE        = 34673   # 800p; completes the Madeen portrait (with Claw)
STRIKING_MUSE      = 34674   # 0p; grants Hammer Time x3
STARRY_MUSE        = 34675   # 0p; the party buff + self effects
# Portraits (oGCD; shared 30s recast — probe: min consecutive gap 31.6s).
MOG_OF_THE_AGES    = 34676   # 1000p
RETRIBUTION_OF_THE_MADEEN = 34677  # 1100p
# Hammer combo (instant GCDs, 2.5s recast, guaranteed crit+DH).
HAMMER_STAMP       = 34678   # 560p
HAMMER_BRUSH       = 34679   # 580p
POLISHING_HAMMER   = 34680   # 600p
# Star Prism: the damage GCD + a 0-damage auto follow-up the game logs ~1.25s
# later under a second id (probe: 34682 has ZERO damage events; it occupies no
# GCD slot — the next GCD lands one hasted recast after 34681).
STAR_PRISM         = 34681   # 1100p, instant, needs Starstruck
STAR_PRISM_FOLLOWUP = 34682  # 0p auto follow-up (cure part); not a slot
# Utility / defensives.
SUBTRACTIVE_PALETTE = 34683  # oGCD; 50 palette (or free via Spectrum) -> 3 CMY
                             # stacks + Monochrome Tones (1 white -> black).
                             # Resource-gated -> NOT in COOLDOWNS (gotcha #2).
SMUDGE             = 34684   # movement dash
TEMPERA_COAT       = 34685   # barrier
TEMPERA_GRASSA     = 34686   # party barrier (upgrades Coat)
RAINBOW_DRIP       = 34688   # 1000p, 4s cast / 6s recast, +1 white paint;
                             # instant + 2.5s recast under Rainbow Bright

# --- Status ids (probe-verified on live logs 2026-07-03) --------------------
AETHERHUES_STATUS_ID          = 3675
AETHERHUES_II_STATUS_ID       = 3676
SUBTRACTIVE_PALETTE_STATUS_ID = 3674
MONOCHROME_TONES_STATUS_ID    = 3691
HAMMER_TIME_STATUS_ID         = 3680
STARRY_MUSE_STATUS_ID         = 3685
STARSTRUCK_STATUS_ID          = 3681
SUBTRACTIVE_SPECTRUM_STATUS_ID = 3690
HYPERPHANTASIA_STATUS_ID      = 3688
INSPIRATION_STATUS_ID         = 3689
RAINBOW_BRIGHT_STATUS_ID      = 3679

# --- Cast times (s) — feeds the HardcastGCD timing preset --------------------
# Absent ids are instant. Probe-verified via begincast->cast deltas (FFLogs
# snapshots ~0.5s before the bar completes): RGB 0.98 -> 1.5s, CMY 1.78 ->
# 2.3s, motifs 2.50 -> 3.0s, Rainbow Drip 3.34 -> 4.0s. Motif casts are NOT
# hasted by Inspiration (constant 2.50 deltas everywhere).
CAST_TIMES: dict[int, float] = {
    FIRE_IN_RED:        1.5,
    AERO_IN_GREEN:      1.5,
    WATER_IN_BLUE:      1.5,
    FIRE_II_IN_RED:     1.5,
    AERO_II_IN_GREEN:   1.5,
    WATER_II_IN_BLUE:   1.5,
    BLIZZARD_IN_CYAN:   2.3,
    STONE_IN_YELLOW:    2.3,
    THUNDER_IN_MAGENTA: 2.3,
    BLIZZARD_II_IN_CYAN:   2.3,
    STONE_II_IN_YELLOW:    2.3,
    THUNDER_II_IN_MAGENTA: 2.3,
    POM_MOTIF:          3.0,
    WING_MOTIF:         3.0,
    CLAW_MOTIF:         3.0,
    MAW_MOTIF:          3.0,
    HAMMER_MOTIF:       3.0,
    STARRY_SKY_MOTIF:   3.0,
    RAINBOW_DRIP:       4.0,
}

# Per-ability recast as a multiple of the 2.5s standard (the Viper pattern).
# Scales with the player's Spell Speed like the base recast. Drives BOTH the
# sim's `gcd_duration` and the idle/clip detector's pacing (JobData field).
RECAST_MULT: dict[int, float] = {
    BLIZZARD_IN_CYAN:   1.32,   # 3.3s (probe: unhasted begincast gaps 3.299s)
    STONE_IN_YELLOW:    1.32,
    THUNDER_IN_MAGENTA: 1.32,
    BLIZZARD_II_IN_CYAN:   1.32,
    STONE_II_IN_YELLOW:    1.32,
    THUNDER_II_IN_MAGENTA: 1.32,
    COMET_IN_BLACK:     1.32,   # 3.3s recast (probe: hasted Comet gaps 2.45s)
    POM_MOTIF:          1.6,    # 4.0s in-combat motif slot
    WING_MOTIF:         1.6,
    CLAW_MOTIF:         1.6,
    MAW_MOTIF:          1.6,
    HAMMER_MOTIF:       1.6,
    STARRY_SKY_MOTIF:   1.6,
    RAINBOW_DRIP:       2.4,    # 6.0s (probe: one hardcast; Bright Drips run 2.5)
}

# --- Potencies (multiplier-deconvolved probe: all within +/-3.4% of these) ---
POTENCIES: dict[int, int] = {
    FIRE_IN_RED:        490,
    AERO_IN_GREEN:      530,
    WATER_IN_BLUE:      570,
    BLIZZARD_IN_CYAN:   860,
    STONE_IN_YELLOW:    900,
    THUNDER_IN_MAGENTA: 940,
    # AoE "II" primary potency (full-to-all; see AOE_POTENCIES).
    FIRE_II_IN_RED:     180,
    AERO_II_IN_GREEN:   200,
    WATER_II_IN_BLUE:   220,
    BLIZZARD_II_IN_CYAN:   360,
    STONE_II_IN_YELLOW:    380,
    THUNDER_II_IN_MAGENTA: 400,
    HOLY_IN_WHITE:      570,
    COMET_IN_BLACK:     940,
    POM_MUSE:           800,
    WINGED_MUSE:        800,
    CLAWED_MUSE:        800,
    FANGED_MUSE:        800,
    MOG_OF_THE_AGES:    1000,
    RETRIBUTION_OF_THE_MADEEN: 1100,
    HAMMER_STAMP:       560,
    HAMMER_BRUSH:       580,
    POLISHING_HAMMER:   600,
    STAR_PRISM:         1100,
    RAINBOW_DRIP:       1000,
    # Buttons / casts with no direct potency.
    POM_MOTIF:          0,
    WING_MOTIF:         0,
    CLAW_MOTIF:         0,
    MAW_MOTIF:          0,
    HAMMER_MOTIF:       0,
    STARRY_SKY_MOTIF:   0,
    STRIKING_MUSE:      0,
    STARRY_MUSE:        0,
    SUBTRACTIVE_PALETTE: 0,
    STAR_PRISM_FOLLOWUP: 0,
    SMUDGE:             0,
    TEMPERA_COAT:       0,
    TEMPERA_GRASSA:     0,
}

# Per-extra-target potency (aoe_potency.potency_for). The "II" spells hit all
# targets at full potency (probe: Thunder II secondary/primary 0.949 ~ 1.0);
# the free-splash secondaries below carry their wiki falloff (probe-consistent:
# hammers ~0.30, Holy 0.347). Free-splash abilities the ST sim already casts
# get window-gated splash credited symmetrically (the multi-target pass).
AOE_POTENCIES: dict[int, int] = {
    FIRE_II_IN_RED:     180,
    AERO_II_IN_GREEN:   200,
    WATER_II_IN_BLUE:   220,
    BLIZZARD_II_IN_CYAN:   360,
    STONE_II_IN_YELLOW:    380,
    THUNDER_II_IN_MAGENTA: 400,
}
SPLASH_POTENCIES: dict[int, int] = {
    HOLY_IN_WHITE:      200,    # 570, -65%
    COMET_IN_BLACK:     329,    # 940, -65%
    HAMMER_STAMP:       168,    # -70%
    HAMMER_BRUSH:       174,
    POLISHING_HAMMER:   180,
    STAR_PRISM:         330,    # 1100, -70%
    RAINBOW_DRIP:       150,    # 1000, -85% (line)
    POM_MUSE:           240,    # -70%
    WINGED_MUSE:        240,
    CLAWED_MUSE:        240,
    FANGED_MUSE:        240,
    MOG_OF_THE_AGES:    300,    # -70% (line)
    RETRIBUTION_OF_THE_MADEEN: 330,
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. Everything else in POTENCIES is a GCD (motifs, hammers, Star
# Prism, Holy/Comet, Rainbow Drip are all genuinely GCDs). The Star Prism
# follow-up (34682) is flagged oGCD-like: it occupies no GCD slot.
OGCD_IDS: frozenset[int] = frozenset({
    POM_MUSE, WINGED_MUSE, CLAWED_MUSE, FANGED_MUSE,
    STRIKING_MUSE, STARRY_MUSE, MOG_OF_THE_AGES, RETRIBUTION_OF_THE_MADEEN,
    SUBTRACTIVE_PALETTE, SMUDGE, TEMPERA_COAT, TEMPERA_GRASSA,
    STAR_PRISM_FOLLOWUP,
})

# --- Guaranteed crit + direct hit (the hammer trio) --------------------------
# Probe: 313/313 hammer damage events crit AND direct across 9 pulls. The
# multiplier is the TIER-MEASURED effective factor from the deconvolved probe
# (amount / FFLogs-multiplier / potency vs the clean-cast baseline): 2.253 /
# 2.279 / 2.271 across the trio — higher than MCH's 2.03 constant because
# caster BiS stacks crit AND in-window crit-rate party buffs convert to damage
# on guaranteed-crit actions. Symmetric on both sides (sim + delivered), so the
# absolute value is mix-neutral; the trio's internal consistency also
# cross-validates the 560/580/600 potency split.
ALWAYS_CRIT_DH_IDS: frozenset[int] = frozenset({
    HAMMER_STAMP, HAMMER_BRUSH, POLISHING_HAMMER,
})
GUARANTEED_CRIT_DH_MULT: float = 2.26

# --- Cooldowns + charges ------------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only the genuinely RECAST-gated
# DPS oGCDs live here. The Living Muse pool is keyed on POM_MUSE (the NIN TEN
# pattern) so the engine's generic multi-charge regen runs — including through
# downtime; the Winged/Clawed/Fanged variants spend the shared pool manually in
# the simulator. Mog / Retribution share ONE 30s portrait recast (probe: min
# consecutive-portrait gap 31.6s), keyed on MOG. Subtractive Palette is
# resource-gated (50 palette / Spectrum), NOT recast-gated -> absent.
COOLDOWNS: dict[int, tuple[float, int]] = {
    POM_MUSE:      (40.0, 3),
    STRIKING_MUSE: (60.0, 2),
    STARRY_MUSE:   (120.0, 1),
    MOG_OF_THE_AGES: (30.0, 1),
}

# Consumer -> pool-source mapping for the shared-charge drift accounting
# (the MCH Bioblaster->Drill semantics).
CHARGE_SHARING: dict[int, int] = {
    WINGED_MUSE: POM_MUSE,
    CLAWED_MUSE: POM_MUSE,
    FANGED_MUSE: POM_MUSE,
    RETRIBUTION_OF_THE_MADEEN: MOG_OF_THE_AGES,
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    POM_MUSE:      800,    # a Living Muse hit
    STRIKING_MUSE: 1700,   # 3 guaranteed-crit hammers net of displaced filler
    STARRY_MUSE:   2100,   # Star Prism + Rainbow Bright + the free Subtractive
    MOG_OF_THE_AGES: 1000,
}

# --- Starry Muse self effects -------------------------------------------------
# Inspiration: -25% cast AND recast on the damaging spells while Hyperphantasia
# stacks remain (probe: hasted CMY recast 2.489s == 3.3 x 0.75, hasted CMY cast
# 1.20+0.5 == 2.3 x 0.75; Star Prism's next-GCD gap 1.91s == 2.5 x 0.75; Comet
# gaps 2.45s == 3.3 x 0.75). Hammers and motifs are NOT hasted and do NOT
# consume stacks (probe: an in-window Hammer Stamp leaves the 5-stack budget
# intact). The 5th consumed stack grants Rainbow Bright.
INSPIRATION_HASTE: float = 0.75
HYPERPHANTASIA_STACKS: int = 5
STARSTRUCK_DURATION_S: float = 20.0
HAMMER_TIME_DURATION_S: float = 30.0
RAINBOW_BRIGHT_DURATION_S: float = 30.0
AETHERHUES_DURATION_S: float = 30.0
STARRY_WINDOW_S: float = 20.0       # the party-buff window (shared catalog)
INSPIRATION_WINDOW_S: float = 30.0  # hard cap; stacks always run out first
# The damaging spells: consume Hyperphantasia + get the Inspiration haste.
INSPIRED_IDS: frozenset[int] = frozenset({
    FIRE_IN_RED, AERO_IN_GREEN, WATER_IN_BLUE,
    FIRE_II_IN_RED, AERO_II_IN_GREEN, WATER_II_IN_BLUE,
    BLIZZARD_IN_CYAN, STONE_IN_YELLOW, THUNDER_IN_MAGENTA,
    BLIZZARD_II_IN_CYAN, STONE_II_IN_YELLOW, THUNDER_II_IN_MAGENTA,
    HOLY_IN_WHITE, COMET_IN_BLACK, STAR_PRISM,
})

# --- Palette / paint economy ---------------------------------------------------
PALETTE_CAP = 100
PALETTE_PER_WATER = 25
SUBTRACTIVE_PALETTE_COST = 50
WHITE_PAINT_CAP = 5
SUBTRACTIVE_STACKS = 3
# Palette gauge (overcap detector + entry gauge). White paint is deliberately
# NOT a gauge — see the module docstring (paint overcap is optimal play).
PALETTE_GAUGE = GaugeModel(
    name="palette",
    generators={WATER_IN_BLUE: PALETTE_PER_WATER, WATER_II_IN_BLUE: PALETTE_PER_WATER},
    spenders={SUBTRACTIVE_PALETTE: SUBTRACTIVE_PALETTE_COST},
    cap=PALETTE_CAP,
    # 50 palette -> the CMY upgrade (3 x ~+370 over RGB) + a Comet via the
    # conversion (~+410 net) ~= 1520 + 410 per 50.
    value_p_per_unit=22.0,
)

# --- Canonical opener -----------------------------------------------------------
# The MEASURED consensus opener (probe 2026-07-03: two of three top Tyrant pulls
# byte-identical, M9S/M12S-P2 cold opens match): pre-pull Rainbow Drip (lands at
# t~0), Pom Muse + Striking Muse + pot in the residual weave space, Wing Motif
# hardcast, Starry Muse, then the Inspiration-hasted burst (Subtractive -> CMY
# -> Comet -> Star Prism) with Winged Muse / Mog weaved, hammers, Bright Drip.
CANONICAL_OPENER: tuple[int, ...] = (
    RAINBOW_DRIP,
    POM_MUSE,
    STRIKING_MUSE,
    WING_MOTIF,
    STARRY_MUSE,
    HAMMER_STAMP,
    SUBTRACTIVE_PALETTE,
    BLIZZARD_IN_CYAN,
    STONE_IN_YELLOW,
    THUNDER_IN_MAGENTA,
    COMET_IN_BLACK,
    WINGED_MUSE,
    MOG_OF_THE_AGES,
    STAR_PRISM,
    HAMMER_BRUSH,
    POLISHING_HAMMER,
    RAINBOW_DRIP,
)

# --- Burst-alignment abilities ---------------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these).
BURST_ABILITIES: frozenset[int] = frozenset({
    MOG_OF_THE_AGES, RETRIBUTION_OF_THE_MADEEN, STAR_PRISM, COMET_IN_BLACK,
})

# Enablers whose value is throughput, not standalone table potency — priced by
# the sim's marginal contribution (scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (STARRY_MUSE, STRIKING_MUSE, SUBTRACTIVE_PALETTE)

# Interchangeable high-value filler GCDs (the diffuse "filler quality" card).
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({
    FIRE_IN_RED, AERO_IN_GREEN, WATER_IN_BLUE,
})

# Defensive / utility oGCDs the simulator never fires (excluded from the DPS
# timeline + cast-diff). Addle/Swiftcast/Lucid/Surecast are shared role actions
# (role_actions.ROLE_ACTION_IDS). Swiftcast is deliberately NOT modeled as DPS:
# every swiftable PCT cast is recast-bound (motif max(3,4)s, Drip max(4,6)s,
# RGB max(1.5,2.5), CMY max(2.3,3.3)), so an instant cast changes no slot
# length — probe-confirmed (a Swiftcast motif still occupies the 4s slot).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    SMUDGE, TEMPERA_COAT, TEMPERA_GRASSA,
})

# Motif id families (used by the simulator + tests).
CREATURE_MOTIFS: tuple[int, ...] = (POM_MOTIF, WING_MOTIF, CLAW_MOTIF, MAW_MOTIF)
CREATURE_MUSES: tuple[int, ...] = (POM_MUSE, WINGED_MUSE, CLAWED_MUSE, FANGED_MUSE)
MOTIF_IDS: frozenset[int] = frozenset(CREATURE_MOTIFS) | {HAMMER_MOTIF, STARRY_SKY_MOTIF}
HAMMER_IDS: tuple[int, ...] = (HAMMER_STAMP, HAMMER_BRUSH, POLISHING_HAMMER)


# --- JOB_DATA bundle -----------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Pictomancer",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    splash_potencies=SPLASH_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(PALETTE_GAUGE,),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    gcd_recast_mult=RECAST_MULT,
    drift_exclusions=frozenset(),
    charge_sharing=CHARGE_SHARING,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    burst_abilities=BURST_ABILITIES,
    raid_buffs={},                      # Starry Muse rides the shared PROVIDER_BUFFS catalog
    role_policy=CASTER_HEALER,
    # A missed cast backfills with an RGB filler (~490); price a miss above that.
    filler_gcd_potency=490,
    # Tincture: effective BiS Intelligence incl. party-comp bonus + food (the
    # xivgear party-bonus-inclusive convention; same-ilvl Casting set as RDM/BLM
    # by construction).
    tincture_main_stat=6838,
)
