"""Throwaway: wall-time + score of the MCH beam ceiling vs the refined greedy.

Run from python/:  python scripts/bench_mch_beam.py [duration]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine                  # noqa: E402
from jobs.machinist import simulator as mch_sim    # noqa: E402
from jobs.machinist.scoring import score_delivered_potency  # noqa: E402


def main() -> None:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 507.0
    dt = [(200.0, 230.0)]
    model = mch_sim.MachinistRotationModel()

    def score(tl, aux, bi):
        return score_delivered_potency(tl, aux, bi)

    t0 = time.perf_counter()
    tl_p, aux_p = engine.perfect(model, score, dur, dt, None)
    t_perfect = time.perf_counter() - t0
    s_perfect = score(tl_p, aux_p, None)

    t0 = time.perf_counter()
    tl_b, aux_b = engine.beam_perfect(model, score, dur, dt, None,
                                      width=mch_sim._BEAM_WIDTH)
    t_beam = time.perf_counter() - t0
    s_beam = score(tl_b, aux_b, None)

    n_gcd_p = sum(1 for t, a in tl_p if t >= 0)
    n_gcd_b = sum(1 for t, a in tl_b if t >= 0)
    print(f"dur={dur:.0f}s width={mch_sim._BEAM_WIDTH}")
    print(f"perfect: {t_perfect:6.1f}s  score={s_perfect:9.0f}  casts={n_gcd_p}")
    print(f"beam:    {t_beam:6.1f}s  score={s_beam:9.0f}  casts={n_gcd_b}  "
          f"delta={s_beam - s_perfect:+.0f}")


if __name__ == "__main__":
    main()
