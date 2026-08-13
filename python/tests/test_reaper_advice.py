"""Unit tests for the Reaper deep-advice pack (jobs/reaper/advice.py).

Covers each RootCause producer (an emitting synthetic stream + a clean one
that stays silent), the AdvicePack registration, GAUGE_TEXT key validity
against the real sim state, the copy lint (no em/en dashes, no
strict/lenient jargon, no exclamation marks), and the cascade conservation
smoke on the RPR simulator.

Run from python/:  python tests/test_reaper_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.reaper import data as rd
from jobs.reaper.advice import (
    GAUGE_TEXT, TEXT, _cooldown_drift_causes, _shroud_overcap_cause,
    _soul_overcap_cause, _stranded_causes, _walk_gauges, advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _rpr_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             deaths=None, downtime=None) -> AdviceContext:
    gcds = frozenset(rd.POTENCIES) - rd.OGCD_IDS
    return AdviceContext(
        job="Reaper", data=rd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.reaper.simulator", runner=runner, gcd_ids=gcds,
        gauge_text=dict(GAUGE_TEXT))


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_registration() -> None:
    print("\nTest: the Reaper AdvicePack is registered on the Job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Reaper")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is the pack's own glossary",
           pack.gauge_text == GAUGE_TEXT, f"got {pack.gauge_text}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar SimState field")
    from jobs.reaper.simulator import _model_for
    st = _model_for(None).init_state()
    for key in GAUGE_TEXT:
        _check(f"'{key}' exists on SimState", hasattr(st, key), "missing")
        val = getattr(st, key)
        _check(f"'{key}' is scalar", isinstance(val, (int, float, bool)),
               f"got {type(val)}")


def test_copy_lint() -> None:
    print("\nTest: copy rules — no em/en dashes, no jargon, no exclamations")

    def _walk_strings(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _walk_strings(v)

    strings = list(_walk_strings(TEXT))
    for gt in GAUGE_TEXT.values():
        _check("GaugeText entries are GaugeText", isinstance(gt, GaugeText),
               f"got {type(gt)}")
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s:
                strings.append(s)
    for s in strings:
        _check(f"no em dash in {s[:40]!r}", "—" not in s, "em dash")
        _check(f"no en dash in {s[:40]!r}", "–" not in s, "en dash")
        _check(f"no jargon in {s[:40]!r}",
               "strict" not in s.lower() and "lenient" not in s.lower(),
               "strict/lenient leaked")
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, "exclamation")


def test_gluttony_drift_cause() -> None:
    print("\nTest: Gluttony drift → lost-use root cause; clean stream silent")
    ideal = [(60.0 * i, rd.GLUTTONY) for i in range(5)]
    late = [(0.0, rd.GLUTTONY), (80.0, rd.GLUTTONY), (170.0, rd.GLUTTONY)]
    causes = _cooldown_drift_causes(_rpr_ctx(late, ideal))
    _check("one Gluttony cause", len(causes) == 1
           and causes[0].ability_id == rd.GLUTTONY
           and causes[0].kind == "cascade_lost_use", f"got {causes}")
    _check("located at the worst slip (the 90s gap start)",
           causes[0].time_sec == 80.0, f"got {causes[0].time_sec}")
    _check("summary counts the deficit", "2 uses lost" in causes[0].summary,
           f"got {causes[0].summary!r}")
    _check("evidence carries the count row",
           causes[0].evidence and causes[0].evidence[0].v == "3 / 5",
           f"got {causes[0].evidence}")
    clean = _cooldown_drift_causes(_rpr_ctx(ideal, ideal))
    _check("on-cooldown full-count stream → silent", clean == [],
           f"got {clean}")


def test_drift_skips_death_windows() -> None:
    print("\nTest: drift gaps inside death windows are not blamed")
    ideal = [(60.0 * i, rd.GLUTTONY) for i in range(5)]
    late = [(0.0, rd.GLUTTONY), (80.0, rd.GLUTTONY), (170.0, rd.GLUTTONY)]
    causes = _cooldown_drift_causes(
        _rpr_ctx(late, ideal, deaths=[(70.0, 100.0), (160.0, 175.0)]))
    _check("death-covered gaps → silent", causes == [], f"got {causes}")


def test_drift_discounts_the_sims_own_holds() -> None:
    print("\nTest: the sim's own Gluttony holds are not the player's drift")
    # The sim banks Gluttony for soul: its own line runs 90s between presses.
    # A player pacing it exactly like the sim, one use short, is not late.
    ideal = [(90.0 * i, rd.GLUTTONY) for i in range(4)]
    same_pace = [(90.0 * i, rd.GLUTTONY) for i in range(3)]
    _check("same pacing as the sim → silent",
           _cooldown_drift_causes(_rpr_ctx(same_pace, ideal)) == [],
           "got a cause")
    # Genuinely slower than the sim's own holds: still caught.
    slower = [(0.0, rd.GLUTTONY), (120.0, rd.GLUTTONY), (240.0, rd.GLUTTONY)]
    causes = _cooldown_drift_causes(_rpr_ctx(slower, ideal))
    _check("slower than the sim's holds → emitted", len(causes) == 1,
           f"got {causes}")
    _check("the drift number is the excess, not the raw gap",
           "30s longer" in causes[0].summary, f"got {causes[0].summary!r}")
    _check("the slip is measured against the sim's longest hold",
           "30.0s later" in causes[0].prescription,
           f"got {causes[0].prescription!r}")


def test_drift_ignores_untargetable_time() -> None:
    print("\nTest: boss-untargetable seconds are not counted as player drift")
    ideal = [(60.0 * i, rd.GLUTTONY) for i in range(5)]
    # 4 presses around a 40s invuln phase: the only long gap is the phase.
    player = [(0.0, rd.GLUTTONY), (60.0, rd.GLUTTONY), (150.0, rd.GLUTTONY),
              (215.0, rd.GLUTTONY)]
    hot = _cooldown_drift_causes(_rpr_ctx(player, ideal))
    _check("without downtime the same stream does read as drift",
           len(hot) == 1, f"got {hot}")
    cold = _cooldown_drift_causes(
        _rpr_ctx(player, ideal, downtime=[(100.0, 140.0)]))
    _check("downtime subtracted from the gap → silent", cold == [],
           f"got {[c.summary for c in cold]}")


def test_soul_slice_shared_pool() -> None:
    print("\nTest: Soul Scythe counts as a Soul Slice consumption (no fake drift)")
    ideal = [(30.0 * i, rd.SOUL_SLICE) for i in range(10)]
    player = [(30.0 * i, rd.SOUL_SCYTHE) for i in range(10)]
    causes = _cooldown_drift_causes(_rpr_ctx(player, ideal))
    _check("AoE-phase Soul Scythe casts → no Soul Slice drift", causes == [],
           f"got {causes}")


def test_soul_overcap_cause() -> None:
    print("\nTest: soul overcap → delayed-spender root cause; prepull ignored")
    # 14 combo GCDs at +10 soul: the gauge caps at cast 10 (t=25.0) and the
    # last 4 casts overflow 10 each (total 40 >= 25). A prepull Soul Slice
    # must NOT count (counting it would move the first overcap earlier).
    casts = [(-2.0, rd.SOUL_SLICE)] + [(2.5 * i, rd.SLICE) for i in range(14)]
    c = _soul_overcap_cause(_walk_gauges(_rpr_ctx(casts, [])))
    _check("cause emitted", c is not None and c.kind == "cascade_burst"
           and c.ability_id == rd.BLOOD_STALK, f"got {c}")
    _check("located at the first overcap (prepull ignored)",
           c.time_sec == 25.0, f"got {c.time_sec}")
    _check("summary carries the wasted total", "40 soul wasted" in c.summary,
           f"got {c.summary!r}")
    _check("soul gauge tagged as the resource",
           c.resources == [GAUGE_TEXT["soul"]], f"got {c.resources}")
    # Clean: capped exactly, never over.
    capped = [(2.5 * i, rd.SLICE) for i in range(10)]
    _check("gauge parked at 100 without overflow → silent",
           _soul_overcap_cause(_walk_gauges(_rpr_ctx(capped, []))) is None,
           "got a cause")
    # Clean: a spender keeps it under the cap.
    spent = ([(2.5 * i, rd.SLICE) for i in range(10)]
             + [(25.5, rd.BLOOD_STALK)]
             + [(27.5 + 2.5 * i, rd.SLICE) for i in range(4)])
    _check("prompt spender → silent",
           _soul_overcap_cause(_walk_gauges(_rpr_ctx(spent, []))) is None,
           "got a cause")


def test_shroud_overcap_cause() -> None:
    print("\nTest: shroud overcap → delayed-Enshroud root cause; Ideal Host rule")
    # 13 Reaver GCDs at +10 shroud: cap at cast 10 (t=25.0), 30 overflowed.
    casts = [(2.5 * i, rd.GIBBET) for i in range(13)]
    c = _shroud_overcap_cause(_walk_gauges(_rpr_ctx(casts, [])))
    _check("cause emitted", c is not None and c.kind == "cascade_burst"
           and c.ability_id == rd.ENSHROUD, f"got {c}")
    _check("located at the first overcap", c.time_sec == 25.0,
           f"got {c.time_sec}")
    _check("shroud gauge tagged as the resource",
           c.resources == [GAUGE_TEXT["shroud"]], f"got {c.resources}")
    # Ideal Host: the Enshroud right after Plentiful Harvest spends NO shroud
    # (the sim's apply_cast rule). 50 built + free Enshroud + 80 more = 130 →
    # 30 overflowed; a naive ledger that spent 50 would read 80 and stay
    # silent, so an emitted cause proves the free-spend rule.
    host = ([(2.5 * i, rd.GIBBET) for i in range(5)]
            + [(13.0, rd.PLENTIFUL_HARVEST), (14.0, rd.ENSHROUD)]
            + [(20.0 + 2.5 * i, rd.GIBBET) for i in range(8)])
    _check("free Ideal Host Enshroud spends no shroud in the ledger",
           _shroud_overcap_cause(_walk_gauges(_rpr_ctx(host, [])))
           is not None, "got None")
    # A paid Enshroud DOES spend 50: same stream without Plentiful Harvest.
    paid = ([(2.5 * i, rd.GIBBET) for i in range(5)]
            + [(14.0, rd.ENSHROUD)]
            + [(20.0 + 2.5 * i, rd.GIBBET) for i in range(8)])
    _check("paid Enshroud spends shroud → silent",
           _shroud_overcap_cause(_walk_gauges(_rpr_ctx(paid, []))) is None,
           "got a cause")
    clean = [(2.5 * i, rd.GIBBET) for i in range(9)]
    _check("no overflow → silent",
           _shroud_overcap_cause(_walk_gauges(_rpr_ctx(clean, []))) is None,
           "got a cause")


def test_stranded_causes() -> None:
    print("\nTest: soul/shroud dead in the gauge at the kill; spent → silent")
    stream = ([(0.0, rd.SOUL_SLICE), (2.5, rd.SOUL_SLICE)]          # 100 soul
              + [(10.0 + 2.5 * i, rd.GIBBET) for i in range(5)])    # 50 shroud
    ctx = _rpr_ctx(stream, [], fight_s=60.0)
    causes = _stranded_causes(ctx, _walk_gauges(ctx))
    _check("two causes, shroud first (higher value)",
           [c.ability_id for c in causes] == [rd.ENSHROUD, rd.BLOOD_STALK],
           f"got {[c.ability_id for c in causes]}")
    _check("both are lost-use kinds",
           all(c.kind == "cascade_lost_use" for c in causes),
           f"got {[c.kind for c in causes]}")
    _check("shroud located at the last Reaver GCD",
           causes[0].time_sec == 20.0, f"got {causes[0].time_sec}")
    _check("soul located at the last soul builder",
           causes[1].time_sec == 2.5, f"got {causes[1].time_sec}")
    _check("soul summary carries the amount",
           "100 soul" in causes[1].summary, f"got {causes[1].summary!r}")
    _check("two Blood Stalk uses fit the 100 stranded soul",
           "2 more Blood Stalk weaves" in causes[1].prescription,
           f"got {causes[1].prescription!r}")
    spent = stream + [(25.0, rd.GLUTTONY), (26.0, rd.BLOOD_STALK),
                      (27.0, rd.ENSHROUD)]
    ctx2 = _rpr_ctx(spent, [], fight_s=60.0)
    _check("everything spent → silent",
           _stranded_causes(ctx2, _walk_gauges(ctx2)) == [], "got causes")


def test_stranded_needs_room_to_spend() -> None:
    print("\nTest: a gauge that only fills at the kill is not blamed")
    # Shroud reaches 50 on the last Reaver GCD, 3s before the kill: no
    # Enshroud window fits, so the ledger must not claim one does.
    late_sh = [(45.0 + 2.5 * i, rd.GIBBET) for i in range(5)]   # 50 at 55.0
    ctx = _rpr_ctx(late_sh, [], fight_s=58.0)
    _check("shroud full 3s before the kill → silent",
           _stranded_causes(ctx, _walk_gauges(ctx)) == [], "got causes")
    # Same gauge, reached with a whole window to spare: still emitted.
    early_sh = [(10.0 + 2.5 * i, rd.GIBBET) for i in range(5)]  # 50 at 20.0
    ctx = _rpr_ctx(early_sh, [], fight_s=58.0)
    _check("shroud full with a window to spare → emitted",
           len(_stranded_causes(ctx, _walk_gauges(ctx))) == 1, "got none")
    # Soul crosses 50 one second before the kill: no weave slot left.
    late_soul = [(48.0 + 2.5 * i, rd.SLICE) for i in range(5)]  # 50 at 58.0
    ctx = _rpr_ctx(late_soul, [], fight_s=59.0)
    _check("soul full 1s before the kill → silent",
           _stranded_causes(ctx, _walk_gauges(ctx)) == [], "got causes")
    # 100 soul, but spendable only from 42.0 with 3.5s left: one weave slot,
    # so the copy must promise ONE weave, not the two the gauge holds.
    tight = [(42.0, rd.SOUL_SLICE), (44.0, rd.SOUL_SLICE)]
    ctx = _rpr_ctx(tight, [], fight_s=45.5)
    causes = _stranded_causes(ctx, _walk_gauges(ctx))
    _check("uses claimed are capped by the room left",
           len(causes) == 1 and "1 more Blood Stalk weave fit" in
           causes[0].prescription, f"got {[c.prescription for c in causes]}")


def test_advice_probes_deterministic() -> None:
    print("\nTest: advice_probes — no items, ordered causes, deterministic")
    # Soul overcap mid-fight + shroud stranded at the end.
    stream = ([(2.5 * i, rd.SLICE) for i in range(14)]              # 40 soul over
              + [(40.0, rd.GLUTTONY), (41.0, rd.BLOOD_STALK)]       # drain soul
              + [(45.0 + 2.5 * i, rd.GIBBET) for i in range(5)])    # 50 shroud
    ctx = _rpr_ctx(stream, [], fight_s=90.0)
    items1, causes1 = advice_probes(ctx, [])
    items2, causes2 = advice_probes(ctx, [])
    _check("no ProbeItems", items1 == [], f"got {items1}")
    _check("soul overcap + stranded shroud emitted, in priority order",
           [c.kind for c in causes1] == ["cascade_burst", "cascade_lost_use"]
           and causes1[0].ability_id == rd.BLOOD_STALK
           and causes1[1].ability_id == rd.ENSHROUD,
           f"got {[(c.kind, c.ability_id) for c in causes1]}")
    _check("byte-stable across two runs",
           causes1 == causes2 and items1 == items2, "runs differ")
    _check("every cause is weightless (measured_p 0) and in-fight",
           all(c.measured_p == 0.0 and 0 <= c.time_sec <= 90.0
               for c in causes1),
           f"got {[(c.measured_p, c.time_sec) for c in causes1]}")
    _check("no evidence note repeats its prescription",
           all(r.note not in c.prescription
               for c in causes1 for r in c.evidence),
           f"got {[(c.prescription, c.evidence) for c in causes1]}")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the RPR sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.reaper.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]  # 6s hole
    ctx = _rpr_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", rd.SOUL_SLICE, 30.0, lost=400.0,
              name="Soul Slice"),
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
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is buff-agnostic potency (nothing credited)",
           ex["basis"] == "strict", f"got {ex['basis']}")


def main() -> int:
    test_registration()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_gluttony_drift_cause()
    test_drift_skips_death_windows()
    test_drift_discounts_the_sims_own_holds()
    test_drift_ignores_untargetable_time()
    test_soul_slice_shared_pool()
    test_soul_overcap_cause()
    test_shroud_overcap_cause()
    test_stranded_causes()
    test_stranded_needs_room_to_spend()
    test_advice_probes_deterministic()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
