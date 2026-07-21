"""Generic actor + fight finders. Job-agnostic helpers consumed by
`analyze_pull` and any aspect that needs to filter actors.
"""
from __future__ import annotations

from typing import Any


def find_player_actor(report: dict[str, Any], fight: dict[str, Any] | None,
                      job_name: str,
                      player_name: str | None = None) -> dict[str, Any] | None:
    """Generic actor finder: subType == job_name, narrowed by fight.friendlyPlayers.

    FFLogs reports a job's `subType` spaceless ("RedMage", "DarkKnight"), while our
    job names carry the space ("Red Mage"); single-word jobs are unaffected. Match
    on the spaceless form so multi-word jobs resolve."""
    want = job_name.replace(" ", "")
    actors = report["masterData"]["actors"]
    candidates = [a for a in actors
                  if a["type"] == "Player"
                  and (a.get("subType") or "").replace(" ", "") == want]
    if fight and fight.get("friendlyPlayers"):
        allowed = set(fight["friendlyPlayers"])
        in_fight = [a for a in candidates if a["id"] in allowed]
        if in_fight:
            candidates = in_fight
    if not candidates:
        return None
    if player_name:
        for a in candidates:
            if a["name"].lower() == player_name.lower():
                return a
    return candidates[0]


def find_fight(report: dict[str, Any], fight_id: int | None) -> dict[str, Any] | None:
    fights = report.get("fights") or []
    if not fights:
        return None
    if fight_id is None:
        kills = [f for f in fights if f.get("kill")]
        return (kills or fights)[-1]
    for f in fights:
        if f["id"] == fight_id:
            return f
    return None
