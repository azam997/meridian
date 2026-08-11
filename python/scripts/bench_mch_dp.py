"""Throwaway: MCH exact-DP convergence + wall time vs the beam seed.

Sizes `_DP_MAX_DURATION_S`. Run from python/:
    python scripts/bench_mch_dp.py 50 90 120
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import optimal                          # noqa: E402
from jobs.machinist import simulator as mch                 # noqa: E402
from jobs.machinist.scoring import score_delivered_potency  # noqa: E402


def score(tl, aux, bi):
    return score_delivered_potency(tl, aux, bi)


def main() -> None:
    durs = [float(a) for a in sys.argv[1:]] or [50.0]
    for dur in durs:
        model = mch._model_for(None)
        t0 = time.perf_counter()
        seed_tl, seed_aux = mch._beam_best(model, score, dur, [], None)
        t_beam = time.perf_counter() - t0
        seed = score(seed_tl, seed_aux, None)
        print(f"dur={dur:.0f}s  beam seed={seed:.0f} ({t_beam:.1f}s)")
        for mw in mch._SWEEP_MAX_WEAVES:
            params = mch.SimParams(max_weaves_per_gcd=mw)
            st: dict = {}
            t0 = time.perf_counter()
            tl, aux, proven = optimal.solve_optimal(
                model, score, dur, [], params, buff_intervals=None,
                incumbent=seed, time_box=60.0, stats=st)
            wall = time.perf_counter() - t0
            s = score(tl, aux, None)
            print(f"  mw={mw}  proven={proven}  wall={wall:5.1f}s  "
                  f"nodes={st['nodes']:>8}  states={st['states']:>8}  "
                  f"pb={st['pruned_bound']:>8}  pd={st['pruned_dominance']:>8}  "
                  f"mlw={st['max_layer_width']:>6}  "
                  f"dp={s:.0f}  delta-vs-seed={s - seed:+.0f}")


if __name__ == "__main__":
    main()
