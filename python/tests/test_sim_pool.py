"""Process-pool acceleration of the perfect-sim (sidecar/sim_pool.py + the scoring
cache dispatch). The pool runs the SAME deterministic sim in a worker process, so its
output must be byte-identical to in-process — this is the safety net for that claim.

Two layers:
  * A pickle-round-trip fake pool (fast, deterministic, no process spawn) — proves the
    dispatch wiring (import-by-name + the exact kwargs), picklability of the args/return,
    and that `prime` actually warms the cache so the following reads are hits.
  * A real ProcessPoolExecutor smoke (slow) — proves true cross-process byte-identity.
    Safe by construction: SimPool falls back to in-process on any pool trouble, so the
    equivalence assertion holds either way.

The real cross-process speedup + byte-identity at scale is measured by
`scripts/bench_sim_pool.py` (≈4.3–4.5x on SAM/MCH).

Run from python/:  python tests/test_sim_pool.py
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import scoring                       # noqa: E402
from jobs.machinist import scoring as mch_scoring        # noqa: E402
from jobs.machinist import simulator as mch              # noqa: E402
from jobs.samurai import scoring as sam_scoring          # noqa: E402
from jobs.samurai import simulator as sam                # noqa: E402
from sidecar import sim_pool                             # noqa: E402


class _RoundTripPool:
    """Mimics SimPool but runs in-process, round-tripping args + result through pickle —
    so it asserts picklability and exercises `_sim_worker` exactly as the real pool
    would, without spawning processes (robust under pytest-xdist)."""

    def __init__(self):
        self.run_calls = 0
        self.run_many_calls = 0
        self.run_many_items = 0

    def reset(self):
        self.run_calls = self.run_many_calls = self.run_many_items = 0

    def _exec(self, m, f, a, k):
        a = pickle.loads(pickle.dumps(a))
        k = pickle.loads(pickle.dumps(k))
        return pickle.loads(pickle.dumps(sim_pool._sim_worker(m, f, a, k)))

    def run(self, m, f, a=(), k=None):
        self.run_calls += 1
        return self._exec(m, f, a, k or {})

    def run_many(self, calls):
        calls = list(calls)
        self.run_many_calls += 1
        self.run_many_items += len(calls)
        return [self._exec(m, f, a, k or {}) for (m, f, a, k) in calls]


def _timeline(simfn, dur):
    return list(simfn(dur, [])[0])


def _rotation(timeline):
    """The GCD/oGCD spine without tincture pot markers. The optimal-pot placement
    (`scoring._finalize` -> `place_optimal_pots`) runs in the MAIN process identically
    on both the pooled and the in-process path, so the only thing the pool can affect —
    and thus the byte-identity that matters here — is the raw sim rotation; the greedy
    vs optimal pot marker is not it (and the raw `simulate_idealized_perfect` reference
    still carries the engine's greedy marker, which `_finalize` strips and re-places)."""
    from jobs._core.tincture import TINCTURE_ACTION_ID
    return sorted((t, a) for t, a in timeline if a != TINCTURE_ACTION_ID)


@pytest.mark.slow
def test_dispatch_byte_identical():
    """A scoring-cache miss dispatched through the pool returns byte-identical to the
    direct in-process sim, for both SAM (DP path) + MCH (beam path). Distinct durations
    avoid cross-test cache hits."""
    cases = [
        (sam, sam_scoring, 141.0),   # SAM DP duration (<= 240s gate)
        (sam, sam_scoring, 301.0),   # SAM beam-only duration (> 240s gate)
        (mch, mch_scoring, 131.0),   # MCH beam-only duration (> 45s gate)
    ]
    for simmod, scoremod, dur in cases:
        ref = _timeline(simmod.simulate_idealized_perfect, dur)
        scoring.set_sim_pool(_RoundTripPool())
        try:
            got = scoremod._FNS.perfect_sim_timeline(dur, [])
        finally:
            scoring.set_sim_pool(None)
        assert _rotation(got) == _rotation(ref), \
            f"pooled != in-process for {scoremod.__name__} @ {dur}s"


@pytest.mark.slow
def test_prime_warms_cache():
    """`prime` runs ONE parallel batch (run_many), and the subsequent reads are served
    from the cache (no further dispatch) and match the direct sim."""
    specs = [(142.0, [], None, None), (143.0, [], None, None)]
    pool = _RoundTripPool()
    scoring.set_sim_pool(pool)
    try:
        pool.reset()
        sam_scoring._FNS.prime(specs)
        assert pool.run_calls == 0
        assert pool.run_many_calls == 1
        assert pool.run_many_items == 2
        # Reads are now cache hits — zero additional dispatch — and correct.
        for dur in (142.0, 143.0):
            got = sam_scoring._FNS.perfect_sim_timeline(dur, [])
            assert _rotation(got) == _rotation(
                _timeline(sam.simulate_idealized_perfect, dur))
        assert pool.run_calls == 0
        assert pool.run_many_calls == 1
    finally:
        scoring.set_sim_pool(None)


@pytest.mark.slow
def test_real_process_pool_equivalence():
    """A real ProcessPoolExecutor returns byte-identical results. Safe even where the
    pool can't start: SimPool falls back to in-process, so the equivalence still holds."""
    dur = 144.0
    ref = _timeline(sam.simulate_idealized_perfect, dur)
    pool = sim_pool.SimPool(max_workers=2)
    scoring.set_sim_pool(pool)
    try:
        got = sam_scoring._FNS.perfect_sim_timeline(dur, [])
    finally:
        scoring.set_sim_pool(None)
        pool.shutdown()
    assert _rotation(got) == _rotation(ref)


def main() -> None:
    test_dispatch_byte_identical()
    test_prime_warms_cache()
    test_real_process_pool_equivalence()
    print("test_sim_pool OK")


if __name__ == "__main__":
    main()
