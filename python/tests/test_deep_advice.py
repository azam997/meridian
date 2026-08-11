"""Unit tests for the deep-advice probes (sidecar/advice.py) — the
deterministic "improvements algorithm" behind the Deeper-analysis button.

Run from python/:  python tests/test_deep_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass

from jobs._core.job import JobData
from jobs.machinist.advice import _probe_window_shift
from sidecar.advice import compute_advice

DRILL = 16498         # GCD
DOUBLE_CHECK = 36979  # oGCD

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


@dataclass
class _Clip:
    worst_idle: list


def _data() -> JobData:
    return JobData(job_name="Test", patch_version="7.x",
                   potencies={DRILL: 600, DOUBLE_CHECK: 180})


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_window_shift_finds_earlier() -> None:
    print("\nTest: window-shift probe finds the ~1s-earlier placement")
    # Now MCH job knowledge: jobs.machinist.advice._probe_window_shift
    # (relocated out of the core per the registry pattern).
    # Wildfire at t=100 (10s window, cap 6). Blazing-cadence casts 102.5..108.5
    # put 5 inside [100, 110]; one more sits just before at 99.0 — shifting the
    # window ~1s EARLIER catches it for the full 6 (the later shift needs 2s,
    # so the smaller magnitude wins and ties prefer earlier).
    gcd_times = [99.0, 102.5, 104.0, 105.5, 107.0, 108.5, 112.0, 113.5]
    it = _probe_window_shift(_card("wildfire", 2878, 100.0, name="Wildfire"),
                             gcd_times)
    _check("one item", it is not None, "got None")
    _check("prescribes an earlier shift with the full count",
           "earlier" in it.prescription and "all six" in it.prescription,
           f"got {it.prescription!r}")
    _check("boundary cast becomes a labelled evidence row",
           it.evidence and it.evidence[0].k == "Sixth WS"
           and "missed by" in it.evidence[0].v,
           f"got {it.evidence}")
    _check("triple matches the card",
           it.kind == "wildfire" and it.time_sec == 100.0,
           f"got {it.kind} @ {it.time_sec}")


def test_window_shift_full_window_silent() -> None:
    print("\nTest: an already-full window gets no item")
    gcd_times = [100.0 + 1.5 * i for i in range(8)]   # 6+ inside 100..110
    it = _probe_window_shift(_card("wildfire", 2878, 100.0), gcd_times)
    _check("no item (static template stands)", it is None, f"got {it}")


def test_ogcd_weave_fit() -> None:
    print("\nTest: missed oGCD → nearest ≥0.75s weave opening")
    casts = ([(50.0 + 2.5 * i, DRILL) for i in range(10)]
             + [(61.3, DOUBLE_CHECK)])   # 60.0→61.3 has sub-gap; 61.3→62.5 too
    # A clear 2.5s cadence: gaps between consecutive GCDs are 2.5s ≥ 0.75.
    out = compute_advice([_card("missed_cast", DOUBLE_CHECK, 58.0,
                                name="Double Check")],
                         casts, [], {"clipping": None}, _data())
    _check("one item", len(out) == 1, f"got {len(out)}")
    _check("prescribes a weave slot",
           out[0]["prescription"].startswith("Weave it after"),
           f"got {out[0]['prescription']!r}")


def test_gcd_fit_idle_then_fallback() -> None:
    print("\nTest: missed GCD → idle stretch when near, filler fallback else")
    clip = {"clipping": _Clip(worst_idle=[(60.0, 1.9)])}
    near = compute_advice([_card("missed_cast", DRILL, 55.0, name="Drill")],
                          [(0.0, DRILL)], [], clip, _data())
    _check("fits the 1.9s gap at 1:00",
           len(near) == 1 and "1.9s gap at 1:00" in near[0]["prescription"],
           f"got {near}")
    far = compute_advice([_card("missed_cast", DRILL, 200.0, lost=340.0)],
                         [(0.0, DRILL)], [], clip, _data())
    _check("far from any idle → displaces-a-filler fallback with per-cast p",
           len(far) == 1 and "displace a filler" in far[0]["prescription"]
           and "~340p" in far[0]["prescription"],
           f"got {far}")


def test_residual_table() -> None:
    print("\nTest: residual card → per-ability count-gap bars, biggest first")
    player = [(float(i), DRILL) for i in range(3)]
    ideal = ([(float(i), DRILL) for i in range(5)]
             + [(float(i), DOUBLE_CHECK) for i in range(1)])
    out = compute_advice([_card("residual", 0, 0.0)],
                         player, ideal, {"clipping": None}, _data())
    _check("one item", len(out) == 1, f"got {len(out)}")
    it = out[0]
    _check("prescription names the top gap as the sim fitting more",
           it["prescription"].startswith("Biggest count gaps: Drill")
           and "sim fits more" in it["prescription"],
           f"got {it['prescription']!r}")
    _check("count gaps lead with the biggest deficit (Drill 3/5)",
           it["countGaps"][0] == {"name": "Drill", "you": 3, "sim": 5},
           f"got {it['countGaps']}")
    _check("residual triple preserved (timeSec 0 → non-located row)",
           it["kind"] == "residual" and it["timeSec"] == 0.0,
           f"got {it}")


def test_residual_table_surplus_not_inverted() -> None:
    print("\nTest: a surplus (you cast MORE) never reads as a loss or a gap")
    # The live-report scenario: +1 Drill (surplus), −1 lower-value GCD
    # (deficit). The 660p Drill surplus outweighs the deficit by raw value —
    # the headline must STILL name the deficit, and the surplus row must say
    # "extra" with no potency figure attached.
    lesser = 7411   # Heated Split Shot — a real GCD id
    data = JobData(job_name="Test", patch_version="7.x",
                   potencies={DRILL: 660, lesser: 220})
    player = ([(float(i), DRILL) for i in range(4)]        # 4 vs sim 3
              + [(float(10 + i), lesser) for i in range(2)])   # 2 vs sim 3
    ideal = ([(float(i), DRILL) for i in range(3)]
             + [(float(10 + i), lesser) for i in range(3)])
    out = compute_advice([_card("residual", 0, 0.0)],
                         player, ideal, {"clipping": None}, data)
    it = out[0]
    _check("headline names the DEFICIT, not the bigger-value surplus",
           "Biggest count gaps: Heated Split Shot" in it["prescription"],
           f"got {it['prescription']!r}")
    _check("deficit row first (you < sim)",
           it["countGaps"][0] == {"name": "Heated Split Shot",
                                  "you": 2, "sim": 3},
           f"got {it['countGaps']}")
    _check("surplus row trails (you > sim), same neutral shape",
           it["countGaps"][1] == {"name": "Drill", "you": 4, "sim": 3},
           f"got {it['countGaps']}")
    _check("prescription names the trade (extras are not free gains)",
           "Your extra Drill casts came out of those slots" in it["prescription"]
           and "worth less than what it displaced" in it["prescription"],
           f"got {it['prescription']!r}")
    # Pure-surplus table: no gap to name → the honest no-gaps framing.
    only_up = compute_advice([_card("residual", 0, 0.0)],
                             [(0.0, DRILL), (1.0, DRILL)], [(0.0, DRILL)],
                             {"clipping": None}, data)
    _check("pure surplus → 'not which buttons you pressed' framing",
           len(only_up) == 1
           and only_up[0]["prescription"].startswith("No ability ran behind"),
           f"got {only_up}")


def test_keys_subset_of_cards() -> None:
    print("\nTest: every advice triple corresponds to an input card")
    cards = [
        _card("wildfire", 2878, 100.0),
        _card("missed_cast", DRILL, 55.0),
        _card("residual", 0, 0.0),
        _card("overcap", 0, 80.0),   # no probe for this kind
    ]
    gcds = [(92.5 + 2.5 * i, DRILL) for i in range(10)]
    clip = {"clipping": _Clip(worst_idle=[(60.0, 1.9)])}
    out = compute_advice(cards, gcds, [(0.0, DRILL)], clip, _data())
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out}
    _check("advice keys ⊆ card keys", item_keys <= card_keys,
           f"extra: {item_keys - card_keys}")
    _check("unprobed kinds get no item",
           all(i["kind"] != "overcap" for i in out),
           f"got {[i['kind'] for i in out]}")


def _mch_ctx(norm_casts, idealized, runner=None, fight_s: float = 150.0):
    from jobs._core.advice import AdviceContext
    from jobs.machinist import data as md
    gcds = frozenset({7411, 7412, 7413, 16497, 16498, 16499, 16500, 25786,
                      25788, 36978, 36981, 36982})
    return AdviceContext(
        job="Machinist", data=md.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=[], death_windows=[],
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.machinist.simulator", runner=runner, gcd_ids=gcds)


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list — conservation, stability, cascade cards")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.machinist.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _mch_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", DRILL, 30.0, lost=400.0, name="Drill"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live1)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("probe prescriptions merged into the cards in place",
           any(c.get("prescription") for c in live1),
           f"got {[c.get('prescription') for c in live1]}")
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    _check("examined payload produced", ex is not None, "got None")
    orig_sum = round(sum(c["lostPotency"] for c in cards), 1)
    new_sum = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("top-level sum conserved to the cent",
           abs(new_sum - orig_sum) <= 0.25,
           f"{new_sum} vs {orig_sum}")
    _check("recoverable echoes the original sum",
           abs(ex["recoverable"] - orig_sum) <= 0.25,
           f"got {ex['recoverable']}")
    cascade = [c for c in ex["improvements"]
               if str(c["kind"]).startswith("cascade_")]
    _check("at least one cascade root cause promoted", len(cascade) >= 1,
           f"kinds={[c['kind'] for c in ex['improvements']]}")
    _check("every cascade card is priced above the floor",
           all(c["lostPotency"] >= 150.0 for c in cascade),
           f"got {[c['lostPotency'] for c in cascade]}")
    _check("every cascade card carries labelled evidence rows",
           all(c.get("evidence")
               and all({"k", "v", "note"} <= set(r) for r in c["evidence"])
               for c in cascade),
           f"got {[c.get('evidence') for c in cascade]}")
    _check("no evidence row repeats the card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in cascade for r in c.get("evidence", [])),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0,
           f"got {resid}")
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")
    _check("notes describe the move",
           ex["notes"] and "resolved into" in ex["notes"][0],
           f"got {ex['notes']}")
    # The in-place advice list still targets only original cards.
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys ⊆ original card keys", item_keys <= card_keys,
           f"extra: {item_keys - card_keys}")


def test_no_runner_degrades_to_analytic() -> None:
    print("\nTest: no runner → advice only, examined None")
    from sidecar.advice import compute_advice_v2
    ctx = _mch_ctx([(2.5 * i, DRILL) for i in range(10)], [])
    out = compute_advice_v2(ctx, [_card("residual", 0, 0.0, lost=2400.0)])
    _check("examined is None", out["examined"] is None, f"got {out}")
    _check("advice list present", isinstance(out["advice"], list), "missing")


def test_mch_heat_overcap_cause() -> None:
    print("\nTest: MCH heat-ledger overcap → delayed-Hypercharge root cause")
    from jobs.machinist.advice import _heat_overcap_cause
    hot = _mch_ctx([(2.5 * i, 7411) for i in range(30)], [])   # 150 heat, no HC
    c = _heat_overcap_cause(hot)
    _check("cause emitted on a 50-heat overflow",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == 17209, f"got {c}")
    cool = _mch_ctx([(2.5 * i, 7411) for i in range(10)], [])  # 50 heat, capped fine
    _check("no cause when the gauge never overflows",
           _heat_overcap_cause(cool) is None, "got a cause")


def test_mch_tool_drift_cause() -> None:
    print("\nTest: MCH tool drift → lost-use root cause")
    from jobs.machinist.advice import _tool_drift_causes
    ideal = [(20.0 * i, DRILL) for i in range(8)]
    late = [(30.0 * i + 1.0, DRILL) for i in range(5)]   # 10s over per gap
    causes = _tool_drift_causes(_mch_ctx(late, ideal, fight_s=160.0))
    _check("Drill lost-use cause emitted",
           any(c.ability_id == DRILL and c.kind == "cascade_lost_use"
               for c in causes), f"got {causes}")
    on_cd = [(20.0 * i, DRILL) for i in range(8)]
    _check("clean on-cooldown stream → no cause",
           _tool_drift_causes(_mch_ctx(on_cd, ideal, fight_s=160.0)) == [],
           "got causes")


def main() -> int:
    test_window_shift_finds_earlier()
    test_window_shift_full_window_silent()
    test_ogcd_weave_fit()
    test_gcd_fit_idle_then_fallback()
    test_residual_table()
    test_residual_table_surplus_not_inverted()
    test_keys_subset_of_cards()
    test_examined_conservation_and_stability()
    test_no_runner_degrades_to_analytic()
    test_mch_heat_overcap_cause()
    test_mch_tool_drift_cause()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
