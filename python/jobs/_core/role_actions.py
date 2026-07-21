"""Shared, job-agnostic role/general actions that never belong on a DPS
timeline or cast-diff.

These are the cross-job utility/movement actions every job shares (Sprint, the
role-action defensives, movement skills). They carry no DPS value and the
simulators never fire them, so the analyzer excludes them — same treatment as a
job's own defensive oGCDs (`JobData.defensive_ids`).

The sidecar unions this set with the active job's `defensive_ids` and tags every
`abilityMeta` entry with `isDefensive`, so the frontend reads one flag off the
wire instead of hand-maintaining per-job id lists. New jobs add their own
defensive oGCDs in their `JobData`; this shared set rarely changes.

IDs are FFXIV/FFLogs action IDs. Kept in sync with the frontend's
`SHARED_NON_ROTATIONAL_NAMES` fallback (src/jobs/types.ts), which only matters
for casts that arrive without a resolved id.
"""
from __future__ import annotations

ROLE_ACTION_IDS: frozenset[int] = frozenset({
    3,     # Sprint (general action)
    # Tank role actions (defensives / utility — show on the tank Defensives lane,
    # never on the DPS diff). ⚠️ verify ids against a live tank log's masterData.
    7531,  # Rampart
    7535,  # Reprisal
    7533,  # Provoke
    7537,  # Shirk
    7538,  # Interject
    7540,  # Low Blow
    # Physical (melee / ranged) role actions
    7541,  # Second Wind
    7542,  # Bloodbath
    7546,  # True North (positional enabler — non-rotational)
    7548,  # Arm's Length
    7549,  # Feint
    7551,  # Head Graze
    7553,  # Foot Graze
    7554,  # Leg Graze
    7557,  # Peloton
    7863,  # Leg Sweep
    # Magic (caster / healer) role actions. Swiftcast's DPS effect (a free
    # instant) IS modeled in the caster simulators, but the button itself carries
    # no potency, so it's non-rotational like the rest — hidden from the timeline /
    # counts / findings. ⚠️ verify these ids against a live caster log's masterData.
    7559,  # Surecast (anti-knockback utility)
    7560,  # Addle (defensive: −enemy damage dealt)
    7561,  # Swiftcast (next spell instant; utility/movement — effect modeled in sim)
    7562,  # Lucid Dreaming (MP regen only)
    # Healer role actions (ids verified vs XIVAPI, scripts/probe_whm_ids.py)
    7568,  # Esuna (debuff cleanse — a GCD, but never a DPS decision)
    7571,  # Rescue (party reposition utility)
    16560, # Repose (enemy sleep — irrelevant in raids)
})
