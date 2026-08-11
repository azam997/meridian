"""Positional hit/miss detection for Reaper (probe-gated).

The raw FFXIV damage line carries a *bonus byte* — "the percentage of the
damage that came from positional and/or combo bonuses" (cactbot LogGuide). RPR's
positional skills (Gibbet, Gallows, Void/Cross Reaping, Executioner's
Gibbet/Gallows) are NOT combo actions, so for those abilities a **nonzero bonus
= positional hit, zero = miss** — a clean signal with no damage back-solving.

The open question is whether FFLogs surfaces that byte in its event JSON. This
aspect reads it from a small list of candidate keys; until the live probe
(Phase 10) confirms the real key, `BONUS_KEYS` is a best guess and the aspect is
intentionally **left out of the Reaper aspect tuple**. If the probe shows the
field is absent, RPR ships assume-always-hit (idealized + delivered both score
positionals at the hit value) and this module simply never runs. If present,
add `PositionalAspect()` to the tuple and a `DamageDone` bundle stream.
"""
from __future__ import annotations

from typing import Any

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs.reaper import data as rd


# Candidate JSON keys for the positional/combo bonus byte on a FFLogs damage
# event. The live probe replaces this with the confirmed key.
BONUS_KEYS: tuple[str, ...] = ("bonusPercent", "directionalBonusPercent", "bonus")
# Damage event types that carry the resolved bonus (snapshot then resolution).
_DAMAGE_TYPES = ("calculateddamage", "damage")


def _bonus_value(ev: dict) -> int | None:
    for k in BONUS_KEYS:
        if k in ev and ev[k] is not None:
            try:
                return int(ev[k])
            except (TypeError, ValueError):
                return None
    return None


def detect_positional_misses(events: list[dict], fight_start_ms: int
                             ) -> dict[str, Any]:
    """From the actor's DamageDone stream, count positional hits/misses on the
    POSITIONAL_IDS. Returns a state dict; `detected=False` when the bonus byte
    isn't present on any event (FFLogs doesn't expose it) — the assume-hit case."""
    seen: set[tuple[int, int]] = set()   # (timestamp, abilityGameID) dedupe
    detected = False
    total = 0
    missed = 0
    miss_times: list[float] = []
    missed_by_ability: dict[int, int] = {}
    misses: list[dict[str, Any]] = []
    lost = 0.0

    for ev in events:
        if ev.get("type") not in _DAMAGE_TYPES:
            continue
        aid = ev.get("abilityGameID")
        if aid not in rd.POSITIONAL_IDS:
            continue
        key = (ev.get("timestamp", 0), aid)
        if key in seen:
            continue
        seen.add(key)
        bonus = _bonus_value(ev)
        if bonus is None:
            continue
        detected = True
        total += 1
        if bonus <= 0:
            missed += 1
            missed_by_ability[aid] = missed_by_ability.get(aid, 0) + 1
            t = (ev.get("timestamp", fight_start_ms) - fight_start_ms) / 1000.0
            miss_times.append(t)
            delta = rd.POTENCIES.get(aid, 0) - rd.POSITIONAL_MISS_POTENCY.get(aid, 0)
            lost += delta
            # Per-miss record (additive) — feeds the improvement card's located
            # children; `miss_times` keeps its historic shape.
            misses.append({"time_s": t, "ability_id": aid,
                           "lost_potency": float(delta)})

    return {
        "detected": detected,
        "total": total,
        "missed": missed,
        "missed_by_ability": missed_by_ability,
        "miss_times": sorted(miss_times),
        "misses": sorted(misses, key=lambda m: m["time_s"]),
        "lost_potency": round(lost, 1),
    }


class PositionalAspect:
    """Counts missed positionals from the bonus byte. Only wired into the Reaper
    aspect tuple once the live probe confirms FFLogs exposes the field."""

    name = "Positionals"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        s, e = fight["startTime"], fight["endTime"]
        try:
            events = client.get_events(code, s, e, actor["id"], data_type="DamageDone")
        except Exception:
            events = []
        state = detect_positional_misses(events, s)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state=state,
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        st = you.state
        findings: list[str] = []
        if st.get("detected") and st.get("missed", 0) > 0:
            findings.append(
                f"[positional] {st['missed']} of {st['total']} positionals missed "
                f"— ~{st.get('lost_potency', 0):.0f}p")
        return AspectComparison(aspect_name=self.name, findings=findings)


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


# Name + required direction per positional id, for the per-miss child summaries.
_POSITIONAL_INFO: dict[int, tuple[str, str]] = {
    rd.GIBBET: ("Gibbet", "flank"),
    rd.GALLOWS: ("Gallows", "rear"),
    rd.EXEC_GIBBET: ("Executioner's Gibbet", "flank"),
    rd.EXEC_GALLOWS: ("Executioner's Gallows", "rear"),
}


def improvements_from_positionals(state: dict) -> list:
    """One priced card for missed positionals (the summed positional delta);
    with 2+ misses it carries one located child per miss (a single miss keeps
    the card a directly-jumpable leaf). Empty when undetected or all-hit."""
    from jobs._core.improvements import Improvement
    if not state.get("detected") or float(state.get("lost_potency", 0) or 0) <= 0:
        return []
    miss_times = state.get("miss_times") or [0.0]
    # Old-shape states (no `misses` records) degrade to a childless card.
    misses = state.get("misses") or []
    # A single miss makes the card a jumpable leaf — carry the ACTUAL missed
    # ability so the timeline's ability-aware highlight lands on the real miss
    # (a fixed representative id would pulse an innocent same-time cast).
    lead_id, lead_name = rd.GIBBET, "Positionals"
    if len(misses) == 1:
        lead_id = int(misses[0].get("ability_id", 0) or 0)
        lead_name = _POSITIONAL_INFO.get(lead_id, ("Positionals", ""))[0]
    children: list = []
    if len(misses) >= 2:
        for m in misses:
            aid = int(m.get("ability_id", 0) or 0)
            name, direction = _POSITIONAL_INFO.get(aid, ("This cast", "flank/rear"))
            t = float(m.get("time_s", 0.0) or 0.0)
            p = float(m.get("lost_potency", 0.0) or 0.0)
            children.append(Improvement(
                kind="positional", ability_id=aid, ability_name=name,
                time_s=t, lost_potency=p,
                summary=f"{_mmss(t)} — {name} missed its {direction} positional "
                        f"(−{p:.0f}p): reposition or use True North"))
    return [Improvement(
        kind="positional", ability_id=lead_id, ability_name=lead_name,
        time_s=miss_times[0], lost_potency=float(state["lost_potency"]),
        summary=f"Missed {state.get('missed', 0)} positionals — "
                f"hit flank/rear on Gibbet/Gallows & Void/Cross Reaping",
        children=children)]
