"""Unit tests for begincast GCD-alignment in `fetch_norm_casts`
(jobs/_core/casts.py).

A hardcast (cast-time spell) logs a `begincast` at GCD start + a `cast` at
completion; an instant cast / oGCD logs only a `cast` at execution. The
normalizer anchors every landed cast at its GCD *start* — the begincast time for
a hardcast, the cast time otherwise — so caster GCD cadence is uniform. Instant
streams (no begincast events) must come out byte-identical to anchoring on cast.

Run from python/:  python tests/test_casts_begincast.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.casts import fetch_norm_casts

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


_FIGHT = {"startTime": 1000, "endTime": 11000}   # ms; start at t=1000
_ACTOR = {"id": 42}


class _Client:
    """Serves a fixed event list regardless of range (the real range filter +
    pagination live in CachedEventsClient; this exercises only normalization)."""

    def __init__(self, events: list[dict]):
        self._events = events

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        return list(self._events)


def _norm(events: list[dict]) -> list[tuple[float, int]]:
    return fetch_norm_casts(_Client(events), "ABC", _FIGHT, _ACTOR)


def test_hardcast_anchored_to_begincast() -> None:
    print("\nTest: a hardcast is anchored to its begincast (GCD start), not completion")
    out = _norm([
        {"type": "begincast", "timestamp": 2000, "abilityGameID": 100},
        {"type": "cast",      "timestamp": 4500, "abilityGameID": 100},
    ])
    _check("one cast emitted at the begincast time (t=1.0s)",
           out == [(1.0, 100)], f"got {out}")


def test_instant_cast_anchored_to_cast() -> None:
    print("\nTest: an instant cast (no begincast) stays at its cast time")
    out = _norm([{"type": "cast", "timestamp": 5000, "abilityGameID": 200}])
    _check("instant emitted at the cast time (t=4.0s)",
           out == [(4.0, 200)], f"got {out}")


def test_cancelled_hardcast_dropped() -> None:
    print("\nTest: a begincast with no matching cast (cancelled) produces no GCD")
    out = _norm([
        {"type": "begincast", "timestamp": 2000, "abilityGameID": 100},  # cancelled
        {"type": "cast",      "timestamp": 5000, "abilityGameID": 200},  # instant, other id
    ])
    _check("only the landed instant survives", out == [(4.0, 200)], f"got {out}")


def test_stale_begincast_beyond_window_treated_as_instant() -> None:
    print("\nTest: a same-id cast far past the begincast isn't mis-anchored")
    # begincast of 100 cancelled at t=2000; a separate instant 100 at t=9000
    # (7s gap > the 5.5s hardcast ceiling) must use its own cast time, not 2000.
    out = _norm([
        {"type": "begincast", "timestamp": 2000, "abilityGameID": 100},
        {"type": "cast",      "timestamp": 9000, "abilityGameID": 100},
    ])
    _check("uses the cast time (t=8.0s), not the stale begincast",
           out == [(8.0, 100)], f"got {out}")


def test_instant_only_stream_byte_identical() -> None:
    print("\nTest: an instant-only stream matches plain cast-anchoring (MCH/RPR/SAM)")
    events = [
        {"type": "cast", "timestamp": 3000, "abilityGameID": 1},
        {"type": "cast", "timestamp": 5500, "abilityGameID": 2},
        {"type": "cast", "timestamp": 8000, "abilityGameID": 3},
    ]
    expected = [(2.0, 1), (4.5, 2), (7.0, 3)]
    _check("identical to anchoring every cast at its cast time",
           _norm(events) == expected, f"got {_norm(events)}")


def test_prepull_hardcast_negative_time() -> None:
    print("\nTest: a pre-pull hardcast keeps its negative (begincast) time")
    # begincast before the pull (t=-0.5s), cast just after start.
    out = _norm([
        {"type": "begincast", "timestamp": 500,  "abilityGameID": 100},
        {"type": "cast",      "timestamp": 2000, "abilityGameID": 100},
    ])
    _check("anchored to the pre-pull begincast (t=-0.5s)",
           out == [(-0.5, 100)], f"got {out}")


def test_back_to_back_hardcasts() -> None:
    print("\nTest: consecutive hardcasts each anchor to their own begincast")
    out = _norm([
        {"type": "begincast", "timestamp": 2000, "abilityGameID": 100},
        {"type": "cast",      "timestamp": 4500, "abilityGameID": 100},
        {"type": "begincast", "timestamp": 4500, "abilityGameID": 101},
        {"type": "cast",      "timestamp": 7000, "abilityGameID": 101},
    ])
    _check("two GCDs spaced ~2.5s apart at their begincast times",
           out == [(1.0, 100), (3.5, 101)], f"got {out}")


def main() -> int:
    test_hardcast_anchored_to_begincast()
    test_instant_cast_anchored_to_cast()
    test_cancelled_hardcast_dropped()
    test_stale_begincast_beyond_window_treated_as_instant()
    test_instant_only_stream_byte_identical()
    test_prepull_hardcast_negative_time()
    test_back_to_back_hardcasts()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
