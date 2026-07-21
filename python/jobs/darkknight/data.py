"""Dark Knight data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

The analyzer's fourth TANK (after Paladin, Warrior, Gunbreaker) and its eighteenth
full idealized simulator. Single source of truth for DRK numbers: potencies, the
recast-gated cooldowns, the Blood + MP dual economy, the Delirium chain, Darkside,
and the Living Shadow (Esteem) pet fold.

What makes DRK structurally distinct from the tanks already shipped:

  * **A dual offensive economy**: the Blood Gauge (Souleater/Stalwart +20, Delirium's
    Blood Weapon +10 per weaponskill; Bloodspiller/Quietus -50) AND an MP budget for
    Edge of Shadow (3000 MP each) fed by the passive combat tick + combo/Carve/chain
    grants. MP is deliberately NOT a `GaugeModel`: most of its income is the TIME-based
    combat tick (200 per 3s), invisible to the cast-id-keyed generators the shared
    OvercapAspect walks — it lives as bespoke simulator state instead (simulator.py).
  * **The Blackest Night is MP-NET-NEUTRAL and deliberately un-modeled.** TBN costs
    3000 MP; a broken shield grants Dark Arts (a free Edge/Flood = a 3000 MP refund).
    Top parses pop 100% of TBNs (probe: 8/5/4 pops, 0 expiries), so the ceiling's MP
    ledger simply ignores TBN — probe-validated on three top M11S pulls: predicted
    Edge budgets 40.3/40.8/38.7 vs actual 40/40/38 casts. A player who lets TBN expire
    net-loses 3000 MP (one Edge) and is docked naturally on the delivered side.
  * **Darkside (+10%, refreshed +30s capped at 60s by Edge/Flood) is INVISIBLE to
    FFLogs** — no status apply event, no aura token on damage snapshots, and not in the
    `multiplier` field (probe: filler-phase hits read mult=1.0 exactly). It is instead
    a pure deterministic function of the Edge/Flood cast times, reconstructed
    identically in the delivered scorer and the sim state (the GNB No-Mercy
    derived-from-timeline pattern, with extend-and-cap semantics).
  * **Living Shadow = the SMN fixed-count pet fold.** Esteem executes a FIXED 5-hit
    sequence (probe: identical in all 17 windows across 5 pulls) whose summed nominal
    potency is folded as a CONSTANT onto the Living Shadow cast id. Esteem's own ids
    (ESTEEM_IDS) never appear in the player's cast stream and are never scored.
  * **The Delirium chain is INTERLEAVABLE** (probe: the basic combo survives the chain;
    Disesteem observed mid-chain) — chain steps are beam candidates, not GNB-style
    hard-forced GCDs.

ACTION IDS + POTENCIES are PROBE-VERIFIED against live top-DRK logs
(scripts/probe_darkknight_{ids,potency,petdump}.py, 2026-07-04/05: 5x M11S, 2x M9S,
2x M12S-P2 pulls): every player potency below matched the multiplier-deconvolved
measurement within +-1% (anchor Edge of Shadow 460; Hard Slash 298.0, Syphon 377.8,
Souleater 482.4, Bloodspiller 597.7, Scarlet 622.0, Comeuppance 718.5, Torcleaver
823.9, Salt and Darkness 499.3, Unmend 150.2). Shadowbringer/Disesteem/Carve read
+1-3% high — they land at burst-window EDGES where the damage snapshots cast-time
buffs but FFLogs' multiplier reflects impact-time auras; nominal tooltip values kept.
Cross-checked vs ffxiv.consolegameswiki.com. Still per-tier: the tincture stat.
"""
from __future__ import annotations

from jobs._core.job import MELEE_TANK, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100; probe-verified from live logs) ---------

# Single-target combo
HARD_SLASH        = 3617     # combo starter
SYPHON_STRIKE     = 3623     # 2nd combo hit; +600 MP on combo
SOULEATER         = 3632     # 3rd combo hit; +20 Blood
# Blood spenders
BLOODSPILLER      = 7392     # -50 Blood
QUIETUS           = 7391     # -50 Blood; AoE (multi-target windows only)
# Delirium chain (enhanced spenders; no Blood cost; each +10 Blood +800 MP via
# Blood Weapon + the chain restore — see MP constants below)
SCARLET_DELIRIUM  = 36928    # chain 1 (from Bloodspiller under Delirium)
COMEUPPANCE       = 36929    # chain 2
TORCLEAVER        = 36930    # chain 3 (capstone)
IMPALEMENT        = 36931    # the AoE Delirium spender (non-chaining)
# AoE combo (multi-target windows only)
UNLEASH           = 3621     # AoE starter
STALWART_SOUL     = 16468    # AoE 2nd; +20 Blood, +600 MP on combo
# Scorn payoff GCD (granted by Living Shadow)
DISESTEEM         = 36932    # requires Scorn; line AoE, 25% falloff
# Ranged filler (not part of the uptime rotation)
UNMEND            = 3624     # ranged spell (live name "Unmend", NOT "Unmending")

# oGCDs — burst + damage
EDGE_OF_SHADOW    = 16470    # 3000 MP; refreshes Darkside; 1s recast (double-weavable)
FLOOD_OF_SHADOW   = 16469    # AoE Edge (shares the 1s recast); multi-target only
DELIRIUM          = 7390     # 60s; grants Delirium x3 + Blood Weapon x3 (both 15s)
LIVING_SHADOW     = 16472    # 120s; summons Esteem (fixed 5-hit fold) + grants Scorn 30s
SALTED_EARTH      = 3639     # 90s; ground DoT, 6 ticks x 50 over 15s (folded)
SALT_AND_DARKNESS = 25755    # 20s recast, gated on the live Salted Earth patch (1:1)
SALT_AND_DARKNESS_DMG = 25756  # its separate damage id (never in the cast stream)
CARVE_AND_SPIT    = 3643     # 60s; +600 MP; shares the recast with Abyssal Drain
ABYSSAL_DRAIN     = 3641     # AoE Carve (shared 60s recast); multi-target only
SHADOWBRINGER     = 25757    # 60s x 2 charges; line AoE, 25% falloff; needs Darkside

# Esteem (the Living Shadow pet). Its damage logs under these SEPARATE ids (also
# mirrored into the owner's DamageDone stream — gotcha #11), which never appear in
# the player's cast stream, so they are never scored directly: the whole sequence
# is folded as a constant onto the LIVING_SHADOW cast id (POTENCIES below).
ESTEEM_ABYSSAL_DRAIN  = 17904   # +7.9s after the summon cast
ESTEEM_SHADOWBRINGER  = 25881   # +11.9s
ESTEEM_EDGE_OF_SHADOW = 17908   # +14.1s
ESTEEM_BLOODSPILLER   = 17909   # +16.4s
ESTEEM_DISESTEEM      = 36933   # +19.5s
ESTEEM_IDS: frozenset[int] = frozenset({
    ESTEEM_ABYSSAL_DRAIN, ESTEEM_SHADOWBRINGER, ESTEEM_EDGE_OF_SHADOW,
    ESTEEM_BLOODSPILLER, ESTEEM_DISESTEEM,
})

# Defensive / utility (NO damage value; excluded from the DPS diff via isDefensive)
GRIT              = 3629     # tank stance
RELEASE_GRIT      = 32067
THE_BLACKEST_NIGHT = 7393    # 3000 MP shield; MP-net-neutral when popped (see header)
SHADOW_WALL       = 3636     # pre-92 mitigation (kept for lower-level logs)
SHADOWED_VIGIL    = 36927    # upgrade of Shadow Wall
DARK_MIND         = 3634
DARK_MISSIONARY   = 16471    # party mitigation
OBLATION          = 25754    # 2 charges
LIVING_DEAD       = 3638     # invuln
SHADOWSTRIDE      = 36926    # gap closer (mobility, no damage)

DEFENSIVE_IDS: frozenset[int] = frozenset({
    GRIT, RELEASE_GRIT, THE_BLACKEST_NIGHT, SHADOW_WALL, SHADOWED_VIGIL,
    DARK_MIND, DARK_MISSIONARY, OBLATION, LIVING_DEAD, SHADOWSTRIDE,
})

# --- Status (buff/debuff) IDs ----------------------------------------------
# Probe-verified from live Buffs streams. Darkside has NO status id — it never
# emits aura events (see the module header). Kept for documentation.
BLOOD_WEAPON_STATUS_ID   = 742    # 3 stacks, granted by Delirium (verified)
DELIRIUM_STATUS_ID       = 3836   # 3 stacks (verified)
SCORN_STATUS_ID          = 3837   # 30s, enables Disesteem (verified)
SALTED_EARTH_STATUS_ID   = 749    # the patch marker; ticks log under 1000749
BLACKEST_NIGHT_STATUS_ID = 1178   # the TBN shield (verified; pops < 7s)

# --- Darkside ----------------------------------------------------------------
# +10% to ALL damage while active. Refreshed by Edge/Flood of Shadow: +30s, capped
# at 60s, starting from zero at the pull. Reconstructed from the Edge/Flood cast
# times identically on the delivered and idealized sides (scoring.py / simulator.py)
# — never from log auras (FFLogs does not record it).
DARKSIDE_MULT: float       = 1.10
DARKSIDE_EXTEND_S: float   = 30.0
DARKSIDE_CAP_S: float      = 60.0

# --- Delirium / Blood Weapon --------------------------------------------------
# One button grants both 15s buffs: Delirium x3 (Bloodspiller -> the Scarlet chain;
# Quietus -> Impalement) and Blood Weapon x3 (each weaponskill +10 Blood + 600 MP).
# The chain GCDs additionally restore 200 MP each. The chain is interleavable and
# the basic combo survives it (probe-verified); stacks die with the 15s buff.
DELIRIUM_DURATION_S: float = 15.0
DELIRIUM_STACKS: int       = 3
BLOOD_WEAPON_STACKS: int   = 3
BLOOD_WEAPON_BLOOD: int    = 10
SCORN_DURATION_S: float    = 30.0
COMBO_TIMEOUT_S: float     = 30.0

# --- MP economy ---------------------------------------------------------------
# See the module header: TBN is MP-net-neutral and un-modeled; the ledger below is
# probe-validated to within one Edge on live top parses.
MP_MAX: int                = 10000
MP_PER_TICK: int           = 200     # the passive combat tick
MP_TICK_S: float           = 3.0
EDGE_MP_COST: int          = 3000
COMBO_MP_GRANT: int        = 600     # Syphon Strike / Stalwart Soul (combo'd)
CARVE_MP_GRANT: int        = 600
BLOOD_WEAPON_MP: int       = 600     # per Blood Weapon stack (any weaponskill)
CHAIN_RESTORE_MP: int      = 200     # per Delirium-chain GCD, on top of Blood Weapon

# --- Potencies ------------------------------------------------------------------
# action_id -> base potency (no buffs / crit modeling). Combo abilities carry their
# COMBO'd value. Probe-verified (multiplier-deconvolved, 5 pulls) within +-1%.
POTENCIES: dict[int, int] = {
    # Single-target combo
    HARD_SLASH:        300,
    SYPHON_STRIKE:     380,   # combo'd (240 uncombo'd)
    SOULEATER:         480,   # combo'd (260 uncombo'd); +20 Blood
    # Blood spenders
    BLOODSPILLER:      600,
    QUIETUS:           240,
    # Delirium chain
    SCARLET_DELIRIUM:  620,
    COMEUPPANCE:       720,
    TORCLEAVER:        820,
    IMPALEMENT:        300,
    # AoE combo
    UNLEASH:           120,
    STALWART_SOUL:     160,   # combo'd (120 uncombo'd); +20 Blood
    # Scorn payoff
    DISESTEEM:        1000,
    # Ranged filler
    UNMEND:            150,
    # oGCDs
    EDGE_OF_SHADOW:    460,
    FLOOD_OF_SHADOW:   160,
    # Salted Earth: the ground DoT folded as a constant — 6 ticks x 50 over the
    # 15s patch (probe: 6 tick events per full window, ticks under id 1000749,
    # per-tick ~50 after crit-averaging). Safe as a flat fold: the 90s recast >>
    # 15s duration means it can never be clip-refreshed, and the ticks snapshot
    # buffs at cast — exactly how the fold prices it (symmetric on both sides).
    SALTED_EARTH:      300,
    SALT_AND_DARKNESS: 500,
    CARVE_AND_SPIT:    540,
    ABYSSAL_DRAIN:     240,
    SHADOWBRINGER:     600,
    # Living Shadow: the SMN fixed-count pet fold. Esteem's 5-hit sequence at
    # NOMINAL tooltip potencies: Abyssal Drain 420 + Shadowbringer 570 + Edge of
    # Shadow 420 + Bloodspiller 420 + Disesteem 620 = 2450, credited at the summon
    # cast on BOTH the delivered and idealized sides (symmetric -> cancels in the
    # ratio; the sim never holds it at the fight tail, so a player's truncated
    # tail summon is always matchable). Measured pet damage-per-potency runs
    # ~1.05-1.07x the player's per tooltip potency (uniform across all 5 hits) —
    # consistent with Esteem snapshotting summon-time buffs that FFLogs'
    # impact-time multiplier misses; kept nominal, a calibration lever only.
    LIVING_SHADOW:    2450,
}

# --- Splash (free-splash secondary potency) ------------------------------------
# ability_id -> potency dealt to EACH additional target beyond the primary, for the
# innately-cleaving casts the SINGLE-TARGET rotation already makes. Credited
# symmetrically on delivered + ceiling when a pull affords multi-target. Falloff
# baked in (25% per the wiki; the M9S falloff probe is target-defense contaminated,
# so wiki values are used — conservative). The LIVING_SHADOW fold stays single-
# target (Esteem's cleaves un-credited = under-credit-safe).
SPLASH_POTENCIES: dict[int, int] = {
    DISESTEEM:         750,   # 1000 x 0.75
    SHADOWBRINGER:     450,   # 600 x 0.75
    SALT_AND_DARKNESS: 375,   # 500 x 0.75
    SALTED_EARTH:      300,   # ground patch ticks all targets in it, full
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts at N>=3) ------
# Full-to-all (point-blank AoEs have no falloff). Only consumed when the
# multi-target fork is active; the single-target sim never casts these.
AOE_POTENCIES: dict[int, int] = {
    UNLEASH:         120,
    STALWART_SOUL:   160,
    QUIETUS:         240,
    IMPALEMENT:      300,
    FLOOD_OF_SHADOW: 160,
    ABYSSAL_DRAIN:   240,
}

# oGCD set — kept job-local and MIRRORED into ability_metadata.BUNDLED (which is what
# the Clipping aspect + GCD-speed inference actually read), so the GCD/oGCD split stays
# hermetic under the test stub. Everything else in POTENCIES is a GCD. Esteem's ids
# are flagged oGCD too: they never occupy a player GCD slot (the SMN pet-id precedent).
# test_darkknight_sim.test_ability_metadata_bundled pins the mirror.
OGCD_IDS: frozenset[int] = frozenset({
    EDGE_OF_SHADOW, FLOOD_OF_SHADOW, DELIRIUM, LIVING_SHADOW, SALTED_EARTH,
    SALT_AND_DARKNESS, CARVE_AND_SPIT, ABYSSAL_DRAIN, SHADOWBRINGER,
    GRIT, RELEASE_GRIT, THE_BLACKEST_NIGHT, SHADOW_WALL, SHADOWED_VIGIL,
    DARK_MIND, DARK_MISSIONARY, OBLATION, LIVING_DEAD, SHADOWSTRIDE,
}) | ESTEEM_IDS

# --- Blood Gauge -----------------------------------------------------------------
# Generated by the combo finishers (+20) and Delirium's Blood Weapon (+10 per
# weaponskill for 3 stacks — attributed to the chain GCDs here, where top parses
# realize it; the in-sim model tracks the actual stacks). Spent by Bloodspiller /
# Quietus (50). Living Shadow costs NO Blood in 7.x (probe: the ledger closes at
# zero LS cost — gen 1250 == spend 1250 exactly on a cold M12S-P2 pull).
BLOOD_GENERATORS: dict[int, int] = {
    SOULEATER:        20,
    STALWART_SOUL:    20,
    SCARLET_DELIRIUM: 10,
    COMEUPPANCE:      10,
    TORCLEAVER:       10,
    IMPALEMENT:       10,
}
BLOOD_SPENDERS: dict[int, int] = {
    BLOODSPILLER: 50,
    QUIETUS:      50,
}
BLOOD_CAP = 100
# Overcap penalty: potency lost per wasted Blood ~ Bloodspiller value per unit.
BLOOD_VALUE_P_PER_UNIT: float = 12.0   # 600 / 50

# --- Cooldowns (recast_s, max_charges) --------------------------------------------
# Only RECAST-gated actions live here (drift-detector-watched + sim recast). The
# state-gated actions (the Delirium chain = stacks; Disesteem = Scorn; Salt and
# Darkness = the live Salted Earth patch) are modeled as simulator state, so
# listing them would read as false drift. Edge of Shadow is MP-gated (1s recast —
# the sim adds it locally for double-weave realism; the drift detector must not
# watch it). Recasts probe-verified (Delirium 60.0, LS 120.0, Salted Earth 90,
# Carve 60 SHARED with Abyssal Drain, Shadowbringer 60 x 2 charges).
COOLDOWNS: dict[int, tuple[float, int]] = {
    DELIRIUM:      (60.0, 1),
    LIVING_SHADOW: (120.0, 1),
    SALTED_EARTH:  (90.0, 1),
    CARVE_AND_SPIT: (60.0, 1),
    SHADOWBRINGER: (60.0, 2),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    DELIRIUM:      2160,   # the chain (620+720+820) over three Bloodspillers
    LIVING_SHADOW: 3450,   # the 2450 fold + the 1000 Disesteem it enables
    SALTED_EARTH:   800,   # 300 fold + the 500 Salt and Darkness it gates
    CARVE_AND_SPIT: 540,
    SHADOWBRINGER:  600,
}

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) --------------
# The measured consensus opener GCD sequence (probe, 3 top M11S pulls): Hard Slash
# (Edge + Living Shadow woven) -> Syphon -> Souleater -> Delirium -> the chain
# (Carve / Shadowbringer x2 / Salted Earth / Salt and Darkness / Edge dumps woven)
# -> Disesteem -> Bloodspiller.
CANONICAL_OPENER: tuple[int, ...] = (
    HARD_SLASH, SYPHON_STRIKE, SOULEATER, SCARLET_DELIRIUM, COMEUPPANCE,
    TORCLEAVER, DISESTEEM, BLOODSPILLER,
)

# --- Detection exclusions -----------------------------------------------------------
CLIP_EXCLUSIONS: frozenset[int] = frozenset()   # no reduced-GCD window (no haste)
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()  # COOLDOWNS lists only recast-gated

# --- Burst-alignment abilities (AlignmentAspect watches these) ----------------------
BURST_ABILITIES: frozenset[int] = frozenset({
    DELIRIUM, LIVING_SHADOW, SALTED_EARTH, SALT_AND_DARKNESS, CARVE_AND_SPIT,
    SHADOWBRINGER, DISESTEEM,
})

# Enablers whose value is throughput/burst, not standalone potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values). Delirium fuels the
# chain; Living Shadow the fold + Disesteem; Salted Earth gates Salt and Darkness.
ENABLER_IDS: tuple[int, ...] = (DELIRIUM, LIVING_SHADOW, SALTED_EARTH)


# --- JOB_DATA bundle ------------------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Dark Knight",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            # Name matches the SimState field (`blood`) so the shared
            # entry_gauge.seed_entry_gauge can seed carried Blood on
            # phase-continuation pulls. (Measured: M12S-P2 opens COLD — the
            # ledger closes at entry 0 — so this is a safety net, byte-identical
            # on every observed pull.)
            name="blood",
            generators=BLOOD_GENERATORS,
            spenders=BLOOD_SPENDERS,
            cap=BLOOD_CAP,
            value_p_per_unit=BLOOD_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),          # no cross-cooldown reductions
    # A delivered Abyssal Drain consumes Carve and Spit's shared 60s recast
    # (probe: interleaved gaps all >= 60s) — the drift detector must see one pool.
    charge_sharing={ABYSSAL_DRAIN: CARVE_AND_SPIT},
    raid_buffs={},         # DRK brings no party buff (Dark Missionary is mitigation)
    role_policy=MELEE_TANK,
    # A dropped GCD backfills with a Souleater combo finisher (~480).
    filler_gcd_potency=480,
    # Tincture: tank Strength (party-comp-inclusive, from xivgear); same tier
    # stat/slope as Paladin/Warrior/Gunbreaker. ⚠ refine per tier via
    # scripts/calibrate_tincture.py.
    tincture_main_stat=6386,
    tincture_role_coeff=190,
    # Pure melee with a gap-closer (Shadowstride); forced disconnects are
    # disengages (like PLD/WAR/GNB), NOT an RPR-Harpe ranged filler. Unmend is
    # still in POTENCIES so delivered casts credit.
    ranged_filler_id=None,
)
