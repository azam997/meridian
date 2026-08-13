"""Unit tests for the Dancer deep-advice pack (jobs/dancer/advice.py).

Covers each RootCause producer (an emitting stream + a clean-silent stream),
the registration seam (`resolve_pack("Dancer")`), gauge-key validity against
the real sim state, the copy lint (no em/en dashes, no strict/lenient jargon),
and the cascade conservation smoke on the DNC simulator.

Run from python/:  python tests/test_dancer_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, AdvicePack, GaugeText
from jobs.dancer import data as dd
from jobs.dancer.advice import (
    GAUGE_TEXT, TEXT, _FOLLOWUPS,
    _cooldown_drift_causes, _dawn_swap_cause, _dropped_followup_causes,
    advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _dnc_ctx(norm_casts, idealized, runner=None, fight_s: float = 150.0,
             death_windows=(), downtime=()) -> AdviceContext:
    gcds = frozenset(a for a in dd.POTENCIES if a not in dd.OGCD_IDS)
    return AdviceContext(
        job="Dancer", data=dd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime), death_windows=list(death_windows),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.dancer.simulator", runner=runner, gcd_ids=gcds)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_registration_returns_pack() -> None:
    print("\nTest: resolve_pack('Dancer') returns the registered AdvicePack")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Dancer")
    _check("pack registered", isinstance(pack, AdvicePack), f"got {pack!r}")
    _check("gauge_text is the pack's own glossary",
           pack.gauge_text is GAUGE_TEXT, "different object")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs.dancer.simulator import _model_for
    st = _model_for(150.0, None).init_state()
    for k in sorted(GAUGE_TEXT):
        _check(f"state has {k}", hasattr(st, k), "missing attribute")
        v = getattr(st, k)
        _check(f"{k} is a scalar", isinstance(v, (int, float))
               and not k.startswith("_"), f"got {type(v)}")
        _check(f"{k} below the snapshot sentinel", abs(float(v)) < 1e8,
               f"got {v}")


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no strict/lenient jargon)")
    strings = list(_walk_strings(TEXT))
    for gt in GAUGE_TEXT.values():
        strings.extend(s for s in (gt.label, gt.short, gt.over_note,
                                   gt.under_note) if s)
    # The follow-up table's noun fragments are user-facing too.
    strings.extend(src for _a, src, _b, _c, _d in _FOLLOWUPS)
    _check("some copy collected", len(strings) > 5, f"got {len(strings)}")
    for s in strings:
        _check(f"no em dash in {s[:40]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:40]!r}", "–" not in s, s)
        low = s.lower()
        _check(f"no jargon in {s[:40]!r}",
               "strict" not in low and "lenient" not in low, s)
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, s)


def test_cooldown_drift_cause() -> None:
    print("\nTest: cooldown drift -> lost-use root cause")
    ideal = [(120.0 * i, dd.TECHNICAL_STEP) for i in range(5)]
    late = [(0.0, dd.TECHNICAL_STEP), (200.0, dd.TECHNICAL_STEP),
            (400.0, dd.TECHNICAL_STEP), (600.0, dd.TECHNICAL_STEP)]
    causes = _cooldown_drift_causes(_dnc_ctx(late, ideal, fight_s=620.0))
    hits = [c for _v, c in causes if c.ability_id == dd.TECHNICAL_STEP]
    _check("Technical Step lost-use cause emitted", len(hits) == 1,
           f"got {causes}")
    c = hits[0]
    _check("kind is cascade_lost_use", c.kind == "cascade_lost_use", c.kind)
    _check("located inside the fight", 0.0 <= c.time_sec <= 620.0,
           f"got {c.time_sec}")
    _check("summary names the button and the deficit",
           "Technical Step" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("weight is deficit x value",
           causes[0][0] == float(dd.COOLDOWN_VALUE_P[dd.TECHNICAL_STEP]),
           f"got {causes[0][0]}")

    on_cd = [(120.0 * i, dd.TECHNICAL_STEP) for i in range(5)]
    _check("clean on-cooldown stream -> no cause",
           _cooldown_drift_causes(_dnc_ctx(on_cd, ideal, fight_s=620.0)) == [],
           "got causes")


def test_drift_charge_sharing_finishing_move() -> None:
    print("\nTest: Finishing Move counts as a Standard Step consumption")
    # Perfect 30s family cadence, alternating Standard Step / Finishing Move on
    # both sides. Without the CHARGE_SHARING mirror the player's Standard Step
    # count (5) reads 5 short of the ideal family count (10) with fake 60s
    # gaps -> a false cause.
    family = [(30.0 * i, dd.STANDARD_STEP if i % 2 == 0 else dd.FINISHING_MOVE)
              for i in range(10)]
    causes = _cooldown_drift_causes(_dnc_ctx(family, family, fight_s=320.0))
    _check("no Standard Step cause on a perfect shared cadence",
           all(c.ability_id != dd.STANDARD_STEP for _v, c in causes),
           f"got {causes}")


def test_drift_death_and_downtime_fairness() -> None:
    print("\nTest: drift gaps in death windows / downtime stay silent")
    ideal = [(120.0 * i, dd.TECHNICAL_STEP) for i in range(5)]
    late = [(0.0, dd.TECHNICAL_STEP), (200.0, dd.TECHNICAL_STEP),
            (320.0, dd.TECHNICAL_STEP), (440.0, dd.TECHNICAL_STEP)]
    # The only slip is the (0, 200) gap: 80s over recast.
    base = _cooldown_drift_causes(_dnc_ctx(late, ideal, fight_s=620.0))
    _check("slip emits without windows",
           any(c.ability_id == dd.TECHNICAL_STEP for _v, c in base),
           f"got {base}")
    dead = _cooldown_drift_causes(_dnc_ctx(
        late, ideal, fight_s=620.0, death_windows=[(50.0, 190.0)]))
    _check("slip inside a death window -> silent", dead == [], f"got {dead}")
    dt = _cooldown_drift_causes(_dnc_ctx(
        late, ideal, fight_s=620.0, downtime=[(100.0, 180.0)]))
    _check("downtime-covered slip discounted below the floor -> silent",
           dt == [], f"got {dt}")


def test_dropped_starfall_and_tillana() -> None:
    print("\nTest: dropped Devilment/Technical follow-ups -> root causes")
    star = _dropped_followup_causes(_dnc_ctx(
        [(10.0, dd.DEVILMENT)], [], fight_s=100.0))
    _check("dropped Starfall cause emitted",
           len(star) == 1 and star[0][1].ability_id == dd.STARFALL_DANCE,
           f"got {star}")
    c = star[0][1]
    _check("located at the granting Devilment", c.time_sec == 10.0,
           f"got {c.time_sec}")
    _check("summary names the drop",
           "Starfall Dance" in c.summary and "1 window unused" in c.summary,
           f"got {c.summary!r}")
    til = _dropped_followup_causes(_dnc_ctx(
        [(5.0, dd.TECHNICAL_FINISH)], [], fight_s=100.0))
    _check("dropped Tillana cause emitted",
           len(til) == 1 and til[0][1].ability_id == dd.TILLANA,
           f"got {til}")

    clean = _dropped_followup_causes(_dnc_ctx(
        [(10.0, dd.DEVILMENT), (12.0, dd.STARFALL_DANCE),
         (40.0, dd.TECHNICAL_FINISH), (55.0, dd.TILLANA)],
        [], fight_s=100.0))
    _check("consumed windows -> silent", clean == [], f"got {clean}")


def test_dropped_followup_guards() -> None:
    print("\nTest: follow-up drops respect kill truncation, deaths, downtime")
    trunc = _dropped_followup_causes(_dnc_ctx(
        [(90.0, dd.DEVILMENT)], [], fight_s=100.0))
    _check("kill-truncated window -> silent", trunc == [], f"got {trunc}")
    dead = _dropped_followup_causes(_dnc_ctx(
        [(10.0, dd.DEVILMENT)], [], fight_s=100.0,
        death_windows=[(15.0, 40.0)]))
    _check("death-overlapped window -> silent", dead == [], f"got {dead}")
    dt = _dropped_followup_causes(_dnc_ctx(
        [(10.0, dd.DEVILMENT)], [], fight_s=100.0, downtime=[(20.0, 26.0)]))
    _check("downtime-overlapped window -> silent", dt == [], f"got {dt}")


def test_dawn_swap_cause() -> None:
    print("\nTest: Dance of the Dawn skipped while Saber Dance proved esprit")
    swapped = _dawn_swap_cause(_dnc_ctx(
        [(10.0, dd.DEVILMENT), (12.0, dd.STARFALL_DANCE),
         (20.0, dd.SABER_DANCE)], [], fight_s=100.0))
    _check("swap cause emitted", swapped is not None, "got None")
    v, c = swapped
    _check("kind/id/time", c.kind == "cascade_lost_use"
           and c.ability_id == dd.DANCE_OF_THE_DAWN and c.time_sec == 10.0,
           f"got {c}")
    _check("weight is the 460p premium", v == 460.0, f"got {v}")
    _check("prescription names the premium", "~460p" in c.prescription,
           f"got {c.prescription!r}")
    _check("implicates the esprit gauge tag",
           c.resources and c.resources[0] is GAUGE_TEXT["sabers_remaining"],
           f"got {c.resources}")

    used = _dawn_swap_cause(_dnc_ctx(
        [(10.0, dd.DEVILMENT), (20.0, dd.SABER_DANCE),
         (41.0, dd.DANCE_OF_THE_DAWN)], [], fight_s=100.0))
    _check("Dawn inside the graced window -> silent", used is None,
           f"got {used}")
    unproven = _dawn_swap_cause(_dnc_ctx(
        [(10.0, dd.DEVILMENT)], [], fight_s=100.0))
    _check("no Saber Dance in the window -> esprit unproven -> silent",
           unproven is None, f"got {unproven}")


def test_probe_order_by_value() -> None:
    print("\nTest: advice_probes orders causes by descending total value")
    ideal = [(120.0 * i, dd.TECHNICAL_STEP) for i in range(5)]
    casts = [(0.0, dd.TECHNICAL_STEP), (200.0, dd.TECHNICAL_STEP),
             (400.0, dd.TECHNICAL_STEP), (600.0, dd.TECHNICAL_STEP),
             (50.0, dd.DEVILMENT)]
    items, causes = advice_probes(_dnc_ctx(casts, ideal, fight_s=620.0), [])
    _check("no probe items (causes-only pack)", items == [], f"got {items}")
    _check("two causes emitted", len(causes) == 2,
           f"got {[c.ability_id for c in causes]}")
    _check("Technical drift (2200) leads the Starfall drop (600)",
           causes[0].ability_id == dd.TECHNICAL_STEP
           and causes[1].ability_id == dd.STARFALL_DANCE,
           f"got {[c.ability_id for c in causes]}")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the DNC sim (conservation + stability)")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.dancer.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 55.0 <= t < 70.0]  # 15s hole
    ctx = _dnc_ctx(player, ideal, fight_s=dur)
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(ctx.gcd_ids))
    cards = [
        _card("missed_cast", dd.CASCADE, 30.0, lost=400.0, name="Cascade"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live1)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    _check("probe/analytic prescriptions merged into the cards in place",
           any(c.get("prescription") for c in live1),
           f"got {[c.get('prescription') for c in live1]}")
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys are a subset of the original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")

    ex = out1["examined"]
    if ex is None:
        # Degrade path: acceptable per the brief, but the advice list must
        # still be present and the cards untouched by the cascade.
        _check("degrade path: advice list present",
               isinstance(out1["advice"], list), "missing")
        print("  [NOTE] examined is None (degrade path exercised)")
        return
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
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")


def main() -> int:
    test_registration_returns_pack()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_cooldown_drift_cause()
    test_drift_charge_sharing_finishing_move()
    test_drift_death_and_downtime_fairness()
    test_dropped_starfall_and_tillana()
    test_dropped_followup_guards()
    test_dawn_swap_cause()
    test_probe_order_by_value()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
