"""Black Mage data tables (Dawntrail, level 100, verified patch 7.51) + `JOB_DATA`.

Single source of truth for BLM numbers: potencies, cast times, cooldowns, the MP
economy, the Astral Fire / Umbral Ice phase machine, Polyglot / Astral Soul, and
the opener. Potencies / cast times / recasts are cross-checked against the
console wiki per-ability pages (whose patch histories carry NO BLM PvE change
between 7.2 and 7.51) and against live top parses (scripts/probe_blm_ids.py,
2026-07-02 audit: ids, buff durations, Ley Lines haste ratio, phase cast mix and
the Polyglot budget all probe-confirmed).

BLM is the analyzer's **second caster** (after Red Mage) and its first job with a
true **MP phase economy**:

  * **Astral Fire / Umbral Ice.** The rotation alternates a Fire phase (Astral
    Fire III — high-potency Fire IV / Flare Star / Despair, DRAINS MP) with a
    short Ice phase (Umbral Ice III — Blizzard III/IV + Paradox, REFILLS MP).
    MP is the hard gate: a fire phase can only afford ~6 Fire IV (+Paradox)
    before the MP runs out and you must return to ice. Modeled inside the
    simulator (a phase state machine), NOT as `GaugeModel` overcap passes — MP
    isn't an overcap-waste gauge, it's a flow constraint.
  * **Essentially RNG-free** (unlike RDM's procs). DT 7.2 grants Thunderhead
    (the DoT enabler), Firestarter (instant+free Fire III), and Polyglot all
    DETERMINISTICALLY (on Enochian time / phase transitions), so the ceiling
    needs no proc-budget `sim_context` — only the per-player GCD + an entry-gauge
    payload for phase-continuation pulls (M12S-P2).

ACTION IDS below are XIVAPI-verified (every id resolves to the named BLM action
with the captured icon + oGCD flag — see ability_metadata.BUNDLED) AND probe-
verified against real top-pull cast streams (no other BLM ids appear). Potencies
/ MP costs / haste% were re-verified in the 2026-07-02 patch-currency audit. The
synthetic test-suite is id/potency-agnostic (it builds fixtures from whatever this
file declares), so tests pass regardless; the live calibration is the authority.

A scoring-basis note (the crit-neutrality analog): under Astral Fire III the fire
spells land ~1.8x on the meter (probe: Fire IV / Despair / Flare Star ≈ +74% per
potency vs the neutral spells; Paradox / Xenoglossy / Thunder are unaspected and
measure ≈ 1.0). This element multiplier is deliberately NOT modeled: the phase
structure is forced (every fire spell is always cast under AF3, for every player
and for the sim), so it is a constant per-ability factor that cancels in
delivered / idealized — exactly like crit RNG. Model it symmetrically on both
sides if per-cast pricing in real-damage units is ever wanted.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, JobData


PATCH_VERSION = "7.51"

# --- Ability IDs (DT 7.2, level 100; XIVAPI-verified) -----------------------

# Fire spells
FIRE_I         = 141     # 180p, →AF1 / removes UI; not in the ST ceiling
FIRE_III       = 152     # 290p, 3.5s cast (instant+free under Firestarter), →AF3
FIRE_IV        = 3577    # 300p, 2.0s cast, 800 MP, +1 Astral Soul; AF only
DESPAIR        = 16505   # 350p, instant @100, dumps all MP (≥800), →AF3; AF only
FLARE          = 162     # 240p (falloff), all MP; +3 Astral Soul (AoE; rare in ST)
FLARE_STAR     = 36989   # 500p (falloff), 2.0s cast; needs 6 Astral Soul
# Ice spells
BLIZZARD_I     = 142     # 180p, →UI1 / removes AF; not in the ST ceiling
BLIZZARD_III   = 154     # 290p, 3.5s cast, 800 MP, →UI3 (the ice entry)
BLIZZARD_IV    = 3576    # 300p, 2.0s cast, 800 MP, +3 Umbral Hearts; UI only
# Lightning (DoT)
HIGH_THUNDER   = 36986   # 150p + 60p/tick DoT (30s), instant; needs Thunderhead
THUNDER_III    = 153     # 120p + 50p/tick DoT (27s) — pre-92 DoT, superseded by High Thunder
# Unaspected / shared
PARADOX        = 25797   # 540p, instant, 1600 MP (AF) / 0 (UI); grants Firestarter under AF
XENOGLOSSY     = 16507   # 890p, instant; 1 Polyglot
FOUL           = 7422    # 600p (falloff), instant; 1 Polyglot (AoE; ST dumps Xenoglossy)
# AoE filler (out of the ST ceiling; potencies kept so a player's AoE casts score)
FIRE_II        = 147
HIGH_FIRE_II   = 25794
BLIZZARD_II    = 25793
HIGH_BLIZZARD_II = 25795
FREEZE         = 159
THUNDER_IV     = 7420
HIGH_THUNDER_II = 36987

# oGCDs / buttons
TRANSPOSE      = 149     # 5s, swap AF<->UI (instant; 0 potency)
MANAFONT       = 158     # 100s: full MP + AF3 + 3 Umbral Hearts + Thunderhead + Paradox
LEY_LINES      = 3573    # 120s: ~15% haste window (a reduced-GCD window)
AMPLIFIER      = 25796   # 120s: +1 Polyglot
TRIPLECAST     = 7421    # 60s, 2 charges: next 3 spells instant (movement/MP utility)
AMANIPULATION  = 155     # Aetherial Manipulation — movement (teleport to party member)
BETWEEN_LINES  = 7419    # movement (teleport to Ley Lines)
RETRACE        = 36988   # reposition Ley Lines (movement)
UMBRAL_SOUL     = 16506  # downtime-only UI/heart builder (0 damage; restores MP
                         # by UI stack — 10000 at UI3; out of combat grants
                         # UI3 + 3 hearts + full MP outright)
MANAWARD       = 157     # defensive (no DPS)
SWIFTCAST      = 7561    # role action: next spell instant; DPS effect modeled in the sim

# --- Status ids (mechanic flags; probe-verified on live logs 2026-07-02) ----
FIRESTARTER_STATUS_ID  = 165    # next Fire III instant + free
THUNDERHEAD_STATUS_ID  = 3870   # enables High Thunder
LEY_LINES_STATUS_ID    = 737    # Ley Lines placed (the full 20s window, on the player)
CIRCLE_OF_POWER_STATUS_ID = 738  # standing IN the lines (the actual haste; drops /
                                 # re-applies as the player steps out and back in)

# --- Cast times (s) — feeds the HardcastGCD timing preset --------------------
# Absent ids are instant. At level 100 the Enhanced traits make Despair / Paradox
# / Foul / High Thunder instant. In the ST line the 3.5s Fire III / Blizzard III
# are the only above-recast hardcasts, and neither ever eats its cast bar: Fire
# III is instant under Firestarter, and Blizzard III is always cast FROM Astral
# Fire III, which halves opposite-element cast times (3.5 -> 1.75s <= recast) —
# probe-confirmed (begincast->cast = cast x haste - 0.5s slidecast; every top-pull
# B3 resolves at/below the recast slot). The sub-recast casts (2.0s) only matter
# for the weave budget; the 3.0s High Fire II / High Blizzard II matter as real
# AoE hardcast slots (N>=3 windows).
CAST_TIMES: dict[int, float] = {
    FIRE_I:       2.0,
    FIRE_III:     3.5,
    FIRE_IV:      2.0,
    FLARE:        2.0,
    FLARE_STAR:   2.0,
    BLIZZARD_I:   2.0,
    BLIZZARD_III: 3.5,
    BLIZZARD_IV:  2.0,
    FIRE_II:      3.0,          # pre-82 pair — 3.0s like the High versions
    HIGH_FIRE_II: 3.0,          # wiki-verified 3.0 (was wrongly 2.0 since ship)
    BLIZZARD_II:  3.0,
    HIGH_BLIZZARD_II: 3.0,      # wiki-verified 3.0 (was wrongly 2.0 since ship)
    FREEZE:       2.0,
}

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / crit modeling). AoE values are the
# primary (full) hit. The DoT direct hits are here; the DoT tick is separate
# (HIGH_THUNDER_DOT_TICK_P), scored per cast by time-to-next (see scoring.py).
POTENCIES: dict[int, int] = {
    FIRE_I:        180,
    FIRE_III:      290,
    FIRE_IV:       300,
    DESPAIR:       350,
    FLARE:         240,
    FLARE_STAR:    500,
    BLIZZARD_I:    180,
    BLIZZARD_III:  290,
    BLIZZARD_IV:   300,
    HIGH_THUNDER:  150,
    THUNDER_III:   120,
    PARADOX:       540,
    XENOGLOSSY:    890,
    FOUL:          600,
    # AoE primary (first-target) potency. Per-extra-target falloff lives in
    # AOE_POTENCIES. Fire II / Blizzard II are the pre-82 versions (80p), replaced
    # at level 100 by High Fire II / High Blizzard II (100p) — never cast in current
    # content, kept correct for completeness.
    FIRE_II:       80,
    HIGH_FIRE_II:  100,
    BLIZZARD_II:   80,
    HIGH_BLIZZARD_II: 100,
    FREEZE:        120,
    THUNDER_IV:    80,
    HIGH_THUNDER_II: 100,
    # Buttons with no direct potency
    TRANSPOSE:     0,
    MANAFONT:      0,
    LEY_LINES:     0,
    AMPLIFIER:     0,
    TRIPLECAST:    0,
    UMBRAL_SOUL:   0,
    MANAWARD:      0,
}

# Per-extra-target potency for the AoE-aware ceiling + delivered scoring (read by
# aoe_potency.potency_for; `secondary = primary x (1 - falloff)`, verified on the
# wiki 2026-06-22, patch 7.2). The three real falloff lines + the full-to-all AoE
# builders + Flare Star (which always cleaves, so it's "free splash" symmetric on
# both sides). High Thunder II / Thunder IV are deliberately ABSENT — their DoT
# isn't N-scored, so leaving them out credits a player's AoE-DoT cast a flat
# primary (under-credit, the <=100%-safe direction; the ceiling keeps the ST High
# Thunder for its DoT). For future modeling: High Thunder II is 100p direct +
# 40p/tick for 24s (wiki-verified 7.51).
AOE_POTENCIES: dict[int, int] = {
    FLARE:            168,   # 240, -30%
    FLARE_STAR:       175,   # 500, -65% (always cleaves)
    FOUL:             450,   # 600, -25% (7.2)
    HIGH_FIRE_II:     100,   # full-to-all
    FIRE_II:           80,   # full-to-all (pre-82)
    HIGH_BLIZZARD_II: 100,   # full-to-all
    BLIZZARD_II:       80,   # full-to-all (pre-82)
    FREEZE:           120,   # full-to-all
}

# DoT tick potencies + durations. High Thunder is the level-100 DoT; scored per
# cast by time-to-next-cast capped at the duration (the SAM Higanbana pattern).
HIGH_THUNDER_DOT_TICK_P: int = 60
HIGH_THUNDER_DOT_DURATION_S: float = 30.0
THUNDER_III_DOT_TICK_P: int = 50
THUNDER_III_DOT_DURATION_S: float = 27.0
DOT_TICK_S: float = 3.0

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under the
# test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    TRANSPOSE, MANAFONT, LEY_LINES, AMPLIFIER, TRIPLECAST,
    AMANIPULATION, BETWEEN_LINES, RETRACE, MANAWARD, SWIFTCAST,
})

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only the genuinely RECAST-gated DPS
# oGCDs live here. The phase/MP-gated GCDs (Fire IV, Flare Star, Despair, …) and
# the proc/transition-gated buttons (Paradox, High Thunder) are NOT cooldowns —
# listing them would read as false drift. Triplecast is movement/MP utility (held
# for mechanics), so it's excluded from the drift / missed-cast diff like RDM's
# gap-closers.
COOLDOWNS: dict[int, tuple[float, int]] = {
    MANAFONT:   (100.0, 1),
    LEY_LINES:  (120.0, 2),    # 2 charges since DT 7.1 (charged-action conversion)
    AMPLIFIER:  (120.0, 1),
}

# Swiftcast recast (s) — Enhanced Swiftcast (lv94) drops it to 40s. Deliberately
# NOT in COOLDOWNS: the idealized sim fires it on cooldown for a free instant, but
# a player who holds Swiftcast for a movement mechanic is never flagged for drift.
SWIFTCAST_RECAST_S: float = 40.0

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    MANAFONT:   2000,   # full MP refill -> a second Fire IV batch + Flare Star
    LEY_LINES:   600,   # ~15% haste over ~22s ≈ one extra GCD of throughput
    AMPLIFIER:   890,   # +1 Polyglot = one extra Xenoglossy
}

# --- MP economy -------------------------------------------------------------
# Max MP and the per-cast MP costs the simulator deducts in the FIRE phase.
# Audit-verified true mechanism (wiki, 7.51): under Astral Fire, fire spells cost
# DOUBLE MP, and each Umbral Heart nullifies the increase for one cast — so a full
# fire phase is 3 hearted Fire IV x800 + 3 unhearted x1600 + Paradox 1600 = 8800,
# and Despair (min 800, consumes all) takes the last 1200: EXACTLY 6 Fire IV +
# Paradox + Despair on 10000 MP. The flat 800/cast abstraction below reproduces
# that cast mix by construction (the picker's finish-the-set gate is denominated
# in the same units); on a partial-MP fire entry (post-downtime edge) it can admit
# a set the doubled-cost economy couldn't quite afford — ceiling-HIGH, i.e. the
# >100%-safe direction. Probe-confirmed live: every non-Manafont segment is
# exactly F4x6 + Pdx + FS + Despair; Manafont segments double it.
MP_CAP = 10000
# FIRE-phase MP costs the simulator deducts. Ice spells are modeled as regen
# (UI3_MP_REGEN_PER_GCD), not a deduction — MP only binds in the fire phase. Fire
# III is free under Firestarter (the only way the ST line casts it; the opener's
# no-Firestarter hardcast is handled specially); Despair / Flare consume "all
# remaining" (also special-cased in the model).
MP_COSTS: dict[int, int] = {
    FIRE_IV:  800,
    PARADOX:  1600,    # under Astral Fire
}
FIRE_III_HARDCAST_MP = 2000     # opener Fire III (no Firestarter; wiki-verified)
DESPAIR_MIN_MP = 800            # Despair requires ≥800 MP to cast
# True mechanism (wiki): no passive tick — ice spells RECOVER MP on hit, by Umbral
# Ice stack (2500 / 5000 / 10000 at UI I/II/III), so the first ice hit under UI3
# refills everything. The per-ice-GCD abstraction reaches full MP by the end of
# the same 2-GCD ice phase (Blizzard III + Blizzard IV), which is all the picker
# reads — behaviorally identical for the rotation.
UI3_MP_REGEN_PER_GCD = 5000

# --- Enochian: Polyglot + Astral Soul ---------------------------------------
# Polyglot accrues 1 stack per 30s of continuous Enochian (always active once the
# opener establishes AF/UI). Max 3 at level 100 (Enhanced Polyglot II). Spent by
# Xenoglossy (890) / Foul (600). Amplifier grants +1 immediately. Deterministic —
# no RNG, so no proc-budget sim_context (unlike RDM).
POLYGLOT_INTERVAL_S: float = 30.0
POLYGLOT_CAP = 3
# Astral Soul: Fire IV +1, Flare +3; at 6 (cap), Flare Star spends all 6.
ASTRAL_SOUL_CAP = 6
UMBRAL_HEARTS_CAP = 3

# --- Ley Lines haste --------------------------------------------------------
# Standing in Ley Lines (Circle of Power) reduces GCD recast + cast time by 15%.
# Modeled as a reduced-GCD window (a gcd_duration multiplier) for its duration —
# the caster analog of MCH Overheated. Live-verified 2026-07-02: measured
# in-window/outside GCD-cadence ratio 0.849-0.855 across three top pulls; window
# 19.96-19.98s. (The ceiling assumes the player stands in their lines full
# duration; a player who walks out simply scores below it.)
LEY_LINES_HASTE: float = 0.85
LEY_LINES_DURATION_S: float = 20.0    # DT 7.2 (reduced from 30s); wiki + probe

# --- Canonical opener -------------------------------------------------------
# First 12 in-fight GCDs in expected order — the MEASURED consensus opener
# (probe 2026-07-02: three independent top Tyrant pulls, byte-identical GCD
# order): pre-pull Fire III, Thunder, five Fire IV, Xenoglossy at five souls
# (Manafont weaved after it), the sixth Fire IV closing the set, Flare Star,
# then the Manafont batch. No fire Paradox in the opener; Triplecast/Amplifier/
# Ley Lines/pot are weaves, not GCDs. OpenerAspect is a zero-priced diagnostic.
CANONICAL_OPENER: tuple[int, ...] = (
    FIRE_III,      # pre-pull hardcast, lands at t≈0
    HIGH_THUNDER,  # DoT up
    FIRE_IV,
    FIRE_IV,
    FIRE_IV,
    FIRE_IV,
    FIRE_IV,
    XENOGLOSSY,
    FIRE_IV,       # 6th soul (Manafont weaved after the Xenoglossy)
    FLARE_STAR,
    FIRE_IV,
    FIRE_IV,
)

# --- Clip-detection skip windows -------------------------------------------
# Ley Lines hastes every GCD inside its window to ~2.1s, which the clip detector
# (tuned to the 2.5s global) would otherwise read as a chain of clips. Exclude the
# window, like MCH Hypercharge's 1.5s Blazing chain.
CLIP_SKIP_WINDOWS: dict[int, float] = {LEY_LINES: LEY_LINES_DURATION_S}

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these): the 2-min
# burst enablers + payoff.
BURST_ABILITIES: frozenset[int] = frozenset({
    MANAFONT, LEY_LINES, AMPLIFIER, FLARE_STAR, XENOGLOSSY, DESPAIR,
})

# Enablers whose value is throughput, not standalone table potency — priced by the
# sim's marginal contribution (scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (MANAFONT, LEY_LINES, AMPLIFIER)

# Interchangeable high-value filler GCDs whose under-count vs the ideal is the
# diffuse "cast a filler where the ideal casts a Fire IV" loss the cooldown
# missed-cast diff structurally can't see. Drives the "Filler quality" card.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({FIRE_IV})

# Defensive / utility oGCDs the simulator never fires (excluded from the DPS
# timeline + cast-diff). Movement teleports + Manaward; Addle/Swiftcast/Lucid/
# Surecast are shared role actions (role_actions.ROLE_ACTION_IDS).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    MANAWARD, AMANIPULATION, BETWEEN_LINES, RETRACE,
})


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Black Mage",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(),                          # MP / Enochian live in the simulator, not as overcap gauges
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_skip_windows=CLIP_SKIP_WINDOWS,
    drift_exclusions=frozenset(),
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),
    charge_sharing={},
    raid_buffs={},                      # raid buffs modeled via buff_windows (job-agnostic)
    role_policy=CASTER_HEALER,
    # A dropped tool backfills with a Fire IV (~300); price a miss above that.
    filler_gcd_potency=300,
    # Tincture: effective BiS Intelligence incl. party-comp bonus + food (the
    # xivgear party-bonus-inclusive convention). Casters share the same-ilvl
    # Casting set, so this equals RDM's value by construction. Audit 2026-07-02:
    # the empirical log route is unrecoverable for BLM too (pots always overlap
    # raid buffs — the calibrate_tincture.py "diluted buff" verdict), so the
    # formula M = f(base+Δ)/f(base) on this stat is authoritative. Same M on both
    # sides, so its effect on rank is self-correcting regardless.
    tincture_main_stat=6838,
)
