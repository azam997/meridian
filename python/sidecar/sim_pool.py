"""Process-pool acceleration for the GIL-bound idealized-rotation simulator.

The ceiling sim — the beam search + the exact DP+B&B in each job's
`simulate_idealized_perfect` — is pure Python, so the threaded ref-warm only ever
overlaps the *network* half: the CPU half runs effectively serially under the GIL.
This module runs those perfect-sims in worker **processes**, behind the scoring cache
(`jobs._core.sim.scoring`), so the CPU work actually parallelizes across cores.

The sim is deterministic (RNG-free; the time-boxed DP returns its provable optimum
within the box), so pooled output is **byte-identical** to in-process — this is a pure
latency win, not a quality trade-off. See `scripts/bench_sim_pool.py` (≈4.3–4.5x on the
heavy jobs, byte-identical on both the DP and beam paths).

Wiring: `sidecar/main.py` calls `install()` once at startup, which hands a `SimPool` to
`scoring.set_sim_pool()`. Until installed, `scoring._SIM_POOL` is `None` and every sim
runs in-process — so the test suite and the mock path stay byte-identical. The pool is
built lazily on first use (not at handshake), and any pool failure degrades to
in-process compute, so a broken pool (e.g. a frozen-exe edge) can never break a run.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# Make the analyzer root importable inside spawned workers (mirrors main.py's ROOT).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _watch_parent_and_exit() -> None:
    """Self-terminate when the spawning sidecar dies. Workers stuck idle on the
    call-queue pipe do NOT reliably exit when the parent is killed un-gracefully
    (Windows: a sibling's inherited write handle keeps the pipe alive), so an
    app-close would strand a full worker brood per session. Wait on the parent's
    process handle and exit hard when it signals."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        ph = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, os.getppid())
        if not ph:
            return
        ctypes.windll.kernel32.WaitForSingleObject(ph, INFINITE)
        os._exit(0)
    except Exception:
        pass


def _worker_init() -> None:
    """Pre-import every supported job's package in each worker so the first real sim
    isn't paying spawn-import cost (warms each worker's own simulator-module caches),
    and arm the parent-death watchdog so killed sidecars don't strand workers.
    Best-effort: a failure here just means the first task imports lazily."""
    try:
        threading.Thread(target=_watch_parent_and_exit, daemon=True,
                         name="parent-watchdog").start()
    except Exception:
        pass
    try:
        from jobs import ALL_JOBS, get_job, is_supported
        for j in ALL_JOBS:
            if is_supported(j):
                try:
                    get_job(j)
                except Exception:
                    pass
    except Exception:
        pass


def _sim_worker(module_name: str, fn_name: str, args: tuple, kwargs: dict):
    """Worker entrypoint: import the simulator module by name, call the module-level
    sim fn. All args (floats, tuple-of-tuples, hashable sim_context dataclasses) and
    the `(timeline, aux)` return are picklable, so this round-trips unchanged."""
    mod = importlib.import_module(module_name)
    return getattr(mod, fn_name)(*args, **(kwargs or {}))


def _default_workers() -> int:
    """Leave a core for the webview / main process; cap so we don't fork the whole
    machine on a big box (each worker re-imports the analyzer)."""
    cpu = os.cpu_count() or 2
    if cpu <= 1:
        return 1
    return min(cpu - 1, 8)


# The canary sim run at pool creation: tiny (worker spawn + init dominate), but a
# real end-to-end task through the exact dispatch path.
_CANARY_CALL = ("jobs.samurai.simulator", "simulate_idealized_perfect", (10.0, []), {})


class SimPool:
    """A health-gated process pool with a transparent in-process fallback.

    `run` / `run_many` never raise on pool trouble — they degrade to computing in this
    process (byte-identical, just serial), so a broken pool can never break analysis.

    The executor is created ONCE, health-gated by a canary task with a bounded wait,
    and every result wait carries a generous safety timeout. This shape is
    load-bearing on Windows: creating the executor lazily under concurrent threaded
    submits (the refs fan-out mid-analysis) can wedge worker spawn-bootstrap
    (observed on CPython 3.14 — workers alive but frameless, tasks never picked up),
    and an unbounded `fut.result()` then hangs the analysis forever with the fallback
    never engaging. The canary catches a wedged pool in bounded time and degrades;
    `install()` prestarts the pool from the still-single-threaded sidecar startup so
    the executor is never born under threaded fire in the first place."""

    PRESTART_CANARY_TIMEOUT_S = 30.0   # spawn + worker init + tiny sim, with margin
    RESULT_SAFETY_TIMEOUT_S = 900.0    # forever-hang guard; >> any legitimate sim

    def __init__(self, max_workers: int | None = None):
        self._max_workers = max_workers or _default_workers()
        self._ex: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self._broken = False
        self._started = False
        self._ready = threading.Event()

    @property
    def max_workers(self) -> int:
        return self._max_workers

    # --- lifecycle ---------------------------------------------------------

    def prestart_async(self) -> None:
        """Kick off executor creation + the canary health check on a background
        thread. Called from sidecar startup (single-threaded, quiet) so the pool is
        warm — or known-broken — before the first analysis dispatches to it."""
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._create_and_canary, daemon=True,
                         name="sim-pool-prestart").start()

    def _create_and_canary(self) -> None:
        """Create the executor and prove it alive with one tiny end-to-end task.
        Any failure (including a bootstrap wedge, seen as a canary timeout) marks
        the pool broken and reaps the workers — analysis then runs in-process."""
        ex = None
        try:
            ex = ProcessPoolExecutor(max_workers=self._max_workers,
                                     initializer=_worker_init)
            fut = ex.submit(_sim_worker, *_CANARY_CALL)
            fut.result(timeout=self.PRESTART_CANARY_TIMEOUT_S)
            self._ex = ex
        except Exception:
            self._broken = True
            print("sim_pool: prestart canary failed -> in-process compute\n"
                  + traceback.format_exc(limit=3), file=sys.stderr, flush=True)
            self._reap(ex)
        finally:
            self._ready.set()

    @staticmethod
    def _reap(ex: ProcessPoolExecutor | None) -> None:
        """Best-effort teardown of a broken executor, including terminating spawn
        workers stuck in bootstrap (shutdown alone leaves them orphaned)."""
        if ex is None:
            return
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            for p in list(getattr(ex, "_processes", {}).values() or []):
                p.terminate()
        except Exception:
            pass

    def _break_pool(self) -> None:
        with self._lock:
            if self._broken:
                return
            self._broken = True
            ex, self._ex = self._ex, None
        print("sim_pool: pool failure/timeout -> in-process compute from here on",
              file=sys.stderr, flush=True)
        self._reap(ex)

    def _executor(self) -> ProcessPoolExecutor | None:
        if self._broken:
            return None
        if self._ex is not None and self._ready.is_set():
            return self._ex
        # First user (when prestart wasn't called, e.g. direct SimPool uses in
        # scripts/benchmarks) creates it; concurrent callers wait for the verdict.
        with self._lock:
            start_needed = not self._started
            if start_needed:
                self._started = True
        if start_needed:
            self._create_and_canary()
        else:
            self._ready.wait(timeout=self.PRESTART_CANARY_TIMEOUT_S + 15.0)
        if self._broken or not self._ready.is_set():
            return None
        return self._ex

    # --- dispatch ----------------------------------------------------------

    def run(self, module_name: str, fn_name: str, args=(), kwargs=None):
        """One sim, in a worker (or in-process on fallback). Blocking, bounded."""
        ex = self._executor()
        if ex is not None:
            try:
                fut = ex.submit(_sim_worker, module_name, fn_name, args, kwargs)
                return fut.result(timeout=self.RESULT_SAFETY_TIMEOUT_S)
            except Exception:
                self._break_pool()
        return _sim_worker(module_name, fn_name, args, kwargs)

    def run_many(self, calls):
        """`calls`: iterable of `(module_name, fn_name, args, kwargs)`. Submits all at
        once and returns results in order — the parallel fan-out for a batch of
        independent sims. In-process serial on fallback (the whole batch is
        recomputed: results are cached upstream, so simplicity beats salvage)."""
        calls = list(calls)
        ex = self._executor()
        if ex is not None:
            try:
                futs = [ex.submit(_sim_worker, m, f, a, k) for (m, f, a, k) in calls]
                return [fut.result(timeout=self.RESULT_SAFETY_TIMEOUT_S)
                        for fut in futs]
            except Exception:
                self._break_pool()
        return [_sim_worker(m, f, a, k) for (m, f, a, k) in calls]

    def shutdown(self) -> None:
        with self._lock:
            ex, self._ex = self._ex, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def drain(self) -> None:
        """Synchronously tear the pool down and WAIT for the worker processes to
        exit, releasing the onedir `_internal/*.dll` handles they map. Used right
        before an app update: `shutdown()` alone returns without proving the
        workers are gone, and the parent-death watchdog fires asynchronously — so
        the NSIS installer would race live grandchild processes and fail every
        locked file ("error opening file for writing"). After this the pool is
        marked broken, so any late sim runs in-process."""
        with self._lock:
            ex, self._ex = self._ex, None
            self._broken = True
        if ex is None:
            return
        procs = list(getattr(ex, "_processes", {}).values() or [])
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        # Force-terminate then join — shutdown(wait=False) doesn't guarantee the
        # workers have exited, and a bootstrap-wedged worker won't drain on its
        # own. Joining is what makes the DLL-handle release observable to NSIS.
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.join(timeout=3.0)
            except Exception:
                pass


def build_pool() -> SimPool | None:
    """Construct the pool unless disabled. `SIDECAR_SIM_WORKERS=0` disables it entirely
    (fully in-process); a positive int caps the worker count; unset → auto."""
    raw = os.environ.get("SIDECAR_SIM_WORKERS", "").strip()
    if raw == "0":
        return None
    workers: int | None = None
    if raw:
        try:
            workers = max(1, int(raw))
        except ValueError:
            workers = None
    return SimPool(max_workers=workers)


_INSTALLED: SimPool | None = None


def install() -> SimPool | None:
    """Build the pool (honoring the env flag) and hand it to the scoring cache. Idempotent;
    returns the installed pool (or None when disabled). Called once from main.py."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    pool = build_pool()
    if pool is not None:
        from jobs._core.sim import scoring
        scoring.set_sim_pool(pool)
        _INSTALLED = pool
    return _INSTALLED


def shutdown() -> None:
    global _INSTALLED
    if _INSTALLED is not None:
        _INSTALLED.shutdown()
        _INSTALLED = None


def drain() -> None:
    """Synchronously drain the installed pool's worker processes (see
    `SimPool.drain`). No-op if the pool was never installed / already down.
    Called from the `prepare_update` request just before the updater installs."""
    p = _INSTALLED
    if p is not None:
        p.drain()
