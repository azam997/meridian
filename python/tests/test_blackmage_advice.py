"""Unit tests for the Black Mage deep-advice pack (jobs/blackmage/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean silent one; registration, gauge-key
validity against the live sim state, the copy lint (no em/en dashes, no
strict/lenient jargon), and the cascade conservation/stability smoke on the
BLM simulator.

Run from python/:  python tests/test_blackmage_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.blackmage import data as bd
from jobs.blackmage.advice import (
    GAUGE_TEXT, TEXT,
    _cooldown_drift_causes, _polyglot_overcap_cause, _polyglot_stranded_cause,
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


def _blm_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             deaths=None) -> AdviceContext:
    gcds = frozenset(a for a in bd.POTENCIES if a not in bd.OGCD_IDS)
    return AdviceContext(
        job="Black Mage", data=bd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=[],
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.blackmage.simulator", runner=runner, gcd_ids=gcds,
        gauge_text=GAUGE_TEXT)


# --- Cooldown drift ---------------------------------------------------------

def test_cd_drift_emits() -> None:
    print("\nTest: Amplifier drift with a use deficit -> lost-use root cause")
    ideal = [(1.0 + 120.0 * i, bd.AMPLIFIER) for i in range(4)]
    player = [(1.0, bd.AMPLIFIER), (201.0, bd.AMPLIFIER)]   # 80s over recast
    causes = _cooldown_drift_causes(_blm_ctx(player, ideal, fight_s=480.0))
    _check("one cause emitted", len(causes) == 1, f"got {causes}")
    c = causes[0]
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == bd.AMPLIFIER, f"got {c.kind} {c.ability_id}")
    _check("located at the worst slip's gap start", c.time_sec == 1.0,
           f"got {c.time_sec}")
    _check("summary carries the deficit", "2 uses lost" in c.summary,
           f"got {c.summary!r}")
    _check("count evidence row is you/ideal",
           c.evidence and c.evidence[0].v == "2 / 4", f"got {c.evidence}")
    _check("measured_p stays 0 (orchestrator prices it)",
           c.measured_p == 0.0, f"got {c.measured_p}")
    _check("time inside the fight", 0.0 <= c.time_sec <= 480.0,
           f"got {c.time_sec}")


def test_cd_drift_silent_when_clean() -> None:
    print("\nTest: on-cooldown stream / no deficit -> no drift cause")
    ideal = [(1.0 + 120.0 * i, bd.AMPLIFIER) for i in range(4)]
    on_cd = list(ideal)
    _check("clean on-cooldown stream is silent",
           _cooldown_drift_causes(_blm_ctx(on_cd, ideal, fight_s=480.0)) == [],
           "got causes")
    # Drift but NO deficit (the sim fit no more than the player): silent.
    drifted = [(1.0, bd.AMPLIFIER), (301.0, bd.AMPLIFIER)]
    two = [(1.0, bd.AMPLIFIER), (121.0, bd.AMPLIFIER)]
    _check("drift without a lost use is silent",
           _cooldown_drift_causes(_blm_ctx(drifted, two, fight_s=480.0)) == [],
           "got causes")


def test_cd_drift_death_window_suppressed() -> None:
    print("\nTest: a drift gap overlapping a death window is not blamed")
    ideal = [(1.0 + 120.0 * i, bd.AMPLIFIER) for i in range(4)]
    player = [(1.0, bd.AMPLIFIER), (201.0, bd.AMPLIFIER)]
    causes = _cooldown_drift_causes(
        _blm_ctx(player, ideal, fight_s=480.0, deaths=[(100.0, 130.0)]))
    _check("death-window gap accumulates no drift", causes == [],
           f"got {causes}")


# --- Polyglot overcap -------------------------------------------------------

def test_polyglot_overcap_emits() -> None:
    print("\nTest: capped Polyglot ticks -> delayed-Xenoglossy root cause")
    # 200s, no spends: ticks 30/60/90 fill the gauge; 120/150/180 land on full.
    filler = [(2.5 * i, bd.FIRE_IV) for i in range(20)]
    c = _polyglot_overcap_cause(_blm_ctx(filler, [], fight_s=200.0))
    _check("cause emitted", c is not None, "got None")
    _check("kind + ability", c.kind == "cascade_burst"
           and c.ability_id == bd.XENOGLOSSY, f"got {c.kind} {c.ability_id}")
    _check("located at the first wasted tick", c.time_sec == 120.0,
           f"got {c.time_sec}")
    _check("summary counts the waste", "3 stacks wasted" in c.summary,
           f"got {c.summary!r}")
    _check("polyglot resource tag attached",
           c.resources and c.resources[0].short == "POLY",
           f"got {c.resources}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_polyglot_overcap_grace_and_clean() -> None:
    print("\nTest: spends just after a capped tick / prompt spending -> silent")
    # Spends 1.5s after each capped tick: anchor fuzz, forgiven.
    fuzz = [(121.5, bd.XENOGLOSSY), (181.5, bd.XENOGLOSSY)]
    _check("grace window forgives the near-miss",
           _polyglot_overcap_cause(_blm_ctx(fuzz, [], fight_s=200.0)) is None,
           "got a cause")
    # Prompt spending keeps the gauge under cap at every tick.
    prompt = [(95.0, bd.XENOGLOSSY), (125.0, bd.XENOGLOSSY),
              (155.0, bd.XENOGLOSSY), (185.0, bd.XENOGLOSSY)]
    _check("prompt spending is silent",
           _polyglot_overcap_cause(_blm_ctx(prompt, [], fight_s=200.0)) is None,
           "got a cause")


def test_polyglot_amplifier_at_cap() -> None:
    print("\nTest: Amplifier pressed on a full gauge counts as waste")
    player = [(100.0, bd.AMPLIFIER)]     # gauge full since the 90s tick
    c = _polyglot_overcap_cause(_blm_ctx(player, [], fight_s=140.0))
    _check("cause emitted", c is not None, "got None")
    _check("first waste is the Amplifier grant", c.time_sec == 100.0,
           f"got {c.time_sec}")
    _check("both wasted stacks counted (amp + 120s tick)",
           "2 stacks wasted" in c.summary, f"got {c.summary!r}")


# --- Polyglot stranded ------------------------------------------------------

def test_polyglot_stranded_emits() -> None:
    print("\nTest: stacks banked past the ideal's own ending gauge")
    filler = [(2.5 * i, bd.FIRE_IV) for i in range(10)]     # never spends
    ideal = [(35.0, bd.XENOGLOSSY), (65.0, bd.XENOGLOSSY),
             (95.0, bd.XENOGLOSSY)]                          # spends all
    c = _polyglot_stranded_cause(_blm_ctx(filler, ideal, fight_s=100.0))
    _check("cause emitted", c is not None, "got None")
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == bd.XENOGLOSSY, f"got {c.kind} {c.ability_id}")
    _check("located at the last grant", c.time_sec == 90.0,
           f"got {c.time_sec}")
    _check("summary counts the stranding", "3 stacks" in c.summary,
           f"got {c.summary!r}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_polyglot_stranded_silent() -> None:
    print("\nTest: ideal strands too / last stack lands late -> silent")
    filler = [(2.5 * i, bd.FIRE_IV) for i in range(10)]
    # The ideal timeline banks the same stacks: the boundary stack is not the
    # player's fault.
    _check("no excess over the ideal's ending gauge",
           _polyglot_stranded_cause(_blm_ctx(filler, filler, fight_s=100.0))
           is None, "got a cause")
    # The surviving stack landed 3s before the kill: no slot to fire it.
    late = [(40.0, bd.XENOGLOSSY), (97.0, bd.AMPLIFIER)]
    ideal = [(35.0, bd.XENOGLOSSY), (65.0, bd.XENOGLOSSY),
             (95.0, bd.XENOGLOSSY)]
    _check("late-grant grace stays silent",
           _polyglot_stranded_cause(_blm_ctx(late, ideal, fight_s=100.0))
           is None, "got a cause")


# --- Registration + allowlist validity --------------------------------------

def test_registration() -> None:
    print("\nTest: the pack is registered on the Black Mage job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Black Mage")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is the BLM glossary", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text}")


def test_gauge_keys_are_live_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key survives the state snapshot")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.blackmage.simulator import _model_for
    state = _model_for(None, None).init_state()
    snap = _snapshot(state)
    for key in sorted(GAUGE_TEXT):
        _check(f"'{key}' is a snapshot gauge field", key in snap["gauges"],
               f"gauges={sorted(snap['gauges'])}")


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no jargon, no exclamations)")

    def _walk(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        strings += [gt.label, gt.short, gt.over_note or "",
                    gt.under_note or ""]
    for s in strings:
        _check(f"no em/en dash in {s[:40]!r}",
               "—" not in s and "–" not in s, s)
        low = s.lower()
        _check(f"no strict/lenient jargon in {s[:40]!r}",
               "strict" not in low and "lenient" not in low, s)
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, s)


# --- Cascade conservation smoke on the BLM sim ------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the BLM sim (conservation, stability)")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.blackmage.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 240.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _blm_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", bd.FIRE_IV, 30.0, lost=400.0, name="Fire IV"),
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
    _check("at least one cascade card promoted", len(cascade) >= 1,
           f"kinds={[c['kind'] for c in ex['improvements']]}")
    _check("every cascade card is priced above the floor",
           all(c["lostPotency"] >= 150.0 for c in cascade),
           f"got {[c['lostPotency'] for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    # The in-place advice list still targets only original cards.
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys subset of original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")


def main() -> int:
    test_cd_drift_emits()
    test_cd_drift_silent_when_clean()
    test_cd_drift_death_window_suppressed()
    test_polyglot_overcap_emits()
    test_polyglot_overcap_grace_and_clean()
    test_polyglot_amplifier_at_cap()
    test_polyglot_stranded_emits()
    test_polyglot_stranded_silent()
    test_registration()
    test_gauge_keys_are_live_state_fields()
    test_copy_lint()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
