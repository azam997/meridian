"""Shared scoring scaffolding for sim-backed jobs.

The *scoring math* is per-job (MCH bakes in Reassemble crit-DH, the Wildfire
payload and the linear battery->potency pet conversion; RPR scales everything by
its maintained Death's Design amp). But the scaffolding around it is identical:

  * the LRU-cached perfect-sim run + scored ceiling (`build_scoring`),
  * the `IdealizedSimulator` wrapper the Timeline lanes use (`make_simulator`),
  * the `Scoring` aspect's whole `analyze` flow (`ScoringAspectBase`).

This module owns all three, parameterized by a small per-job adapter:

  * `score_timeline(timeline, aux, coverage_intervals, buff_intervals) -> float`
    — the job's uniform scoring entry. `aux` is the sim's opaque scalar (MCH =
    Queen battery; everyone else = 0). `coverage_intervals` is a job-wide
    multiplier overlay the *idealized* side assumes full (RPR Death's Design);
    `None` for jobs without one (MCH). `buff_intervals` is the raid-buff overlay.
  * `coverage_intervals(duration) -> list | None` — the full-coverage overlay for
    the idealized ceiling (RPR: full DD; MCH: None).

Each job calls `build_scoring(...)` once at import, getting closures with their
own caches — no cross-job cache-key collisions.

**Per-pull sim context.** Most jobs' idealized ceiling is pure
`(duration, downtime, buffs)` data, reused across the player and the warm-cache.
A caster like RDM needs one more axis: a per-pull scalar fed into the rotation
(RDM's proc budget — the count of Verfire/Verstone the player actually got, so
the ceiling spends the *same* number of procs and bad luck never costs
efficiency). That scalar threads through as the optional, hashable `sim_context`:
it joins the cache key and is forwarded to the job's `simulate_*` functions when
not `None`. Jobs that don't use it (MCH/RPR) pass `None` throughout — their
`simulate_*` are never called with the kwarg, so they stay byte-identical.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.downtime import read_downtime_from_report
from jobs._core.job import SimResult


# Optional process pool for the perfect-sim (the GIL-bound beam/DP ceiling). Installed
# by sidecar/sim_pool.py at runtime; `None` by default so tests and the mock path run
# every sim in-process, byte-identical. The pool runs the SAME deterministic sim in a
# worker process, so its output matches in-process exactly — see sidecar/sim_pool.py.
_SIM_POOL: Any = None


def set_sim_pool(pool: Any) -> None:
    """Install (or clear, with `None`) the process pool the perfect-sim cache dispatches
    misses to. Read at call time by every job's cache closures, so a single call here
    flips all jobs onto the pool at once."""
    global _SIM_POOL
    _SIM_POOL = pool


# Uniform per-job scoring signature. The trailing `target_intervals` is the
# multi-target `N(t)` schedule (`((start, end, n), ...)`); `None`/absent ->
# single target, byte-identical. Each cast is valued per-target via
# `jobs._core.sim.aoe_potency.potency_for` inside the job's scorer.
ScoreTimeline = Callable[
    [list[tuple[float, int]], int,
     Optional[list[tuple[float, float, float]]],
     Optional[list[tuple[float, float, float]]],
     Optional[tuple[tuple[float, float, int], ...]]], float]
CoverageFn = Callable[[float], Optional[list[tuple[float, float, float]]]]


@dataclass
class ScoringFns:
    """The per-job scoring closures (each with its own LRU cache). Re-exported
    by the job's scoring module under the names the sidecar / tests expect."""
    sim_cache_keys: Callable[..., tuple]
    perfect_sim_cached: Callable[..., tuple]
    idealized_at_duration: Callable[..., float]
    perfect_sim_timeline: Callable[..., list]
    enabler_net_values: Callable[..., dict]
    # Warm the perfect-sim cache for a batch of independent (duration, downtime, buffs,
    # ctx) specs IN PARALLEL (via the process pool), so a following sequential `max(...)`
    # over `idealized_at_duration` is all cache hits. No-op without a pool installed.
    prime: Callable[..., None]


def build_scoring(*, sim_module: Any, score_timeline: ScoreTimeline,
                  enabler_ids: tuple[int, ...],
                  coverage_intervals: CoverageFn | None = None) -> ScoringFns:
    """Build the cache scaffolding for one job. `sim_module` is the job's
    simulator module (`simulate_idealized`, `simulate_idealized_perfect`,
    `SimParams`). The job's tincture is placed INSIDE the sim (the model's
    `tincture_spec` → in-timeline pot markers) and scored at cast time by the job's
    own `score_timeline` (via `tincture.merge_tincture_markers`), so there is no
    placement sweep here — the cached perfect timeline already carries the optimally
    -placed pot, and the ceiling is ≥ delivered by construction (no >100% guard)."""
    simulate_idealized = sim_module.simulate_idealized
    simulate_perfect = sim_module.simulate_idealized_perfect
    SimParams = sim_module.SimParams
    perfect_module = sim_module.__name__   # for the pool worker (import-by-name)

    def _perfect_kwargs(buff_intervals, sim_context) -> dict:
        """The kwargs for ONE `simulate_idealized_perfect` call — used by BOTH the
        in-process and the pooled path so the two are byte-identical. `sim_context` is
        forwarded only when set (jobs that don't use it stay called unchanged)."""
        kw = {"buff_intervals": buff_intervals}
        if sim_context is not None:
            kw["sim_context"] = sim_context
        return kw

    def _run_perfect(duration, downtime, buff_intervals, sim_context):
        """Run the perfect-sim for one cache key — in a worker PROCESS when a pool is
        installed (the GIL-bound beam/DP runs off the main thread), else in-process.
        Deterministic, so pooled == in-process byte-for-byte (see sidecar/sim_pool.py)."""
        kw = _perfect_kwargs(buff_intervals, sim_context)
        pool = _SIM_POOL
        if pool is not None:
            return pool.run(perfect_module, "simulate_idealized_perfect",
                            (duration, downtime), kw)
        return simulate_perfect(duration, downtime, **kw)

    def _run_idealized(duration, downtime, params, sim_context):
        kw: dict = {}
        if params is not None:
            kw["params"] = params
        if sim_context is not None:
            kw["sim_context"] = sim_context
        return simulate_idealized(duration, downtime, **kw)

    def sim_cache_keys(duration_s: float,
                       downtime_windows: list[tuple[float, float]],
                       buff_intervals: list[tuple[float, float, float]] | None,
                       sim_context: Any = None,
                       ) -> tuple:
        """Rounded cache keys shared by every perfect-sim consumer so their
        calls collapse onto the same cached run (score + timeline). `sim_context`
        (RDM proc budget; `None` otherwise) is the trailing per-pull axis."""
        return (
            round(duration_s, 1),
            tuple((round(s, 1), round(e, 1)) for s, e in downtime_windows),
            tuple((round(s, 1), round(e, 1), round(m, 4))
                  for s, e, m in (buff_intervals or [])),
            sim_context,
        )

    # Manual LRU (vs @lru_cache) so `prime_perfect` can fill it from a parallel batch and
    # the pool dispatch lives in `_run_perfect`. Per-job (closure); lock-guarded because
    # the ref-warm fans this across threads.
    _pcache: "OrderedDict[tuple, tuple]" = OrderedDict()
    _pcache_lock = threading.Lock()
    _PCACHE_MAX = 256

    def _normalize(result):
        timeline, aux = result
        return tuple(timeline), aux

    # Universal tincture placement: the job's spec is read off its sim module, and the
    # COVERAGE-OPTIMAL pots (shared `tincture.place_optimal_pots`, gap-constrained
    # max-coverage DP) are placed over every finished perfect-sim timeline, per
    # scenario — superseding the sim's greedy in-rotation `should_pot` markers. Applied
    # in BOTH `perfect_sim_cached` (cache miss) and `prime_perfect` (the parallel pool
    # fan-out) so a cached entry is identical whichever path filled it. No-op for a job
    # that doesn't pot (`_TINCTURE_SPEC` None / absent).
    tincture_spec = getattr(sim_module, "_TINCTURE_SPEC", None)

    def _target_spec(sim_context):
        """The scorer's target axis off a `sim_context`: the bare schedule when
        uncapped (byte-identical to the pre-cap scorer), a `TargetSpec` when the
        observed-reach ability caps ride the `MultiTargetContext`, `None` on a
        single-target pull. Passed as the trailing `score_timeline` arg — every
        job adapter feeds it to `schedule_target_fn`, which handles both."""
        from jobs._core.downtime_sources import (caps_from_context,
                                                 schedule_from_context)
        schedule = schedule_from_context(sim_context)
        if not schedule:
            return None
        caps = caps_from_context(sim_context)
        if not caps:
            return schedule
        from jobs._core.sim.aoe_potency import TargetSpec
        return TargetSpec(schedule=schedule, ability_caps=caps)

    def _finalize(keys, raw):
        timeline, aux = _normalize(raw)
        if tincture_spec is None or tincture_spec.multiplier <= 1.0:
            return timeline, aux
        from jobs._core.tincture import place_optimal_pots
        duration_key, _dt, buff_tuple, sim_context = keys
        cov = coverage_intervals(duration_key) if coverage_intervals else None
        spec = _target_spec(sim_context)
        buff_iv = list(buff_tuple) or None

        def _score(tl):
            return score_timeline(list(tl), aux, cov, buff_iv, spec)

        opt = place_optimal_pots(
            list(timeline), duration_key, tincture_spec, buff_iv, _score)
        return tuple(opt), aux

    def perfect_sim_cached(duration_key: float,
                           downtime_tuple: tuple[tuple[float, float], ...],
                           buff_tuple: tuple[tuple[float, float, float], ...],
                           sim_context: Any = None,
                           ) -> tuple[tuple[tuple[float, int], ...], int]:
        """LRU-cached perfect-sim RUN -> (timeline, aux). The single source for
        both the scored ceiling and the Timeline's idealized lane, so a given
        (duration, downtime, buffs, sim_context) is simulated exactly once. A miss
        dispatches to the process pool when one is installed (`_run_perfect`)."""
        keys = (duration_key, downtime_tuple, buff_tuple, sim_context)
        with _pcache_lock:
            hit = _pcache.get(keys)
            if hit is not None:
                _pcache.move_to_end(keys)
                return hit
        buff_intervals = list(buff_tuple) or None
        val = _finalize(keys, _run_perfect(
            duration_key, list(downtime_tuple), buff_intervals, sim_context))
        with _pcache_lock:
            _pcache[keys] = val
            _pcache.move_to_end(keys)
            while len(_pcache) > _PCACHE_MAX:
                _pcache.popitem(last=False)
        return val

    def prime_perfect(specs) -> None:
        """Warm `_pcache` for several independent specs IN PARALLEL via the pool, so a
        following sequential `max(idealized_at_duration(...))` is all cache hits. `specs`:
        iterable of (duration_s, downtime_windows, buff_intervals, sim_context). No-op
        without a pool — the sequential path then computes in-process, unchanged. Keys
        are computed via `sim_cache_keys`, so the warmed entries match the later reads."""
        pool = _SIM_POOL
        if pool is None:
            return
        todo: list = []
        todo_keys: list = []
        seen: set = set()
        for duration_s, downtime_windows, buff_intervals, sim_context in specs:
            keys = sim_cache_keys(duration_s, downtime_windows, buff_intervals, sim_context)
            if keys in seen:
                continue
            seen.add(keys)
            with _pcache_lock:
                if keys in _pcache:
                    continue
            buff_iv = list(keys[2]) or None
            todo.append((perfect_module, "simulate_idealized_perfect",
                         (keys[0], list(keys[1])), _perfect_kwargs(buff_iv, keys[3])))
            todo_keys.append(keys)
        if not todo:
            return
        results = pool.run_many(todo)
        with _pcache_lock:
            for keys, res in zip(todo_keys, results):
                _pcache[keys] = _finalize(keys, res)
                _pcache.move_to_end(keys)
            while len(_pcache) > _PCACHE_MAX:
                _pcache.popitem(last=False)

    def idealized_at_duration(duration_s: float,
                              downtime_windows: list[tuple[float, float]],
                              buff_intervals: list[tuple[float, float, float]]
                              | None = None,
                              sim_context: Any = None) -> float:
        """Perfect-sim ceiling for a fight duration. The tincture is placed INSIDE the
        sim (the model's in-timeline pot marker), so this simply scores the cached
        perfect timeline: coverage overlay × raid buffs × the in-sim tincture (folded
        in at cast time by the job's `score_timeline`, which calls
        `tincture.merge_tincture_markers`). `buff_intervals` (optional) is the raid-buff
        overlay. Normalizing by duration keeps efficiency comparisons
        (delivered / idealized-at-same-duration) apples-to-apples. The ceiling is ≥
        delivered by construction — the optimizer placed the pot — so there is no
        post-hoc sweep and no tincture-factor guard."""
        keys = sim_cache_keys(duration_s, downtime_windows, buff_intervals, sim_context)
        timeline, aux = perfect_sim_cached(*keys)
        cov = coverage_intervals(keys[0]) if coverage_intervals else None
        # The multi-target N(t) schedule (+ the observed-reach ability caps)
        # rides `sim_context` (already part of the cache key, since it's
        # hashable); peel it for the per-target scoring. `None` on a
        # single-target pull -> byte-identical.
        return score_timeline(
            list(timeline), aux, cov, list(keys[2]) or None,
            _target_spec(sim_context))

    def perfect_sim_timeline(duration_s: float,
                             downtime_windows: list[tuple[float, float]],
                             buff_intervals: list[tuple[float, float, float]]
                             | None = None,
                             sim_context: Any = None) -> list[tuple[float, int]]:
        """The cached perfect-sim timeline (Timeline lanes + missed-cast diff), with the
        in-sim tincture pot markers placed. The sidecar derives the idealized pot band
        from the markers and filters them out of the rendered cast lane / the diff."""
        return list(perfect_sim_cached(
            *sim_cache_keys(duration_s, downtime_windows, buff_intervals, sim_context))[0])

    @lru_cache(maxsize=128)
    def enabler_net_values_cached(
            duration_key: float,
            downtime_tuple: tuple[tuple[float, float], ...],
            sim_context: Any = None,
            ) -> tuple[tuple[int, float], ...]:
        """Per-cast *net* potency of each throughput enabler: score the idealized
        rotation, then re-score with the enabler forbidden for the whole fight;
        the drop / cast-count is the average marginal value of one cast. This
        captures the real exchange (the GCDs / resources the rotation gets back
        when the enabler is gone). Buff-agnostic, coverage-agnostic (raw potency)
        — the improvements panel's reconcile_to_budget bounds the priced total to
        the measured gap regardless. Single-point sims so the with/without
        subtraction is apples-to-apples and cheap."""
        downtime = list(downtime_tuple)
        base_tl, base_aux = _run_idealized(duration_key, downtime, None, sim_context)
        base_score = score_timeline(base_tl, base_aux, None, None)
        out: list[tuple[int, float]] = []
        for aid in enabler_ids:
            n = sum(1 for t, a in base_tl if a == aid and t >= 0)
            if n == 0:
                continue
            params = SimParams(forbidden_windows=((aid, 0.0, float(duration_key)),))
            tl, aux = _run_idealized(duration_key, downtime, params, sim_context)
            marginal = (base_score - score_timeline(tl, aux, None, None)) / n
            out.append((aid, max(0.0, marginal)))
        return tuple(out)

    def enabler_net_values(duration_s: float,
                           downtime_windows: list[tuple[float, float]],
                           sim_context: Any = None,
                           ) -> dict[int, float]:
        """{enabler_id: net potency per missed cast}."""
        return dict(enabler_net_values_cached(
            round(duration_s, 1),
            tuple((round(s, 1), round(e, 1)) for s, e in downtime_windows),
            sim_context,
        ))

    return ScoringFns(
        sim_cache_keys=sim_cache_keys,
        perfect_sim_cached=perfect_sim_cached,
        idealized_at_duration=idealized_at_duration,
        perfect_sim_timeline=perfect_sim_timeline,
        enabler_net_values=enabler_net_values,
        prime=prime_perfect,
    )


def make_simulator(fns: ScoringFns, *, score_timeline: ScoreTimeline,
                   canonical_fn: Callable[..., tuple],
                   coverage_intervals: CoverageFn | None = None):
    """Wrap the perfect-sim closures in the `IdealizedSimulator` interface
    (`.simulate` + `.simulate_canonical`), routing through the scoring cache so
    a given (duration, downtime, buffs, sim_context) is simulated once.
    `delivered_potency` is the idealized ceiling scored with full coverage.

    `sim_context` is the optional per-pull scalar (RDM proc budget). The sidecar
    passes the run's own value (stashed on its Scoring state); `None` for jobs
    that don't use it, leaving their sims byte-identical."""

    import importlib
    # Same universal tincture placement as build_scoring, for the canonical lane (its
    # own sim path, not the perfect-sim cache). Spec read off the canonical fn's module.
    _tincture_spec = getattr(
        importlib.import_module(canonical_fn.__module__), "_TINCTURE_SPEC", None)

    def _cov(duration_s: float):
        return coverage_intervals(round(duration_s, 1)) if coverage_intervals else None

    class _Simulator:
        def prime(self, specs) -> None:
            """Warm the perfect-sim cache for a batch of independent specs IN PARALLEL
            (no-op without a pool). `specs`: iterable of (duration_s, downtime,
            buff_intervals, sim_context). Lets the sidecar fan out the Tier-B lenient and
            multi-target sweeps before their sequential `max(...)`/credit loops."""
            fns.prime(specs)

        def simulate(self, duration_s, downtime, buff_intervals=None,
                     sim_context=None):
            # Tincture-aware ceiling: the pot is placed inside the sim, so the cached
            # perfect timeline already carries it and scoring credits it at cast time.
            bi = list(buff_intervals) if buff_intervals else None
            score = fns.idealized_at_duration(
                duration_s, list(downtime), bi, sim_context)
            timeline = fns.perfect_sim_timeline(
                duration_s, list(downtime), bi, sim_context)
            return SimResult(delivered_potency=score, timeline=tuple(timeline))

        def simulate_canonical(self, duration_s, downtime, buff_intervals=None,
                               sim_context=None):
            """The 'hold burst for the 2-min window' line — comparison lane only,
            not a ceiling."""
            bi = list(buff_intervals) if buff_intervals else None
            kw = {"buff_intervals": bi}
            if sim_context is not None:
                kw["sim_context"] = sim_context
            timeline, aux = canonical_fn(duration_s, list(downtime), **kw)
            if _tincture_spec is not None and _tincture_spec.multiplier > 1.0:
                from jobs._core.tincture import place_optimal_pots
                cov = _cov(duration_s)
                timeline = place_optimal_pots(
                    list(timeline), duration_s, _tincture_spec, bi,
                    lambda tl: score_timeline(list(tl), aux, cov, bi))
            potency = score_timeline(list(timeline), aux, _cov(duration_s), bi)
            return SimResult(delivered_potency=potency, timeline=tuple(timeline))

    return _Simulator()


class ScoringAspectBase:
    """The `Scoring` aspect's shared flow. Hidden from the per-aspect UI (no
    findings, no detail table) — exists so the sidecar can read scalars off
    `mr.aspects['Scoring'].state` with no job-specific knowledge. Emits the
    canonical state-key shape so the dashboard headline lights up unchanged.

    A subclass sets `fns` (from `build_scoring`) and overrides:
      * `prepare(...)` -> a per-pull context (MCH: Queen battery; RPR: measured
        DD intervals). Returned object is passed to the next three.
      * `score_delivered(ctx, in_fight_casts, buff_intervals)` -> the delivered
        score under the given raid-buff overlay.
      * `extra_state(ctx)` -> job-specific state keys (MCH: queen_battery_spent).
      * `sim_context(ctx)` -> the optional per-pull sim scalar (RDM proc budget)
        threaded into the idealized ceiling and stashed on state for the sidecar.
        Default `None` -> the ceiling is pure (duration, downtime, buffs) data.
    """

    name = "Scoring"
    fns: ScoringFns = None  # set by subclass
    # The job's tincture (a tincture.TinctureSpec), set by the subclass alongside
    # `fns`. None ⇒ the job doesn't pot (SAM) and the delivered path stays
    # tincture-free / byte-identical.
    tincture_spec: Optional[Any] = None
    # Pre-pull *channel* abilities (cast-time GCDs precast during the countdown so
    # they resolve at t≈0 — RDM Verthunder/Veraero III, RPR Harpe). Their potency
    # is real in-fight damage, so the single one nearest t=0 is credited to
    # delivered even though it lands at a negative (begincast-anchored) time,
    # matching the channel the sim emits in prepull. Empty (MCH/SAM) → the plain
    # t>=0 filter, byte-identical.
    prepull_channel_ids: frozenset[int] = frozenset()
    # The job's GCD constant (the effective GCD the sim assumes at this tier's typical
    # gear). Set by a GCD-bound subclass to enable per-player Skill/Spell-Speed: the
    # analyze flow infers the player's effective GCD from cast cadence and threads
    # `min(constant, inferred)` into the ceiling (only ever FASTER → monotonically safe;
    # equal-to-constant → not threaded → byte-identical). None ⇒ the job opts out.
    gcd_constant: Optional[float] = None
    # Opt in to the DEMONSTRATED-cadence sweep anchor (uptime / player-GCD-count): the
    # ceiling is also scored at the exact cadence that gives it the GCD budget the player
    # provably cast, catching a parse that sustains tighter server-tick queuing than the
    # fixed sub-GCD band floor reaches (the live GNB M12S-P1 >100%). ONLY valid for a
    # FLAT-GCD job — one whose ceiling has no modeled sub-GCD-speed window. A job with a
    # haste/Overheated window (MCH, BLM, RPR, RDM…) must leave this False: the count-based
    # cadence would fold those already-modeled fast GCDs in and double-credit them, wrongly
    # inflating the ceiling. Requires `gcd_constant`; no-op otherwise.
    demonstrated_cadence_anchor: bool = False

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts: list[tuple[float, int]]) -> Any:
        return None

    def score_delivered(self, ctx: Any, in_fight_casts: list[tuple[float, int]],
                        buff_intervals: list[tuple[float, float, float]]
                        | None = None) -> float:
        raise NotImplementedError

    def extra_state(self, ctx: Any) -> dict:
        return {}

    def sim_context(self, ctx: Any) -> Any:
        """Per-pull scalar fed into the job's rotation (RDM proc budget). `None`
        for jobs whose ceiling is pure (duration, downtime, buffs) data."""
        return None

    def gcd_inference_exclusions(
            self, norm_casts: list[tuple[float, int]]
    ) -> list[tuple[float, float]]:
        """Windows whose GCD pairs must be excluded from the per-player GEAR-GCD
        inference because a temporary self-haste BUFF (not gear Skill/Spell Speed)
        speeds them. Without this the inference reads the haste as gear and the
        ceiling double-counts it — it already models the haste window in
        `gcd_duration` (e.g. BLM Ley Lines hastes GCDs to ~2.1s, which falls inside
        the inference band, unlike MCH Overheated's sub-band 1.5s). Unioned with
        downtime for the inference ONLY; the ceiling still models the window. Empty
        by default (no in-band self-haste); a haste-window job overrides."""
        return []

    def _demonstrated_cadence(self, norm_casts, is_gcd, fight_duration_s,
                              downtime_windows, haste_windows):
        """The player's demonstrated sustained GCD cadence, fed as an extra sweep
        anchor when `demonstrated_cadence_anchor` is set. Defaults to the shared
        `uptime / GCD-count` (valid for a uniform-GCD job). A MIXED fixed-rate job
        (SGE, whose Eukrasia is a speed-immune ~1.0s GCD) overrides this to exclude
        its fixed fast-GCD time and count, so the anchor reflects the sustained
        NORMAL cadence rather than an average that folds the fast slots in."""
        from jobs._core.gcd_speed import demonstrated_cadence
        return demonstrated_cadence(
            norm_casts, is_gcd, fight_duration_s, downtime_windows, haste_windows)

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        from jobs._core.buff_windows import (
            expected_windows,
            fetch_observed_buff_windows,
            multiplier_intervals,
            party_jobs_in_fight,
        )

        norm_casts = fetch_norm_casts(client, code, fight, actor)
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        downtime_windows, downtime_source = read_downtime_from_report(
            report, norm_casts, fight_duration_s,
        )
        ctx = self.prepare(client, code, fight, actor, report, norm_casts)
        sim_ctx = self.sim_context(ctx)
        # Per-player Skill/Spell-Speed + sub-GCD timing. We can't read SkS/SpS from the
        # client-credentials API, so the player's GEAR GCD is deconvolved from cast
        # cadence (inferred cadence / sub-GCD tightness, snapped to the constant within
        # measurement noise — see gcd_speed.effective_gcd_for). The ceiling is scored
        # over a sub-GCD cadence BAND and the BEST (highest) ceiling kept — modelling the
        # server-tick/queue effect that lets a top parse fit a fractionally-faster
        # EFFECTIVE GCD than nominal recast (the proven cause of the live >100% on
        # nominal-GCD ceilings — pot/buff/oGCD/choice search are all already optimal).
        # When the gear inference threads a faster-than-constant GCD, the sweep also
        # carries the constant's calibrated band (executable on any faster gear), so the
        # maxed ceiling is >= the calibrated constant ceiling BY CONSTRUCTION — the
        # structural monotonicity that lets the inference run without a safety floor
        # despite the GCD search's non-monotonicity; efficiency can only fall, never
        # rise, from threading. The gear (nominal) context is authoritative for the
        # sidecar / Timeline recompute — the displayed rotation runs at the player's
        # gear; the band only lifts the SCORE.
        sweep_ctxs: list = [sim_ctx]
        if self.gcd_constant is not None:
            from jobs._core.ability_metadata import get_metadata
            from jobs._core.gcd_speed import (
                CeilingContext, demonstrated_cadence, effective_gcd_for,
                subgcd_gcd_sweep)

            def _is_gcd(aid: int) -> bool:
                m = get_metadata(aid)
                return m is not None and not m.is_ogcd
            # Exclude self-haste windows (BLM Ley Lines) from the GEAR inference so
            # their buff-hasted pairs aren't misread as fast gear (the ceiling models
            # the window separately — double-counting it inflates the ceiling).
            haste_excl = self.gcd_inference_exclusions(norm_casts)
            infer_excl = list(downtime_windows) + haste_excl
            gear = effective_gcd_for(
                norm_casts, _is_gcd, self.gcd_constant, infer_excl)
            cadences = list(subgcd_gcd_sweep(gear, self.gcd_constant))
            # Anchor the sweep at the player's DEMONSTRATED sustained cadence: they provably
            # cast this many GCDs over the uptime, so the ceiling must be allowed the same
            # GCD budget. The fixed sub-GCD band floor can fall short of a parse that sustains
            # tighter server-tick queuing than the clean-pair inference reads (the live GNB
            # M12S-P1 >100% — top parses cast ~162 GCDs = ~2.467s where the band floored at
            # 2.47s). Added only when it's FASTER than the band floor (else the band already
            # covers it), and clamped to a sane sub-GCD range so a miscount / uncaught-downtime
            # pull can't inflate the ceiling absurdly. Monotone-safe (max keeps the highest
            # ceiling → efficiency can only fall, never break 100%); byte-identical for a parse
            # that didn't out-pace the band. sweep_ctxs[0] stays the gear context (authoritative
            # for the displayed rotation) — the anchor only lifts the SCORE, like the rest of
            # the sub-GCD band.
            if self.demonstrated_cadence_anchor:
                dem = self._demonstrated_cadence(
                    norm_casts, _is_gcd, fight_duration_s, downtime_windows, haste_excl)
                if dem is not None:
                    dem = max(dem, self.gcd_constant * 0.95)   # floor: <=5% under gear
                    if dem < cadences[-1] - 1e-6:
                        cadences.append(dem)
            sweep_ctxs = [CeilingContext(gcd_base_s=g, payload=sim_ctx)
                          for g in cadences]
            sim_ctx = sweep_ctxs[0]   # the gear (nominal) context — authoritative downstream

        # Raid-buff windows for the two idealized scenarios:
        #  - observed: buffs that actually landed in this pull. Both delivered
        #    and idealized_observed use these — a fair, player-accountable ceiling.
        #  - expected: on-cadence buffs assuming the whole party played perfectly.
        #    idealized_master uses these — the "party-optimal" ceiling.
        observed_windows = fetch_observed_buff_windows(
            client, code, report, fight, actor["id"])
        observed_intervals = multiplier_intervals(observed_windows)
        expected_intervals = multiplier_intervals(
            expected_windows(fight_duration_s, party_jobs_in_fight(report, fight)))

        # The player's actual tincture windows (read from the same player Buffs
        # stream — a cache hit). Design A: the tincture is part of the rank pair,
        # so it rides the raid-agnostic `delivered` (against the optimally-placed
        # pot the idealized sweep adds to `idealized_strict`); `delivered_observed`
        # stacks it onto the observed raid buffs. Empty for non-potting jobs (SAM).
        tincture_windows: list = []
        if self.tincture_spec is not None and self.tincture_spec.multiplier > 1.0:
            from jobs._core.tincture import fetch_observed_tincture_windows
            tincture_windows = fetch_observed_tincture_windows(
                client, code, fight, actor["id"], self.tincture_spec.multiplier)
        tincture_intervals = multiplier_intervals(tincture_windows) or None
        observed_plus_tincture = multiplier_intervals(
            observed_windows + tincture_windows)

        # Score the in-fight timeline. Pre-pull casts are excluded so an opener's
        # pre-pull setup doesn't apply a bonus the real game credited pre-fight —
        # EXCEPT the single pre-pull *channel* (RDM 440 precast / RPR Harpe),
        # which deals real in-fight damage and is credited symmetrically with the
        # channel the sim emits. Keep only the one nearest t=0 (matching the one
        # the sim precasts), so a stray earlier channel cast can't double-count.
        prepull_ch_t = max(
            (t for t, aid in norm_casts
             if t < 0 and aid in self.prepull_channel_ids),
            default=None)
        in_fight_casts = [
            (t, aid) for t, aid in norm_casts
            if t >= 0 or (prepull_ch_t is not None and t == prepull_ch_t
                          and aid in self.prepull_channel_ids)]
        delivered = self.score_delivered(ctx, in_fight_casts, tincture_intervals)
        delivered_observed = self.score_delivered(
            ctx, in_fight_casts, observed_plus_tincture or None)

        # Per-phase delivered potency (phasic analysis) — the SAME delivered
        # scorer applied to the casts inside each boss phase, so a phase's number
        # is directly comparable to the top clears' per-phase delivered (the
        # sidecar aggregates refs → medians). Only on phased fights (the analyze
        # flow sets `report["__phases__"]` for ultimates); Savage never sets it,
        # so this whole block is skipped and the aspect stays byte-identical.
        # DoT/buff windows spanning a boundary are attributed to the cast's own
        # phase (a documented approximation; symmetric across subject and refs).
        phase_delivered: dict[int, float] = {}
        phase_segs = report.get("__phases__") or ()
        if phase_segs:
            from jobs._core.phases import split_casts_by_phase
            slices = split_casts_by_phase(in_fight_casts, phase_segs)
            for seg, sl in zip(phase_segs, slices):
                phase_delivered[seg.id] = self.score_delivered(
                    ctx, list(sl), tincture_intervals)

        # Tincture pot-*timing* loss for the Potential-Improvements card: how much
        # more the best pot placement would add on YOUR rotation vs the pots you
        # actually used (strict / raid-agnostic, matching the panel's budget).
        tincture_loss = 0.0
        tincture_optimal_count = 0
        tincture_loss_time_s = 0.0
        if self.tincture_spec is not None and self.tincture_spec.multiplier > 1.0:
            from jobs._core.tincture import tincture_timing_loss
            tincture_loss, tincture_optimal_count, tincture_loss_time_s = \
                tincture_timing_loss(
                    self.tincture_spec, fight_duration_s,
                    lambda iv: self.score_delivered(ctx, in_fight_casts, iv),
                    tincture_windows)

        # Strict idealized: Tier-A downtime, buff-agnostic. The lenient variant
        # (Tier A u Tier B consensus) is injected by the sidecar after refs.
        # Strict is the rank/headline ceiling (efficiencyPctStrict) and what the live
        # >100% shows up in, so it gets the full sub-GCD cadence sweep: the best (highest)
        # ceiling over the gear-GCD + tighter-cadence band. `max` with the gear context
        # (sweep_ctxs[0]) keeps it >= the nominal ceiling, so the band can only lift it
        # (efficiency only falls) — monotonic-safe. Observed/master are NOT the displayed
        # efficiency (and are raid-buffed → lower efficiency → far less >100% risk), so
        # they stay at the gear GCD to bound the per-pull analyze cost — the ref-warm fans
        # 10 refs out per (job, encounter), and a full 3-scenario sweep on the heaviest
        # job (SAM's width-256 beam) overran the warm's idle budget. The lenient ceiling
        # (sidecar `_inject_tier_b`) is swept there, so both displayed ceilings get it.
        # Warm every perfect-sim this pull needs — the strict sub-GCD sweep plus the
        # observed and master ceilings — in ONE parallel batch via the process pool, so
        # the assignments below are all cache hits instead of a serial GIL-bound chain.
        # No-op without a pool (each then computes in-process). This is also the per-ref
        # fan-out: every ref runs `analyze`, so the warm's ~5 sims/ref parallelize here.
        self.fns.prime(
            [(fight_duration_s, downtime_windows, None, c) for c in sweep_ctxs]
            + [(fight_duration_s, downtime_windows, observed_intervals, sim_ctx),
               (fight_duration_s, downtime_windows, expected_intervals, sim_ctx)])
        idealized_strict = max(self.fns.idealized_at_duration(
            fight_duration_s, downtime_windows, None, sim_context=c) for c in sweep_ctxs)
        idealized_observed = self.fns.idealized_at_duration(
            fight_duration_s, downtime_windows, observed_intervals, sim_context=sim_ctx)
        idealized_master = self.fns.idealized_at_duration(
            fight_duration_s, downtime_windows, expected_intervals, sim_context=sim_ctx)

        # No tincture-factor guard: the pot is placed INSIDE the sim (the optimizer
        # chooses its timing on the ceiling's own rotation), so each idealized ceiling
        # is ≥ what the player delivered with their pot by construction. The old
        # overlay-sweep + `idealized_untinctured` max() that papered over the >100%
        # asymmetry are gone. See jobs._core.tincture.merge_tincture_markers.

        enabler_values = self.fns.enabler_net_values(
            fight_duration_s, downtime_windows, sim_context=sim_ctx)

        state = {
            "delivered_potency": delivered,
            "delivered_observed": delivered_observed,
            # Back-compat scalar name; equals idealized_strict.
            "idealized_potency": idealized_strict,
            "idealized_strict": idealized_strict,
            # Filled in by sidecar post-ref-fetch; equals strict with no Tier-B.
            "idealized_lenient": idealized_strict,
            "idealized_observed": idealized_observed,
            "idealized_master": idealized_master,
            "enabler_net_values": enabler_values,
            "observed_buff_windows": [
                (w.start_s, w.end_s, w.multiplier, w.label)
                for w in observed_windows],
            "tincture_multiplier": (self.tincture_spec.multiplier
                                    if self.tincture_spec else None),
            "observed_tincture_windows": [
                (w.start_s, w.end_s) for w in tincture_windows],
            "tincture_loss": tincture_loss,
            "tincture_optimal_count": tincture_optimal_count,
            "tincture_loss_time_s": tincture_loss_time_s,
            "master_buff_intervals": expected_intervals,
            "downtime_windows": downtime_windows,
            "downtime_source": downtime_source,
            # Filled in by sidecar; ConsensusWindow tuples.
            "downtime_tier_b": [],
            "fight_duration_s": fight_duration_s,
        }
        state.update(self.extra_state(ctx))
        # Additive, phased-fights-only (empty dict is falsy → key absent on
        # Savage, so the contract snapshot stays byte-identical).
        if phase_delivered:
            state["phase_delivered"] = phase_delivered
        # The gcd-wrapped sim_context is authoritative for the sidecar's lenient /
        # Timeline recompute (`_user_sim_context`). Only override when non-None so a
        # no-op pull stays byte-identical (no stray `sim_context: null` key); an active
        # pull's CeilingContext supersedes what extra_state emitted.
        if self.gcd_constant is not None and sim_ctx is not None:
            state["sim_context"] = sim_ctx
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state=state,
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        # No findings / detail rows — the sidecar reads scalars directly.
        return AspectComparison(aspect_name=self.name, findings=[])
