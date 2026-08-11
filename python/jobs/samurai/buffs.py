"""SAM per-pull buff measurements: Fugetsu coverage + Tengentsu Kenki.

Two things the SAM scorer reads from the player's own event streams:

  * **Fugetsu coverage** — Fugetsu is a maintained 13% personal-damage self-buff
    (the SAM analog of WAR's Surging Tempest). The idealized ceiling assumes full
    coverage; here we measure the *actual* coverage from the Fugetsu token in the
    `buffs` snapshot of the player's DamageDone events, so a dropped/late Fugetsu
    costs efficiency (and at 100% uptime the x1.13 cancels in the ratio).
  * **Tengentsu Kenki** — the defensive Tengentsu grants +10 Kenki each time it
    blocks a hit, applying `Tengentsu's Foresight`. We count those applications
    (each = +10 Kenki) and feed the total into the idealized ceiling as
    `sim_context`, so the ceiling spends the same Kenki the player got. This can't
    be simmed from first principles (it depends on the boss damage timeline, which
    isn't in the player's log), so the measured count is the honest, symmetric
    handling — see jobs/samurai/data.py.

Both fetches are cached per-pull (the second caller is free).
"""
from __future__ import annotations

from typing import Any

from jobs._core.entry_gauge import measure_entry_gauge as _shared_measure
from jobs.samurai import data as sd


# FFLogs encodes a status in a damage event's `buffs` string as (1000000 + id).
_FFLOGS_BUFF_OFFSET = 1000000
# A Fugetsu-bearing hit credits coverage forward to the next hit, capped here so
# a pause (movement / downtime) doesn't credit a long empty span.
_FUGETSU_HIT_HORIZON_S = 4.0


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            ls, le = out[-1]
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def measured_fugetsu_intervals(client, code: str, report: dict[str, Any],
                               fight: dict[str, Any], actor: dict[str, Any]
                               ) -> list[tuple[float, float, float]]:
    """Fugetsu coverage as a `(start, end, 1.13)` timeline, reconstructed from the
    Fugetsu token in the `buffs` snapshot of the player's DamageDone events.
    Falls back to full coverage (assume 100%, no penalty) if the stream is
    unavailable or carries no Fugetsu token — never penalize on missing data."""
    s, e = fight["startTime"], fight["endTime"]
    duration = (e - s) / 1000.0
    full = [(-10.0, duration + 1.0, sd.FUGETSU_MULT)]
    tokens = {str(sd.FUGETSU_STATUS_ID),
              str(_FFLOGS_BUFF_OFFSET + sd.FUGETSU_STATUS_ID)}

    try:
        dmg = client.get_events(code, s, e, actor["id"], data_type="DamageDone")
    except Exception:
        return full

    pts: list[tuple[float, bool]] = []
    for ev in dmg:
        if ev.get("type") != "calculateddamage":
            continue
        bs = (ev.get("buffs") or "").split(".")
        t = (ev.get("timestamp", s) - s) / 1000.0
        pts.append((t, any(tok in bs for tok in tokens)))
    pts.sort(key=lambda x: x[0])
    if not pts or not any(has for _, has in pts):
        return full

    covered: list[tuple[float, float]] = []
    for (t0, has0), (t1, _h1) in zip(pts, pts[1:]):
        if has0 and t1 > t0:
            covered.append((t0, min(t1, t0 + _FUGETSU_HIT_HORIZON_S)))
    if pts[-1][1]:
        covered.append((pts[-1][0], min(pts[-1][0] + _FUGETSU_HIT_HORIZON_S, duration)))
    return [(cs, ce, sd.FUGETSU_MULT) for cs, ce in _union(covered)]


def measured_tengentsu_kenki(client, code: str, fight: dict[str, Any],
                             actor: dict[str, Any]) -> int:
    """Total Kenki the player gained from Tengentsu blocks = (count of
    `Tengentsu's Foresight` applications) x 10. 0 if the stream is unavailable."""
    s, e = fight["startTime"], fight["endTime"]
    try:
        buffs = client.get_aura_events(code, s, e, actor["id"], data_type="Buffs")
    except Exception:
        return 0
    procs = sum(1 for ev in buffs
                if ev.get("type") == "applybuff"
                and ev.get("abilityGameID") == sd.TENGENTSU_FORESIGHT_STATUS_ID)
    return procs * sd.TENGENTSU_KENKI_PER_PROC


def fugetsu_coverage_pct(intervals: list[tuple[float, float, float]],
                         duration_s: float) -> float:
    """Coverage % over the fight span (for the human-facing state alias)."""
    covered = sum(min(e, duration_s) - max(s, 0.0)
                  for s, e, _m in intervals if e > 0 and s < duration_s)
    return round(100.0 * covered / duration_s, 1) if duration_s > 0 else 100.0


# Meditation is granted by Iaijutsu + Ogi Namikiri (mirrors the simulator's
# apply_cast); Shoha spends 3.
_MEDITATION_GENERATORS = frozenset({
    sd.MIDARE_SETSUGEKKA, sd.TENDO_SETSUGEKKA, sd.HIGANBANA, sd.OGI_NAMIKIRI})
_ENTRY_WINDOW_S = 25.0   # tight enough that Tengentsu/Meditate barely contaminate


def measure_entry_gauge(norm_casts) -> tuple[int, int]:
    """Gauge the player carried INTO the pull (a phased fight's P1->P2 leftover),
    inferred from their opening: the deepest deficit of each resource — what they
    must have started with to afford their early spends. Cold-starts never go
    negative -> 0 (a no-op outside phased fights). The ceiling is then seeded with
    the same entry gauge, so a loaded P2 opener is matched symmetrically (<=100%).

    Kenki is a `GaugeModel` -> the shared deepest-deficit measure (windowed: SAM's
    Meditate channel adds Kenki the cast stream can't see, so a full-fight measure
    would read phantom carry; the 25s window predates the first Meditate). Meditation
    isn't a `GaugeModel` (Iaijutsu/Ogi grant +1, Shoha spends 3) so it stays bespoke
    over the same window."""
    k = _shared_measure(norm_casts, sd.JOB_DATA.gauges,
                        window_s=_ENTRY_WINDOW_S).get("kenki", 0)
    m = m_min = 0
    for _t, aid in sorted((t, aid) for t, aid in norm_casts
                          if 0.0 <= t <= _ENTRY_WINDOW_S):
        if aid in _MEDITATION_GENERATORS:
            m += 1
        elif aid == sd.SHOHA:
            m -= 3
        m_min = min(m_min, m)
    return (k, min(3, max(0, -m_min)))
