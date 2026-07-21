"""Player-death windows.

When a player dies they cast nothing until they're resurrected, leaving a
long gap in their cast stream. That gap is NOT clipping (the boss is still
up, so it isn't downtime either) and NOT cooldown drift — it's death. This
module reconstructs the dead windows so the delivered-side fairness aspects
(Clipping, Drift) can exclude them, and so the improvements panel can price
death as its own first-class, located loss.

`resolve_deaths` is the single entry point called once per pull in
`analyze_pull`; it stashes the windows on `report["__deaths__"]`. Aspects
read them back via `read_deaths_from_report`. The idealized ceiling
deliberately ignores death (death is the player's fault, so its potency
cost stays inside the recoverable gap the improvements panel decomposes).
"""
from __future__ import annotations

from typing import Any


def _coalesce(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent windows (two deaths close together, or a
    death whose recovery runs into the next death)."""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: w[0])
    out: list[tuple[float, float]] = [ordered[0]]
    for s, e in ordered[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def compute_death_windows(death_times_s: list[float],
                          norm_casts: list[tuple[float, int]],
                          fight_duration_s: float,
                          ) -> list[tuple[float, float]]:
    """Pure function. For each death at `t_d`, the dead window runs until the
    player's next in-fight cast (resurrection + resume), or the end of the
    fight if they never cast again. Pre-pull casts (t < 0) are ignored.

    Fully testable without a client — `resolve_deaths` does the fetching.
    """
    cast_times = sorted(t for t, _ in norm_casts if t >= 0.0)
    windows: list[tuple[float, float]] = []
    for t_d in sorted(death_times_s):
        if t_d < 0:
            continue
        recovery = next((t for t in cast_times if t > t_d), fight_duration_s)
        if recovery > t_d:
            windows.append((t_d, recovery))
    return _coalesce(windows)


def resolve_deaths(client: Any, code: str, fight: dict[str, Any],
                   actor: dict[str, Any],
                   norm_casts: list[tuple[float, int]],
                   ) -> list[tuple[float, float]]:
    """Fetch the actor's death events and reconstruct dead windows.

    Best-effort: any failure (network error, unsupported report) returns
    `[]`, which degrades gracefully to the prior behavior (death reads as
    clipping). The `Deaths` event stream filtered by `sourceID = actor` is
    the actor's own deaths; each event's timestamp is when they died.
    """
    start = fight["startTime"]
    end = fight["endTime"]
    fight_duration_s = (end - start) / 1000.0
    try:
        events = client.get_events(code, start, end, actor["id"],
                                   data_type="Deaths")
    except Exception:
        return []
    death_times_s = [
        (ev["timestamp"] - start) / 1000.0
        for ev in events
        if ev.get("type") == "death" and ev.get("timestamp") is not None
    ]
    return compute_death_windows(death_times_s, norm_casts, fight_duration_s)


def read_deaths_from_report(report: dict[str, Any]
                            ) -> list[tuple[float, float]]:
    """Aspect-side helper. Reads the death windows stashed on the report dict
    by `analyze_pull`. Missing stash → `[]` (the path exercised by unit tests
    that call an aspect's `.analyze()` directly, and any non-`analyze_pull`
    caller)."""
    stash = report.get("__deaths__") if report else None
    if stash:
        return list(stash.get("windows") or [])
    return []
