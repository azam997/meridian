"""Diagnostic: dissect the SMN strict ceiling vs one specific pull.

The `--decompose` view compares against the DEFAULT-GCD lane; this reproduces
the production strict ceiling (the sub-GCD cadence sweep, `max` over contexts)
and diffs the WINNING ceiling timeline against the player's cast stream:

  * the gear inference + every swept cadence with its scored ceiling,
  * raw and RECAST-WEIGHTED GCD counts (an Emerald Rite is 0.6 of a standard
    GCD, a Ruby 1.2 — raw counts mislead on a mixed-recast job),
  * the player's recast-weighted demonstrated cadence (uptime / sum of mults)
    vs the sweep's floor — if the player demonstrably sustained a tighter
    standard-GCD-equivalent cadence than the band reaches, that IS the gap,
  * per-demi-window impulse counts and the fight-tail composition.

Run from python/:
    python scripts/diag_smn_ceiling.py --report fzRJFrV3vnqk7GyY --fight 19
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull                                   # noqa: E402
from jobs._core import ability_metadata                         # noqa: E402
from jobs._core.gcd_speed import (                              # noqa: E402
    CeilingContext, effective_gcd_for, subgcd_gcd_sweep,
)
from jobs.summoner import data as sd                            # noqa: E402
from jobs.summoner import scoring as sc                         # noqa: E402
from jobs.summoner import simulator as sim                      # noqa: E402
from sidecar.main import _client                                # noqa: E402

JOB = "Summoner"


def _is_gcd(aid: int) -> bool:
    m = ability_metadata.get_metadata(aid)
    return m is not None and not m.is_ogcd


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _weighted_gcds(tl) -> tuple[int, float]:
    n = 0
    w = 0.0
    for t, a in tl:
        if t >= 0 and a > 0 and _is_gcd(a):
            n += 1
            w += sd.RECAST_MULT.get(a, 1.0)
    return n, w


def _window_impulses(tl) -> list[tuple[float, str, int]]:
    demis = [(t, a) for t, a in tl if a in sd.DEMI_SUMMON_IDS and t >= 0]
    out = []
    for t, a in demis:
        n = sum(1 for tt, aa in tl
                if aa in sd.IMPULSE_IDS and t <= tt <= t + sd.DEMI_WINDOW_S)
        out.append((t, _name(a).replace("Summon ", ""), n))
    return out


def run(code: str, fid: int) -> None:
    c = _client()
    you = analyze_pull(JOB, c, code, fid, ranking_name=None, label="You")
    st = you.aspects["Scoring"].state
    dur = st["fight_duration_s"]
    dt = st["downtime_windows"]
    deliv, ideal = st["delivered_potency"], st["idealized_strict"]
    print(f"=== {code}#{fid}  dur={dur:.0f}s  downtime={dt} ===")
    print(f"delivered={deliv:.0f}  idealized_strict={ideal:.0f}  "
          f"eff={100 * deliv / ideal:.2f}%")

    pc = [(t, a) for t, a in you.norm_casts if t >= 0]
    gear = effective_gcd_for(you.norm_casts, _is_gcd, sim.SMN_GCD_S, dt)
    cadences = list(subgcd_gcd_sweep(gear, sim.SMN_GCD_S))
    print(f"\ngear inference: {gear:.4f}s  sweep cadences: "
          f"{[round(g, 4) for g in cadences]}")

    scored: list[tuple[float, float]] = []
    for g in cadences:
        ctx = CeilingContext(gcd_base_s=g, payload=None)
        v = sc.idealized_at_duration(dur, dt, None, sim_context=ctx)
        scored.append((v, g))
        print(f"  cadence {g:.4f}s -> scored ceiling {v:.0f}"
              f"{'   <-- eff basis' if abs(v - ideal) < 0.5 else ''}")
    best_v, best_g = max(scored)

    # The player's recast-weighted demonstrated cadence.
    pn, pw = _weighted_gcds(pc)
    off = sum(min(dur, e) - max(0.0, s) for s, e in dt)
    uptime = dur - off
    dem_raw = uptime / pn if pn else float("nan")
    dem_w = uptime / pw if pw else float("nan")
    print(f"\nplayer GCDs: raw n={pn}  weighted={pw:.1f}")
    print(f"demonstrated cadence: raw uptime/n={dem_raw:.4f}s   "
          f"RECAST-WEIGHTED uptime/w={dem_w:.4f}s")
    print(f"sweep floor: {min(cadences):.4f}s   winner: {best_g:.4f}s"
          f"{'   <-- player out-paces the band' if dem_w < min(cadences) - 1e-6 else ''}")

    # Diff the winning ceiling timeline vs the player.
    ctx = CeilingContext(gcd_base_s=best_g, payload=None)
    tl, _aux = sim.simulate_idealized_perfect(dur, dt, None, sim_context=ctx)
    tl = [(t, a) for t, a in tl if a > 0]
    sn, sw = _weighted_gcds(tl)
    print(f"\nwinning ceiling: GCDs raw n={sn}  weighted={sw:.1f}  "
          f"(player {pn} / {pw:.1f})")

    pcc = Counter(a for _t, a in pc)
    scc = Counter(a for t, a in tl if t >= 0)
    rows = []
    for a in set(pcc) | set(scc):
        d = (pcc.get(a, 0) - scc.get(a, 0)) * sd.POTENCIES.get(a, 0)
        if d:
            rows.append((d, _name(a), pcc.get(a, 0), scc.get(a, 0)))
    print("\nper-ability diff vs the WINNING ceiling (player - ceiling, by swing):")
    for d, name, p, s in sorted(rows, key=lambda x: -abs(x[0]))[:12]:
        print(f"  {name:<22} player={p:<3} ceiling={s:<3} p*d={d:+d}")

    print("\nper-window impulses (player):")
    for t, k, n in _window_impulses(pc):
        print(f"  {t:6.1f}  {k:14s} x{n}")
    print("per-window impulses (winning ceiling):")
    for t, k, n in _window_impulses(tl):
        print(f"  {t:6.1f}  {k:14s} x{n}")

    print(f"\nfight tail (last 30s), player vs ceiling:")
    print("  player: ", [(round(t, 1), _name(a)) for t, a in pc if t > dur - 30])
    print("  ceiling:", [(round(t, 1), _name(a)) for t, a in tl if t > dur - 30])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--fight", type=int, required=True)
    a = ap.parse_args()
    run(a.report, a.fight)
    return 0


if __name__ == "__main__":
    sys.exit(main())
