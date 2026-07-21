"""Infer the analyzed player's effective GCD (gear Skill/Spell Speed + any self-haste
buff) from cast cadence.

We CANNOT read SkS/SpS from the client-credentials FFLogs API — gear/stats come back
empty (the same reason `tincture.py` uses a per-job main-stat constant). So the recast
is *inferred* from the cleanest back-to-back GCD intervals: a player's true GCD shows
in uninterrupted, un-clipped GCD pairs, while every mistake only ever ADDS time — a
clipped GCD, an extra oGCD weave, a fractional melee disengage, a downtime/mechanic
gap. Because mistakes are one-sided, the signal is the distribution's tight FLOOR,
estimated as a low percentile of the clean pairs (`_TIGHT_PCT`) — robust to outliers
below it, unbiased by slack above it. (The original median estimator was right for
top DPS parses, whose pairs are almost all queue-tight so median == floor, but a
HEALER's clean pairs are right-shifted by triage/heal-weave delays: the first live
WHM calibration showed a bimodal stream — a 2.41 tight cluster under a 2.45-nominal
majority — whose median read as the nominal recast and then deconvolved into a
slower-than-real gear estimate, leaving the sweep's band floor short of the cadence
the player demonstrably hit.)

The measured cadence folds TWO effects together — gear recast × sub-GCD tightness (the
server-tick/queue effect modeled by `_SUBGCD_SWEEP_FACTORS`) — so `effective_gcd_for`
deconvolves: gear = inferred / SUB_GCD_CADENCE_FACTOR, snapped to the job constant
within measurement noise. The gear GCD only ever SPEEDS the idealized ceiling (a
genuinely fast-SkS/SpS player); safety against inference error is structural, not a
floor: `subgcd_gcd_sweep` anchors every threaded sweep with the constant's calibrated
band (executable on any faster gear), so the maxed ceiling is monotone — threading can
never make a pull read better than not threading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class CeilingContext:
    """Uniform per-pull `sim_context` for a GCD-bound job: the effective GCD
    (`min(job constant, inferred SkS/SpS)`, or `None` to use the job default) plus the
    job's own bespoke payload (entry-gauge state, RDM proc budget, …). A falsy instance
    (both fields empty) collapses to `None` so the sim stays byte-identical. Hashable so
    it joins the perfect-sim cache key.

    Keeps the GCD axis orthogonal to each job's existing context — a job wraps its old
    payload in `.payload` and reads its effective GCD off `.gcd_base_s`, without the two
    concerns polluting each other."""
    gcd_base_s: Optional[float] = None
    payload: Any = None

    def __bool__(self) -> bool:
        return self.gcd_base_s is not None or bool(self.payload)


def unwrap_ceiling_context(sim_context) -> tuple[Optional[float], Any]:
    """`(gcd_base_s, payload)` from a sim_context that may be a `CeilingContext`
    (gcd-bound job, gcd active) or the bare job payload (no gcd → byte-identical).
    Every gcd-bound job's `_model_for` calls this to split the two concerns."""
    if isinstance(sim_context, CeilingContext):
        return sim_context.gcd_base_s, sim_context.payload
    return None, sim_context


# Inference resolution: a true-BiS player at the constant infers ~±0.01s off it, so a
# deconvolved gear estimate within this margin of the constant is indistinguishable from
# it and snaps to the constant (keeping the common BiS case byte-identical / cache-warm).
# This is a MEASUREMENT-precision snap, not a safety floor — the threaded ceiling is
# monotone-safe structurally (the constant-anchored union in `subgcd_gcd_sweep`), so a
# mis-threaded GCD can never read better than not threading. (The old `_MIN_HASTE_S=0.03`
# safety gate this replaces was implicitly `constant × (1 - SUB_GCD_CADENCE_FACTOR)` — it
# existed to keep the sub-GCD tightness in a measured cadence from being double-counted
# as gear; `effective_gcd_for` now divides that tightness out explicitly instead.)
_INFER_NOISE_S: float = 0.01

# Empirical sub-GCD cadence band — the heart of the sub-GCD TIMING model. A GCD's nominal
# recast (the gear value, e.g. 2.50s) is NOT the cadence a real top parse achieves: the
# server resolves a queued GCD on the tick its recast completes, which lands a few
# hundredths early, so clean tight play fits a fractionally-faster EFFECTIVE GCD over a
# fight (measured down to ~2.47s on 2.50 gear from live top parses). The idealized ceiling
# is scored over this band and the BEST (highest) ceiling kept, so elite play approaches
# but cannot exceed it — the >100% a nominal-recast (true-2.50) ceiling showed on live top
# parses (PLD 100.25%, MCH 100.77%) was exactly this unmodelled sub-GCD effect, NOT a
# search/pot/choice gap (all proved already-optimal). The band runs from the gear GCD
# (factor 1.0) down to the tightest cadence; because the GCD search is NON-MONOTONIC (a
# faster GCD can score a worse local optimum), the band is swept and `max`'d rather than a
# single faster GCD substituted — monotonic-safe (never below the gear ceiling). Job/gear-
# agnostic (a server mechanic), so it scales a self-haste GCD (SAM Fuka) the same way.
# Calibrate per tier with scripts/calibrate_subgcd_cadence.py.
# Two points suffice at this tier (the gear GCD + the calibrated tight cadence): the
# `max` keeps the gear ceiling, and 0.988 (~2.47s on 2.50 gear) is the empirical cadence
# that lifts every live >100.5% offender under the gate (MCH 100.77%→99.96%) with no
# regression. Add intermediate points only if a future tier shows a parse whose sweet
# spot the search's non-monotonicity skips between these two.
_SUBGCD_SWEEP_FACTORS: tuple[float, ...] = (1.0, 0.988)
SUB_GCD_CADENCE_FACTOR: float = _SUBGCD_SWEEP_FACTORS[-1]   # the tightest cadence (band floor)

# Denser band for a THREADED (inferred-faster-than-constant) gear GCD. The ceiling search
# (`engine.perfect`'s sweep + hill-climb) is non-monotonic in the GCD — a single cadence
# can land in a poor local optimum (the measured PLD case: a 2.495 ceiling scoring ~1.1%
# under the 2.50 one). At the calibrated constant that risk is absorbed by tier-wide
# live calibration; at an arbitrary per-player GCD it isn't, so the threaded band is a
# multi-start along the GCD axis: four nearby cadences, best kept, so one collapsed
# search can't sink the ceiling. Unthreaded pulls keep the cheap 2-point band above.
_SUBGCD_THREADED_FACTORS: tuple[float, ...] = (1.0, 0.996, 0.992, 0.988)

# The clean-pair percentile that estimates the player's TIGHT cadence (the
# distribution's floor — see the module docstring for why the floor, not the median,
# is the signal). Low enough to land inside the tight cluster even for a healer whose
# majority pairs carry triage slack; high enough (over the >= min_samples pairs) that
# a single ms-jitter outlier can't set the estimate.
_TIGHT_PCT: float = 0.15


def effective_gcd_for(norm_casts, is_gcd: Callable[[int], bool], constant_s: float,
                      downtime_windows=None) -> float:
    """The player's GEAR GCD, deconvolved from cast cadence. The inference measures the
    player's EFFECTIVE tight cadence = gear recast × sub-GCD tightness (the server-tick/
    queue effect lets clean play land each GCD fractionally early), so the raw estimate
    is NOT gear: a maximally-tight true-BiS player measures `constant × SUB_GCD_CADENCE_FACTOR`.
    Dividing the band floor back out recovers gear (threading the raw cadence would
    double-count the tightness — the ceiling's sub-GCD sweep multiplies the band on top
    of whatever gear value is threaded, so the band floor must anchor at the OBSERVED
    cadence, which `gear_est × SUB_GCD_CADENCE_FACTOR == inferred` guarantees).

    Estimates within `_INFER_NOISE_S` of the constant snap to it (measurement resolution
    — the common BiS case stays byte-identical). A genuinely faster estimate is returned
    as-is: safety no longer rests on a floor, because `subgcd_gcd_sweep` anchors every
    threaded sweep with the constant's own band, making the threaded ceiling >= the
    calibrated constant ceiling by construction."""
    inferred = infer_effective_gcd(norm_casts, is_gcd, constant_s, downtime_windows)
    gear_est = inferred / SUB_GCD_CADENCE_FACTOR
    return gear_est if gear_est <= constant_s - _INFER_NOISE_S else constant_s


def subgcd_gcd_sweep(gear_gcd: float, constant_s: float | None = None) -> tuple[float, ...]:
    """The GCD cadences the ceiling's sub-GCD sweep scores, best (highest ceiling) kept;
    `[0]` is always the gear GCD (the authoritative context downstream). A real top parse
    fits a fractionally-faster EFFECTIVE GCD than nominal recast (server-tick/queue), so
    its true ceiling sits at the best cadence in the tight-play band down to
    `SUB_GCD_CADENCE_FACTOR × gear`; `max`-with-gear keeps the band monotonic-safe
    despite the search's GCD non-monotonicity.

    With `constant_s` given and a THREADED gear GCD (faster than the constant), the sweep
    is the union of two bands — the structural monotonicity guarantee that lets the gear
    inference run without a safety floor:
      * the gear band, DENSE (`_SUBGCD_THREADED_FACTORS`): a multi-start along the GCD
        axis so a single collapsed search can't sink the per-player ceiling;
      * the constant's calibrated band: any rotation executable at a slower cadence is
        executable on faster gear (queue at the slower rhythm), so its ceiling is a valid
        lower bound — `max`-ing it in makes the threaded ceiling >= the calibrated
        constant ceiling BY CONSTRUCTION (a mis-inferred gear GCD can therefore never
        make a pull read better than not threading, only tighter).
    `gear == constant` (the common BiS case) keeps today's 2-point band, byte-identical."""
    if constant_s is None or gear_gcd >= constant_s - 1e-9:
        return tuple(gear_gcd * f for f in _SUBGCD_SWEEP_FACTORS)
    return (tuple(gear_gcd * f for f in _SUBGCD_THREADED_FACTORS)
            + tuple(constant_s * f for f in _SUBGCD_SWEEP_FACTORS))


def demonstrated_cadence(
    norm_casts: list[tuple[float, int]],
    is_gcd: Callable[[int], bool],
    fight_duration_s: float,
    downtime_windows: list[tuple[float, float]] | None = None,
    haste_windows: list[tuple[float, float]] | None = None,
    *,
    min_gcds: int = 12,
) -> float | None:
    """The cadence at which the ceiling casts as many GCDs as the player demonstrably did =
    active uptime / player GCD count.

    A HARD FACT, not an estimate: the player provably cast `n` scoring GCDs, and the ceiling
    (which produces ~uptime/cadence GCDs) must be allowed at least that budget or a real parse
    out-GCDs it. The clean-pair tight floor (`infer_effective_gcd`) samples ISOLATED pairs and
    its 15th-percentile can sit above the sustained rate for a parse whose tight burst queuing
    is diluted by looser filler pairs (measured: top GNB M12S-P1 parses cast 162 GCDs where
    the beam ceiling at the clean-pair-derived ~2.49s cadence produced only ~160 → the human
    out-GCD'd it → >100%). Feeding uptime/n as an extra sweep anchor closes that by
    construction. Purely a faster LOWER-bound anchor the caller max'es in → monotone-safe: it
    can only raise the ceiling, never push a pull over 100%.

    `downtime_windows` are removed from the uptime (the ceiling can't cast while the boss is
    untargetable) but their GCDs are still COUNTED — a downtime ranged-filler GCD is real
    output the ceiling can't replicate, so crediting the ceiling one extra uptime GCD to cover
    it is correct and conservative. `haste_windows` (self-haste like BLM Ley Lines) are
    removed from BOTH the uptime AND the count, because the ceiling already models those GCDs
    with its own faster `gcd_duration` — counting them here would double-credit the haste.
    Returns None below `min_gcds` (too little signal → no anchor)."""
    downtime = downtime_windows or []
    haste = haste_windows or []

    def _in(t: float, wins: list[tuple[float, float]]) -> bool:
        return any(s <= t < e for s, e in wins)

    n = sum(1 for t, a in norm_casts
            if t >= 0.0 and is_gcd(a) and not _in(t, haste))
    if n < min_gcds:
        return None
    off_total = sum(min(fight_duration_s, e) - max(0.0, s)
                    for s, e in [*downtime, *haste] if e > 0.0 and s < fight_duration_s)
    uptime = fight_duration_s - max(0.0, off_total)
    return uptime / n if uptime > 0 else None


def _straddles_downtime(a: float, b: float,
                        downtime: list[tuple[float, float]]) -> bool:
    return any(a < end and start < b for start, end in downtime)


def infer_effective_gcd(
    norm_casts: list[tuple[float, int]],
    is_gcd: Callable[[int], bool],
    base_s: float,
    downtime_windows: list[tuple[float, float]] | None = None,
    *,
    max_weaves: int = 1,
    band: tuple[float, float] = (0.80, 1.12),
    min_samples: int = 12,
) -> float:
    """Tight-floor percentile of CLEAN consecutive-GCD intervals — the player's
    effective (tight) GCD cadence.

    A pair (GCD_i, GCD_i+1) is CLEAN when:
      * **<= `max_weaves` oGCDs** are woven between them — a 2nd weave's animation lock
        can clip the GCD and stretch the interval (the user's "devoid of weaves>1");
      * the interval is within **`band` x `base_s`** — drops downtime / mechanic /
        fractional-disengage gaps (too long) and sub-band instant-GCD mechanics (too
        short, e.g. MCH Overheated's 1.5s Blazing chain). Centering the band on the
        job's GCD constant also targets the right cluster for a self-haste job (SAM's
        ~2.14s Fuka GCDs land in-band; its rare un-buffed 2.5s ones fall out);
      * the pair does not straddle a downtime window.

    `base_s` should be the job's GCD constant (the effective GCD the sim assumes).
    Returns `base_s` unchanged when there are fewer than `min_samples` clean pairs (not
    enough signal to trust an inference)."""
    downtime = downtime_windows or []
    casts = sorted((t, a) for t, a in norm_casts if t >= 0.0)
    lo, hi = band[0] * base_s, band[1] * base_s
    clean: list[float] = []
    prev_gcd_t: float | None = None
    weaves_since = 0
    for t, aid in casts:
        if is_gcd(aid):
            if prev_gcd_t is not None and weaves_since <= max_weaves:
                gap = t - prev_gcd_t
                if lo <= gap <= hi and not _straddles_downtime(prev_gcd_t, t, downtime):
                    clean.append(gap)
            prev_gcd_t = t
            weaves_since = 0
        else:
            weaves_since += 1
    if len(clean) < min_samples:
        return base_s
    clean.sort()
    return clean[int(_TIGHT_PCT * (len(clean) - 1))]
