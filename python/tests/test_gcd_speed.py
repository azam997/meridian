"""Unit tests for the per-player GCD inference (jobs/_core/gcd_speed.py).

Pins the clean-sampling spec: only un-clipped, lightly-woven, uninterrupted
back-to-back GCD pairs feed the estimate, so sloppy play (clips, extra weaves,
downtime/disengage gaps) can't inflate the inferred GCD. Runs standalone or under
pytest."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs._core.gcd_speed import (  # noqa: E402
    CeilingContext,
    effective_gcd_for,
    infer_effective_gcd,
    subgcd_gcd_sweep,
    unwrap_ceiling_context,
)

# Ability-id convention for the tests: 1 = a GCD weaponskill, 9 = an oGCD.
GCD, OGCD = 1, 9


def _is_gcd(aid: int) -> bool:
    return aid == GCD


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def test_clean_back_to_back() -> None:
    # 30 clean GCDs at exactly 2.45 -> inferred 2.45 (< base 2.5).
    casts = [(i * 2.45, GCD) for i in range(30)]
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5, min_samples=12)
    assert _approx(g, 2.45), g


def test_bimodal_healer_stream_reads_tight_floor() -> None:
    # The live-WHM calibration case: a healer's clean pairs are RIGHT-SHIFTED by
    # triage slack — a tight cluster at the real cadence (2.41) under a nominal-
    # recast majority (2.45). Mistakes only ever ADD time, so the tight floor is
    # the signal: the estimate must read ~2.41, not the 2.45 the median saw.
    casts: list[tuple[float, int]] = []
    t = 0.0
    for i in range(60):
        casts.append((t, GCD))
        t += 2.41 if i % 3 == 0 else 2.45   # ~1/3 tight pairs, 2/3 with slack
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5, min_samples=12)
    assert _approx(g, 2.41), g
    # A single jitter outlier below the tight cluster must NOT set the estimate.
    casts_outlier = list(casts) + [(t, GCD), (t + 2.05, GCD)]
    g2 = infer_effective_gcd(casts_outlier, _is_gcd, base_s=2.5, min_samples=12)
    assert _approx(g2, 2.41), g2


def test_single_weave_kept_double_weave_excluded() -> None:
    # Each GCD at 2.45; between some pairs put ONE oGCD (kept), others TWO (the pair
    # AFTER a double-weave is dropped). Build a stream of 25 GCDs, weaving 1 oGCD after
    # the even ones and 2 after a few odd ones, all GCDs still 2.45 apart.
    casts: list[tuple[float, int]] = []
    t = 0.0
    for i in range(25):
        casts.append((t, GCD))
        weaves = 1 if i % 2 == 0 else (2 if i % 5 == 0 else 0)
        for w in range(weaves):
            casts.append((t + 0.6 + 0.1 * w, OGCD))
        t += 2.45
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5, min_samples=8)
    # Still 2.45 — the kept (<=1 weave) pairs all measure the true GCD.
    assert _approx(g, 2.45), g


def test_clips_and_downtime_excluded() -> None:
    # 20 clean 2.45 GCDs, then a clipped 2.9 (out of band) and a 30s downtime gap —
    # both must be ignored, leaving the 2.45 estimate.
    casts = [(i * 2.45, GCD) for i in range(20)]
    last = casts[-1][0]
    casts.append((last + 2.9, GCD))      # clipped (> 1.12*2.5=2.8) -> out of band
    casts.append((last + 2.9 + 30.0, GCD))  # 30s gap -> out of band
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5,
                            downtime_windows=[(last + 3.0, last + 32.0)], min_samples=12)
    assert _approx(g, 2.45), g


def test_insufficient_samples_returns_base() -> None:
    casts = [(i * 2.45, GCD) for i in range(5)]  # only 4 intervals < min_samples
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5, min_samples=12)
    assert _approx(g, 2.5), g


def test_band_excludes_instant_mechanic_gcds() -> None:
    # A 1.5s instant-mechanic chain (e.g. MCH Overheated) interleaved with 2.45 normals
    # must not drag the estimate down — 1.5 is below the 0.8*2.5=2.0 band floor.
    casts: list[tuple[float, int]] = []
    t = 0.0
    for i in range(20):
        casts.append((t, GCD)); t += 2.45
        if i % 4 == 3:                       # a short burst of 1.5s GCDs
            for _ in range(5):
                casts.append((t, GCD)); t += 1.5
    g = infer_effective_gcd(casts, _is_gcd, base_s=2.5, min_samples=10)
    assert _approx(g, 2.45), g


def test_effective_gcd_for_deconvolves_gear() -> None:
    # The measured cadence = gear x sub-GCD tightness, so effective_gcd_for DIVIDES the
    # band floor back out: gear = inferred / SUB_GCD_CADENCE_FACTOR, snapped to the
    # constant within measurement noise. The sub-GCD band is then applied by the
    # ceiling's max-guarded sweep, whose floor lands back on the observed cadence.
    from jobs._core.gcd_speed import SUB_GCD_CADENCE_FACTOR
    fast = [(i * 2.42, GCD) for i in range(30)]      # genuine fast SkS
    g = effective_gcd_for(fast, _is_gcd, 2.5)
    assert _approx(g, 2.42 / SUB_GCD_CADENCE_FACTOR), g
    assert g * SUB_GCD_CADENCE_FACTOR <= 2.42 + 1e-9  # band floor reaches the cadence
    # A maximally TIGHT true-BiS player measures constant x band-floor — that is
    # sub-GCD tightness, NOT gear; the deconvolution must snap it to the constant.
    tight = [(i * 2.5 * SUB_GCD_CADENCE_FACTOR, GCD) for i in range(30)]
    assert _approx(effective_gcd_for(tight, _is_gcd, 2.5), 2.5)
    slow = [(i * 2.55, GCD) for i in range(30)]
    assert _approx(effective_gcd_for(slow, _is_gcd, 2.5), 2.5)        # slower-than-const
    assert _approx(effective_gcd_for([(i * 2.5, GCD) for i in range(30)], _is_gcd, 2.5), 2.5)


def test_subgcd_sweep_band() -> None:
    # Unthreaded (gear == constant, or no constant given): the sweep runs from the gear
    # GCD (factor 1.0) down to the tightest, all <= gear, first element == gear (so
    # max-with-it is monotonic-safe). Passing the equal constant must change nothing.
    sweep = subgcd_gcd_sweep(2.5)
    assert _approx(sweep[0], 2.5), sweep
    assert all(g <= 2.5 + 1e-9 for g in sweep), sweep
    assert sweep[-1] < 2.5, sweep   # the band has a tighter floor than gear
    assert subgcd_gcd_sweep(2.5, 2.5) == sweep   # byte-identical common case


def test_subgcd_sweep_threaded_union_is_monotone_anchored() -> None:
    # Threaded (gear < constant): the sweep is gear band UNION constant band. The
    # constant band makes the maxed ceiling >= the calibrated constant ceiling by
    # construction (any rotation executable at a slower cadence is executable on faster
    # gear) — the structural guarantee that replaced the _MIN_HASTE_S safety floor.
    gear, const = 2.44, 2.5
    threaded = subgcd_gcd_sweep(gear, const)
    assert _approx(threaded[0], gear), threaded       # [0] = gear, authoritative downstream
    for c in subgcd_gcd_sweep(const, const):          # the whole constant band is anchored in
        assert any(_approx(c, g) for g in threaded), (c, threaded)
    gear_band = [g for g in threaded if g <= gear + 1e-9]
    assert len(gear_band) >= 3, threaded              # dense gear band: GCD-axis multi-start
    assert min(threaded) >= gear * 0.98, threaded     # nothing faster than the gear band floor


def test_unwrap_ceiling_context() -> None:
    assert unwrap_ceiling_context(None) == (None, None)
    assert unwrap_ceiling_context("payload") == (None, "payload")
    assert unwrap_ceiling_context(CeilingContext(2.42, "p")) == (2.42, "p")
    # Falsy CeilingContext collapses semantics: bool() False when empty.
    assert not CeilingContext()
    assert CeilingContext(gcd_base_s=2.42)
    assert CeilingContext(payload="x")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK] {name}")
    print("gcd_speed: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
