"""Unit tests for the Gunbreaker deep-advice pack (jobs/gunbreaker/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean-silent stream; registration, gauge-key
validity against the real SimState, the copy lint (no em/en dashes, no
strict/lenient jargon), and the cascade conservation smoke on the GNB sim.

Run from python/:  python tests/test_gunbreaker_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.gunbreaker import data as gd
from jobs.gunbreaker.advice import (
    GAUGE_TEXT, TEXT, _cartridge_overcap_cause, _cartridge_stranded_cause,
    _cooldown_drift_causes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


_GCD_IDS = frozenset(gd.POTENCIES) - gd.OGCD_IDS


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 150.0,
         death_windows=None) -> AdviceContext:
    return AdviceContext(
        job="Gunbreaker", data=gd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=[],
        death_windows=list(death_windows or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.gunbreaker.simulator", runner=runner,
        gcd_ids=_GCD_IDS, gauge_text=GAUGE_TEXT)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_cooldown_drift_cause() -> None:
    print("\nTest: No Mercy drift -> lost-use root cause; clean stream silent")
    ideal = [(60.0 * i, gd.NO_MERCY) for i in range(7)]        # 0..360
    late = [(0.0, gd.NO_MERCY), (70.0, gd.NO_MERCY), (160.0, gd.NO_MERCY),
            (260.0, gd.NO_MERCY), (370.0, gd.NO_MERCY)]        # 5 casts, drifting
    causes = _cooldown_drift_causes(_ctx(late, ideal, fight_s=400.0))
    nm = [c for c in causes if c.ability_id == gd.NO_MERCY]
    _check("No Mercy lost-use cause emitted",
           len(nm) == 1 and nm[0].kind == "cascade_lost_use",
           f"got {causes}")
    _check("located at the worst slip (gap start 260)",
           nm[0].time_sec == 260.0, f"got {nm[0].time_sec}")
    _check("summary counts the 2 lost uses",
           "2 uses lost" in nm[0].summary, f"got {nm[0].summary!r}")
    _check("evidence carries the cast-count row",
           nm[0].evidence and nm[0].evidence[0].v == "5 / 7",
           f"got {nm[0].evidence}")
    clean = _cooldown_drift_causes(_ctx(ideal, ideal, fight_s=400.0))
    _check("clean on-cooldown stream -> no cause", clean == [],
           f"got {clean}")


def test_drift_silent_in_death_windows() -> None:
    print("\nTest: drift inside death windows attributes nothing")
    ideal = [(60.0 * i, gd.NO_MERCY) for i in range(7)]
    late = [(0.0, gd.NO_MERCY), (70.0, gd.NO_MERCY), (160.0, gd.NO_MERCY),
            (260.0, gd.NO_MERCY), (370.0, gd.NO_MERCY)]
    causes = _cooldown_drift_causes(
        _ctx(late, ideal, fight_s=400.0, death_windows=[(50.0, 400.0)]))
    _check("every slip overlaps a death window -> silent",
           all(c.ability_id != gd.NO_MERCY for c in causes),
           f"got {causes}")


def test_bloodfest_prepull_counted() -> None:
    print("\nTest: prepull Bloodfest counts toward the deficit ledger")
    # Player presses Bloodfest prepull (t<0, reconstructed) then drifts badly:
    # 5 casts total vs the sim's 7. The prepull cast must count (deficit 2,
    # not 3) but the drift ledger walks in-fight times only.
    ideal = [(60.0 * i, gd.BLOODFEST) for i in range(7)]
    player = [(-1.5, gd.BLOODFEST), (100.0, gd.BLOODFEST),
              (200.0, gd.BLOODFEST), (300.0, gd.BLOODFEST),
              (400.0, gd.BLOODFEST)]
    causes = _cooldown_drift_causes(_ctx(player, ideal, fight_s=410.0))
    bf = [c for c in causes if c.ability_id == gd.BLOODFEST]
    _check("Bloodfest cause emitted", len(bf) == 1, f"got {causes}")
    _check("count row includes the prepull cast (5 / 7)",
           bf[0].evidence[0].v == "5 / 7", f"got {bf[0].evidence}")


def test_cartridge_overcap_cause() -> None:
    print("\nTest: cartridge overcap -> delayed-spender root cause")
    # Five Solid Barrels, no spender: carts 1, 2, 3, then two overflows.
    hot = [(2.5 * (i + 1), gd.SOLID_BARREL) for i in range(5)]
    c = _cartridge_overcap_cause(_ctx(hot, []))
    _check("cause emitted on a 2-cartridge overflow",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == gd.BURST_STRIKE, f"got {c}")
    _check("located at the FIRST overflow (t=10)",
           c.time_sec == 10.0, f"got {c.time_sec}")
    _check("summary counts the waste",
           "2 cartridges wasted" in c.summary, f"got {c.summary!r}")
    _check("resources tag the Powder Gauge",
           c.resources and c.resources[0] is GAUGE_TEXT["cartridges"],
           f"got {c.resources}")
    # Clean build/spend cadence: never overflows.
    cool = [(2.5, gd.SOLID_BARREL), (5.0, gd.BURST_STRIKE),
            (7.5, gd.SOLID_BARREL), (10.0, gd.GNASHING_FANG)]
    _check("no cause when the gauge never overflows",
           _cartridge_overcap_cause(_ctx(cool, [])) is None, "got a cause")


def test_bloodfest_cap_window_mirrored() -> None:
    print("\nTest: the Bloodfest 3->6 cap window and its expiry are mirrored")
    # Bloodfest (+3, cap 6) then three Solid Barrels -> 6 carts, NO overflow
    # while the window lives; the first cast after 30s clamps 6 -> 3 (bonus
    # cartridges expire) and THAT is the overflow.
    stream = [(0.0, gd.BLOODFEST), (2.5, gd.SOLID_BARREL),
              (5.0, gd.SOLID_BARREL), (7.5, gd.SOLID_BARREL),
              (35.0, gd.KEEN_EDGE)]
    c = _cartridge_overcap_cause(_ctx(stream, []))
    _check("expiry clamp emits the overflow",
           c is not None and "3 cartridges wasted" in c.summary, f"got {c}")
    _check("located at the expiry cast (t=35)",
           c.time_sec == 35.0, f"got {c.time_sec}")
    # Spending the bonus cartridges inside the window: clean.
    spent = [(0.0, gd.BLOODFEST), (2.5, gd.SOLID_BARREL),
             (5.0, gd.SOLID_BARREL), (7.5, gd.SOLID_BARREL),
             (10.0, gd.DOUBLE_DOWN), (12.5, gd.BURST_STRIKE),
             (15.0, gd.BURST_STRIKE), (17.5, gd.BURST_STRIKE),
             (20.0, gd.BURST_STRIKE), (22.5, gd.BURST_STRIKE),
             (35.0, gd.KEEN_EDGE)]
    _check("bonus cartridges spent in the window -> no cause",
           _cartridge_overcap_cause(_ctx(spent, [])) is None, "got a cause")


def test_cartridge_stranded_cause() -> None:
    print("\nTest: cartridges stranded at the kill")
    ideal = [(50.0, gd.SOLID_BARREL), (60.0, gd.SOLID_BARREL),
             (70.0, gd.BURST_STRIKE), (75.0, gd.BURST_STRIKE)]  # ends 0
    player = [(50.0, gd.SOLID_BARREL), (60.0, gd.SOLID_BARREL)]  # ends 2
    c = _cartridge_stranded_cause(_ctx(player, ideal, fight_s=100.0))
    _check("cause emitted for 2 stranded cartridges",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == gd.BURST_STRIKE, f"got {c}")
    _check("located at the last generator (t=60)",
           c.time_sec == 60.0, f"got {c.time_sec}")
    _check("summary counts the stranding",
           "2 cartridges left" in c.summary, f"got {c.summary!r}")
    spent = player + [(70.0, gd.BURST_STRIKE), (75.0, gd.BURST_STRIKE)]
    _check("gauge spent down -> no cause",
           _cartridge_stranded_cause(_ctx(spent, ideal, fight_s=100.0))
           is None, "got a cause")


def test_stranded_guards() -> None:
    print("\nTest: stranded-cartridge guards (kill timing, ideal end state)")
    # The last cartridge landed inside the final GCD: no slot to spend it.
    edge = [(50.0, gd.SOLID_BARREL), (99.0, gd.SOLID_BARREL)]
    _check("last generator inside the final GCD -> silent",
           _cartridge_stranded_cause(_ctx(edge, [], fight_s=100.0)) is None,
           "got a cause")
    # The sim's own line ends with the same cartridges (kill mid-build).
    same = [(50.0, gd.SOLID_BARREL), (60.0, gd.SOLID_BARREL)]
    _check("ideal strands the same -> silent",
           _cartridge_stranded_cause(_ctx(same, same, fight_s=100.0)) is None,
           "got a cause")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Gunbreaker job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Gunbreaker")
    _check("resolve_pack returns the GNB pack",
           pack is not None and pack.gauge_text is GAUGE_TEXT,
           f"got {pack}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar SimState field")
    from jobs.gunbreaker.simulator import _model_for
    st = _model_for(150.0, None).init_state()
    for key in GAUGE_TEXT:
        _check(f"'{key}' exists on SimState", hasattr(st, key),
               f"missing field {key}")
        val = getattr(st, key)
        _check(f"'{key}' is a scalar (delta-comparable)",
               isinstance(val, (bool, int, float)), f"got {type(val)}")
        _check(f"'{key}' is public (snapshot-visible)",
               not key.startswith("_"), "underscore-prefixed")


def test_copy_lint() -> None:
    print("\nTest: copy rules — no em/en dashes, no jargon, no exclamations")

    def _walk(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from _walk(v)

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        strings.extend([gt.label, gt.short, gt.over_note or "",
                        gt.under_note or ""])
    for s in strings:
        _check(f"no em dash in {s[:40]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:40]!r}", "–" not in s, s)
        _check(f"no jargon in {s[:40]!r}",
               "strict" not in s.lower() and "lenient" not in s.lower(), s)
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, s)


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the GNB sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.gunbreaker.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", gd.BLASTING_ZONE, 30.0, lost=400.0,
              name="Blasting Zone"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    _check("examined payload produced", ex is not None, "got None")
    orig_sum = round(sum(c["lostPotency"] for c in cards), 1)
    new_sum = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("top-level sum conserved to the cent",
           abs(new_sum - orig_sum) <= 0.25, f"{new_sum} vs {orig_sum}")
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
    _check("no evidence row repeats the card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in cascade for r in c.get("evidence", [])),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is buff-agnostic", ex["basis"] in ("strict", "multiTarget"),
           f"got {ex['basis']}")


def test_clean_stream_emits_nothing() -> None:
    print("\nTest: the ideal stream as the player -> zero causes")
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.gunbreaker.advice import advice_probes
    from jobs.gunbreaker.simulator import SimParams, _model_for

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    items, causes = advice_probes(_ctx(ideal, ideal, fight_s=dur), [])
    _check("no ProbeItems by design", items == [], f"got {items}")
    _check("clean stream -> no causes", causes == [], f"got {causes}")


def main() -> int:
    test_cooldown_drift_cause()
    test_drift_silent_in_death_windows()
    test_bloodfest_prepull_counted()
    test_cartridge_overcap_cause()
    test_bloodfest_cap_window_mirrored()
    test_cartridge_stranded_cause()
    test_stranded_guards()
    test_registration()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_examined_conservation_and_stability()
    test_clean_stream_emits_nothing()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
