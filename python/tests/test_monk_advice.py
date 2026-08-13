"""Unit tests for the Monk deep-advice pack (jobs/monk/advice.py).

Covers the three RootCause producers (emit + clean-silent for each), the
registry wiring, the GAUGE_TEXT allowlist validity against the real sim
state, the copy rules, and the cascade conservation smoke.

NOTE on the smoke: `jobs.monk.simulator._model_for` takes
`(duration_s, sim_context)` — the second convention the counterfactual
resolver probes, and the only one MNK can use (it needs the duration to size
its chakra budget). Both cascade tests therefore drive the runner with a
PRODUCTION-shaped sim_context (`None`, and the CeilingContext wrapping a
MonkCtx that `sidecar/main.py::_user_sim_context` really hands it) and assert
a real `examined` payload, never the degrade path.

Run from python/:  python tests/test_monk_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.monk import data as md
from jobs.monk.advice import (
    GAUGE_TEXT, TEXT, _botched_blitz_cause, _cooldown_drift_causes,
    _stranded_blitz_cause, advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ctx(casts, ideal, fight_s: float = 160.0, downtime=None, deaths=None,
         runner=None, gcd_ids=frozenset()):
    return AdviceContext(
        job="Monk", data=md.JOB_DATA,
        norm_casts=list(casts), idealized=list(ideal),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.monk.simulator", runner=runner,
        gcd_ids=gcd_ids, gauge_text=dict(GAUGE_TEXT))


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


_ROF_IDEAL = [(0.5, md.RIDDLE_OF_FIRE), (60.5, md.RIDDLE_OF_FIRE),
              (120.5, md.RIDDLE_OF_FIRE)]


def test_cooldown_drift_cause() -> None:
    print("\nTest: burst-cooldown drift -> lost-use root cause")
    # RoF at 0.5 then 95.5 (35s over the 60s recast), sim fit 3 -> deficit 1.
    late = [(0.5, md.RIDDLE_OF_FIRE), (95.5, md.RIDDLE_OF_FIRE)]
    causes = _cooldown_drift_causes(_ctx(late, _ROF_IDEAL))
    _check("Riddle of Fire lost-use cause emitted",
           len(causes) == 1 and causes[0].ability_id == md.RIDDLE_OF_FIRE
           and causes[0].kind == "cascade_lost_use", f"got {causes}")
    c = causes[0]
    _check("located at the worst slip's start", c.time_sec == 0.5,
           f"got {c.time_sec}")
    _check("summary carries the idle total and the deficit",
           "35s" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("prescription names the slip time",
           "1:35" not in c.prescription and "0:00" in c.prescription
           and "35.0s late" in c.prescription, f"got {c.prescription!r}")
    _check("evidence: count row + idle row",
           len(c.evidence) == 2 and c.evidence[0].v == "2 / 3",
           f"got {c.evidence}")
    _check("clean on-cooldown stream stays silent",
           _cooldown_drift_causes(_ctx(_ROF_IDEAL, _ROF_IDEAL)) == [],
           "got causes")


def test_cooldown_drift_respects_downtime_and_deaths() -> None:
    print("\nTest: drift gaps covered by downtime / death windows stay silent")
    late = [(0.5, md.RIDDLE_OF_FIRE), (95.5, md.RIDDLE_OF_FIRE)]
    dt = _cooldown_drift_causes(_ctx(late, _ROF_IDEAL,
                                     downtime=[(55.0, 95.0)]))
    _check("downtime-covered gap is not drift", dt == [], f"got {dt}")
    de = _cooldown_drift_causes(_ctx(late, _ROF_IDEAL,
                                     deaths=[(55.0, 95.0)]))
    _check("death-covered gap is not drift (the death card owns it)",
           de == [], f"got {de}")


def test_botched_blitz_cause() -> None:
    print("\nTest: Celestial Revolution -> mixed-set root cause")
    c = _botched_blitz_cause(_ctx([(100.0, md.CELESTIAL_REVOLUTION)], []))
    _check("cause emitted on one CR cast",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == md.CELESTIAL_REVOLUTION, f"got {c}")
    _check("located at the first CR cast", c.time_sec == 100.0,
           f"got {c.time_sec}")
    _check("summary names the resolve and the count",
           "Celestial Revolution" in c.summary and "1 time" in c.summary,
           f"got {c.summary!r}")
    _check("prescription carries the derived 300p shortfall",
           "300p" in c.prescription and "1:40" in c.prescription,
           f"got {c.prescription!r}")
    _check("beast-chakra resource tag attached",
           c.resources and c.resources[0].label == "Beast Chakra",
           f"got {c.resources}")
    _check("no CR casts -> silent",
           _botched_blitz_cause(_ctx([(10.0, md.DRAGON_KICK)], [])) is None,
           "got a cause")
    _check("prepull CR (t < 0) ignored",
           _botched_blitz_cause(
               _ctx([(-1.0, md.CELESTIAL_REVOLUTION)], [])) is None,
           "got a cause")


def test_stranded_blitz_cause() -> None:
    print("\nTest: a fully charged blitz dead at the kill")
    # The trailing GCD is what makes this a strand rather than a fight that
    # simply ended on the third Beast Chakra: it is the slot the blitz should
    # have taken.
    pb_set = [(140.0, md.PERFECT_BALANCE), (141.5, md.LEAPING_OPO),
              (143.5, md.DRAGON_KICK), (145.5, md.LEAPING_OPO),
              (147.5, md.DRAGON_KICK)]
    c = _stranded_blitz_cause(_ctx(pb_set, [], fight_s=150.0))
    _check("3-same-opo set -> stranded Elixir Burst",
           c is not None and c.ability_id == md.ELIXIR_BURST
           and c.kind == "cascade_lost_use", f"got {c}")
    _check("located at the third Beast Chakra", c.time_sec == 145.5,
           f"got {c.time_sec}")
    _check("summary prices the unfired blitz",
           "Elixir Burst" in c.summary and "900p" in c.summary,
           f"got {c.summary!r}")
    resolved = _stranded_blitz_cause(
        _ctx(pb_set + [(146.5, md.ELIXIR_BURST)], [], fight_s=150.0))
    _check("a resolved blitz stays silent", resolved is None,
           f"got {resolved}")
    partial = _stranded_blitz_cause(_ctx(pb_set[:3], [], fight_s=150.0))
    _check("a partial set (2 banked) stays silent", partial is None,
           f"got {partial}")
    dead = _stranded_blitz_cause(_ctx(pb_set, [], fight_s=150.0,
                                      deaths=[(146.0, 150.0)]))
    _check("a death over the tail stays silent (the death card owns it)",
           dead is None, f"got {dead}")
    expired = _stranded_blitz_cause(_ctx(
        [(100.0, md.PERFECT_BALANCE), (126.0, md.LEAPING_OPO),
         (128.0, md.DRAGON_KICK), (130.0, md.LEAPING_OPO)],
        [], fight_s=150.0))
    _check("form GCDs after the 20s PB stacks expired never bank",
           expired is None, f"got {expired}")
    solar = _stranded_blitz_cause(_ctx(
        [(140.0, md.PERFECT_BALANCE), (141.5, md.TWIN_SNAKES),
         (143.5, md.DEMOLISH), (145.5, md.DRAGON_KICK),
         (147.5, md.LEAPING_OPO)],
        [], fight_s=150.0))
    _check("3-distinct set -> stranded Rising Phoenix",
           solar is not None and solar.ability_id == md.RISING_PHOENIX,
           f"got {solar}")
    pr = _stranded_blitz_cause(_ctx(
        [(20.0, md.ELIXIR_BURST), (80.0, md.RISING_PHOENIX)] + pb_set,
        [], fight_s=150.0))
    _check("both Nadi lit -> stranded Phantom Rush at 1500p",
           pr is not None and pr.ability_id == md.PHANTOM_RUSH
           and "1500p" in pr.summary, f"got {pr}")


def test_stranded_blitz_drops_stacks_lost_to_death() -> None:
    print("\nTest: Perfect Balance stacks do not survive a death")
    # PB pressed, one Beast Chakra banked, then the Monk dies mid-window.
    # The stacks are a status and die with them, so the normal form GCDs they
    # resume on must not bank against that press: without the reset the ledger
    # invents a complete set nobody ever held (and named Celestial Revolution,
    # the mistake blitz, as the loss).
    dead_window = [(100.0, md.PERFECT_BALANCE), (101.0, md.LEAPING_OPO),
                   (113.0, md.DRAGON_KICK), (115.0, md.TWIN_SNAKES),
                   (117.0, md.DEMOLISH), (119.0, md.LEAPING_OPO)]
    lost = _stranded_blitz_cause(_ctx(dead_window, [], fight_s=150.0,
                                      deaths=[(103.0, 112.0)]))
    _check("stale PB stacks never bank the post-death form wheel",
           lost is None, f"got {lost}")
    # A set built entirely after the death is still a real strand.
    rebuilt = _stranded_blitz_cause(_ctx(
        dead_window + [(140.0, md.PERFECT_BALANCE), (141.5, md.LEAPING_OPO),
                       (143.5, md.DRAGON_KICK), (145.5, md.LEAPING_OPO),
                       (147.5, md.DRAGON_KICK)],
        [], fight_s=150.0, deaths=[(103.0, 112.0)]))
    _check("a set rebuilt after the death still counts",
           rebuilt is not None and rebuilt.ability_id == md.ELIXIR_BURST
           and rebuilt.time_sec == 145.5, f"got {rebuilt}")


def test_stranded_blitz_needs_a_slot_after_the_set() -> None:
    print("\nTest: a set completed on the buzzer is not a strand")
    # The third Beast Chakra lands 0.4s before the kill: there was no GCD
    # left to press the blitz in, so blaming the player would be a false
    # positive on clean play.
    buzzer = [(140.0, md.PERFECT_BALANCE), (141.5, md.LEAPING_OPO),
              (143.5, md.DRAGON_KICK), (149.6, md.LEAPING_OPO)]
    _check("no cast after the set completes -> silent",
           _stranded_blitz_cause(_ctx(buzzer, [], fight_s=150.0)) is None,
           "got a cause")
    _check("a weaved oGCD is not a GCD slot -> still silent",
           _stranded_blitz_cause(
               _ctx(buzzer + [(149.9, md.THE_FORBIDDEN_CHAKRA)], [],
                    fight_s=150.0)) is None, "got a cause")
    fired = _stranded_blitz_cause(_ctx(
        buzzer + [(151.6, md.DRAGON_KICK)], [], fight_s=156.0))
    _check("one GCD later and it is a genuine strand",
           fired is not None and fired.time_sec == 149.6, f"got {fired}")


def test_probe_order_and_determinism() -> None:
    print("\nTest: advice_probes order (drift by value, then CR, stranded)")
    fight = 260.0
    ideal = ([(0.5 + 60.0 * i, md.RIDDLE_OF_FIRE) for i in range(5)]
             + [(0.5 + 120.0 * i, md.BROTHERHOOD) for i in range(3)])
    casts = ([(0.5, md.RIDDLE_OF_FIRE), (95.5, md.RIDDLE_OF_FIRE),
              (155.5, md.RIDDLE_OF_FIRE), (215.5, md.RIDDLE_OF_FIRE)]
             + [(0.5, md.BROTHERHOOD), (200.5, md.BROTHERHOOD)]
             + [(100.0, md.CELESTIAL_REVOLUTION)]
             + [(250.0, md.PERFECT_BALANCE), (251.5, md.LEAPING_OPO),
                (253.5, md.DRAGON_KICK), (255.5, md.LEAPING_OPO),
                (257.5, md.DRAGON_KICK)])
    ctx = _ctx(casts, ideal, fight_s=fight)
    items, causes = advice_probes(ctx, [])
    _check("no probe items (causes only)", items == [], f"got {items}")
    got = [c.ability_id for c in causes]
    _check("order: RoF drift, BH drift, CR, stranded blitz",
           got == [md.RIDDLE_OF_FIRE, md.BROTHERHOOD,
                   md.CELESTIAL_REVOLUTION, md.ELIXIR_BURST],
           f"got {got}")
    _check("every cause is weightless (measured_p == 0)",
           all(c.measured_p == 0.0 for c in causes),
           f"got {[c.measured_p for c in causes]}")
    _check("every located time inside the fight",
           all(0.0 <= c.time_sec <= fight for c in causes),
           f"got {[c.time_sec for c in causes]}")
    again = advice_probes(_ctx(casts, ideal, fight_s=fight), [])
    _check("deterministic across two runs",
           repr(again) == repr((items, causes)), "runs differ")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Monk job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Monk")
    _check("resolve_pack returns the Monk pack",
           pack is not None and pack.gauge_text is GAUGE_TEXT,
           f"got {pack}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs.monk.simulator import _model_for
    st = _model_for(600.0, None).init_state()
    snapshot_skip = {"t", "charges", "cd_ready", "last_gcd_t", "timeline",
                     "fight_duration_s", "downtime_windows", "buff_intervals",
                     "tincture_cd_ready", "tincture_used", "lock_done"}
    for key in GAUGE_TEXT:
        _check(f"{key} exists on SimState", hasattr(st, key), "missing")
        val = getattr(st, key)
        _check(f"{key} is scalar", isinstance(val, (int, float, bool)),
               f"got {type(val)}")
        _check(f"{key} is snapshot-visible",
               not key.startswith("_") and key not in snapshot_skip,
               "excluded by _snapshot")


def test_copy_rules() -> None:
    print("\nTest: copy rules (no em/en dashes, no strict/lenient jargon)")

    def _walk(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        assert isinstance(gt, GaugeText)
        strings.extend(s for s in (gt.label, gt.short, gt.over_note,
                                   gt.under_note) if s)
    _check("some copy exists", len(strings) >= 10, f"got {len(strings)}")
    for s in strings:
        # `ascii()` so the check labels survive a cp1252 console (the copy
        # itself carries non-ASCII, e.g. the approx sign in the idle row).
        tag = ascii(s[:30])
        _check(f"no em dash in {tag}", "—" not in s, tag)
        _check(f"no en dash in {tag}", "–" not in s, tag)
        _check(f"no jargon in {tag}",
               "strict" not in s.lower() and "lenient" not in s.lower(), tag)
        _check(f"no exclamation in {tag}", "!" not in s, tag)


def _ideal_and_holed_player(dur: float):
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.monk.simulator import SimParams, _model_for
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [],
                                         params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]  # 6s hole
    return ideal, player


_GCD_IDS = frozenset(a for a in md.POTENCIES if a not in md.OGCD_IDS)


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the MNK sim (conservation, stability)")
    import json

    from jobs._core.sim.counterfactual import Runner
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    ideal, player = _ideal_and_holed_player(dur)
    # `None` is the production sim_context for a pull with no gear/budget
    # payload, and it is what the ideal timeline above was built from.
    runner = Runner("jobs.monk.simulator", dur, (), None, player,
                    gcd_ids=sorted(_GCD_IDS))
    cards = [
        _card("missed_cast", md.DRAGON_KICK, 30.0, lost=400.0,
              name="Dragon Kick"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    ctx1 = _ctx(player, ideal, fight_s=dur, runner=runner, gcd_ids=_GCD_IDS)
    out1 = compute_advice_v2(ctx1, live1)
    ctx2 = _ctx(player, ideal, fight_s=dur, runner=runner, gcd_ids=_GCD_IDS)
    out2 = compute_advice_v2(ctx2, [dict(c) for c in cards])
    _check("analytic prescriptions merged into the cards in place",
           any(c.get("prescription") for c in live1),
           f"got {[c.get('prescription') for c in live1]}")
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
    # A quiet segment's generic card may carry zero rows (the allowlist
    # thresholds staying silent is by design); rows that DO exist must be
    # labelled, and at least one cascade card must carry some.
    _check("evidence rows are labelled wherever present",
           all(all({"k", "v", "note"} <= set(r) for r in c["evidence"])
               for c in cascade if c.get("evidence")),
           f"got {[c.get('evidence') for c in cascade]}")
    _check("at least one cascade card carries evidence rows",
           any(c.get("evidence") for c in cascade),
           f"got {[c.get('evidence') for c in cascade]}")
    _check("no evidence row repeats the card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in cascade for r in c.get("evidence", [])),
           "an evidence note copies its prescription")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")
    _check("notes describe the move",
           ex["notes"] and "resolved into" in ex["notes"][0],
           f"got {ex['notes']}")
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys subset of original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")


def test_production_sim_context_builds_examined() -> None:
    print("\nTest: the real production sim_context resolves the cascade")
    # The exact shape `sidecar/main.py::_user_sim_context` hands the runner
    # for MNK: a CeilingContext carrying the player's effective GCD and the
    # measured chakra budget. MNK is the case the resolver's context-only
    # convention cannot serve (the budget is sized from the duration), so
    # this pins the pairing end to end: with the two arguments the other way
    # round the model build raises and Monk ships no examined panel at all.
    from jobs._core.gcd_speed import CeilingContext
    from jobs._core.sim.counterfactual import Runner
    from jobs.monk.simulator import MNK_GCD_S, MonkCtx
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    ideal, player = _ideal_and_holed_player(dur)
    sim_ctx = CeilingContext(gcd_base_s=MNK_GCD_S,
                             payload=MonkCtx(tfc_budget=12))
    runner = Runner("jobs.monk.simulator", dur, (), sim_ctx, player,
                    gcd_ids=sorted(_GCD_IDS))
    cards = [_card("residual", 0, 0.0, lost=2400.0)]
    ctx = _ctx(player, ideal, fight_s=dur, runner=runner, gcd_ids=_GCD_IDS)
    out = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("advice list present", isinstance(out["advice"], list), "missing")
    ex = out["examined"]
    _check("examined payload produced from the production context",
           ex is not None, "got None (the resolver could not build a model)")
    new_sum = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("examined conserves the original sum",
           abs(new_sum - 2400.0) <= 0.25, f"got {new_sum}")


def main() -> int:
    test_cooldown_drift_cause()
    test_cooldown_drift_respects_downtime_and_deaths()
    test_botched_blitz_cause()
    test_stranded_blitz_cause()
    test_stranded_blitz_drops_stacks_lost_to_death()
    test_stranded_blitz_needs_a_slot_after_the_set()
    test_probe_order_and_determinism()
    test_registration()
    test_gauge_keys_are_real_state_fields()
    test_copy_rules()
    test_examined_conservation_and_stability()
    test_production_sim_context_builds_examined()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
