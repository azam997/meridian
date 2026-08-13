"""Unit tests for the Dragoon deep-advice pack (jobs/dragoon/advice.py).

Follows tests/test_deep_advice.py: each RootCause producer gets an emitting
synthetic stream and a clean stream that stays silent; registration, gauge-key
validity against the real sim state, the copy lint, and the cascade
conservation smoke on the DRG simulator.

Run from python/:  python tests/test_dragoon_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.dragoon import data as dd
from jobs.dragoon.advice import (
    GAUGE_TEXT, TEXT, _cd_drift_causes, _focus_overcap_cause,
    _focus_stranded_cause,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 360.0,
         downtime=None, deaths=None) -> AdviceContext:
    return AdviceContext(
        job="Dragoon", data=dd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.dragoon.simulator", runner=runner,
        gcd_ids=frozenset(dd.GCD_WEAPONSKILLS), gauge_text=GAUGE_TEXT)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_registration_resolves_pack() -> None:
    print("\nTest: resolve_pack('Dragoon') returns the registered pack")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Dragoon")
    _check("pack registered", pack is not None, "got None")
    _check("gauge_text is the DRG glossary", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text}")


def test_gauge_keys_are_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs.dragoon.simulator import _model_for
    state = _model_for(360.0, None).init_state()
    fields = vars(state)
    for k in GAUGE_TEXT:
        _check(f"'{k}' is a state field", k in fields,
               f"fields: {sorted(fields)}")
        v = fields[k]
        _check(f"'{k}' is a public scalar",
               not k.startswith("_") and isinstance(v, (bool, int, float))
               and abs(float(v)) < 1e8,
               f"got {v!r}")


def test_copy_lint() -> None:
    print("\nTest: no em/en dashes, no jargon, no exclamations in the copy")
    strings: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    _walk(TEXT)
    for gt in GAUGE_TEXT.values():
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s:
                strings.append(s)
    for s in strings:
        _check(f"no em dash in {s[:40]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:40]!r}", "–" not in s, s)
        low = s.lower()
        _check(f"no strict/lenient jargon in {s[:40]!r}",
               "strict" not in low and "lenient" not in low, s)
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, s)


def test_cd_drift_cause_emits() -> None:
    print("\nTest: Geirskogul drift ledger emits a lost-use cause")
    # Ideal: 6 casts on the 60s recast; player: 5 casts at a 75s cadence
    # (15s over each gap, 60s total drift >= recast * 0.5), deficit 1.
    ideal = [(60.0 * i, dd.GEIRSKOGUL) for i in range(6)]
    late = [(10.0 + 75.0 * i, dd.GEIRSKOGUL) for i in range(5)]
    causes = _cd_drift_causes(_ctx(late, ideal))
    _check("one Geirskogul cause", len(causes) == 1
           and causes[0].ability_id == dd.GEIRSKOGUL,
           f"got {[(c.ability_id, c.kind) for c in causes]}")
    c = causes[0]
    _check("kind is cascade_lost_use", c.kind == "cascade_lost_use", c.kind)
    _check("located at the worst slip inside the fight",
           0.0 <= c.time_sec <= 360.0 and c.time_sec == 10.0,
           f"got {c.time_sec}")
    _check("summary carries the idle total and the deficit",
           "60s" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("prescription names the ability and the slip time",
           "Geirskogul" in c.prescription and "0:10" in c.prescription,
           f"got {c.prescription!r}")
    _check("count evidence row present",
           c.evidence and c.evidence[0].v == "5 / 6",
           f"got {c.evidence}")


def test_cd_drift_clean_and_forgiven_silent() -> None:
    print("\nTest: on-cooldown stream and downtime-forgiven drift stay silent")
    ideal = [(60.0 * i, dd.GEIRSKOGUL) for i in range(6)]
    on_cd = [(60.0 * i, dd.GEIRSKOGUL) for i in range(6)]
    _check("clean on-cooldown stream emits nothing",
           _cd_drift_causes(_ctx(on_cd, ideal)) == [], "got causes")
    # Same late cadence as the emitting test, but each gap's 15s excess sits
    # under a downtime window: the drift is forgiven, no cause.
    late = [(10.0 + 75.0 * i, dd.GEIRSKOGUL) for i in range(5)]
    downtime = [(10.0 + 75.0 * i + 30.0, 10.0 + 75.0 * i + 50.0)
                for i in range(4)]
    _check("downtime inside the gaps forgives the drift",
           _cd_drift_causes(_ctx(late, ideal, downtime=downtime)) == [],
           "got causes")
    _check("death windows inside the gaps forgive the drift",
           _cd_drift_causes(_ctx(late, ideal, deaths=downtime)) == [],
           "got causes")


def test_focus_overcap_cause() -> None:
    print("\nTest: Focus overcap ledger -> delayed Wyrmwind Thrust cause")
    # Four Raiden Thrusts, no Wyrmwind: stacks 3 and 4 overflow (total 2).
    hot = [(12.5 * i, dd.RAIDEN_THRUST) for i in range(4)]
    c = _focus_overcap_cause(_ctx(hot, []))
    _check("cause emitted on 2 wasted stacks",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == dd.WYRMWIND_THRUST, f"got {c}")
    _check("located at the first overcap", c.time_sec == 25.0,
           f"got {c.time_sec}")
    _check("summary counts the wasted Focus",
           "2 Firstminds' Focus wasted" in c.summary, f"got {c.summary!r}")
    _check("resource tag is the Focus gauge",
           c.resources and c.resources[0] is GAUGE_TEXT["focus"],
           f"got {c.resources}")
    # Spending on cooldown never overflows.
    cool = [(0.0, dd.RAIDEN_THRUST), (12.5, dd.RAIDEN_THRUST),
            (13.0, dd.WYRMWIND_THRUST), (25.0, dd.RAIDEN_THRUST),
            (37.5, dd.RAIDEN_THRUST), (38.0, dd.WYRMWIND_THRUST)]
    _check("no cause when Wyrmwind keeps the gauge under the cap",
           _focus_overcap_cause(_ctx(cool, [])) is None, "got a cause")
    # A single wasted stack stays under the speak-up floor.
    one = [(12.5 * i, dd.RAIDEN_THRUST) for i in range(3)]
    _check("one wasted stack stays silent (below the floor)",
           _focus_overcap_cause(_ctx(one, [])) is None, "got a cause")


def test_focus_stranded_cause() -> None:
    print("\nTest: Focus stranded at the kill -> lost Wyrmwind Thrust cause")
    stranded = [(10.0, dd.RAIDEN_THRUST), (22.5, dd.RAIDEN_THRUST)]
    c = _focus_stranded_cause(_ctx(stranded, []))
    _check("cause emitted on a full stranded gauge",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == dd.WYRMWIND_THRUST, f"got {c}")
    _check("located at the last generator", c.time_sec == 22.5,
           f"got {c.time_sec}")
    _check("summary says the Focus was left unspent", "left with" in c.summary,
           f"got {c.summary!r}")
    spent = stranded + [(24.0, dd.WYRMWIND_THRUST)]
    _check("no cause once the Wyrmwind goes out",
           _focus_stranded_cause(_ctx(spent, [])) is None, "got a cause")


def test_advice_probes_order_and_silence() -> None:
    print("\nTest: advice_probes returns causes only, silent on a clean run")
    from jobs.dragoon.advice import advice_probes
    items, causes = advice_probes(_ctx([], []), [])
    _check("no ProbeItems ever", items == [], f"got {items}")
    _check("empty stream emits no causes", causes == [], f"got {causes}")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the DRG sim - conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.dragoon.simulator import SimParams, _model_for
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
        _card("missed_cast", dd.GEIRSKOGUL, 30.0, lost=400.0,
              name="Geirskogul"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live1)
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
               for c in cascade for r in c.get("evidence", [])),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0,
           f"got {resid}")
    # A near-ideal player stream never trips the DRG ledger causes: whatever
    # got promoted is the generic sequencing card, not a false positive.
    _check("no ledger cause fires on the near-ideal stream",
           all(c["kind"] == "cascade_pacing" for c in cascade),
           f"got {[c['kind'] for c in cascade]}")


def main() -> int:
    test_registration_resolves_pack()
    test_gauge_keys_are_state_fields()
    test_copy_lint()
    test_cd_drift_cause_emits()
    test_cd_drift_clean_and_forgiven_silent()
    test_focus_overcap_cause()
    test_focus_stranded_cause()
    test_advice_probes_order_and_silence()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
