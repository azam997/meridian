"""Phase 0+1 spike: measure the idealized-sim cost and prove a process pool is a
byte-identical, faster way to run it.

The analyzer's ceiling sim is pure-Python (GIL-bound). The 10-ref warm fans out over
threads, so its CPU half runs effectively serially. This script:

  1. Times in-process `simulate_idealized_perfect` for the heavy jobs (SAM/MCH).
  2. Dispatches the SAME calls through a ProcessPoolExecutor (the exact worker shape
     planned for sidecar/sim_pool.py) and asserts the output is byte-identical
     (picklability + determinism, incl. the time-boxed DP at a short duration).
  3. Compares serial-in-process vs pooled fan-out across N distinct-duration solves
     (mimicking the 10-ref batch) and reports the speedup.

Run from python/:
    python scripts/bench_sim_pool.py            # SAM + MCH, defaults
    python scripts/bench_sim_pool.py Samurai 10 420
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# job display name -> (simulator module, perfect-sim fn)
_JOBS = {
    "Samurai":   ("jobs.samurai.simulator", "simulate_idealized_perfect"),
    "Machinist": ("jobs.machinist.simulator", "simulate_idealized_perfect"),
}


def _sim_worker(module_name: str, fn_name: str, args: tuple, kwargs: dict):
    """The worker entrypoint — identical in shape to the planned sim_pool._sim_worker:
    import the simulator module by name, resolve the module-level sim fn, call it.
    Returns the (timeline, aux) tuple, which pickles trivially."""
    mod = importlib.import_module(module_name)
    return getattr(mod, fn_name)(*args, **kwargs)


def _pool_init() -> None:
    """Pre-import every job's simulator module so the first real task isn't paying
    spawn-import cost (mirrors the real pool initializer)."""
    for module_name, _fn in _JOBS.values():
        importlib.import_module(module_name)


def _durations(n: int, base: float) -> list[float]:
    """N distinct durations (distinct perfect-sim cache keys), spaced 5s apart so each
    is genuine work — mimics 10 reference kills at slightly different times."""
    return [round(base + 5.0 * i, 1) for i in range(n)]


def _run_inprocess(module_name: str, fn_name: str, durs: list[float]):
    mod = importlib.import_module(module_name)
    fn = getattr(mod, fn_name)
    out = []
    t0 = time.perf_counter()
    for d in durs:
        out.append(fn(d, []))
    return out, time.perf_counter() - t0


def _run_pooled(pool: ProcessPoolExecutor, module_name: str, fn_name: str,
                durs: list[float]):
    t0 = time.perf_counter()
    futs = [pool.submit(_sim_worker, module_name, fn_name, (d, []), {}) for d in durs]
    out = [f.result() for f in futs]
    return out, time.perf_counter() - t0


def _equal(a, b) -> bool:
    """Byte-identical (timeline, aux) compare, tolerant of list/tuple after pickle."""
    (tl_a, aux_a), (tl_b, aux_b) = a, b
    return aux_a == aux_b and [tuple(x) for x in tl_a] == [tuple(x) for x in tl_b]


def main() -> None:
    argv = sys.argv[1:]
    jobs = [argv[0]] if argv and argv[0] in _JOBS else list(_JOBS)
    n = int(argv[1]) if len(argv) > 1 else 8
    base = float(argv[2]) if len(argv) > 2 else 420.0

    cpu = os.cpu_count() or 2
    workers = min(cpu - 1, 8) if cpu > 1 else 1
    print(f"cpu_count={cpu}  pool_workers={workers}  start_method={mp.get_start_method()}")

    pool = ProcessPoolExecutor(max_workers=workers, initializer=_pool_init)
    try:
        # Warm the pool (spawn + per-worker imports) so the batch timing is steady-state.
        list(pool.map(int, range(workers)))

        for job in jobs:
            module_name, fn_name = _JOBS[job]
            print(f"\n=== {job} ({module_name}.{fn_name}) ===")

            # --- Equivalence: a short duration (exercises the time-boxed DP for SAM/
            # MCH, which PROVES within the box at this length -> deterministic) and a
            # long one (beam-only at realistic kill length).
            for d in (150.0, base):
                local = importlib.import_module(module_name)
                ref = getattr(local, fn_name)(d, [])
                remote = pool.submit(_sim_worker, module_name, fn_name, (d, []), {}).result()
                ok = _equal(ref, remote)
                print(f"  equivalence @ {d:.0f}s: {'OK (byte-identical)' if ok else 'MISMATCH!'}"
                      f"  (casts={len(ref[0])})")
                if not ok:
                    raise SystemExit(f"NON-DETERMINISTIC: {job} @ {d:.0f}s differs pooled vs in-process")

            # --- Throughput: N distinct-duration solves, serial vs pooled.
            durs = _durations(n, base)
            ser_out, ser_t = _run_inprocess(module_name, fn_name, durs)
            par_out, par_t = _run_pooled(pool, module_name, fn_name, durs)
            assert all(_equal(a, b) for a, b in zip(ser_out, par_out)), "batch mismatch"
            speedup = ser_t / par_t if par_t > 0 else float("inf")
            print(f"  batch n={n} durs={durs[0]:.0f}-{durs[-1]:.0f}s:")
            print(f"    serial  (in-process): {ser_t:6.2f}s  ({ser_t / n:5.2f}s/solve)")
            print(f"    pooled  ({workers:>2}w proc):   {par_t:6.2f}s  ({par_t / n:5.2f}s/solve)")
            print(f"    speedup: {speedup:.2f}x")
    finally:
        pool.shutdown(wait=True)


if __name__ == "__main__":
    mp.freeze_support()   # the call the frozen sidecar will also need
    main()
