"""Downtime detection — Tier A primary, legacy heuristic as fallback.

`resolve_downtime` is the single entry point called once per pull in
`analyze_pull`. It tries Tier A (boss `targetabilityupdate` events) and
falls back to the legacy cast-gap heuristic only when the Tier A fetch
itself fails (network error / unsupported report). An empty Tier A
result with `fetched=True` is a *confirmed* "boss never went
untargetable" signal, not a missing-data case — we trust it.

The legacy heuristic stays here as `detect_downtime_windows` because the
fallback path needs it and `test_downtime_baseline.py` pins its current
output. Aspects no longer call it directly; they read the resolved
windows off `report["__downtime__"]`.
"""
from __future__ import annotations

from typing import Any

from .downtime_sources import fetch_tier_a_windows
from .job import PHYSICAL_RANGED, RolePolicy


# Default Tier-C fallback threshold for callers without a RolePolicy
# (e.g. test_downtime_baseline.py, which exists specifically to pin the
# unparameterized behavior). Production callers pass through a policy
# so the threshold can be role-tuned — caster/healer want a longer gap
# floor because hardcast slidecast pauses can legitimately exceed 8s.
DOWNTIME_GAP_THRESHOLD_S: float = 8.0


def detect_downtime_windows(norm_casts: list[tuple[float, int]],
                            fight_duration_s: float,
                            threshold_s: float = DOWNTIME_GAP_THRESHOLD_S
                            ) -> list[tuple[float, float]]:
    """Legacy cast-gap heuristic. Pinned by test_downtime_baseline.py.

    Returns sorted (start_s, end_s) tuples relative to fight start.
    Pre-pull casts (t<0) are ignored. Only used as Tier-C fallback when
    Tier A is unavailable.
    """
    windows: list[tuple[float, float]] = []
    last_t = 0.0
    for t, _ in norm_casts:
        if t < 0:
            continue
        if t - last_t > threshold_s:
            windows.append((last_t, t))
        last_t = t
    if fight_duration_s - last_t > threshold_s:
        windows.append((last_t, fight_duration_s))
    return windows


def overlap_seconds(interval_start: float, interval_end: float,
                    windows: list[tuple[float, float]]) -> float:
    """Total seconds of [interval_start, interval_end] that fall inside
    any of the provided windows. Used by both drift (cap minus downtime)
    and clipping (pair minus downtime)."""
    total = 0.0
    for w_start, w_end in windows:
        a = max(interval_start, w_start)
        b = min(interval_end, w_end)
        if b > a:
            total += b - a
    return total


def read_downtime_from_report(report: dict[str, Any],
                               norm_casts: list[tuple[float, int]],
                               fight_duration_s: float,
                               ) -> tuple[list[tuple[float, float]], str]:
    """Aspect-side helper. Reads the downtime stash placed on the report
    dict by `analyze_pull`. Falls back to local heuristic detection if
    the stash is missing — that path is exercised by unit tests that
    invoke an aspect's `.analyze()` directly without going through the
    pipeline.
    """
    stash = report.get("__downtime__") if report else None
    if stash:
        return stash["windows"], stash["source"]
    return detect_downtime_windows(norm_casts, fight_duration_s), \
        "fallback_heuristic"


def resolve_downtime(client: Any, code: str,
                     report_summary: dict[str, Any],
                     fight: dict[str, Any],
                     norm_casts: list[tuple[float, int]],
                     policy: RolePolicy = PHYSICAL_RANGED,
                     actor: dict[str, Any] | None = None,
                     ) -> tuple[list[tuple[float, float]], str]:
    """Single entry point. Returns `(windows, source)`.

    Source values:
      "targetability"      — Tier A confirmed (events fetched, may be empty)
      "fallback_heuristic" — Tier A failed entirely, legacy heuristic used

    `policy` only influences the Tier-C fallback threshold today; Tier A
    is identical across roles. Defaults to PHYSICAL_RANGED to keep
    pre-existing test callers compatible without rewiring. `actor` (the
    analyzed player) feeds the silent-despawn activity evidence; None just
    means the player-damage half of that evidence is skipped.
    """
    windows, fetched = fetch_tier_a_windows(client, code, report_summary, fight,
                                            actor=actor)
    if fetched:
        return windows, "targetability"
    duration = (fight["endTime"] - fight["startTime"]) / 1000.0
    return detect_downtime_windows(
        norm_casts, duration, policy.fallback_gap_threshold_s,
    ), "fallback_heuristic"
