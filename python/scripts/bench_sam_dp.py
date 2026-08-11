"""Throwaway: SAM exact-DP convergence + wall time vs the beam seed.

Sizes `_DP_MAX_DURATION_S` (the SAM counterpart of bench_mch_dp.py). Run from python/:
    python scripts/bench_sam_dp.py 120 180 240
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine, optimal                  # noqa: E402
from jobs.samurai import simulator as sam                   # noqa: E402


def main() -> None:
    durs = [float(a) for a in sys.argv[1:]] or [120.0]
    for dur in durs:
        model = sam._model_for(dur, 0)
        t0 = time.perf_counter()
        seed_tl, seed_aux = engine.beam_perfect(
            model, sam._score, dur, [], None, width=sam._BEAM_WIDTH)
        t_beam = time.perf_counter() - t0
        seed = sam._score(seed_tl, seed_aux, None)
        print(f"dur={dur:.0f}s  beam seed={seed:.0f} ({t_beam:.1f}s)")
        for mw in sam._SWEEP_MAX_WEAVES:
            params = sam.SimParams(max_weaves_per_gcd=mw)
            st: dict = {}
            t0 = time.perf_counter()
            tl, aux, proven = optimal.solve_optimal(
                model, sam._score, dur, [], params, buff_intervals=None,
                incumbent=seed, time_box=120.0, stats=st)
            wall = time.perf_counter() - t0
            s = sam._score(tl, aux, None)
            print(f"  mw={mw}  proven={proven}  wall={wall:5.1f}s  "
                  f"nodes={st['nodes']:>8}  states={st['states']:>8}  "
                  f"pb={st['pruned_bound']:>8}  pd={st['pruned_dominance']:>8}  "
                  f"mlw={st['max_layer_width']:>6}  "
                  f"dp={s:.0f}  delta-vs-seed={s - seed:+.0f}")


if __name__ == "__main__":
    main()
