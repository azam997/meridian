"""Flamethrower downtime-edge squeeze.

Flamethrower is NOT part of the MCH rotation — a real GCD always out-potencies
it in uptime. It's a gain ONLY in one niche spot: cast as the boss goes
untargetable so a tick lands in the retarget gap. The simulator models exactly
that and nothing more — it emits a single Flamethrower at each downtime
boundary (worth one 120p tick), never during uptime.

These tests pin that narrow behavior so a future rotation/sim change can't
silently start firing Flamethrower as filler.

Run from python/:  python tests/test_flamethrower.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.improvements import improvements_from_flamethrower
from jobs.machinist.data import JOB_DATA as MCH_DATA, POTENCIES
from jobs.machinist.scoring import score_delivered_potency
from jobs.machinist.simulator import FLAMETHROWER, simulate_idealized


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ft_times(downtime: list[tuple[float, float]]) -> list[float]:
    timeline, _q = simulate_idealized(300.0, downtime)
    return [t for t, aid in timeline if aid == FLAMETHROWER]


def test_no_flamethrower_without_downtime() -> None:
    print()
    print("Test: no downtime -> no Flamethrower in the idealized rotation")
    _check("zero Flamethrower casts", _ft_times([]) == [], f"got {_ft_times([])}")


def test_short_downtime_gets_no_flamethrower() -> None:
    print()
    print("Test: downtime <= one GCD is too short to squeeze Flamethrower")
    # 2.0s < 2.5s GCD: no room to press Flamethrower without delaying return.
    short = _ft_times([(100.0, 102.0)])
    _check("no Flamethrower for a sub-GCD window", short == [], f"got {short}")
    # A window just over one GCD does qualify.
    ok = _ft_times([(100.0, 104.0)])
    _check("Flamethrower for a window > one GCD", len(ok) == 1, f"got {ok}")


def test_one_flamethrower_per_downtime_window() -> None:
    print()
    print("Test: one Flamethrower squeezed per downtime window, inside it")
    windows = [(100.0, 130.0), (200.0, 215.0)]
    times = _ft_times(windows)
    _check("exactly one Flamethrower per window", len(times) == len(windows),
           f"got {len(times)} for {len(windows)} windows: {times}")
    for t in times:
        inside = any(s <= t < e for s, e in windows)
        _check(f"Flamethrower @ {t:.1f}s sits inside a downtime window", inside)


def test_flamethrower_scores_one_tick() -> None:
    print()
    print("Test: a Flamethrower cast is worth exactly one 120p tick")
    _check("POTENCIES[Flamethrower] == 120", POTENCIES.get(FLAMETHROWER) == 120,
           f"got {POTENCIES.get(FLAMETHROWER)}")
    score = score_delivered_potency([(10.0, FLAMETHROWER)])
    _check("score of a lone Flamethrower == 120", abs(score - 120.0) < 1e-6,
           f"got {score}")


def test_downtime_with_flamethrower_beats_bare_skip() -> None:
    """The squeeze is a strict (if tiny) gain: scoring the idealized with the
    Flamethrower tick must exceed scoring the same timeline with it removed."""
    print()
    print("Test: the squeezed tick is a strict potency gain")
    timeline, q = simulate_idealized(300.0, [(100.0, 130.0)])
    with_ft = score_delivered_potency(timeline, q)
    without_ft = score_delivered_potency(
        [(t, aid) for t, aid in timeline if aid != FLAMETHROWER], q)
    _check(f"with-FT ({with_ft:.0f}) > without-FT ({without_ft:.0f})",
           with_ft > without_ft, f"delta={with_ft - without_ft}")


def test_missed_flamethrower_card() -> None:
    print()
    print("Test: a missed Flamethrower squeeze -> one full-value improvement card")
    idealized = [(100.0, FLAMETHROWER), (300.0, 7411)]
    actual = [(99.0, 7411), (300.0, 7411)]   # player never cast Flamethrower
    cards = improvements_from_flamethrower(actual, idealized, MCH_DATA, None, 600.0)
    _check("one card", len(cards) == 1, f"got {len(cards)}")
    _check("kind == flamethrower", cards[0].kind == "flamethrower")
    _check("priced at full 120p (not net-of-filler)",
           abs(cards[0].lost_potency - 120.0) < 1e-6, f"got {cards[0].lost_potency}")


def test_taken_flamethrower_no_card() -> None:
    print()
    print("Test: a Flamethrower you did take -> no missed card")
    idealized = [(100.0, FLAMETHROWER)]
    actual = [(101.0, FLAMETHROWER)]
    cards = improvements_from_flamethrower(actual, idealized, MCH_DATA, None, 600.0)
    _check("no card", cards == [], f"got {cards}")


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_no_flamethrower_without_downtime()
    test_short_downtime_gets_no_flamethrower()
    test_one_flamethrower_per_downtime_window()
    test_flamethrower_scores_one_tick()
    test_downtime_with_flamethrower_beats_bare_skip()
    test_missed_flamethrower_card()
    test_taken_flamethrower_no_card()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
