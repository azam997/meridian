"""The hand-authored defensive-action library the mitigation planner draws from.

Level-100, patch 7.x values, verified against the in-game tooltips (via the
community wiki) on 2026-07-16 — see scripts/validate_mit_values.py for the
empirical cross-check of every mit percentage against observed per-hit
`multiplier` values in top logs (the FFLogs `buffs` field on a damage-taken
event lists exactly the statuses that entered that hit's calculation, so the
table is self-auditing).

Modeling conventions:
- Percentages are fractions (0.10 = 10%). `mit_all` applies to every damage
  school; `mit_phys`/`mit_magic` add on top for matching schools (Feint =
  mit_phys 0.10 + mit_magic 0.05, mit_all 0).
- Shields: `shield_potency` is the cure-potency equivalent per target
  (converted to HP via the per-log calibration factor, or overridden by the
  observed per-status median from `applybuff.absorb`); `shield_pct_maxhp` is
  used instead for max-HP-defined barriers (TBN, Shake It Off, Tempera
  Grassa, Divine Veil).
- Regens: `regen_potency_per_tick` × `regen_ticks` on a 3s server tick.
- `status_names` are the FFLogs aura names as they appear in
  `masterData.abilities` (aura-form ids resolve per report by name, the same
  pattern raid_buffs.resolve_status_ids uses). Multi-status actions list every
  aura that carries part of the effect.
- `resource`: a shared token bucket (Addersgall/Aetherflow/Lily) consumed on
  cast — see RESOURCE_POOLS; the per-action cooldown alone would overstate
  how often the SGE/SCH/WHM spot tools can flow.
- `gcd_cost_potency`: damage potency lost by weaving this on the GCD (0 for
  oGCDs and for the DPS-neutral lily heals, whose spent lilies feed Afflatus
  Misery).
- The healer AMPLIFIER layer is modeled COHERENTLY on one principle: an amp never
  occupies its own value slot (that double-counts) — it SCALES a host. Magnitudes
  are wiki-derived + LOG-VALIDATED via scripts/calibrate_amplifiers.py. Four kinds:
    * Baked-in shield producers (stand alone, no partner): AST Neutral Sect
      (~400p Helios barrier; the +20% is already inside it). Kept as shield_potency.
    * Shield-mult RIDERS (shield_mult + amp_partner; multiply a co-cast GCD shield
      at placement, no standalone barrier): SGE Zoe (+50% → E.Prognosis/Diagnosis),
      SCH Recitation (+54% crit → Adloquium/Concitation), SCH Seraphism (+20% →
      Concitation, i.e. Accession — +20% baked, so NO heal_mult).
    * +heal% WINDOWS (heal_mult + heal_mult_scope; a windowed multiplier in the
      sweep, NOT a flat heal): caster (own GCD spells) WHM Temperance +0.20, SGE
      Philosophia +0.18; receiver (all incoming) SGE Physis II/Krasis, SCH
      Protraction. Genuine base regen/heal stays; only the % rides the window.
    * Base GCD shields Adloquium / Eukrasian Diagnosis are their own single-target
      entries the shield riders act on; Deployment spreads Galvanize party-wide.
  Rule (user): a heal boost in a healer's OWN kit is planned for; cross-JOB
  interplay is out of scope. WHM Plenary's Confession stays a flat +200 (a real
  fixed heal, not a % window). AST Synastry (co-tank copy) + Macrocosmos's 50%
  heal-back (accumulator) are approximated as recovery. SCH Emergency Tactics /
  SGE Pepsis (shield→heal converters) are extremely niche and intentionally NOT
  modeled — the honest way to ignore a button is to simply not model it.
- Also out: MNK Mantra, RPR Arcane Crest, DNC Improvisation (awkward to
  schedule), AST The Arrow/The Ewer/Lady of Crowns (recovery cards), SGE
  Rhizomata (extra gall stack).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Tier(Enum):
    PARTY_OTHER = "party_other"        # mitigation the 6 non-healers bring
    HEALER_OGCD = "healer_ogcd"        # healer tools that cost no GCD
    HEALER_GCD = "healer_gcd"          # healer GCDs (priced against DPS)
    TANK_SUGGESTION = "tank_suggestion"  # tank personals (suggested, not owned)
    INVULN = "invuln"                  # suggested only when a buster stays lethal


class Target(Enum):
    PARTY = "party"
    SELF = "self"
    SINGLE = "single"
    ENEMY = "enemy"    # debuff on the boss — party-wide effect vs that enemy


@dataclass(frozen=True)
class MitAction:
    job: str                     # internal job name ("White Mage", "Dark Knight")
    name: str
    action_id: int               # game action id (icons via ability_metadata)
    status_names: tuple[str, ...] = ()
    mit_all: float = 0.0
    mit_phys: float = 0.0
    mit_magic: float = 0.0
    shield_potency: float = 0.0
    shield_pct_maxhp: float = 0.0
    heal_potency: float = 0.0
    heal_pct_maxhp: float = 0.0  # Benediction-style
    regen_potency_per_tick: float = 0.0
    regen_ticks: int = 0
    duration_s: float = 0.0
    cooldown_s: float = 0.0
    charges: int = 1
    target: Target = Target.PARTY
    is_gcd: bool = False
    cast_time_s: float = 0.0
    gcd_cost_potency: float = 0.0
    tier: Tier = Tier.PARTY_OTHER
    stack_group: str | None = None  # same group never counts twice on one hit
    resource: str | None = None     # RESOURCE_POOLS key consumed per cast
    recovery: bool = False          # contributes to the between-hits HPS budget
    # --- amplifier layer: abilities that SCALE a host rather than stand alone.
    # An amp never occupies its own value slot (that would double-count); it
    # rides the heal/shield it powers. See the module docstring.
    heal_mult: float = 0.0          # +% healing WINDOW, applied in the sweep
    heal_mult_scope: str = ""       # "caster" (own GCD spells) | "receiver" (all incoming)
    shield_mult: float = 0.0        # rider: multiplies a co-cast partner shield's barrier
    amp_partner: tuple[str, ...] = ()   # host action names a shield_mult rider
    # One-shot rider (Zoe, Recitation: "the next Adloquium/Prognosis") vs a
    # WINDOW that transforms every host cast for duration_s (Seraphism: "while
    # under the effect of"). A window amps all its hosts, not just the first.
    shield_mult_windowed: bool = False
    # Invulns only: the tank comes out at ~1 HP (Superbolide) or must be healed
    # to full (Living Dead) or was damage-floored (Holmgang) — the HP sweep
    # models a full re-heal debt. Hallowed Ground leaves HP untouched.
    post_hp1: bool = False
    notes: str = ""

    @property
    def is_mit(self) -> bool:
        return (self.mit_all + self.mit_phys + self.mit_magic) > 0

    @property
    def is_shield(self) -> bool:
        return self.shield_potency > 0 or self.shield_pct_maxhp > 0

    @property
    def is_amplifier(self) -> bool:
        """Scales a host heal/shield instead of carrying its own value."""
        return self.heal_mult > 0 or self.shield_mult > 0


# Shared token buckets: (capacity, seconds per token). Aetherflow is really
# 3-at-once every 60s; a 20s trickle is the schedule-friendly equivalent.
RESOURCE_POOLS: dict[str, tuple[int, float]] = {
    "addersgall": (3, 20.0),
    "aetherflow": (3, 20.0),
    "lily": (3, 20.0),
}

# Damage potency a healer filler GCD would have dealt — prices GCD heals.
FILLER_GCD_POTENCY: dict[str, float] = {
    "White Mage": 350.0,   # Glare III
    "Scholar": 320.0,      # Broil IV
    "Sage": 380.0,         # Dosis III
    "Astrologian": 270.0,  # Fall Malefic
}

# Fallback per-role max HP (current savage tier) — used only when the log
# fetch could not supply targetResources (hpSource: "constants").
ROLE_MAX_HP_DEFAULT: dict[str, float] = {
    "tank": 320_000.0,
    "healer": 235_000.0,
    "dps": 227_000.0,
}

# Fallback HP restored per point of cure potency (calibrated per log set from
# observed heal amounts whenever possible).
HP_PER_POTENCY_DEFAULT: float = 55.0

TANK_JOBS = ("Paladin", "Warrior", "Dark Knight", "Gunbreaker")
HEALER_JOBS = ("White Mage", "Scholar", "Astrologian", "Sage")
SHIELD_HEALERS = ("Sage", "Scholar")
REGEN_HEALERS = ("White Mage", "Astrologian")
MELEE_JOBS = ("Monk", "Dragoon", "Ninja", "Samurai", "Reaper", "Viper")
RANGED_JOBS = ("Bard", "Machinist", "Dancer")
CASTER_JOBS = ("Black Mage", "Summoner", "Red Mage", "Pictomancer")
DPS_JOBS = MELEE_JOBS + RANGED_JOBS + CASTER_JOBS
ALL_JOB_NAMES = TANK_JOBS + HEALER_JOBS + DPS_JOBS

# The sub-role a premade ("PF") plan can pin a shared-id party mit to (Feint =
# any melee, Addle = any caster, Reprisal = any tank). The planner resolves the
# role to an eligible comp job at placement time (distributing by cooldown).
ROLE_JOBS: dict[str, tuple[str, ...]] = {
    "tank": TANK_JOBS, "melee": MELEE_JOBS,
    "ranged": RANGED_JOBS, "caster": CASTER_JOBS,
}

# FFLogs actor `subType` is the spaceless form ("WhiteMage", "DarkKnight").
_BY_SPACELESS = {j.replace(" ", ""): j for j in ALL_JOB_NAMES}


def internal_job_name(sub_type: str) -> str | None:
    """FFLogs subType -> internal job name (None for Unknown/LimitBreak/pets)."""
    return _BY_SPACELESS.get((sub_type or "").replace(" ", ""))


def role_for_job(job: str) -> str:
    if job in TANK_JOBS:
        return "tank"
    if job in HEALER_JOBS:
        return "healer"
    return "dps"


def _a(**kw) -> MitAction:
    return MitAction(**kw)


ACTIONS: tuple[MitAction, ...] = (
    # ------------------------------------------------------------------ tanks
    _a(job="Paladin", name="Reprisal", action_id=7535, status_names=("Reprisal",),
       mit_all=0.10, duration_s=15, cooldown_s=60, target=Target.ENEMY,
       stack_group="reprisal"),
    _a(job="Warrior", name="Reprisal", action_id=7535, status_names=("Reprisal",),
       mit_all=0.10, duration_s=15, cooldown_s=60, target=Target.ENEMY,
       stack_group="reprisal"),
    _a(job="Dark Knight", name="Reprisal", action_id=7535, status_names=("Reprisal",),
       mit_all=0.10, duration_s=15, cooldown_s=60, target=Target.ENEMY,
       stack_group="reprisal"),
    _a(job="Gunbreaker", name="Reprisal", action_id=7535, status_names=("Reprisal",),
       mit_all=0.10, duration_s=15, cooldown_s=60, target=Target.ENEMY,
       stack_group="reprisal"),

    _a(job="Paladin", name="Divine Veil", action_id=3540,
       status_names=("Divine Veil",), shield_pct_maxhp=0.10, heal_potency=400,
       duration_s=30, cooldown_s=90,
       notes="Barrier sized off the Paladin's own max HP."),
    _a(job="Paladin", name="Passage of Arms", action_id=7385,
       status_names=("Passage of Arms", "Arms Up"), mit_all=0.15,
       duration_s=5, cooldown_s=120,
       notes="Channel — modeled at a 5s practical window; the Paladin can hold it up to 18s."),
    _a(job="Warrior", name="Shake It Off", action_id=7388,
       status_names=("Shake It Off", "Shake It Off (Over Time)"),
       shield_pct_maxhp=0.15, heal_potency=100,
       regen_potency_per_tick=100, regen_ticks=5,
       duration_s=30, cooldown_s=90, recovery=True,
       notes="Barrier is 15% of each target's own max HP (+2% per consumed self-buff, unmodeled)."),
    _a(job="Dark Knight", name="Dark Missionary", action_id=16471,
       status_names=("Dark Missionary",), mit_magic=0.10, mit_phys=0.05,
       duration_s=15, cooldown_s=90),
    _a(job="Gunbreaker", name="Heart of Light", action_id=16160,
       status_names=("Heart of Light",), mit_magic=0.10, mit_phys=0.05,
       duration_s=15, cooldown_s=90),

    # ------------------------------------------------------- dps party tools
    _a(job="Monk", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),
    _a(job="Dragoon", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),
    _a(job="Ninja", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),
    _a(job="Samurai", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),
    _a(job="Reaper", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),
    _a(job="Viper", name="Feint", action_id=7549, status_names=("Feint",),
       mit_phys=0.10, mit_magic=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="feint"),

    _a(job="Black Mage", name="Addle", action_id=7560, status_names=("Addle",),
       mit_magic=0.10, mit_phys=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="addle"),
    _a(job="Summoner", name="Addle", action_id=7560, status_names=("Addle",),
       mit_magic=0.10, mit_phys=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="addle"),
    _a(job="Red Mage", name="Addle", action_id=7560, status_names=("Addle",),
       mit_magic=0.10, mit_phys=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="addle"),
    _a(job="Pictomancer", name="Addle", action_id=7560, status_names=("Addle",),
       mit_magic=0.10, mit_phys=0.05, duration_s=15, cooldown_s=90,
       target=Target.ENEMY, stack_group="addle"),

    _a(job="Bard", name="Troubadour", action_id=7405, status_names=("Troubadour",),
       mit_all=0.15, duration_s=15, cooldown_s=90, stack_group="ranged_mit"),
    _a(job="Machinist", name="Tactician", action_id=16889, status_names=("Tactician",),
       mit_all=0.15, duration_s=15, cooldown_s=90, stack_group="ranged_mit"),
    _a(job="Dancer", name="Shield Samba", action_id=16012, status_names=("Shield Samba",),
       mit_all=0.15, duration_s=15, cooldown_s=90, stack_group="ranged_mit"),
    _a(job="Machinist", name="Dismantle", action_id=2887, status_names=("Dismantled",),
       mit_all=0.10, duration_s=10, cooldown_s=120, target=Target.ENEMY),
    _a(job="Red Mage", name="Magick Barrier", action_id=25857,
       status_names=("Magick Barrier",), mit_magic=0.10,
       duration_s=10, cooldown_s=120,
       notes="Also +5% healing received (unmodeled)."),
    _a(job="Pictomancer", name="Tempera Grassa", action_id=34686,
       status_names=("Tempera Grassa",), shield_pct_maxhp=0.10,
       duration_s=10, cooldown_s=120,
       notes="Requires Tempera Coat (120s) up first; modeled on the Coat cadence."),

    # ------------------------------------------------------------------- SGE
    _a(job="Sage", name="Kerachole", action_id=24298,
       status_names=("Kerachole", "Kerakeia"), mit_all=0.10,
       regen_potency_per_tick=100, regen_ticks=5, duration_s=15, cooldown_s=30,
       tier=Tier.HEALER_OGCD, resource="addersgall", recovery=True),
    _a(job="Sage", name="Ixochole", action_id=24299, heal_potency=400,
       cooldown_s=30, tier=Tier.HEALER_OGCD, resource="addersgall",
       recovery=True),
    _a(job="Sage", name="Taurochole", action_id=24303, status_names=("Taurochole",),
       mit_all=0.10, heal_potency=700, duration_s=15, cooldown_s=45,
       target=Target.SINGLE, tier=Tier.HEALER_OGCD, resource="addersgall"),
    _a(job="Sage", name="Physis II", action_id=24302, status_names=("Physis II",),
       regen_potency_per_tick=130, regen_ticks=5,
       heal_mult=0.10, heal_mult_scope="receiver",
       duration_s=15, cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True,
       notes="Base regen + a +10% healing-received party WINDOW (log +11%) — the "
             "+10% applied as a windowed receiver multiplier on all party heals."),
    _a(job="Sage", name="Philosophia", action_id=37035,
       status_names=("Philosophia", "Eudaimonia"),
       regen_potency_per_tick=150, regen_ticks=5, duration_s=20, cooldown_s=180,
       tier=Tier.HEALER_OGCD, recovery=True,
       heal_mult=0.18, heal_mult_scope="caster",
       notes="Eudaimonia party auto-heal ~150p/spell (the regen) + a +20% caster "
             "GCD-heal WINDOW (log ~+18%) applied as a windowed multiplier."),
    _a(job="Sage", name="Krasis", action_id=24317, status_names=("Krasis",),
       heal_mult=0.20, heal_mult_scope="receiver",
       duration_s=10, cooldown_s=60, target=Target.SINGLE, tier=Tier.HEALER_OGCD,
       notes="+20% healing RECEIVED on the target (all sources; log ~+14%) — a "
             "windowed receiver multiplier on the duo's heals into that target."),
    _a(job="Sage", name="Holos", action_id=24310, status_names=("Holos", "Holosakos"),
       mit_all=0.10, heal_potency=300, shield_potency=300,
       duration_s=20, cooldown_s=120, tier=Tier.HEALER_OGCD),
    _a(job="Sage", name="Panhaima", action_id=24311,
       status_names=("Panhaima", "Panhaimatinon"), shield_potency=1000,
       duration_s=15, cooldown_s=120, tier=Tier.HEALER_OGCD,
       notes="5 regenerating stacks of 200p; leftover stacks convert to healing."),
    _a(job="Sage", name="Haima", action_id=24305,
       status_names=("Haima", "Haimatinon"), shield_potency=1500,
       duration_s=15, cooldown_s=120, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD,
       notes="5 regenerating stacks of 300p on one target."),
    _a(job="Sage", name="Eukrasian Prognosis II", action_id=37034,
       status_names=("Eukrasian Prognosis",), heal_potency=100,
       shield_potency=360, duration_s=30, cooldown_s=0,
       is_gcd=True, cast_time_s=2.0, gcd_cost_potency=370.0,
       tier=Tier.HEALER_GCD,
       notes="Barrier = 360% of the heal; the classic pre-shield GCD."),
    _a(job="Sage", name="Eukrasian Diagnosis", action_id=24291,
       status_names=("Eukrasian Diagnosis",), heal_potency=300,
       shield_potency=540, duration_s=30, cooldown_s=0,
       is_gcd=True, cast_time_s=2.0, gcd_cost_potency=380.0,
       target=Target.SINGLE, tier=Tier.HEALER_GCD,
       notes="ST base shield (barrier 180% of a 300p heal); Zoe's ST partner."),
    _a(job="Sage", name="Zoe", action_id=24300, status_names=("Zoe",),
       shield_mult=0.50,
       amp_partner=("Eukrasian Prognosis II", "Eukrasian Diagnosis"),
       duration_s=30, cooldown_s=90, tier=Tier.HEALER_OGCD,
       notes="Amplifier RIDER: +50% (log ~+47%) to the NEXT Eukrasian barrier — "
             "multiplies a co-cast E.Prognosis II / E.Diagnosis, never a standalone "
             "shield (that would double-count)."),

    # ------------------------------------------------------------------- SCH
    _a(job="Scholar", name="Sacred Soil", action_id=188,
       status_names=("Sacred Soil",), mit_all=0.10,
       regen_potency_per_tick=100, regen_ticks=5, duration_s=15, cooldown_s=30,
       tier=Tier.HEALER_OGCD, resource="aetherflow", recovery=True),
    _a(job="Scholar", name="Fey Illumination", action_id=16538,
       status_names=("Fey Illumination", "Seraphic Illumination"),
       mit_magic=0.05, duration_s=20, cooldown_s=120, tier=Tier.HEALER_OGCD,
       notes="Also +10% healing magic potency (unmodeled); logs as Seraphic "
             "Illumination while Seraph is out."),
    _a(job="Scholar", name="Expedient", action_id=25868,
       status_names=("Desperate Measures", "Expedience"), mit_all=0.10,
       duration_s=20, cooldown_s=120, tier=Tier.HEALER_OGCD),
    _a(job="Scholar", name="Whispering Dawn", action_id=16537,
       status_names=("Whispering Dawn", "Angel's Whisper"),
       regen_potency_per_tick=80, regen_ticks=7, duration_s=21, cooldown_s=60,
       tier=Tier.HEALER_OGCD, recovery=True),
    _a(job="Scholar", name="Summon Seraph", action_id=16545,
       status_names=("Seraphic Veil",),
       heal_potency=500, shield_potency=500, duration_s=22, cooldown_s=120,
       tier=Tier.HEALER_OGCD,
       notes="Modeled as both Consolations (250p heal + 250p barrier each)."),
    _a(job="Scholar", name="Seraphism", action_id=37014,
       status_names=("Seraphism",), regen_potency_per_tick=100, regen_ticks=7,
       duration_s=20, cooldown_s=180, tier=Tier.HEALER_OGCD, recovery=True,
       shield_mult=0.20, amp_partner=("Concitation", "Adloquium"),
       shield_mult_windowed=True,
       notes="Party regen aura + for its whole 20s it transforms BOTH GCD shields: "
             "Adloquium→Manifestation (300→360p) and Concitation→Accession "
             "(200→240p), each exactly x1.20, so the barrier (180% of the heal) "
             "goes 540→648 / 360→432. A WINDOW rider, not a next-cast one — the "
             "tooltip gates on 'while under the effect of Seraphism', so every host "
             "cast inside the 20s is amped. NO heal_mult: the wiki tooltip has no "
             "healing-potency clause at all (it transforms exactly these two GCDs "
             "and touches no oGCD heal), and the +20% is already baked into the "
             "transformed potencies — a heal_mult would double-count."),
    _a(job="Scholar", name="Excogitation", action_id=7434, heal_potency=800,
       duration_s=45, cooldown_s=45, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD, resource="aetherflow", recovery=True,
       notes="Fires at 50% HP or expiry — treated as an on-hit heal."),
    _a(job="Scholar", name="Protraction", action_id=25867,
       status_names=("Protraction",), shield_pct_maxhp=0.10,
       heal_mult=0.10, heal_mult_scope="receiver",
       duration_s=10, cooldown_s=60, target=Target.SINGLE, tier=Tier.HEALER_OGCD,
       notes="10% max-HP buffer + a +10% healing-received WINDOW on the target "
             "(log ~+8%) — the +10% applied as a windowed receiver multiplier."),
    _a(job="Scholar", name="Concitation", action_id=37013,
       status_names=("Galvanize",), heal_potency=200, shield_potency=360,
       duration_s=30, cooldown_s=0, is_gcd=True, cast_time_s=2.0,
       gcd_cost_potency=320.0, tier=Tier.HEALER_GCD,
       notes="Barrier = 180% of the heal."),
    _a(job="Scholar", name="Recitation", action_id=16542,
       status_names=("Recitation",),
       shield_mult=0.54, amp_partner=("Concitation", "Adloquium"),
       duration_s=30, cooldown_s=60, tier=Tier.HEALER_OGCD,
       notes="Amplifier RIDER: forces a crit (+Catalyze) on the next Adloquium / "
             "Concitation — log-measured ~×1.54 on the barrier. Multiplies a co-cast "
             "Adlo/Concitation shield; the 'Spreadlo' backbone."),
    _a(job="Scholar", name="Deployment Tactics", action_id=3585,
       status_names=("Galvanize",), heal_potency=300, shield_potency=540,
       duration_s=30, cooldown_s=90, is_gcd=True, cast_time_s=2.0,
       gcd_cost_potency=320.0, tier=Tier.HEALER_GCD,
       notes="Adloquium on a tank, deployed party-wide (barrier 180% of a 300p heal)."),
    _a(job="Scholar", name="Adloquium", action_id=185,
       status_names=("Galvanize",), heal_potency=300, shield_potency=540,
       duration_s=30, cooldown_s=0, is_gcd=True, cast_time_s=2.0,
       gcd_cost_potency=320.0, target=Target.SINGLE, tier=Tier.HEALER_GCD,
       notes="ST base shield (Galvanize 180% of a 300p heal); the host Recitation "
             "rides and Deployment spreads."),

    # ------------------------------------------------------------------- WHM
    _a(job="White Mage", name="Temperance", action_id=16536,
       status_names=("Temperance",), mit_all=0.10,
       heal_mult=0.20, heal_mult_scope="caster",
       duration_s=20, cooldown_s=120, tier=Tier.HEALER_OGCD,
       notes="10% party mit + a +20% healing-potency WINDOW on the WHM's own GCD "
             "spells (log ~+23%) — applied as a windowed multiplier in the sweep, "
             "not a flat heal; also readies Divine Caress."),
    _a(job="White Mage", name="Divine Caress", action_id=37011,
       status_names=("Divine Grace", "Divine Aura"), shield_potency=400,
       regen_potency_per_tick=200, regen_ticks=5, duration_s=10, cooldown_s=120,
       tier=Tier.HEALER_OGCD, recovery=True,
       notes="Follow-up granted by Temperance."),
    _a(job="White Mage", name="Asylum", action_id=3569, status_names=("Asylum",),
       regen_potency_per_tick=100, regen_ticks=8, duration_s=24, cooldown_s=90,
       tier=Tier.HEALER_OGCD, recovery=True,
       notes="Ground placement; also +10% healing received inside (unmodeled)."),
    _a(job="White Mage", name="Plenary Indulgence", action_id=7433,
       status_names=("Confession",), mit_all=0.10, heal_potency=200,
       duration_s=10, cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True,
       notes="10% party DR (Dawntrail) + Confession adds ~200p to the next "
             "Medica III / Rapture — modeled as the extra heal it enables."),
    _a(job="White Mage", name="Liturgy of the Bell", action_id=25862,
       heal_potency=1200, duration_s=20, cooldown_s=180,
       tier=Tier.HEALER_OGCD, recovery=True,
       notes="Reactive 400p per hit taken, up to 5 — modeled at 3 procs."),
    _a(job="White Mage", name="Assize", action_id=3571, heal_potency=400,
       cooldown_s=40, tier=Tier.HEALER_OGCD, recovery=True,
       notes="Also deals damage — DPS-positive."),
    _a(job="White Mage", name="Divine Benison", action_id=7432,
       status_names=("Divine Benison",), shield_potency=500,
       duration_s=15, cooldown_s=30, charges=2, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD),
    _a(job="White Mage", name="Aquaveil", action_id=25861,
       status_names=("Aquaveil",), mit_all=0.15, duration_s=8, cooldown_s=60,
       target=Target.SINGLE, tier=Tier.HEALER_OGCD),
    _a(job="White Mage", name="Tetragrammaton", action_id=3570,
       heal_potency=700, cooldown_s=60, charges=2, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD, recovery=True),
    _a(job="White Mage", name="Benediction", action_id=140,
       heal_pct_maxhp=1.0, cooldown_s=180, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD),
    _a(job="White Mage", name="Afflatus Rapture", action_id=16534,
       heal_potency=400, cooldown_s=0, is_gcd=True, gcd_cost_potency=0.0,
       tier=Tier.HEALER_GCD, resource="lily", recovery=True,
       notes="Lily heal — DPS-neutral (spent lilies feed Afflatus Misery)."),
    _a(job="White Mage", name="Medica III", action_id=37010,
       status_names=("Medica III",), heal_potency=250,
       regen_potency_per_tick=175, regen_ticks=5, duration_s=15,
       cooldown_s=0, is_gcd=True, cast_time_s=2.0, gcd_cost_potency=350.0,
       tier=Tier.HEALER_GCD),

    # ------------------------------------------------------------------- AST
    _a(job="Astrologian", name="Collective Unconscious", action_id=3613,
       status_names=("Collective Unconscious", "Wheel of Fortune"),
       mit_all=0.10, regen_potency_per_tick=100, regen_ticks=5,
       duration_s=5, cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True,
       notes="Channel — 10% only while channeling (5s modeled); the regen persists."),
    _a(job="Astrologian", name="Exaltation", action_id=25873,
       status_names=("Exaltation",), mit_all=0.10, heal_potency=500,
       duration_s=8, cooldown_s=60, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD),
    _a(job="Astrologian", name="Celestial Intersection", action_id=16556,
       heal_potency=200, shield_potency=400, duration_s=30, cooldown_s=30,
       charges=2, target=Target.SINGLE, tier=Tier.HEALER_OGCD),
    _a(job="Astrologian", name="Celestial Opposition", action_id=16553,
       status_names=("Opposition",), heal_potency=200,
       regen_potency_per_tick=100, regen_ticks=5, duration_s=15,
       cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True),
    _a(job="Astrologian", name="Earthly Star", action_id=7439,
       heal_potency=720, cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True,
       notes="Pre-planted; detonated as Stellar Explosion (720p)."),
    _a(job="Astrologian", name="Neutral Sect", action_id=37031,
       status_names=("Sun Sign", "Neutral Sect"),
       mit_all=0.10, shield_potency=400, duration_s=15,
       cooldown_s=120, tier=Tier.HEALER_OGCD,
       notes="Neutral Sect (120s): 10% party mit (Sun Sign) PLUS a barrier from "
             "the amplified Helios it enables — a major healing cooldown, not a "
             "bare 10% mit. shield_potency=400 is LOG-VALIDATED: the delivered "
             "Helios Conjunction barrier measures ~400p (≈160% of the heal, above "
             "the 125% tooltip due to crit inflation), matched on two encounters."),
    _a(job="Astrologian", name="Macrocosmos", action_id=25874,
       status_names=("Macrocosmos",), heal_potency=600, duration_s=15,
       cooldown_s=180, tier=Tier.HEALER_OGCD, recovery=True,
       notes="200p direct + Microcosmos repays 50% of damage compiled in the "
             "window. The repay is a damage-accumulator; approximated here as a "
             "conservative +~400p party recovery (like Liturgy's reactive model)."),
    _a(job="Astrologian", name="Synastry", action_id=3612,
       status_names=("Synastry",), heal_potency=250, duration_s=20,
       cooldown_s=120, target=Target.SINGLE, tier=Tier.HEALER_OGCD, recovery=True,
       notes="The AST's single-target heals also heal the bonded co-tank 40% — a "
             "co-tank healing efficiency; approximated as a small tank recovery "
             "over the window."),
    _a(job="Astrologian", name="Horoscope", action_id=16557,
       status_names=("Horoscope", "Horoscope Helios"), heal_potency=400,
       duration_s=10, cooldown_s=60, tier=Tier.HEALER_OGCD, recovery=True,
       notes="Delayed party heal, upgraded to 400p when Helios/Helios Conjunction "
             "is cast in-window (modeled at the upgraded 400p, like Earthly Star)."),
    _a(job="Astrologian", name="The Bole", action_id=37027,
       status_names=("The Bole",), mit_all=0.10, duration_s=15,
       cooldown_s=110, target=Target.SINGLE, tier=Tier.HEALER_OGCD,
       notes="Umbral Draw card — cadence approximated at one per two draws."),
    _a(job="Astrologian", name="The Spire", action_id=37025,
       status_names=("The Spire",), shield_potency=400, duration_s=30,
       cooldown_s=110, target=Target.SINGLE, tier=Tier.HEALER_OGCD,
       notes="Astral Draw card — cadence approximated at one per two draws."),
    _a(job="Astrologian", name="Essential Dignity", action_id=3614,
       heal_potency=400, cooldown_s=40, charges=3, target=Target.SINGLE,
       tier=Tier.HEALER_OGCD, recovery=True,
       notes="Scales up to 900p at low HP; modeled at base."),
    _a(job="Astrologian", name="Helios Conjunction", action_id=37030,
       status_names=("Helios Conjunction",), heal_potency=250,
       regen_potency_per_tick=175, regen_ticks=5, duration_s=15,
       cooldown_s=0, is_gcd=True, cast_time_s=1.5, gcd_cost_potency=270.0,
       tier=Tier.HEALER_GCD),

    # -------------------------------------------- tank personals (suggested)
    _a(job="Paladin", name="Rampart", action_id=7531, status_names=("Rampart",),
       mit_all=0.20, duration_s=20, cooldown_s=90, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),
    _a(job="Warrior", name="Rampart", action_id=7531, status_names=("Rampart",),
       mit_all=0.20, duration_s=20, cooldown_s=90, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),
    _a(job="Dark Knight", name="Rampart", action_id=7531, status_names=("Rampart",),
       mit_all=0.20, duration_s=20, cooldown_s=90, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),
    _a(job="Gunbreaker", name="Rampart", action_id=7531, status_names=("Rampart",),
       mit_all=0.20, duration_s=20, cooldown_s=90, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),

    _a(job="Paladin", name="Guardian", action_id=36920, status_names=("Guardian",),
       mit_all=0.40, duration_s=15, cooldown_s=120, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),
    _a(job="Paladin", name="Holy Sheltron", action_id=25746,
       status_names=("Holy Sheltron", "Knight's Resolve"), mit_all=0.15,
       duration_s=8, cooldown_s=25, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION,
       notes="Oath-gauge cadence approximated as a 25s cooldown."),
    _a(job="Warrior", name="Damnation", action_id=36923,
       status_names=("Damnation",), mit_all=0.40, duration_s=15,
       cooldown_s=120, target=Target.SELF, tier=Tier.TANK_SUGGESTION),
    _a(job="Warrior", name="Bloodwhetting", action_id=25751,
       status_names=("Bloodwhetting", "Stem the Flow"), mit_all=0.10,
       heal_potency=400, duration_s=8, cooldown_s=25, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION),
    _a(job="Dark Knight", name="Shadowed Vigil", action_id=36927,
       status_names=("Shadowed Vigil",), mit_all=0.40, duration_s=15,
       cooldown_s=120, target=Target.SELF, tier=Tier.TANK_SUGGESTION),
    _a(job="Dark Knight", name="The Blackest Night", action_id=7393,
       status_names=("Blackest Night",), shield_pct_maxhp=0.25,
       duration_s=7, cooldown_s=30, target=Target.SELF,
       tier=Tier.TANK_SUGGESTION,
       notes="MP-gated; modeled at a 30s cadence."),
    _a(job="Gunbreaker", name="Great Nebula", action_id=36935,
       status_names=("Great Nebula",), mit_all=0.40, duration_s=15,
       cooldown_s=120, target=Target.SELF, tier=Tier.TANK_SUGGESTION),
    _a(job="Gunbreaker", name="Heart of Corundum", action_id=25758,
       status_names=("Catharsis of Corundum", "Clarity of Corundum"),
       mit_all=0.15, heal_potency=250, duration_s=8, cooldown_s=25,
       target=Target.SELF, tier=Tier.TANK_SUGGESTION),

    _a(job="Paladin", name="Hallowed Ground", action_id=30,
       status_names=("Hallowed Ground",), duration_s=10, cooldown_s=420,
       target=Target.SELF, tier=Tier.INVULN, notes="Invulnerable."),
    _a(job="Warrior", name="Holmgang", action_id=43, status_names=("Holmgang",),
       duration_s=10, cooldown_s=240, target=Target.SELF, tier=Tier.INVULN,
       post_hp1=True, notes="HP cannot drop below 1."),
    _a(job="Dark Knight", name="Living Dead", action_id=3638,
       status_names=("Living Dead", "Walking Dead"), duration_s=10,
       cooldown_s=300, target=Target.SELF, tier=Tier.INVULN,
       post_hp1=True, notes="Requires healing to full afterward."),
    _a(job="Gunbreaker", name="Superbolide", action_id=16152,
       status_names=("Superbolide",), duration_s=10, cooldown_s=360,
       target=Target.SELF, tier=Tier.INVULN,
       post_hp1=True, notes="Sets HP to 1 on use."),
)


def actions_for_job(job: str) -> list[MitAction]:
    return [a for a in ACTIONS if a.job == job]


def party_actions_for_comp(comp: list[str]) -> list[MitAction]:
    """Every non-suggestion action the comp brings (healer + party tiers)."""
    out: list[MitAction] = []
    for job in comp:
        out.extend(a for a in actions_for_job(job)
                   if a.tier in (Tier.PARTY_OTHER, Tier.HEALER_OGCD,
                                 Tier.HEALER_GCD))
    return out
