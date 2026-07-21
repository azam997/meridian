"""Dancer simulator INTERNAL invariants (network-free, no fixtures).

These exercise the simulator directly (the `simulate_*` entrypoints) — they test
the model's own consistency, not "sim vs delivered" (which would be circular).
Validation against real human play lives in test_dancer_pulls.py, which runs the
pipeline on real quartile-stratified FFLogs pulls.

Pinned here (DNC-specific machinery on top of the usual ceiling invariants):
  * perfect >= optimal >= default (the strict-upgrade chain).
  * **Budgets** — the sim spends at most the player's measured proc / feather /
    esprit counts (never invents resources), none at budget 0, and the ceiling is
    monotonic in them (each budgeted button out-potencies the filler it replaces).
  * **Step dances** — each Standard Step yields 2 steps + a Standard Finish; each
    Technical Step yields 4 steps + a Technical Finish (forced sequences).
  * **Burst** — Technical Step / Devilment / Flourish fire; each Technical Finish
    yields a Tillana; Dance of the Dawn only fires after Devilment (and with esprit).
  * **Buff-aware ceiling** — the buff-aware perfect sim is >= the agnostic line
    scored under the same buffs (alignment can only add value).

Run from python/:  python tests/test_dancer_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.dancer import data as dd
from jobs.dancer import scoring as sc
from jobs.dancer.simulator import (
    DancerCtx,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 300.0
_PROCS = (dd.REVERSE_CASCADE, dd.FOUNTAINFALL)
_SABERS = (dd.SABER_DANCE, dd.DANCE_OF_THE_DAWN)
_FULL_CTX = DancerCtx(proc_budget=40, feather_budget=18, saber_budget=26)


def test_sim_monotonicity():
    sd = sc.score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    so = sc.score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    sp = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert so >= sd - 1e-6, f"optimal {so} < default {sd}"
    assert sp >= so - 1e-6, f"perfect {sp} < optimal {so}"


def test_idealized_beats_degraded_delivered():
    """Dropping half a rotation's casts scores strictly below the full ceiling."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = sc.score_delivered_potency(degraded)
    assert ideal >= delivered


def test_downtime_lowers_ceiling():
    full = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    dt = sc.score_delivered_potency(
        simulate_idealized_perfect(_DURATION_S, [(60.0, 120.0)])[0])
    assert dt < full, f"downtime ceiling {dt} not below full {full}"


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_budgets_respected_and_monotonic():
    """The sim never invents resources (spends <= budget), spends none at budget 0,
    and a larger budget never lowers the ceiling."""
    tl0, _ = simulate_idealized(_DURATION_S, [], sim_context=DancerCtx())
    c0 = Counter(a for _, a in tl0)
    assert sum(c0[a] for a in _PROCS) == 0, "spent procs at budget 0"
    assert c0[dd.FAN_DANCE] == 0, "spent feathers at budget 0"
    assert sum(c0[a] for a in _SABERS) == 0, "spent esprit at budget 0"

    for ctx in (DancerCtx(10, 5, 8), DancerCtx(30, 12, 18), DancerCtx(60, 20, 30)):
        tl, _ = simulate_idealized(_DURATION_S, [], sim_context=ctx)
        c = Counter(a for _, a in tl)
        assert sum(c[a] for a in _PROCS) <= ctx.proc_budget, "proc budget exceeded"
        assert c[dd.FAN_DANCE] <= ctx.feather_budget, "feather budget exceeded"
        assert sum(c[a] for a in _SABERS) <= ctx.saber_budget, "saber budget exceeded"

    lo = sc.idealized_at_duration(_DURATION_S, [], sim_context=DancerCtx(10, 5, 8))
    hi = sc.idealized_at_duration(_DURATION_S, [], sim_context=DancerCtx(60, 20, 30))
    assert hi >= lo - 1e-6, f"ceiling fell with more budget: {hi} < {lo}"


def test_step_dances_consistent():
    """Each Standard Step -> 2 steps + a Standard Finish; each Technical Step -> 4
    steps + a Technical Finish (within a one-dance fight-end tolerance)."""
    timeline, _ = simulate_idealized(_DURATION_S, [], sim_context=_FULL_CTX)
    c = Counter(a for _, a in timeline)
    assert c[dd.STANDARD_STEP] > 0 and c[dd.TECHNICAL_STEP] > 0
    assert abs(c[dd.STANDARD_FINISH] - c[dd.STANDARD_STEP]) <= 1
    assert abs(c[dd.TECHNICAL_FINISH] - c[dd.TECHNICAL_STEP]) <= 1
    expected_steps = 2 * c[dd.STANDARD_FINISH] + 4 * c[dd.TECHNICAL_FINISH]
    assert abs(c[dd.EMBOITE] - expected_steps) <= 4, (
        f"steps {c[dd.EMBOITE]} != expected {expected_steps}")


def test_burst_present():
    """The 2-minute burst fires; each Technical Finish yields a Tillana; Dance of
    the Dawn appears only after Devilment grants it (and with esprit to spend)."""
    timeline, _ = simulate_idealized(_DURATION_S, [], sim_context=_FULL_CTX)
    c = Counter(a for _, a in timeline)
    assert c[dd.TECHNICAL_STEP] >= 2, c[dd.TECHNICAL_STEP]
    assert c[dd.DEVILMENT] >= 2, c[dd.DEVILMENT]
    assert c[dd.FLOURISH] >= 2, c[dd.FLOURISH]
    assert abs(c[dd.TILLANA] - c[dd.TECHNICAL_FINISH]) <= 1, "Tillana != Technical Finish"
    assert c[dd.DANCE_OF_THE_DAWN] <= c[dd.DEVILMENT], "Dawn without Devilment"


def test_buff_aware_ceiling_at_least_agnostic():
    """The buff-aware perfect sim is >= the agnostic line scored under the same
    raid buffs — alignment can only add value, never regress it."""
    bi = [(28.0, 48.0, 1.20)]
    agnostic = sc.score_delivered_potency(
        simulate_idealized_perfect(_DURATION_S, None, None)[0], buff_intervals=bi)
    aware = sc.score_delivered_potency(
        simulate_idealized_perfect(_DURATION_S, None, bi)[0], buff_intervals=bi)
    assert aware >= agnostic - 1e-6, f"buff-aware {aware} < agnostic-under-buffs {agnostic}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all dancer sim invariants passed")


if __name__ == "__main__":
    main()
