"""Red Mage simulator INTERNAL invariants (network-free, no fixtures).

These exercise the simulator directly (the `simulate_*` entrypoints) — they test
the model's own consistency, not "sim vs delivered" (which would be circular).
Validation against real human play lives in test_redmage_pulls.py, which runs
the pipeline on real quartile-stratified FFLogs pulls.

Pinned here (caster-specific machinery on top of the usual ceiling invariants):
  * perfect >= optimal >= default (the strict-upgrade chain).
  * **Dualcast alternation** — every Dualcasted 440 (Verthunder III / Veraero
    III, run instant) is paired with a 2 s-cast enabler (Jolt III / Verfire /
    Verstone) that granted the Dualcast, so the counts stay balanced.
  * **Proc budget** — the sim spends at most the player's proc count (never
    invents procs), none at budget 0, and the ceiling is monotonic in it.
  * The enchanted melee combo + finisher chain is consistent, and the 2-minute
    burst (Embolden / Manafication, with Manafication's 3 FREE Magicked
    Swordplay combo casts) fires.

Run from python/:  python tests/test_redmage_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.redmage import data as rd
from jobs.redmage import scoring as sc
from jobs.redmage.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 300.0
_ENABLERS = (rd.JOLT_III, rd.VERFIRE, rd.VERSTONE)
_DUALCAST_SPELLS = (rd.VERTHUNDER_III, rd.VERAERO_III)
_PROCS = (rd.VERFIRE, rd.VERSTONE)


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


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_dualcast_alternation():
    """The filler is Jolt III / Verfire / Verstone (2 s hardcast, grants Dualcast)
    -> Verthunder III / Veraero III (Dualcasted instant), 1:1 — EXCEPT for the
    free instants banked by Acceleration AND Swiftcast, each of which runs an
    extra Dualcast-less 440. So n(VT3+VA3) == n(enablers) + n(free-instant
    sources), within opener/end tolerance."""
    timeline, _ = simulate_idealized(_DURATION_S, [], sim_context=40)
    c = Counter(a for _, a in timeline)
    n_dual = sum(c[a] for a in _DUALCAST_SPELLS)
    n_enabler = sum(c[a] for a in _ENABLERS)
    n_free_instant = c[rd.ACCELERATION] + c[rd.SWIFTCAST]
    assert n_dual > 0
    assert abs(n_dual - (n_enabler + n_free_instant)) <= 2, (
        f"dualcast {n_dual} != enablers {n_enabler} + free-instants {n_free_instant}")


def test_proc_budget_respected_and_monotonic():
    """The sim never invents procs (spends <= budget), spends none at budget 0,
    and a larger budget never lowers the ceiling (procs >= the Jolt III they
    replace)."""
    tl0, _ = simulate_idealized(_DURATION_S, [], sim_context=0)
    c0 = Counter(a for _, a in tl0)
    assert sum(c0[a] for a in _PROCS) == 0, "spent procs at budget 0"

    for budget in (10, 30, 80):
        tl, _ = simulate_idealized(_DURATION_S, [], sim_context=budget)
        c = Counter(a for _, a in tl)
        assert sum(c[a] for a in _PROCS) <= budget, f"budget {budget} exceeded"

    lo = sc.idealized_at_duration(_DURATION_S, [], sim_context=10)
    hi = sc.idealized_at_duration(_DURATION_S, [], sim_context=80)
    assert hi >= lo - 1e-6, f"ceiling fell with more procs: {hi} < {lo}"


def test_melee_combo_and_finisher_consistent():
    """One Scorch and one Resolution per Verflare/Verholy, and each combo opens
    with an Enchanted Riposte."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    finishers = c[rd.VERFLARE] + c[rd.VERHOLY]
    assert finishers > 0, "no melee finisher fired"
    assert c[rd.SCORCH] == finishers, f"Scorch {c[rd.SCORCH]} != finishers {finishers}"
    assert c[rd.RESOLUTION] == finishers, f"Resolution {c[rd.RESOLUTION]} != {finishers}"
    assert c[rd.ENCHANTED_RIPOSTE] == finishers, "combo openers != finishers"


def test_burst_present_with_free_manafication_combo():
    """The 2-minute burst fires, and each Manafication yields a FREE combo: the
    total Enchanted Riposte count exceeds the number of paid combos the mana
    alone could buy (i.e. Manafication's 3 Magicked Swordplay stacks add combos,
    not just refund mana — which it no longer does as of 7.0)."""
    timeline, _ = simulate_idealized(_DURATION_S, [], sim_context=24)
    c = Counter(a for _, a in timeline)
    assert c[rd.EMBOLDEN] >= 2, c[rd.EMBOLDEN]
    assert c[rd.MANAFICATION] >= 2, c[rd.MANAFICATION]
    assert c[rd.PREFULGENCE] == c[rd.MANAFICATION]
    assert c[rd.VICE_OF_THORNS] == c[rd.EMBOLDEN]
    # Every Manafication adds one free combo on top of the mana-fed ones.
    assert c[rd.ENCHANTED_RIPOSTE] > c[rd.MANAFICATION], (
        f"combos {c[rd.ENCHANTED_RIPOSTE]} <= Manafications {c[rd.MANAFICATION]} "
        "— free combo not credited")


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all red mage sim invariants passed")


if __name__ == "__main__":
    main()
