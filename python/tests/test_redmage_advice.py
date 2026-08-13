"""Unit tests for the Red Mage deep-advice pack (jobs/redmage/advice.py).

Covers the three RootCause producers (emit + clean-stream silence), the pack
registration, the GAUGE_TEXT-vs-sim-state key validity, the user-facing copy
rules, and the cascade conservation smoke on the RDM simulator.

Run from python/:  python tests/test_redmage_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, AdvicePack
from jobs.redmage import data as rd
from jobs.redmage.advice import (
    GAUGE_TEXT, TEXT, _accel_banked_cause, _cooldown_drift_causes,
    _mana_overcap_cause, advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def _gcd_ids() -> frozenset[int]:
    return frozenset(a for a in rd.POTENCIES if a not in rd.OGCD_IDS)


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
         deaths=None, downtime=None) -> AdviceContext:
    return AdviceContext(
        job="Red Mage", data=rd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.redmage.simulator", runner=runner,
        gcd_ids=_gcd_ids(), gauge_text=dict(GAUGE_TEXT))


def test_registration() -> None:
    print("\nTest: the pack registers on the Job and resolves by name")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Red Mage")
    _check("pack resolves", pack is not None, "got None")
    _check("pack is an AdvicePack", isinstance(pack, AdvicePack),
           f"got {type(pack)}")
    _check("gauge_text is the RDM glossary", pack.gauge_text is GAUGE_TEXT,
           "different object")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.redmage.simulator import _model_for
    state = _model_for(300.0, None).init_state()
    for k in GAUGE_TEXT:
        _check(f"state has {k}", hasattr(state, k), "missing attribute")
        val = getattr(state, k)
        _check(f"{k} is scalar", isinstance(val, (int, float, bool)),
               f"got {type(val)}")
    snap = _snapshot(state)
    for k in GAUGE_TEXT:
        _check(f"{k} survives the snapshot gauge filter",
               k in snap["gauges"], f"gauges={sorted(snap['gauges'])}")


def test_copy_rules() -> None:
    print("\nTest: copy rules — no em/en dashes, no strict/lenient jargon")

    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from _walk(v)
        elif isinstance(x, str):
            yield x

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        strings += [gt.label, gt.short, gt.over_note or "",
                    gt.under_note or ""]
    for s in strings:
        _check(f"no em dash in {s[:30]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:30]!r}", "–" not in s, s)
        low = s.lower()
        _check(f"no jargon in {s[:30]!r}",
               "strict" not in low and "lenient" not in low, s)


def test_cd_drift_cause() -> None:
    print("\nTest: Manafication drift → lost-use root cause")
    ideal = [(0.0, rd.MANAFICATION), (110.0, rd.MANAFICATION),
             (220.0, rd.MANAFICATION)]
    late = [(5.0, rd.MANAFICATION), (195.0, rd.MANAFICATION)]  # 80s over
    causes = _cooldown_drift_causes(_ctx(late, ideal, fight_s=300.0))
    _check("one cause emitted", len(causes) == 1, f"got {causes}")
    c = causes[0]
    _check("kind and ability",
           c.kind == "cascade_lost_use" and c.ability_id == rd.MANAFICATION,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the worst slip's start", c.time_sec == 5.0,
           f"got {c.time_sec}")
    _check("summary counts the lost use", "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence rows labelled",
           len(c.evidence) == 2 and c.evidence[0].note == "casts vs the "
           "sim's line", f"got {c.evidence}")


def test_cd_drift_silent_when_clean() -> None:
    print("\nTest: drift silence — on-cooldown play and sub-floor drift")
    ideal = [(0.0, rd.MANAFICATION), (110.0, rd.MANAFICATION),
             (220.0, rd.MANAFICATION)]
    on_cd = list(ideal)
    _check("clean on-cooldown stream → no cause",
           _cooldown_drift_causes(_ctx(on_cd, ideal, fight_s=300.0)) == [],
           "got causes")
    tiny = [(0.0, rd.MANAFICATION), (112.0, rd.MANAFICATION)]  # 2s over
    _check("deficit with sub-floor drift → silent",
           _cooldown_drift_causes(_ctx(tiny, ideal, fight_s=300.0)) == [],
           "got causes")


def test_cd_drift_death_window_suppressed() -> None:
    print("\nTest: a gap explained by a death window stays silent")
    ideal = [(0.0, rd.MANAFICATION), (110.0, rd.MANAFICATION),
             (220.0, rd.MANAFICATION)]
    late = [(5.0, rd.MANAFICATION), (195.0, rd.MANAFICATION)]
    causes = _cooldown_drift_causes(
        _ctx(late, ideal, fight_s=300.0, deaths=[(60.0, 130.0)]))
    _check("death overlap subtracted → no cause", causes == [],
           f"got {causes}")


def test_mana_overcap_cause() -> None:
    print("\nTest: mana overcap → delayed-combo root cause")
    # 30 Verthunder III: black hits 100 after 17 casts, then overflows 6/cast.
    hot = [(2.5 * i, rd.VERTHUNDER_III) for i in range(30)]
    c = _mana_overcap_cause(_ctx(hot, [], fight_s=300.0))
    _check("cause emitted", c is not None, "got None")
    _check("kind and ability",
           c.kind == "cascade_burst" and c.ability_id == rd.ENCHANTED_RIPOSTE,
           f"got {c.kind} / {c.ability_id}")
    # First >=5 overflow: cast 18 (index 17) at t=42.5.
    _check("located at the first meaningful overflow", c.time_sec == 42.5,
           f"got {c.time_sec}")
    _check("summary carries the wasted total", "mana wasted" in c.summary,
           f"got {c.summary!r}")
    _check("resources tag both gauges",
           [g.label for g in c.resources] == ["White Mana", "Black Mana"],
           f"got {c.resources}")


def test_mana_overcap_silent_when_clean() -> None:
    print("\nTest: no overflow (and free Magicked Swordplay ids) → silent")
    cool = [(2.5 * i, rd.VERTHUNDER_III) for i in range(16)]   # 96 black
    _check("near-cap without overflow → no cause",
           _mana_overcap_cause(_ctx(cool, [], fight_s=300.0)) is None,
           "got a cause")
    # A spending player: build 50/50, combo, repeat — never overcaps.
    spend: list[tuple[float, int]] = []
    t = 0.0
    for _cycle in range(6):
        for _i in range(9):                       # 9 Jolt III: +18/+18
            spend.append((t, rd.JOLT_III))
            t += 2.5
        for _i in range(6):                       # 6 x (+6) via VT3/VA3
            spend.append((t, rd.VERTHUNDER_III))
            t += 2.5
            spend.append((t, rd.VERAERO_III))
            t += 2.5
        for aid in (rd.ENCHANTED_RIPOSTE, rd.ENCHANTED_ZWERCHHAU,
                    rd.ENCHANTED_REDOUBLEMENT):
            spend.append((t, aid))
            t += 1.6
    _check("spending stream → no cause",
           _mana_overcap_cause(_ctx(spend, [], fight_s=600.0)) is None,
           "got a cause")
    # Magicked Swordplay combo ids spend nothing — and generate nothing, so a
    # free-combo stream stays silent too.
    free = [(1.6 * i, rd.ENCHANTED_RIPOSTE_M) for i in range(10)]
    _check("free-combo ids stay out of the ledger",
           _mana_overcap_cause(_ctx(free, [], fight_s=300.0)) is None,
           "got a cause")


def test_accel_banked_cause() -> None:
    print("\nTest: Acceleration charges banked → lost-use root cause")
    ideal = [(50.0 * i, rd.ACCELERATION) for i in range(6)]
    late = [(200.0, rd.ACCELERATION), (210.0, rd.ACCELERATION)]
    c = _accel_banked_cause(_ctx(late, ideal, fight_s=300.0))
    _check("cause emitted", c is not None, "got None")
    _check("kind and ability",
           c.kind == "cascade_lost_use" and c.ability_id == rd.ACCELERATION,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the longest capped stretch's start", c.time_sec == 0.0,
           f"got {c.time_sec}")
    _check("summary counts the lost uses", "4 uses lost" in c.summary,
           f"got {c.summary!r}")
    _check("capped-time evidence row",
           any(r.k == "Full" and "200" in r.v for r in c.evidence),
           f"got {c.evidence}")


def test_accel_banked_silent_when_clean() -> None:
    print("\nTest: charges kept moving → silent even with a deficit")
    ideal = [(45.0 * i, rd.ACCELERATION) for i in range(6)]
    # One charge spent every 55s: the pool refills exactly at each press, so
    # capped time never accrues; deficit alone must not speak.
    moving = [(55.0 * i, rd.ACCELERATION) for i in range(5)]
    _check("no capped time → no cause",
           _accel_banked_cause(_ctx(moving, ideal, fight_s=300.0)) is None,
           "got a cause")
    _check("no deficit → no cause",
           _accel_banked_cause(
               _ctx(list(ideal), ideal, fight_s=300.0)) is None,
           "got a cause")


def test_probes_order_and_determinism() -> None:
    print("\nTest: cause priority order and byte-for-byte determinism")
    ideal = ([(0.0, rd.MANAFICATION), (110.0, rd.MANAFICATION),
              (220.0, rd.MANAFICATION)]
             + [(50.0 * i, rd.ACCELERATION) for i in range(6)])
    stream = ([(5.0, rd.MANAFICATION), (195.0, rd.MANAFICATION)]
              + [(2.5 * i + 20.0, rd.VERTHUNDER_III) for i in range(30)])
    ctx = _ctx(stream, ideal, fight_s=300.0)
    items1, causes1 = advice_probes(ctx, [])
    items2, causes2 = advice_probes(ctx, [])
    _check("no probe items (causes only)", items1 == [], f"got {items1}")
    _check("three causes", len(causes1) == 3, f"got {len(causes1)}")
    _check("priority order: Manafication drift, overcap, Acceleration",
           [c.ability_id for c in causes1] == [rd.MANAFICATION,
                                               rd.ENCHANTED_RIPOSTE,
                                               rd.ACCELERATION],
           f"got {[c.ability_id for c in causes1]}")
    _check("deterministic across two runs", causes1 == causes2,
           "runs differ")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list — conservation, stability, cascade cards")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.redmage.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [],
                                         params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _ctx(player, ideal, fight_s=dur)
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(ctx.gcd_ids))
    cards = [_card("residual", 0, 0.0, lost=2400.0)]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2,
                                                          sort_keys=True),
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
               for c in cascade for r in c.get("evidence", []) if r["note"]),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is buff-agnostic (nothing credited)",
           ex["basis"] == "strict", f"got {ex['basis']}")


def main() -> int:
    test_registration()
    test_gauge_keys_are_real_state_fields()
    test_copy_rules()
    test_cd_drift_cause()
    test_cd_drift_silent_when_clean()
    test_cd_drift_death_window_suppressed()
    test_mana_overcap_cause()
    test_mana_overcap_silent_when_clean()
    test_accel_banked_cause()
    test_accel_banked_silent_when_clean()
    test_probes_order_and_determinism()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
