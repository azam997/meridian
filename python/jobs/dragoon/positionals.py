"""Positional hit/miss detection for Dragoon.

The raw FFXIV damage line carries a *bonus byte* — "the percentage of the damage
that came from positional and/or combo bonuses" (cactbot LogGuide). DRG's three
positional GCDs — **Chaotic Spring** and **Wheeling Thrust** (rear) and **Fang and
Claw** (flank) — are combo actions, so the byte folds combo + positional together;
but the COMBO bonus is always present in the rotation (these only fire mid-combo),
so a byte at the combo-only level = positional MISS, a higher byte = HIT. The
detector below keys off the byte being above the combo-only floor.

DRG leans on positionals far more than SAM (three of them, every combo), so this
aspect is wired from v1 (unlike RPR, which left its identical scaffold behind a
probe). Until the live probe (Phase: calibration) confirms FFLogs exposes the byte
and pins the combo-only floor, `BONUS_KEYS` / `_COMBO_ONLY_MAX` are best guesses and
the aspect degrades gracefully: when the byte isn't present on any event, it reports
`detected=False` (assume-always-hit, no card) and the idealized + delivered both
score positionals at the hit value.
"""
from __future__ import annotations

from typing import Any

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs.dragoon import data as dd


# Candidate JSON keys for the positional/combo bonus byte on a FFLogs damage event.
# The live probe replaces this with the confirmed key.
BONUS_KEYS: tuple[str, ...] = ("bonusPercent", "directionalBonusPercent", "bonus")
# One cast emits BOTH a `calculateddamage` (snapshot) and a `damage` (resolution)
# event ~1s apart (verified live: 72 positional casts -> 72+72 events), so the
# (timestamp, id) dedupe can't collapse the pair — count ONE type only, preferring
# the resolved `damage` events, falling back to the snapshots for logs without
# them. (Before this fix the detector counted every cast twice, doubling the
# positional totals and the priced lost_potency.)
# NOTE: True North (the melee role oGCD) grants the positional bonus from any
# direction, so a TN-covered cast carries the HIT byte — the byte-based
# detection handles it with no window cross-referencing.
_DAMAGE_TYPES = ("damage", "calculateddamage")
# A combo'd-but-positional-MISSED DRG GCD still shows the combo bonus. The combo-only
# byte sits at/below this; a positional HIT pushes it higher. VERIFIED live: FFLogs
# exposes `bonusPercent`, and a HIT on Chaotic Spring / Wheeling Thrust / Fang and Claw
# reads 58 = (340-140)/340 (combo + positional); a MISS reads 53 = (300-140)/300
# (combo only). 55 is the midpoint floor (miss <= 53 < 55 < 58 = hit).
_COMBO_ONLY_MAX: int = 55


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
    POSITIONAL_IDS. Returns a state dict; `detected=False` when the bonus byte isn't
    present on any event (FFLogs doesn't expose it) — the assume-hit case."""
    by_type: dict[str, list[dict]] = {k: [] for k in _DAMAGE_TYPES}
    seen: set[tuple[str, int, int]] = set()
    for ev in events:
        typ = ev.get("type")
        if typ not in by_type:
            continue
        aid = ev.get("abilityGameID")
        if aid not in dd.POSITIONAL_IDS:
            continue
        key = (typ, ev.get("timestamp", 0), aid)
        if key in seen:
            continue
        seen.add(key)
        by_type[typ].append(ev)
    # One event per cast: the resolved `damage` stream when present, else the
    # `calculateddamage` snapshots.
    rows = by_type["damage"] or by_type["calculateddamage"]

    detected = False
    total = 0
    missed = 0
    miss_times: list[float] = []
    missed_by_ability: dict[int, int] = {}
    misses: list[dict[str, Any]] = []
    lost = 0.0
    for ev in rows:
        aid = ev.get("abilityGameID")
        bonus = _bonus_value(ev)
        if bonus is None:
            continue
        detected = True
        total += 1
        if bonus <= _COMBO_ONLY_MAX:
            missed += 1
            missed_by_ability[aid] = missed_by_ability.get(aid, 0) + 1
            t = (ev.get("timestamp", fight_start_ms) - fight_start_ms) / 1000.0
            miss_times.append(t)
            delta = dd.POTENCIES.get(aid, 0) - dd.POSITIONAL_MISS_POTENCY.get(aid, 0)
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
    """Counts missed positionals from the bonus byte (Chaotic Spring / Wheeling
    Thrust / Fang and Claw). Degrades to no-finding when FFLogs doesn't expose the
    byte (detected=False)."""

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
    dd.CHAOTIC_SPRING: ("Chaotic Spring", "rear"),
    dd.WHEELING_THRUST: ("Wheeling Thrust", "rear"),
    dd.FANG_AND_CLAW: ("Fang and Claw", "flank"),
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
    lead_id, lead_name = dd.CHAOTIC_SPRING, "Positionals"
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
        summary=f"Missed {state.get('missed', 0)} positionals — hit rear on "
                f"Chaotic Spring/Wheeling Thrust & flank on Fang and Claw",
        children=children)]
