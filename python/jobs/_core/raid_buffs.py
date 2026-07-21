"""Standard party damage-buff providers, keyed by job.

The alignment detector needs to know two things about a pull: *whether*
there are raid-buff providers in the party at all, and *how much* a
2-minute burst window is worth. Both come from the party composition,
which FFLogs reports reliably as `masterData.actors[].subType` (the job
name) — far more robust than hard-coding patch-specific buff status IDs
that silently break when SE renumbers them.

This is deliberately comp-driven: we only ever model buffs whose provider
job is actually present in the pull. The analyzer must never imply a
composition change ("bring a Dancer"); it can only talk about aligning
your own cooldowns to the buffs that were really there.

Multipliers are approximate whole-party damage contributions (the real
value depends on crit/DH RNG and snapshot timing). They exist to rank
findings, not to be exact — same spirit as the flat 10% they replace.
Single-target buffs (AST cards, etc.) are excluded: a 2-min burst stacks
into *party-wide* buffs, which is what alignment is about.
"""
from __future__ import annotations

from dataclasses import dataclass


# Fallback opener offset for a provider we have no measured sample for (see
# `opener_offset_s` below) — the all-provider median from the last refresh.
_DEFAULT_OPENER_OFFSET_S: float = 7.0


@dataclass(frozen=True)
class BuffProvider:
    job: str
    name: str               # FFLogs ability/status name (used to resolve IDs)
    dmg_multiplier: float    # approximate whole-party damage multiplier
    duration_s: float = 20.0
    # True  -> applied as a DEBUFF on the boss (Chain Stratagem, Dokumori):
    #          observed window comes from debuff events on the enemy.
    # False -> applied as a BUFF on party members (Battle Litany, Embolden…):
    #          observed window comes from buff events on the player.
    on_enemy: bool = False
    # Canonical opener timing: seconds into the fight at which this provider's
    # buff actually lands in a top-parse opener. Party 2-min buffs are held to
    # ~the 3rd GCD while jobs ramp into burst, not fired at t=0 — and the exact
    # offset is job-specific (Dokumori ~4.6s, Embolden ~8.4s). Used by
    # `buff_windows.expected_windows` to phase the "master" ceiling's first
    # burst per provider. Empirically derived; refresh with
    # `scripts/calibrate_buff_timing.py` (prints paste-ready values).
    opener_offset_s: float = _DEFAULT_OPENER_OFFSET_S


# Job name (FFLogs subType) -> the party damage buff that job brings on the
# 2-minute cadence. `name` matches the FFLogs status name so we can resolve
# the per-report status ID from masterData.abilities rather than hard-coding
# patch-specific IDs (the window is detected from this status; the multiplier
# is applied to it).
#
# `dmg_multiplier` is the approximate **flat expected-damage** multiplier for
# Dawntrail (7.x). Unlike the crit-DH calibration (cleanly empirical because
# guaranteed-crit-DH hits are individually identifiable), raid buffs co-stack
# at every burst window, so isolating one buff's effect from logs is
# unreliable — these use established theorycraft values instead:
#   * flat-damage buffs use their listed %;
#   * crit-RATE buffs use rate × (crit_mult-1), crit_mult≈1.638 from
#     scripts/calibrate_crit_dh.py → +10% crit ≈ +6.4%;
#   * DH-RATE buffs use rate × 0.25 → +20% DH ≈ +5%.
# Jobs that bring two stacking party buffs carry the combined multiplier
# (e.g. Bard = Battle Voice ×1.05 · Radiant Finale ×1.06 ≈ 1.113), detected
# via the primary status. Single-target buffs (AST cards, DNC Devilment on a
# dance partner) are excluded — only party-wide damage is modeled.
# `opener_offset_s` values are median first-window starts measured across the
# current tier's top MCH parses (n in the trailing comment), via
# scripts/calibrate_buff_timing.py. Re-run + re-paste per tier. Bard had no
# sample in the measured set, so it keeps the all-provider default.
PROVIDER_BUFFS: dict[str, BuffProvider] = {
    # Bard: Battle Voice (+20% DH ≈ +5%) × Radiant Finale (+6%) ≈ 1.113. Duration
    # live-verified 20s (probe_bard_ids.py: BV status 19.97s on every pull).
    "Bard":        BuffProvider("Bard", "Battle Voice", 1.113, 20.0),  # opener offset: no sample → default
    "Dancer":      BuffProvider("Dancer", "Technical Finish", 1.05, 20.0),  # n=3 too low → default
    "Astrologian": BuffProvider("Astrologian", "Divination", 1.06, 20.0, opener_offset_s=8.2),  # n=13
    # Chain Stratagem / Battle Litany: +10% crit rate ≈ +6.4%.
    "Scholar":     BuffProvider("Scholar", "Chain Stratagem", 1.064, 20.0, on_enemy=True, opener_offset_s=6.0),  # n=25
    "Dragoon":     BuffProvider("Dragoon", "Battle Litany", 1.064, 20.0, opener_offset_s=7.8),  # n=20
    "Monk":        BuffProvider("Monk", "Brotherhood", 1.05, 20.0, opener_offset_s=6.7),  # n=12
    "Ninja":       BuffProvider("Ninja", "Dokumori", 1.05, 20.0, on_enemy=True, opener_offset_s=4.6),  # n=10
    "Reaper":      BuffProvider("Reaper", "Arcane Circle", 1.03, 20.0, opener_offset_s=6.6),  # n=11
    "Summoner":    BuffProvider("Summoner", "Searing Light", 1.05, 20.0, opener_offset_s=5.6),  # n=10
    # Embolden ramps +5%→+1% over 20s; ~+4% time-averaged.
    "RedMage":     BuffProvider("RedMage", "Embolden", 1.04, 20.0, opener_offset_s=8.4),  # n=17
    "Pictomancer": BuffProvider("Pictomancer", "Starry Muse", 1.05, 20.0),  # n=5 too low → default
}


def resolve_status_ids(
    report_abilities: list[dict],
    present_jobs: list[str],
) -> dict[int, BuffProvider]:
    """Map FFLogs status IDs -> BuffProvider for the providers present in the
    party, by matching `masterData.abilities[].name` against the registry.

    Robust to patch-specific ID renumbering: we key on the (stable) status
    name and read whatever ID this report assigned it. Only the *status*
    application IDs (the 1,000,000+ aura forms) are returned — not the cast
    action that shares the name.
    """
    wanted = {p.name: p for p in present_providers(present_jobs)}
    out: dict[int, BuffProvider] = {}
    for ab in report_abilities:
        prov = wanted.get(ab.get("name"))
        gid = ab.get("gameID")
        # The applied status/aura is the 1,000,000+ form; the bare cast action
        # (e.g. 3557 Battle Litany) shares the name but isn't what lands on a
        # target. Prefer the aura form.
        if prov is not None and isinstance(gid, int) and gid >= 1_000_000:
            out[gid] = prov
    return out


def resolve_cast_action_id(report_abilities: list[dict], name: str) -> int | None:
    """Return the *cast action* game ID for a buff `name` (the bare action,
    < 1,000,000 — not the 1,000,000+ aura form). Used for on-enemy debuffs
    (Chain Stratagem, Dokumori) whose application we infer from the
    provider's cast rather than a debuff stream that FFLogs doesn't surface
    cleanly."""
    for ab in report_abilities:
        gid = ab.get("gameID")
        if ab.get("name") == name and isinstance(gid, int) and gid < 1_000_000:
            return gid
    return None


def present_providers(party_jobs: list[str]) -> list[BuffProvider]:
    """The buff providers actually present in this party. Dedupes by job —
    two Bards still only describe one Battle Voice buff for window purposes
    (a real double-provider comp would stack, but that's rare enough to
    leave as a later refinement)."""
    seen: set[str] = set()
    out: list[BuffProvider] = []
    for job in party_jobs:
        prov = PROVIDER_BUFFS.get(job)
        if prov is not None and prov.job not in seen:
            seen.add(prov.job)
            out.append(prov)
    return out


def combined_multiplier(party_jobs: list[str]) -> float:
    """Approximate combined whole-party damage multiplier inside a stacked
    2-min window, from the providers actually present. 1.0 when none."""
    mult = 1.0
    for prov in present_providers(party_jobs):
        mult *= prov.dmg_multiplier
    return mult


def burst_window_duration_s(party_jobs: list[str], default_s: float = 20.0) -> float:
    """Representative window length from the present providers (the max of
    their durations). Falls back to `default_s` when none are present."""
    provs = present_providers(party_jobs)
    if not provs:
        return default_s
    return max(p.duration_s for p in provs)
