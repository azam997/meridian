"""Current Savage tier + ultimate catalog. Hardcoded for now; refresh when a
new tier (or ultimate) drops."""
from __future__ import annotations

AAC_HEAVYWEIGHT_ZONE_ID = 73
DIFFICULTY_SAVAGE = 101

# Ultimates live in their own single-encounter zones, and FFLogs ranks them
# under difficulty 100 ("Normal" — the only difficulty those zones expose),
# not the Savage 101.
DANCING_MAD_ZONE_ID = 76
DIFFICULTY_ULTIMATE = 100

AAC_HEAVYWEIGHT_ENCOUNTERS: list[tuple[int, str]] = [
    (101, "Vamp Fatale (M9S)"),
    (102, "Red Hot and Deep Blue (M10S)"),
    (103, "The Tyrant (M11S)"),
    (104, "Lindwurm (M12S P1)"),
    (105, "Lindwurm II (M12S P2)"),
]

ULTIMATE_ENCOUNTERS: list[tuple[int, str]] = [
    (1085, "Dancing Mad (Ultimate)"),
]

# Everything the encounter select / catalog / warm matrix offers — tier order,
# ultimates last.
ALL_ENCOUNTERS: list[tuple[int, str]] = (
    AAC_HEAVYWEIGHT_ENCOUNTERS + ULTIMATE_ENCOUNTERS
)

# (zone_id, difficulty, [encounter ids]) — the batching unit for the one-shot
# SetupView query (`get_character_setup` aliases one zoneRankings per group).
ZONE_GROUPS: list[tuple[int, int, list[int]]] = [
    (AAC_HEAVYWEIGHT_ZONE_ID, DIFFICULTY_SAVAGE,
     [eid for eid, _ in AAC_HEAVYWEIGHT_ENCOUNTERS]),
    (DANCING_MAD_ZONE_ID, DIFFICULTY_ULTIMATE,
     [eid for eid, _ in ULTIMATE_ENCOUNTERS]),
]

_ULTIMATE_IDS = frozenset(eid for eid, _ in ULTIMATE_ENCOUNTERS)


def encounter_difficulty(encounter_id: int) -> int:
    """FFLogs difficulty for a catalog encounter (rankings, pulls, refs)."""
    return DIFFICULTY_ULTIMATE if encounter_id in _ULTIMATE_IDS else DIFFICULTY_SAVAGE


def zone_difficulty(zone_id: int) -> int:
    """FFLogs difficulty for a catalog zone (zoneRankings queries)."""
    return DIFFICULTY_ULTIMATE if zone_id == DANCING_MAD_ZONE_ID else DIFFICULTY_SAVAGE


def encounter_category(encounter_id: int) -> str:
    """Which Setup tab an encounter belongs to: 'ultimate' or 'savage'.

    The frontend splits the encounter picker into a Savage tab and an Ultimates
    tab; this is the single source of truth for the wire-level tag."""
    return "ultimate" if encounter_id in _ULTIMATE_IDS else "savage"


SERVER_REGIONS = ["NA", "EU", "JP", "OC"]
