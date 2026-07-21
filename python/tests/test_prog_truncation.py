"""Tests for the prog (wipe) scored-window truncation (`jobs/_core/prog.py`).

Layers:
  1. `terminal_death_ms` pure cases (alive / recovered / terminal /
     raise-then-die-again / raised-but-never-acted).
  2. The clamp interplay with `compute_death_windows`: a terminal death at the
     clamped boundary collapses to a zero-length (unpriced) window while
     mid-pull recovered deaths keep their cards.
  3. `build_prog_context` against a stub client: clamp target, full-span
     Tier-A pass-through, and the fetch-failure degradation.

Run from python/:  python tests/test_prog_truncation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.deaths import compute_death_windows
from jobs._core.prog import _MIN_SCORED_S, build_prog_context, terminal_death_ms

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


WIPE_END = 400_000


def test_terminal_death_cases() -> None:
    _check("no deaths -> None",
           terminal_death_ms([], [1000, 2000], WIPE_END) is None)
    _check("recovered -> None",
           terminal_death_ms([100_000], [1000, 150_000], WIPE_END) is None)
    _check("terminal death found",
           terminal_death_ms([300_000], [1000, 200_000], WIPE_END) == 300_000)
    _check("raise then die again -> second death",
           terminal_death_ms([100_000, 300_000], [1000, 150_000], WIPE_END)
           == 300_000)
    _check("raised but never acted -> first death",
           terminal_death_ms([100_000, 300_000], [1000, 50_000], WIPE_END)
           == 100_000)
    _check("no casts at all -> first death",
           terminal_death_ms([100_000, 300_000], [], WIPE_END) == 100_000)
    _check("cast at exact death ms is not recovery",
           terminal_death_ms([100_000], [1000, 100_000], WIPE_END) == 100_000)
    _check("deaths past wipe end ignored",
           terminal_death_ms([500_000], [1000], WIPE_END) is None)


def test_clamped_death_windows() -> None:
    """After the clamp the terminal death sits AT fight end: zero-length
    window, filtered — no death card. A mid-pull recovered death keeps its
    window."""
    duration_s = 300.0  # clamped at the terminal death
    casts = [(1.0, 1), (50.0, 1), (120.0, 1)]  # recovery cast at 120s
    windows = compute_death_windows([100.0, 300.0], casts, duration_s)
    _check("terminal death unpriced", all(e > s for s, e in windows)
           and not any(abs(s - 300.0) < 1e-6 for s, _ in windows),
           f"got {windows}")
    _check("mid-pull death priced", any(abs(s - 100.0) < 1e-6
                                        and abs(e - 120.0) < 1e-6
                                        for s, e in windows), f"got {windows}")


# --- Stub client for build_prog_context --------------------------------------

class _ProgClient:
    """Canned Deaths/Casts; Tier-A downtime is exercised through
    get_targetability_events (no events -> no downtime windows, was_fetched
    True). `fail` switches every fetch to raising."""

    def __init__(self, deaths_ms: list[int], casts_ms: list[int],
                 fail: bool = False):
        self._deaths = deaths_ms
        self._casts = casts_ms
        self._fail = fail
        self.primed: list = []

    def prime_bundle(self, code, streams):
        self.primed.append(list(streams))

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if self._fail:
            raise RuntimeError("boom")
        src = self._deaths if data_type == "Deaths" else self._casts
        typ = "death" if data_type == "Deaths" else "cast"
        return [{"timestamp": t, "type": typ} for t in src
                if start <= t <= end]

    def get_targetability_events(self, code, start, end):
        if self._fail:
            raise RuntimeError("boom")
        return []

    def get_enemy_cast_events(self, code, start, end):
        return []


_FIGHT = {
    "startTime": 1_000_000, "endTime": 1_000_000 + WIPE_END,
    "kill": False, "fightPercentage": 39.0, "bossPercentage": 45.0,
    "lastPhase": 4, "phaseTransitions": [{"id": 1, "startTime": 0}],
    "enemyNPCs": [],
}


def test_build_prog_context_clamps() -> None:
    client = _ProgClient(deaths_ms=[1_000_000 + 300_000],
                         casts_ms=[1_000_000 + 10_000])
    ctx = build_prog_context(client, "CODE", {"fights": [_FIGHT]},
                             dict(_FIGHT), {"id": 7})
    _check("wipe duration", abs(ctx.wipe_duration_s - 400.0) < 1e-6, str(ctx))
    _check("scored end at terminal death",
           ctx.scored_end_ms == 1_000_000 + 300_000
           and abs(ctx.scored_end_s - 300.0) < 1e-6, str(ctx))
    _check("terminal death reported",
           ctx.terminal_death_s is not None
           and abs(ctx.terminal_death_s - 300.0) < 1e-6, str(ctx))
    _check("fight meta carried", ctx.fight_pct == 39.0
           and ctx.boss_pct == 45.0 and ctx.last_phase == 4
           and len(ctx.phase_transitions) == 1, str(ctx))
    _check("tier-a fetched", ctx.full_downtime_source == "targetability",
           str(ctx))
    _check("bundle primed once", len(client.primed) == 1, str(client.primed))


def test_build_prog_context_alive() -> None:
    """Recovered (cast after the death): no truncation."""
    client = _ProgClient(deaths_ms=[1_000_000 + 100_000],
                         casts_ms=[1_000_000 + 150_000])
    ctx = build_prog_context(client, "CODE", {"fights": [_FIGHT]},
                             dict(_FIGHT), {"id": 7})
    _check("no truncation when recovered",
           ctx.scored_end_ms == _FIGHT["endTime"]
           and ctx.terminal_death_s is None, str(ctx))


def test_build_prog_context_degrades() -> None:
    """Every fetch failing -> no truncation, downtime unavailable."""
    client = _ProgClient(deaths_ms=[1_000_000 + 300_000], casts_ms=[],
                         fail=True)
    ctx = build_prog_context(client, "CODE", {"fights": [_FIGHT]},
                             dict(_FIGHT), {"id": 7})
    _check("degrades to wipe end", ctx.scored_end_ms == _FIGHT["endTime"]
           and ctx.terminal_death_s is None, str(ctx))
    _check("downtime unavailable",
           ctx.full_downtime_source == "unavailable"
           and ctx.full_downtime_windows == (), str(ctx))


def test_min_scored_floor() -> None:
    """A death during the very first second floors the scored window."""
    client = _ProgClient(deaths_ms=[1_000_000 + 200], casts_ms=[])
    ctx = build_prog_context(client, "CODE", {"fights": [_FIGHT]},
                             dict(_FIGHT), {"id": 7})
    _check("floored at _MIN_SCORED_S",
           abs(ctx.scored_end_s - _MIN_SCORED_S) < 1e-6, str(ctx))


def main() -> None:
    for fn in [test_terminal_death_cases, test_clamped_death_windows,
               test_build_prog_context_clamps, test_build_prog_context_alive,
               test_build_prog_context_degrades, test_min_scored_floor]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
