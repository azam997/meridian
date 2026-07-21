"""Tincture (damage potion) modeling: value, detection, placement, scoring.

The contract/pipeline snapshots can't exercise the tincture path — their
fixtures carry no Medicated aura and their stub client has no `get_aura_events`,
so the observed-pot side is always empty there. This file pins the real
behavior with synthetic data:

  * the value formula `f(base+Δ)/f(base)` (and that it's stronger for less gear),
  * Medicated aura pairing incl. the pre-pull (orphan-remove) case,
  * `fetch_observed_tincture_windows` filtering + failure-safety,
  * the placement DP (value-maximizing, cooldown-constrained, pot-capped — still
    used by the player-side pot-timing-loss card),
  * the in-sim pot adding bounded value to the idealized ceiling (placed inside the
    sim as a timeline marker, credited at cast time — no overlay sweep, no guard),
  * delivered scoring crediting an in-window pot at exactly M (RDM scorer —
    metadata-free, so the multiplier check is exact under the offline conftest).

Run from python/:  python tests/test_tincture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.tincture import (
    TINCTURE_ACTION_ID,
    TINCTURE_DELTA,
    TinctureSpec,
    _pair_medicated_intervals,
    candidate_pot_starts,
    fetch_observed_tincture_windows,
    make_tincture_windows,
    max_pots,
    select_best_starts,
    spec_for_job,
    tincture_multiplier,
    tincture_timing_loss,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ev(typ: str, ms: int, sid: int = 1000049) -> dict:
    return {"type": typ, "timestamp": ms, "abilityGameID": sid}


# --- 1. Value formula -------------------------------------------------------

def test_multiplier_value_and_monotonicity() -> None:
    m = tincture_multiplier(6838, TINCTURE_DELTA, 237)
    _check("MCH M ~= 1.0821", abs(m - 1.0821) < 0.001, f"M={m}")
    # Stronger for less gear, weaker for more — the whole reason we'd want
    # per-player stats; lower base => bigger relative bump.
    _check("less gear -> stronger",
           tincture_multiplier(6000, TINCTURE_DELTA, 237) > m, "")
    _check("more gear -> weaker",
           tincture_multiplier(7000, TINCTURE_DELTA, 237) < m, "")
    # Role coefficient (tank 190 vs non-tank 237) barely moves the ratio.
    _check("coeff-insensitive (<0.1pp)",
           abs(tincture_multiplier(6838, TINCTURE_DELTA, 190) - m) < 0.001, "")


# --- 2. Medicated aura pairing ---------------------------------------------

def test_pair_medicated_intervals() -> None:
    # Normal apply -> remove.
    _check("normal pair",
           _pair_medicated_intervals([_ev("applybuff", 1000),
                                      _ev("removebuff", 31000)], 0, 60000)
           == [(1.0, 31.0)], "")
    # Orphan remove = pot popped pre-pull => active from t=0.
    _check("orphan remove -> from pull",
           _pair_medicated_intervals([_ev("removebuff", 29900)], 0, 60000)
           == [(0.0, 29.9)], "")
    # Open apply at fight end => auto-closes at fight end.
    _check("open apply -> to fight end",
           _pair_medicated_intervals([_ev("applybuff", 50000)], 0, 60000)
           == [(50.0, 60.0)], "")
    # Two pots.
    _check("two pots",
           _pair_medicated_intervals(
               [_ev("applybuff", 1000), _ev("removebuff", 31000),
                _ev("applybuff", 300000), _ev("removebuff", 330000)], 0, 400000)
           == [(1.0, 31.0), (300.0, 330.0)], "")


# --- 3. Observed-window fetch ----------------------------------------------

class _StubClient:
    def __init__(self, evs: list[dict]):
        self._evs = evs

    def get_aura_events(self, code, s, e, sid, data_type="Buffs"):
        assert data_type == "Buffs"
        return self._evs


class _BadClient:
    def get_aura_events(self, *a, **k):
        raise RuntimeError("boom")


def test_fetch_observed_tincture_windows() -> None:
    fight = {"startTime": 0, "endTime": 60000}
    evs = [_ev("applybuff", 1000), _ev("removebuff", 31000),
           _ev("applybuff", 5000, sid=1000050)]   # 1000050 is NOT Medicated
    wins = fetch_observed_tincture_windows(_StubClient(evs), "c", fight, 7, 1.0866)
    _check("one Medicated window", len(wins) == 1, f"got {len(wins)}")
    _check("window bounds", (wins[0].start_s, wins[0].end_s) == (1.0, 31.0), "")
    _check("window carries M + label",
           abs(wins[0].multiplier - 1.0866) < 1e-9 and wins[0].label == "Tincture", "")
    # M<=1 (job doesn't pot / degenerate) => no windows, no fetch needed.
    _check("M<=1 -> []",
           fetch_observed_tincture_windows(_StubClient(evs), "c", fight, 7, 1.0) == [], "")
    # Client error is non-fatal — tinctures are a bonus.
    _check("client error -> []",
           fetch_observed_tincture_windows(_BadClient(), "c", fight, 7, 1.0866) == [], "")


# --- 4. Placement DP --------------------------------------------------------

def test_placement_selection() -> None:
    spec = TinctureSpec(multiplier=1.0866)
    cands = candidate_pot_starts(600.0, None, spec)
    _check("opener is a candidate", 0.0 in cands, "")

    def val_aligned(s: float) -> float:
        return {0.0: 100.0, 300.0: 90.0}.get(round(s, 1), 0.0)

    _check("picks both high-value (>= cooldown apart)",
           select_best_starts(cands, val_aligned, spec, max_pots(600.0, spec))
           == [0.0, 300.0], "")

    def val_too_close(s: float) -> float:
        return {0.0: 100.0, 180.0: 95.0}.get(round(s, 1), 0.0)   # 180 < 270 CD

    _check("cooldown blocks the closer pick",
           select_best_starts(cands, val_too_close, spec, max_pots(600.0, spec))
           == [0.0], "")

    _check("respects n_pots cap",
           len(select_best_starts(cands, lambda s: 1.0, spec, 1)) == 1, "")
    _check("no positive value -> no pots",
           select_best_starts(cands, lambda s: 0.0, spec, 3) == [], "")


# --- 5. In-sim pot raises the idealized ceiling (bounded) ------------------

def test_idealized_in_sim_pot_adds_bounded_value() -> None:
    """The idealized ceiling places the tincture INSIDE the sim (the model's
    `tincture_spec` → a TINCTURE_ACTION_ID marker in the perfect-sim timeline) and
    credits it at cast time — no overlay sweep, no >100% guard. The pot raises the
    ceiling, bounded by M applied to the whole fight. PLD: a clean per-cast scorer
    (no pet/aux path), so stripping the marker isolates exactly the tincture credit."""
    from jobs.paladin import scoring as pld

    dur, downtime = 400.0, []
    tl, aux = pld._perfect_sim_cached(*pld._sim_cache_keys(dur, downtime, None, None))
    tl = list(tl)
    pots = [t for t, a in tl if a == TINCTURE_ACTION_ID]
    _check("sim placed a pot marker in-timeline", len(pots) >= 1, f"pots={pots}")

    m = pld._TINCTURE_SPEC.multiplier
    i_yes = pld._score_timeline(tl, aux, None, None)               # credits the marker
    clean = [c for c in tl if c[1] != TINCTURE_ACTION_ID]
    i_no = pld._score_timeline(clean, aux, None, None)             # no marker → no tincture
    _check("tincture raises the ceiling", i_yes > i_no, f"{i_yes} vs {i_no}")
    # Upper bound: the tincture can't beat M applied to the whole fight.
    _check("ceiling <= full-fight M bound", i_yes <= i_no * m + 1.0,
           f"{i_yes} vs {i_no * m}")


# --- 6. Delivered scoring credits the in-window pot (RDM, metadata-free) ----

def test_delivered_credits_in_window_pot() -> None:
    from jobs.redmage import data as rdm_data
    from jobs.redmage.scoring import score_delivered_potency

    aid, pot = max(rdm_data.POTENCIES.items(), key=lambda kv: kv[1])
    timeline = [(0.0, aid), (2.0, aid), (4.0, aid)]   # all inside [0, 30]
    base = score_delivered_potency(timeline)
    _check("base potency = 3 * pot", abs(base - 3 * pot) < 1e-6, f"{base}")

    withp = score_delivered_potency(timeline, buff_intervals=[(0.0, 30.0, 1.0866)])
    _check("in-window pot credits exactly M",
           abs(withp - base * 1.0866) < 1.0, f"{withp} vs {base * 1.0866}")

    out = score_delivered_potency(timeline, buff_intervals=[(100.0, 130.0, 1.0866)])
    _check("out-of-window pot credits nothing",
           abs(out - base) < 1e-6, f"{out} vs {base}")


# --- 7. spec_for_job gate ---------------------------------------------------

def test_spec_for_job() -> None:
    _check("no main stat -> no spec (byte-identical path)",
           spec_for_job(None) is None, "")
    spec = spec_for_job(6516, 237)
    _check("with main stat -> spec with M>1",
           spec is not None and spec.multiplier > 1.0
           and spec.duration_s == 30.0 and spec.cooldown_s == 270.0, "")


def test_timing_loss() -> None:
    """The Potential-Improvements card value: optimal pot placement on the
    player's own rotation minus what their actual pots did."""
    from jobs._core.buff_windows import multiplier_at
    spec = TinctureSpec(multiplier=1.10)               # clean +10% math
    # Dense burst in [0,30), filler after. 200s ⇒ max_pots == 1.
    casts = ([(float(t), 1000.0) for t in range(30)]
             + [(float(t), 100.0) for t in range(30, 200)])

    def score_with(iv):
        return sum(p * multiplier_at(t, iv) for t, p in casts)

    dur = 200.0
    on_burst = make_tincture_windows([0.0], spec)      # pot covers the burst
    loss, n, _ = tincture_timing_loss(spec, dur, score_with, on_burst)
    _check("potted on burst -> ~0 loss", loss < 1.0, f"loss={loss} n={n}")
    _check("1 pot fits in 200s", n == 1, f"n={n}")

    loss_none, _, _ = tincture_timing_loss(spec, dur, score_with, [])
    _check("no pot -> full burst pot value (3000)",
           abs(loss_none - 3000.0) < 1.0, f"loss={loss_none}")

    off = make_tincture_windows([100.0], spec)          # pot on filler
    loss_off, _, _ = tincture_timing_loss(spec, dur, score_with, off)
    _check("misaligned pot -> recoverable (2700)",
           abs(loss_off - 2700.0) < 1.0, f"loss={loss_off}")


def main() -> int:
    print()
    print("Test: tincture modeling")
    tests = [
        test_multiplier_value_and_monotonicity,
        test_pair_medicated_intervals,
        test_fetch_observed_tincture_windows,
        test_placement_selection,
        test_idealized_in_sim_pot_adds_bounded_value,
        test_delivered_credits_in_window_pot,
        test_spec_for_job,
        test_timing_loss,
    ]
    for t in tests:
        try:
            t()
        except AssertionError:
            pass
    print()
    print("============================================================")
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
