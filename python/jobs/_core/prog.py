"""Prog-pull (wipe) context: the pre-pass that runs before the normal pipeline
when the analyzed fight is not a kill.

Two jobs, one bundled round trip:
  1. Find the *terminal death* — the earliest player death with no cast after
     it — so `analyze_pull` can clamp the scored window there (the ceiling
     then never charges the dead tail of a wipe; the terminal death itself
     collapses to a zero-length death window and is not priced as a card).
  2. Compute the FULL-span Tier-A downtime for the kill-time projector, which
     is party-scoped: the party fought to the wipe timestamp even if the
     analyzed player died earlier, so the projector's active-time denominator
     must cover the whole pull, not the truncated scored window.

Runs against the wipe's un-clamped window, before the clamp — the pipeline's
own `_prime_pull_bundle` then re-primes at the clamped window (a second round
trip only when a terminal death actually shortens the fight; otherwise the
spans coincide and the pre-pass streams are reused as cache hits).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Floor for the scored window: a sub-second scored duration (death during the
# opener's first GCD) has no meaningful ceiling and risks zero-denominator
# efficiency. One second keeps the math defined; the analysis is degenerate
# either way and the UI still shows the full pull duration + projection.
_MIN_SCORED_S = 1.0


@dataclass(frozen=True)
class ProgContext:
    wipe_duration_s: float
    scored_end_s: float            # fight-relative; == wipe_duration_s if alive
    scored_end_ms: int             # absolute; what analyze_pull clamps endTime to
    terminal_death_s: float | None
    fight_pct: float | None        # FFLogs fightPercentage (whole-fight % left)
    boss_pct: float | None         # current boss HP % (display-only)
    last_phase: int | None
    phase_transitions: tuple = ()  # untouched pass-through (phase-aware seam)
    full_downtime_windows: tuple[tuple[float, float], ...] = ()
    full_downtime_source: str = "unavailable"


def terminal_death_ms(death_times_ms: list[int],
                      cast_times_ms: list[int],
                      wipe_end_ms: int) -> int | None:
    """The earliest death with no player cast strictly after it, or None if
    the player was alive (or re-raised and acting) at the wipe. Pure.

    Raise-then-die-again chains resolve naturally: a death followed by any
    cast is recovered; the terminal death is the first one after the last
    cast. A raise the player never acted on does NOT count as recovery —
    standing there dead-weight scores the same as staying dead.
    """
    deaths = sorted(t for t in death_times_ms if t <= wipe_end_ms)
    if not deaths:
        return None
    last_cast = max((t for t in cast_times_ms if t <= wipe_end_ms),
                    default=None)
    for d in deaths:
        if last_cast is None or last_cast <= d:
            return d
    return None


def build_prog_context(client: Any, code: str, report: dict[str, Any],
                       fight: dict[str, Any], actor: dict[str, Any],
                       ) -> ProgContext:
    """Best-effort: every fetch failure degrades to 'no truncation, no
    downtime data' — the wipe is then scored to its end (historic behavior)
    and the projector treats the whole pull as active time."""
    start = fight["startTime"]
    end = fight["endTime"]
    wipe_duration_s = (end - start) / 1000.0
    aid = actor["id"]

    # One aliased round trip seeding exactly the keys the fetches below (and
    # fetch_tier_a_windows) use at the full wipe span. Best-effort.
    prime = getattr(client, "prime_bundle", None)
    if prime is not None:
        try:
            from fflogs_api import BundleStream
            prime(code, [
                BundleStream(data_type="Deaths", start=start, end=end,
                             source_id=aid),
                BundleStream(data_type="All", start=start, end=end,
                             filter_expression='type="targetabilityupdate"'),
                BundleStream(data_type="Casts", start=start, end=end,
                             hostility="Enemies"),
                BundleStream(data_type="DamageDone", start=start, end=end,
                             source_id=aid),
            ])
        except Exception:
            pass

    # Terminal death: deaths first, then (only if any) the player's casts from
    # the first death on — one small extra fetch.
    term_ms: int | None = None
    try:
        death_events = client.get_events(code, start, end, aid,
                                         data_type="Deaths")
        death_times = [ev["timestamp"] for ev in death_events
                       if ev.get("type") == "death"
                       and ev.get("timestamp") is not None]
        if death_times:
            first_death = min(death_times)
            try:
                cast_events = client.get_events(code, first_death, end, aid,
                                                data_type="Casts")
                cast_times = [ev["timestamp"] for ev in cast_events
                              if ev.get("timestamp") is not None]
            except Exception:
                cast_times = []
            term_ms = terminal_death_ms(death_times, cast_times, end)
    except Exception:
        term_ms = None

    scored_end_ms = end if term_ms is None else term_ms
    scored_end_ms = max(scored_end_ms, start + int(_MIN_SCORED_S * 1000))

    # Full-span Tier-A downtime (party-scoped projector input). The un-clamped
    # `fight` keys these fetches at the wipe span — primed above.
    from jobs._core.downtime_sources import fetch_tier_a_windows
    try:
        windows, was_fetched = fetch_tier_a_windows(
            client, code, report, fight, actor=actor)
    except Exception:
        windows, was_fetched = [], False

    return ProgContext(
        wipe_duration_s=wipe_duration_s,
        scored_end_s=(scored_end_ms - start) / 1000.0,
        scored_end_ms=scored_end_ms,
        terminal_death_s=(None if term_ms is None
                          else (scored_end_ms - start) / 1000.0),
        fight_pct=fight.get("fightPercentage"),
        boss_pct=fight.get("bossPercentage"),
        last_phase=fight.get("lastPhase"),
        phase_transitions=tuple(fight.get("phaseTransitions") or ()),
        full_downtime_windows=tuple((float(a), float(b))
                                    for a, b in windows),
        full_downtime_source=("targetability" if was_fetched
                              else "unavailable"),
    )
