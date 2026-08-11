"""Sage data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for SGE numbers: potencies, cast times, cooldowns, and the
opener. SGE is the analyzer's fourth **healer** (after WHM + AST + SCH) and the
second shield healer (H1). It models on the damage side like AST/SCH — a nearly
pure Dosis III filler line, no GCD-recast haste window — with two additions:

  * **The Eukrasia DoT sequence** — the DoT (Eukrasian Dosis III) is applied by a
    2-GCD chain: **Eukrasia** (a 0-potency instant GCD) transforms the next spell
    into **Eukrasian Dosis III**. This is the NIN-lite forced sequence; the id
    itself carries the "eukrasified" state (`eukrasia_active` in the simulator).
    The DoT is scored per cast by time-to-next-application (scoring.py), so an
    early refresh credits less — overcap-safe.
  * **The Phlegma charge economy** — Phlegma III is a 2-charge GCD (~40s/charge)
    that out-potencies the filler, so the ceiling dumps a charge whenever one is
    ready (regen never wasted). Psyche is the lone damage oGCD (~60s).

Three facts shape the model (see jobs/sage/__init__.py):
  * **SGE brings NO party damage buff** — Kardia is a single-target heal link, not
    a raid buff — so there is NO raid_buffs.py entry (unlike AST Divination / SCH
    Chain Stratagem). The scorer is a pure `table × multiplier_at(buff_intervals)`
    + the Eukrasian Dosis DoT; buff_intervals only ever carries OTHER jobs' buffs.
  * **SGE has NO pet** (no fairy/egi) — aux is always 0.
  * **SGE has NO GCD-recast haste** (no Presence-of-Mind analog), so the GCD is
    flat 2.5s (SpS-scaled) → `demonstrated_cadence_anchor` is valid (scoring.py),
    like AST/SCH, the opposite of WHM.

**Gauges = () — no gauge int is modeled (simpler than SCH's Aetherflow):**
  * **Addersgall** (4 / timer-regen) is HEAL-ONLY (Druochole/Taurochole/Kerachole/
    Ixochole) → not modeled (gotcha #4, defensive-only).
  * **Addersting** (3, from Eukrasian Diagnosis shields popping) feeds **Toxikon
    II** — Toxikon II (380) EQUALS Dosis III (380), so casting it is potency-neutral
    with the filler: the pure-damage ceiling casting Dosis in that slot is
    equivalent, so Toxikon stays delivered-scored movement filler only (the SCH Ruin
    II pattern) — it needs a POTENCIES entry so real casts score, but Addersting
    gates nothing the ceiling needs, so there is no SimState resource int at all.
    (It's instant and the sim already assumes an un-clipped Dosis, so a real player
    can't out-score the ceiling with it.)
  MP is deliberately NOT modeled (only gauges that bind the offensive rotation).

**Pneuma** (~120s GCD, 380p) likewise EQUALS Dosis III (380) and additionally heals,
so the pure-damage ceiling casting a Dosis in that slot is potency-equivalent →
Pneuma is delivered-scored only. A real SGE casts it on cooldown for the free party
heal; because it's potency-neutral with the filler there's no throughput give-up
either way (the heal is the point).

Potencies are wiki-verified (ffxiv.consolegameswiki.com, 2026-07-18): Dosis III 380,
Eukrasian Dosis III 90/tick over 30s, Phlegma III 690 (2 charges / 40s), Psyche 690
(60s oGCD), Toxikon II 380, Pneuma 380, Dyskrasia II 170. Ability IDs + names + icons
+ oGCD category are XIVAPI-verified (scripts/probe_sage_ids.py, 39/39). Eukrasian
Prognosis II id 37034 confirmed from python/mitplan/library.py.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, JobData


PATCH_VERSION = "7.2"

# --- Ability IDs (XIVAPI-verified names/icons/category; ⚠️ = probe potency) ---

# Damage rotation (level-100 upgraded ids)
DOSIS_III           = 24312   # 380p, 1.5s cast — the filler nuke
EUKRASIA            = 24290   # 0-potency instant GCD — transforms the next spell
EUKRASIAN_DOSIS_III = 24314   # the DoT (90p/tick over 30s), applied via Eukrasia; instant
PHLEGMA_III         = 24313   # 2-charge GCD (40s/charge), 690p — burst + movement
PSYCHE              = 37033   # damage oGCD (60s CD, 690p) — the lone oGCD nuke
DYSKRASIA_II        = 24315   # AoE nuke GCD (MT-only filler swap)
EUKRASIAN_DYSKRASIA = 37032   # AoE DoT (deferred — see SPLASH/POTENCIES notes)
TOXIKON_II          = 24316   # instant GCD (Addersting spender); delivered-only filler
PNEUMA              = 24318   # ~120s GCD, damage + heal; delivered-only (below Dosis)

# --- Rotation constants ------------------------------------------------------
DOSIS_III_CAST_S: float = 1.5
SGE_GCD_S: float = 2.5
# Eukrasia has a REDUCED ~1.0s recast (wiki-verified), NOT the standard 2.5s GCD —
# the NIN-mudra pattern. So SGE is a MIXED fixed-rate job (like NIN): the fast ~1.0s
# Eukrasia interleaves with the normal 2.5s GCDs. This is load-bearing — modeling
# Eukrasia at a full 2.5s wastes ~1.5s per DoT refresh and pushes the ceiling too LOW
# (live M11S top parses read >100%). The demonstrated-cadence anchor is therefore made
# MIXED-AWARE (scoring.py: it subtracts the fixed Eukrasia time so the anchor reflects
# the sustained NORMAL cadence, not an average that folds the fast slots in — the NIN
# caveat). Modeled as a fixed 1.0s constant: the wiki tags the recast as a GCD (so it
# may scale a hair with Spell Speed, ~0.98s on fast gear), but the ~0.02s difference is
# negligible and self-correcting (a faster Eukrasia only RAISES the ceiling → safe).
EUKRASIA_GCD_S: float = 1.0
# Eukrasian Dosis III ALSO runs at a fixed, speed-IMMUNE reduced recast — wiki:
# "Instant / 1.5s (GCD)"; live top parses read a rock-stable 1.51s cast→next-GCD
# across all gear (940 samples), vs the ~2.40s hasted filler. So the DoT-refresh
# sequence Eukrasia (1.0s) + Eukrasian Dosis III (1.5s) = 2.5s = exactly ONE base
# GCD (the NIN mudra+ninjutsu pattern). Modeling Eukrasian Dosis at the full 2.5s
# recast over-charged every refresh by ~1.0s → the ceiling wasted ~18s of GCD
# budget over a fight → read >100% on tight M12S-P2 parses.
EUKRASIAN_DOSIS_GCD_S: float = 1.5

# The speed-IMMUNE fixed-rate GCDs (id → recast). Everything else runs at the
# (SpS-hasted) filler GCD. Single source of truth, consumed by BOTH the simulator's
# gcd_duration/weave_budget AND the scorer's mixed-aware demonstrated-cadence anchor,
# so the two can never disagree about which GCDs are fast.
FIXED_RATE_GCDS: dict[int, float] = {
    EUKRASIA:            EUKRASIA_GCD_S,        # 1.0s setup GCD
    EUKRASIAN_DOSIS_III: EUKRASIAN_DOSIS_GCD_S,  # 1.5s DoT-application GCD
}

EUKRASIAN_DOSIS_DOT_DURATION_S: float = 30.0
EUKRASIAN_DOSIS_DOT_TICK_S: float = 3.0
EUKRASIAN_DOSIS_DOT_TICK_P: int = 90     # per-tick (wiki-verified: 90 potency / 30s)

PHLEGMA_CHARGES: int = 2
PHLEGMA_CD_S: float = 40.0               # ⚠️ probe (per-charge recharge)
PSYCHE_CD_S: float = 60.0                # ⚠️ probe

# --- Healing / mitigation / utility kit ability ids (non-rotational) ---------
# GCD heals / shields (also the costed-heal currency). The Eukrasian* shields are
# each a 2-GCD sequence (Eukrasia -> the shield); we count the shield cast itself.
DIAGNOSIS              = 24284   # is_gcd ST heal
PROGNOSIS              = 24286   # is_gcd AoE heal
EGEIRO                 = 24287   # is_gcd raise
EUKRASIAN_DIAGNOSIS    = 24291   # is_gcd ST shield
EUKRASIAN_PROGNOSIS    = 24292   # is_gcd AoE shield
EUKRASIAN_PROGNOSIS_II = 37034   # is_gcd AoE shield (Dawntrail); the LOCKED heal (mitplan)
# oGCD heals / mit / utility (Addersgall + cooldowns)
KARDIA        = 24285   # heal-link (single target); oGCD toggle
SOTERIA       = 24294   # oGCD (boosts Kardia healing)
ICARUS        = 24295   # oGCD gap-closer (dash)
DRUOCHOLE     = 24296   # Addersgall oGCD ST heal
KERACHOLE     = 24298   # Addersgall oGCD AoE mit + regen (from mitplan)
IXOCHOLE      = 24299   # Addersgall oGCD AoE heal (from mitplan)
ZOE           = 24300   # oGCD (boosts the next heal/shield)
PEPSIS        = 24301   # oGCD (pops shields for a heal)
PHYSIS_II     = 24302   # oGCD AoE regen + healing-received (from mitplan)
TAUROCHOLE    = 24303   # Addersgall oGCD ST heal + mit (from mitplan)
HAIMA         = 24305   # oGCD ST stacking shield (from mitplan)
RHIZOMATA     = 24309   # oGCD Addersgall refill (heal-side)
HOLOS         = 24310   # oGCD AoE mit + heal + shield (from mitplan)
PANHAIMA      = 24311   # oGCD AoE stacking shield (from mitplan)
KRASIS        = 24317   # oGCD (boosts healing-received)
PHILOSOPHIA   = 37035   # oGCD healer stance (Dawntrail)

# --- Cast times (s) — feeds the HardcastGCD timing preset -------------------
# Absent ids are instant (Eukrasia / Eukrasian Dosis III / Phlegma III / Toxikon /
# Pneuma / Dyskrasia / all oGCDs). Dosis III's 1.5s cast is shorter than the 2.5s
# recast, so the slot is always recast-bound; the cast only costs a weave slot.
# Eukrasian Prognosis II (2.0s cast) is only ever cast by the sim as a mit-plan
# LOCKED heal — the unlocked rotation never fires it, so adding it here leaves every
# unlocked run byte-identical (the AST/SCH pattern).
CAST_TIMES: dict[int, float] = {
    DOSIS_III:              DOSIS_III_CAST_S,
    EUKRASIAN_PROGNOSIS_II: 2.0,   # locked heal GCD (mit-plan integration)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) -----------
# ability_id -> per-target potency. Dyskrasia II is the AoE filler the sim swaps to
# at high target counts. ⚠️ verify-live. Eukrasian Dyskrasia (AoE DoT) is a deferred
# lever (out of scope for v1, like AST/SCH AoE-beyond-free-splash) — not scored.
AOE_POTENCIES: dict[int, int] = {
    DYSKRASIA_II: 170,   # wiki-verified
}

# --- Potencies ---------------------------------------------------------------
# ability_id -> base potency. EUKRASIAN_DOSIS_III carries 0 here — the DoT is scored
# per cast by time-to-next-application (scoring.py, the SCH Biolysis / AST Combust
# pattern) so an early refresh credits less. EUKRASIA is a 0-potency setup GCD.
# TOXIKON_II and PNEUMA are delivered-only fillers (both below Dosis III → the sim
# never casts them; the ceiling prefers Dosis), scored when the player casts them.
# ⚠️ probe values.
POTENCIES: dict[int, int] = {
    DOSIS_III:           380,   # wiki-verified (filler)
    EUKRASIA:              0,    # setup GCD (transforms next spell)
    EUKRASIAN_DOSIS_III:   0,    # DoT scored per-cast by time-to-next (scoring.py)
    PHLEGMA_III:         690,   # wiki-verified (charge burst GCD)
    PSYCHE:              690,   # wiki-verified (oGCD nuke)
    DYSKRASIA_II:        170,   # wiki-verified (AoE filler; MT-only)
    TOXIKON_II:          380,   # wiki-verified (== Dosis III → delivered-only filler)
    PNEUMA:              380,   # wiki-verified (== Dosis III → delivered-only; heals)
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under the
# test stub. Psyche is the ONLY damage oGCD; everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({PSYCHE})

# --- Cooldowns + charges -----------------------------------------------------
# The genuinely RECAST-gated DPS abilities the sim casts. Phlegma III is a 2-charge
# GCD (engine multi-charge regen); Psyche is a 60s oGCD. Eukrasia is a state-flag
# sequence, NOT a cooldown (would read as false drift, gotcha #2). Toxikon/Pneuma
# are never cast by the sim, so they are not gated here.
COOLDOWNS: dict[int, tuple[float, int]] = {
    PHLEGMA_III: (PHLEGMA_CD_S, PHLEGMA_CHARGES),
    PSYCHE:      (PSYCHE_CD_S, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    PHLEGMA_III: 690,   # ~one Phlegma III
    PSYCHE:      690,   # ~one Psyche
}

# --- Canonical opener --------------------------------------------------------
# First ~12 in-fight GCDs (the pre-pull Dosis III channel is separate). The DoT is
# applied first via the Eukrasia sequence, Phlegma charges dumped early, then Dosis
# III filler with Psyche weaving. OpenerAspect is a zero-priced diagnostic.
# ⚠️ refine to the measured M11S top-parse consensus during calibration.
CANONICAL_OPENER: tuple[int, ...] = (
    EUKRASIA,
    EUKRASIAN_DOSIS_III,
    DOSIS_III,
    PHLEGMA_III,
    PHLEGMA_III,
    DOSIS_III,
    DOSIS_III,
    DOSIS_III,
    DOSIS_III,
    DOSIS_III,
    DOSIS_III,
    DOSIS_III,
)

# --- Non-rotational ids ------------------------------------------------------
# The healing / mitigation / utility kit — real casts with no SGE-self DPS value the
# simulator never fires. Excluded from the DPS timeline + cast-diff (isDefensive on
# the wire), rendered on the Defensives lane. NOT here: Dosis III / Eukrasia /
# Eukrasian Dosis III / Phlegma III / Dyskrasia II / Toxikon II / Pneuma (damage
# GCDs); Psyche (damage oGCD).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    DIAGNOSIS, PROGNOSIS, EGEIRO, EUKRASIAN_DIAGNOSIS, EUKRASIAN_PROGNOSIS,
    EUKRASIAN_PROGNOSIS_II,
    KARDIA, SOTERIA, ICARUS, DRUOCHOLE, KERACHOLE, IXOCHOLE, ZOE, PEPSIS,
    PHYSIS_II, TAUROCHOLE, HAIMA, RHIZOMATA, HOLOS, PANHAIMA, KRASIS, PHILOSOPHIA,
})

# --- Costed heal GCDs ---------------------------------------------------------
# GCD heals/shields that displace a damage GCD (a Dosis III) when cast during uptime
# — the currency of the mit-plan lock accounting and the "extra healing GCDs beyond
# the plan" improvement card. The Eukrasian shields count the shield cast itself (the
# paired Eukrasia is not separately counted).
COSTED_HEAL_GCD_IDS: frozenset[int] = frozenset({
    DIAGNOSIS, PROGNOSIS, EUKRASIAN_DIAGNOSIS, EUKRASIAN_PROGNOSIS,
    EUKRASIAN_PROGNOSIS_II,
})

# --- Burst-alignment abilities ----------------------------------------------
# SGE has no self damage-buff window; Phlegma III + Psyche are the high-potency casts
# a good SGE aligns into the party's 2-minute raid windows.
BURST_ABILITIES: frozenset[int] = frozenset({PHLEGMA_III, PSYCHE})

# SGE has no enabler (nothing a cast unlocks, unlike SCH Chain Stratagem -> Baneful).
ENABLER_IDS: tuple[int, ...] = ()

# Interchangeable filler GCDs whose under-count vs the ideal is the diffuse "healed
# with a GCD where the ideal casts Dosis III" loss — THE healer story, structurally
# invisible to the cooldown missed-cast diff.
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({DOSIS_III})

# --- Multi-target ------------------------------------------------------------
# Free-splash crediting is a DEFERRED lever for v1 (like AST/SCH AoE-beyond-free-
# splash): Phlegma III / Pneuma cleave and Eukrasian Dyskrasia is an AoE DoT, but
# their falloffs need live probing and the ST gate (M11S) does not exercise them.
# Empty -> single-target crediting only, no multi-target over-credit risk. ⚠️ add
# Phlegma III's secondary-target potency here during AoE calibration if needed.
SPLASH_POTENCIES: dict[int, int] = {}


# --- JOB_DATA bundle ---------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Sage",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    # No JobData GaugeModel: Addersgall is heal-only and Addersting gates only
    # Toxikon (which the ceiling never casts, being below Dosis III), so no gauge
    # binds the offensive rotation. MP never binds the optimized line either.
    gauges=(),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    burst_abilities=BURST_ABILITIES,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    role_policy=CASTER_HEALER,
    filler_gcd_potency=380,
    # Tincture of Mind. ⚠️ Placeholder: effective BiS Mind incl. party bonus + food,
    # mirrored from the WHM/AST/SCH/RDM caster value; refine per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat=6838,
)
