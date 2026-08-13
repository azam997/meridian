"""Unit tests for the Astrologian deep-advice pack (jobs/astrologian/advice.py).

Mirrors the MCH coverage in test_deep_advice.py for the AST ledgers: each root
cause fires on a synthetic stream that earns it and stays silent on a clean
one, the healer guardrails (downtime / deaths / raise bars / credited heal
GCDs) suppress stretches the player never controlled, the gauge allowlist keys
are real sim-state fields, the copy obeys the register rules, and the cascade
conserves the panel's top-level sum byte-stably on AST's own simulator.

Run from python/:  python tests/test_astrologian_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.astrologian import data as ad
from jobs.astrologian.advice import (
    GAUGE_TEXT, TEXT, _combust_lapse_cause, _ogcd_drift_causes,
    _oracle_unfired_cause,
)

_GCD_IDS = frozenset({ad.FALL_MALEFIC, ad.COMBUST_III, ad.GRAVITY_II})

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


def _ctx(norm_casts, idealized, *, fight_s: float = 360.0, runner=None,
         downtime=(), deaths=(), scoring=None) -> AdviceContext:
    return AdviceContext(
        job="Astrologian", data=ad.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=[tuple(w) for w in downtime],
        death_windows=[tuple(w) for w in deaths],
        clipping_state={"clipping": None}, scoring_state=dict(scoring or {}),
        enabler_values={}, sim_context=None,
        sim_module="jobs.astrologian.simulator", runner=runner,
        gcd_ids=_GCD_IDS, gauge_text=dict(GAUGE_TEXT))


# --- Oracle economy ---------------------------------------------------------

def _div_oracle_stream(oracle_after: set[int], n: int = 3):
    """`n` Divinations on a 120s cadence, Oracle fired only after the indexed
    ones, with filler in between so the stream looks like a real pull."""
    casts: list[tuple[float, int]] = []
    for i in range(n):
        t = 5.0 + 120.0 * i
        casts.append((t, ad.DIVINATION))
        if i in oracle_after:
            casts.append((t + 1.0, ad.ORACLE))
    casts += [(2.5 * k, ad.FALL_MALEFIC) for k in range(140)]
    return casts


def test_oracle_unfired_cause() -> None:
    print("\nTest: Divining overwritten / stranded -> unfired-Oracle cause")
    player = _div_oracle_stream({0})            # 2 of 3 Divinings never spent
    ideal = _div_oracle_stream({0, 1, 2})
    hit = _oracle_unfired_cause(_ctx(player, ideal))
    _check("cause emitted", hit is not None, "got None")
    value, cause = hit
    _check("lost-use kind, located on Oracle",
           cause.kind == "cascade_lost_use" and cause.ability_id == ad.ORACLE,
           f"got {cause.kind} / {cause.ability_id}")
    _check("located where the last stack was lost (4:05), not where it was "
           "granted", abs(cause.time_sec - 245.0) < 0.05, f"got {cause.time_sec}")
    _check("time sits inside the fight",
           0.0 <= cause.time_sec <= 360.0, f"got {cause.time_sec}")
    _check("evidence names the first stack, at 2:05",
           "granted at 2:05" in cause.evidence[1].note,
           f"got {cause.evidence[1].note!r}")
    _check("summary counts them and prices them",
           "2 Oracles left unfired" in cause.summary
           and f"{2 * ad.POTENCIES[ad.ORACLE]:.0f}p" in cause.summary,
           f"got {cause.summary!r}")
    _check("value ranks by whole Oracles",
           abs(value - 2 * ad.POTENCIES[ad.ORACLE]) < 1e-6, f"got {value}")
    _check("prescription says the weave costs no healing GCD",
           "never takes a healing GCD" in cause.prescription,
           f"got {cause.prescription!r}")
    _check("Divining tagged as the implicated resource",
           [g.label for g in cause.resources] == ["Oracle"],
           f"got {cause.resources}")
    _check("no evidence note repeats the prescription",
           all(r.note not in cause.prescription for r in cause.evidence),
           f"got {cause.evidence}")


def test_oracle_clean_and_tail_silent() -> None:
    print("\nTest: every Divining spent (and a late one) stays silent")
    clean = _div_oracle_stream({0, 1, 2})
    _check("clean stream emits nothing",
           _oracle_unfired_cause(_ctx(clean, clean)) is None, "got a cause")
    # One Divination 6s before the kill: no window to fire Oracle in.
    tail = [(5.0, ad.DIVINATION), (6.0, ad.ORACLE), (114.0, ad.DIVINATION)]
    ideal = [(5.0, ad.DIVINATION), (6.0, ad.ORACLE), (114.0, ad.DIVINATION),
             (115.0, ad.ORACLE)]
    _check("a Divining granted just before the kill is not blamed",
           _oracle_unfired_cause(_ctx(tail, ideal, fight_s=120.0)) is None,
           "got a cause")


def test_oracle_count_never_exceeds_the_sim_line() -> None:
    print("\nTest: the claim is capped by the sim's own Oracle count")
    # Three Divinings destroyed, but the sim's line only fits two Oracles (a
    # short or downtime-heavy pull): claiming three would contradict the card's
    # own "0 / 2" evidence row.
    player = [(5.0, ad.DIVINATION), (125.0, ad.DIVINATION),
              (245.0, ad.DIVINATION)]
    ideal = [(5.0, ad.DIVINATION), (6.0, ad.ORACLE),
             (125.0, ad.DIVINATION), (126.0, ad.ORACLE)]
    hit = _oracle_unfired_cause(_ctx(player, ideal))
    _check("cause emitted", hit is not None, "got None")
    value, cause = hit
    _check("counts only the Oracles the sim fits",
           "2 Oracles left unfired" in cause.summary, f"got {cause.summary!r}")
    _check("priced at the capped count",
           f"{2 * ad.POTENCIES[ad.ORACLE]:.0f}p" in cause.summary
           and abs(value - 2 * ad.POTENCIES[ad.ORACLE]) < 1e-6,
           f"got {cause.summary!r} / {value}")
    _check("evidence row agrees with the summary",
           cause.evidence[0].v == "0 / 2"
           and "2 unspent" in cause.evidence[1].v,
           f"got {cause.evidence[0].v} / {cause.evidence[1].v}")
    _check("located at the latest stack still recoverable",
           abs(cause.time_sec - 245.0) < 0.05, f"got {cause.time_sec}")


def test_oracle_forced_stretch_silent() -> None:
    print("\nTest: a Divining lost to a death or a raise is not blamed")
    player = _div_oracle_stream({0})
    ideal = _div_oracle_stream({0, 1, 2})
    dead = _ctx(player, ideal, deaths=[(120.0, 200.0), (240.0, 320.0)])
    _check("deaths own that loss (their own card prices it)",
           _oracle_unfired_cause(dead) is None, "got a cause")
    rez = _ctx(player, ideal, scoring={
        "heal_lock_rez_casts": [[125.0, ad.ASCEND, 3]],
        "heal_lock_rez_count": 1})
    hit = _oracle_unfired_cause(rez)
    _check("the raise bar clears the Divining under it",
           hit is not None and abs(hit[1].time_sec - 245.0) < 0.05,
           f"got {hit[1].time_sec if hit else None}")
    _check("only the stretch the player controlled is counted",
           "1 Oracle left unfired" in hit[1].summary, f"got {hit[1].summary!r}")


# --- oGCD cooldown drift ----------------------------------------------------

def _star_stream(step: float, n: int):
    return [(step * i, ad.EARTHLY_STAR) for i in range(n)]


def test_ogcd_drift_cause() -> None:
    print("\nTest: Earthly Star held past its recast -> lost-use cause")
    ideal = _star_stream(60.0, 6)
    player = _star_stream(90.0, 4)              # 30s over per gap
    causes = _ogcd_drift_causes(_ctx(player, ideal))
    _check("one cause, on Earthly Star",
           len(causes) == 1 and causes[0][1].ability_id == ad.EARTHLY_STAR,
           f"got {[c[1].ability_id for c in causes]}")
    value, cause = causes[0]
    _check("lost-use kind", cause.kind == "cascade_lost_use", cause.kind)
    _check("located at the start of the worst gap",
           abs(cause.time_sec - 0.0) < 0.05, f"got {cause.time_sec}")
    _check("summary carries the drift and the deficit",
           "90s past its recast" in cause.summary
           and "2 uses lost" in cause.summary, f"got {cause.summary!r}")
    _check("value ranks by deficit x per-use potency",
           abs(value - 2 * ad.COOLDOWN_VALUE_P[ad.EARTHLY_STAR]) < 1e-6,
           f"got {value}")
    _check("prescription scopes to the healing plan's weave slots",
           "weave slot the healing plan leaves open" in cause.prescription,
           f"got {cause.prescription!r}")
    _check("count evidence compares to the sim's line",
           cause.evidence[0].v == "4 / 6", f"got {cause.evidence[0]}")
    _check("no evidence note repeats the prescription",
           all(r.note not in cause.prescription for r in cause.evidence),
           f"got {cause.evidence}")


def test_ogcd_drift_clean_silent() -> None:
    print("\nTest: on-cooldown oGCDs and RNG cards stay silent")
    ideal = _star_stream(60.0, 6)
    _check("on-recast stream emits nothing",
           _ogcd_drift_causes(_ctx(_star_stream(60.0, 6), ideal)) == [],
           "got causes")
    # Lord of Crowns is card RNG (drift_exclusions): a drawn-card famine must
    # never read as drift, even with the exact same shape as the Star ledger.
    lord_ideal = [(120.0 * i, ad.LORD_OF_CROWNS) for i in range(3)]
    lord_player = [(180.0 * i, ad.LORD_OF_CROWNS) for i in range(2)]
    _check("Lord of Crowns is never read as drift",
           _ogcd_drift_causes(_ctx(lord_player, lord_ideal)) == [],
           "got causes")


def test_ogcd_drift_forced_stretch_silent() -> None:
    print("\nTest: a hold across downtime or a raise is not drift")
    ideal = _star_stream(60.0, 6)
    player = _star_stream(90.0, 4)
    dt = _ctx(player, ideal, downtime=[(30.0, 60.0), (120.0, 150.0),
                                       (210.0, 240.0)])
    _check("downtime removed from every gap -> under the floor",
           _ogcd_drift_causes(dt) == [], "got causes")
    # Three raises, 3 locked slots each: 7.5s of every 30s slip is the raise's,
    # so the reported hold drops from 90s to 68s and is never called healing.
    rez = _ogcd_drift_causes(_ctx(player, ideal, scoring={
        "heal_lock_rez_casts": [[35.0, ad.ASCEND, 3], [125.0, ad.ASCEND, 3],
                                [215.0, ad.ASCEND, 3]]}))
    _check("raise cast bars come out of the drift ledger",
           len(rez) == 1 and "68s past its recast" in rez[0][1].summary,
           f"got {[c[1].summary for c in rez]}")


# --- Combust III uptime -----------------------------------------------------

def test_combust_lapse_cause() -> None:
    print("\nTest: Combust III lapsing between refreshes -> pacing cause")
    player = [(45.0 * i, ad.COMBUST_III) for i in range(5)]   # 15s down x4
    hit = _combust_lapse_cause(_ctx(player, player))
    _check("cause emitted", hit is not None, "got None")
    value, cause = hit
    _check("pacing kind, located on Combust III",
           cause.kind == "cascade_pacing" and cause.ability_id == ad.COMBUST_III,
           f"got {cause.kind} / {cause.ability_id}")
    _check("located where the DoT first fell off in the worst gap",
           abs(cause.time_sec - 30.0) < 0.05, f"got {cause.time_sec}")
    _check("summary carries the downtime and the ticks",
           "60s between refreshes" in cause.summary
           and "20 ticks lost" in cause.summary, f"got {cause.summary!r}")
    _check("value is the lost ticks' potency",
           abs(value - (60.0 / ad.COMBUST_DOT_TICK_S)
               * ad.COMBUST_DOT_TICK_P) < 1e-6, f"got {value}")
    _check("prescription defers to the healing that comes first",
           "the first GCD the healing leaves free" in cause.prescription,
           f"got {cause.prescription!r}")
    _check("no evidence note repeats the prescription",
           all(r.note not in cause.prescription for r in cause.evidence),
           f"got {cause.evidence}")


def test_combust_clean_silent() -> None:
    print("\nTest: a maintained DoT (and a healed-through gap) stays silent")
    kept = [(30.0 * i, ad.COMBUST_III) for i in range(8)]
    _check("refreshed on cadence -> nothing",
           _combust_lapse_cause(_ctx(kept, kept)) is None, "got a cause")
    tight = [(28.0 * i, ad.COMBUST_III) for i in range(8)]
    _check("early refreshes never read as a lapse",
           _combust_lapse_cause(_ctx(tight, tight)) is None, "got a cause")
    single = [(0.0, ad.COMBUST_III)]
    _check("one application has no gap to measure",
           _combust_lapse_cause(_ctx(single, single)) is None, "got a cause")


def test_combust_healing_and_raise_pardoned() -> None:
    print("\nTest: a lapse spent healing or raising is never blamed")
    player = [(45.0 * i, ad.COMBUST_III) for i in range(5)]
    heals = [(30.0 + 45.0 * i + 2.5 * k, ad.HELIOS_CONJUNCTION)
             for i in range(4) for k in range(6)]
    _check("credited heal GCDs cover the gaps -> silent",
           _combust_lapse_cause(_ctx(player + heals, player)) is None,
           "got a cause")
    rez = _ctx(player, player, scoring={
        "heal_lock_rez_casts": [[30.0, ad.ASCEND, 3], [75.0, ad.ASCEND, 3],
                                [120.0, ad.ASCEND, 3], [165.0, ad.ASCEND, 3]]})
    _check("raise bars plus recovery cover the gaps -> silent",
           _combust_lapse_cause(rez) is None, "got a cause")
    # Macrocosmos is a heal GCD the costed set does not carry, but it still
    # spends the slot: a lapse the player spent casting it is never a lapse
    # they chose (regression — the ledger used to blame the whole stretch).
    macro = [(0.0, ad.COMBUST_III), (40.0, ad.COMBUST_III)] + \
            [(30.0 + 2.5 * k, ad.MACROCOSMOS) for k in range(4)]
    _check("a lapse spent on Macrocosmos is pardoned like any heal GCD",
           _combust_lapse_cause(_ctx(macro, macro)) is None, "got a cause")
    bare = [(0.0, ad.COMBUST_III), (40.0, ad.COMBUST_III)]
    _check("the same gap with no healing in it is still measured",
           _combust_lapse_cause(_ctx(bare, bare)) is not None, "got None")


# --- Pack wiring, gauges, copy ----------------------------------------------

def test_pack_registered() -> None:
    print("\nTest: the pack is registered on the Astrologian Job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Astrologian")
    _check("resolve_pack finds it", pack is not None, "got None")
    _check("it carries the AST gauge glossary",
           pack.gauge_text == GAUGE_TEXT, f"got {pack.gauge_text}")
    items, causes = pack.probes(_ctx([], []), [], None)
    _check("a bare context yields no items and no causes",
           items == [] and causes == [], f"got {items} / {causes}")


def test_gauge_keys_are_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a real AST sim-state gauge")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.astrologian.simulator import _model_for
    state = _model_for(360.0, None).init_state()
    gauges = _snapshot(state)["gauges"]
    for key in GAUGE_TEXT:
        _check(f"{key} is a state attribute", hasattr(state, key), "missing")
        _check(f"{key} survives into the state delta", key in gauges,
               f"snapshot gauges: {sorted(gauges)}")


def _strings():
    def walk(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                yield from walk(v)
    yield from walk(TEXT)
    for g in GAUGE_TEXT.values():
        assert isinstance(g, GaugeText)
        for s in (g.label, g.short, g.over_note, g.under_note):
            if s:
                yield s


def test_copy_lint() -> None:
    print("\nTest: copy register (no dashes, no jargon, no shouting)")
    bad = [s for s in _strings() if "—" in s or "–" in s]
    _check("no em or en dashes", not bad, f"got {bad}")
    jargon = [s for s in _strings()
              if "strict" in s.lower() or "lenient" in s.lower()]
    _check("no strict/lenient jargon", not jargon, f"got {jargon}")
    shouty = [s for s in _strings() if "!" in s]
    _check("no exclamation marks", not shouty, f"got {shouty}")
    blame = [s for s in _strings()
             if "too much" in s.lower() or "wasted a heal" in s.lower()]
    _check("nothing blames the player for healing", not blame, f"got {blame}")


# --- Cascade smoke ----------------------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: cascade on the AST sim — conservation + byte stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.astrologian.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    # The player's own line, minus a 12s hole and minus every Oracle (both
    # Divinings left to expire) so the cascade has a job cause to promote.
    player = [(t, a) for t, a in ideal
              if a != ad.ORACLE and not 60.0 <= t < 72.0]
    ctx = _ctx(player, ideal, fight_s=dur)
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(ctx.gcd_ids))
    cards = [
        _card("missed_cast", ad.EARTHLY_STAR, 30.0, lost=310.0,
              name="Earthly Star"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    _check("advice list present", isinstance(out1["advice"], list), "missing")
    ex = out1["examined"]
    _check("examined payload produced", ex is not None, "got None")
    orig = round(sum(c["lostPotency"] for c in cards), 1)
    new = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("top-level sum conserved to the cent", abs(new - orig) <= 0.25,
           f"{new} vs {orig}")
    _check("recoverable echoes the original sum",
           abs(ex["recoverable"] - orig) <= 0.25, f"got {ex['recoverable']}")
    cascade = [c for c in ex["improvements"]
               if str(c["kind"]).startswith("cascade_")]
    _check("at least one cascade card promoted", len(cascade) >= 1,
           f"kinds={[c['kind'] for c in ex['improvements']]}")
    _check("the unfired-Oracle root cause is one of them",
           any(c["kind"] == "cascade_lost_use" and c["abilityId"] == ad.ORACLE
               and "left unfired after Divination" in c["summary"]
               for c in cascade),
           f"got {[(c['kind'], c['summary']) for c in cascade]}")
    _check("every cascade card is priced above the promotion floor",
           all(c["lostPotency"] >= 150.0 for c in cascade),
           f"got {[c['lostPotency'] for c in cascade]}")
    _check("every cascade card carries labelled evidence",
           all(c.get("evidence")
               and all({"k", "v", "note"} <= set(r) for r in c["evidence"])
               for c in cascade),
           f"got {[c.get('evidence') for c in cascade]}")
    _check("no evidence note repeats its card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in cascade for r in c.get("evidence", [])),
           "a note duplicates the prescription")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and 60.0 <= resid[0]["lostPotency"] < 2400.0,
           f"got {resid}")


def main() -> int:
    test_oracle_unfired_cause()
    test_oracle_clean_and_tail_silent()
    test_oracle_count_never_exceeds_the_sim_line()
    test_oracle_forced_stretch_silent()
    test_ogcd_drift_cause()
    test_ogcd_drift_clean_silent()
    test_ogcd_drift_forced_stretch_silent()
    test_combust_lapse_cause()
    test_combust_clean_silent()
    test_combust_healing_and_raise_pardoned()
    test_pack_registered()
    test_gauge_keys_are_state_fields()
    test_copy_lint()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
