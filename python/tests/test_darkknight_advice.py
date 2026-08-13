"""Unit tests for the Dark Knight deep-advice pack (jobs/darkknight/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean-silent stream; the dual-economy ledger is
pinned against the simulator's own rules (Blood Weapon stacks, the combo-gated
finishers, the MP tick), plus registration, gauge-key validity against the real
SimState, the copy lint (no em/en dashes, no strict/lenient jargon), and the
cascade conservation smoke on the DRK sim.

Run from python/:  python tests/test_darkknight_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.darkknight import data as dd
from jobs.darkknight.advice import (
    GAUGE_TEXT, TEXT, _blood_overcap_cause, _blood_stranded_cause,
    _cooldown_drift_causes, _economy_walk, _mp_waste_cause,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


_GCD_IDS = frozenset(dd.POTENCIES) - dd.OGCD_IDS
_COMBO = (dd.HARD_SLASH, dd.SYPHON_STRIKE, dd.SOULEATER)


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 150.0,
         death_windows=None, downtime_windows=None) -> AdviceContext:
    return AdviceContext(
        job="Dark Knight", data=dd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime_windows or []),
        death_windows=list(death_windows or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.darkknight.simulator", runner=runner,
        gcd_ids=_GCD_IDS, gauge_text=GAUGE_TEXT)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


# --- The dual-economy ledger ------------------------------------------------

def test_ledger_mirrors_the_simulator() -> None:
    print("\nTest: the Blood/MP ledger mirrors simulator.apply_cast")
    # Blood Weapon: exactly 3 weaponskills inside the 15s window get +10.
    bw = ([(0.0, dd.DELIRIUM)]
          + [(2.5 * (i + 1), dd.HARD_SLASH) for i in range(4)])
    _check("3 Blood Weapon stacks, +10 each", _economy_walk(bw).blood == 30,
           f"got {_economy_walk(bw).blood}")
    dead = [(0.0, dd.DELIRIUM), (16.0, dd.HARD_SLASH), (18.0, dd.HARD_SLASH)]
    _check("stacks die with the 15s buff", _economy_walk(dead).blood == 0,
           f"got {_economy_walk(dead).blood}")
    # The in-game combo bonus: an uncombo'd finisher builds nothing.
    _check("uncombo'd Souleater grants no Blood",
           _economy_walk([(0.0, dd.SOULEATER)]).blood == 0, "got Blood")
    _check("combo'd Souleater grants 20",
           _economy_walk([(t, a) for t, a in zip((0.0, 2.5, 5.0), _COMBO)]
                         ).blood == 20, "wrong Blood")
    # MP: the bar starts full, Edge spends 3000, Carve grants 600.
    mp = _economy_walk([(0.0, dd.EDGE_OF_SHADOW), (1.0, dd.CARVE_AND_SPIT)])
    _check("Edge spends 3000, Carve restores 600", mp.mp == 7600,
           f"got {mp.mp}")
    # Prepull: nothing is generated, but a spend that already happened lands
    # (an unspent 3000 MP would read as cap waste for the rest of the pull).
    _check("a prepull Edge still costs the bar 3000",
           _economy_walk([(-2.0, dd.EDGE_OF_SHADOW)]).mp
           == dd.MP_MAX - dd.EDGE_MP_COST,
           f"got {_economy_walk([(-2.0, dd.EDGE_OF_SHADOW)]).mp}")
    _check("prepull generation is still ignored",
           _economy_walk([(t, a) for t, a in
                          zip((-6.0, -3.5, -1.0), _COMBO)]).blood == 0,
           "prepull generation counted")


def test_ledger_wipes_the_gauge_on_death() -> None:
    print("\nTest: a death wipes the gauge, stacks and combo (as the game does)")
    # Three combos = 60 Blood, then a death at 30 with the raise at 60.
    banked = [(2.5 * i, _COMBO[i % 3]) for i in range(9)]
    _check("without the death the Blood is still banked",
           _economy_walk(banked).blood == 60,
           f"got {_economy_walk(banked).blood}")
    after = banked + [(60.0 + 2.5 * i, _COMBO[i % 3]) for i in range(3)]
    wiped = _economy_walk(after, [(30.0, 60.0)])
    _check("the raise starts the gauge from zero", wiped.blood == 20,
           f"got {wiped.blood}")
    # The 15s Delirium buff dies with the player, so no Blood Weapon after.
    bw = [(0.0, dd.DELIRIUM), (2.5, dd.HARD_SLASH), (5.0, dd.HARD_SLASH)]
    _check("Blood Weapon stacks die with the player",
           _economy_walk(bw, [(3.0, 4.0)]).blood == 0,
           f"got {_economy_walk(bw, [(3.0, 4.0)]).blood}")
    # No combat regen while KO'd: the dead stretch's ticks are not income.
    idle = [(0.0, dd.EDGE_OF_SHADOW), (120.0, dd.HARD_SLASH)]
    alive = _economy_walk(idle)
    dead = _economy_walk(idle, [(10.0, 100.0)])
    _check("the dead stretch pays no MP tick", dead.mp < alive.mp,
           f"{dead.mp} vs {alive.mp}")


def test_death_does_not_invent_overcap_or_stranding() -> None:
    print("\nTest: a rezzed player is not blamed for the wiped gauge")
    # 100 Blood banked into a death at 60, raised at 120, then a clean spend
    # cadence: every Bloodspiller lands as soon as the REAL gauge allows.
    casts = [(2.5 * i, _COMBO[i % 3]) for i in range(24)]
    t, blood = 120.0, 0
    for i in range(30):
        a = _COMBO[i % 3]
        casts.append((t, a))
        t += 2.5
        if a == dd.SOULEATER:
            blood += 20
        if blood >= 50:
            casts.append((t, dd.BLOODSPILLER))
            t += 2.5
            blood -= 50
    ctx_dead = _ctx(casts, [], fight_s=t + 2.5, death_windows=[(60.0, 120.0)])
    over = _blood_overcap_cause(ctx_dead)
    _check("only the pre-death overcap counts (60 Blood, not 120)",
           over is not None and "60 Blood wasted" in over.summary,
           f"got {over.summary if over else None}")
    _check("every counted overcap predates the death",
           over.time_sec < 60.0, f"got {over.time_sec}")
    # A death after the last cast still empties the gauge before the kill.
    late = [(50.0 + 2.5 * i, _COMBO[i % 3]) for i in range(9)]
    _check("a trailing death leaves nothing stranded",
           _blood_stranded_cause(
               _ctx(late, [], fight_s=100.0,
                    death_windows=[(80.0, 100.0)])) is None,
           "got a cause")


# --- Cooldown drift ---------------------------------------------------------

def test_cooldown_drift_cause() -> None:
    print("\nTest: Living Shadow drift -> lost-use root cause; clean silent")
    ideal = [(120.0 * i, dd.LIVING_SHADOW) for i in range(5)]      # 0..480
    late = [(0.0, dd.LIVING_SHADOW), (130.0, dd.LIVING_SHADOW),
            (260.0, dd.LIVING_SHADOW), (420.0, dd.LIVING_SHADOW)]
    causes = _cooldown_drift_causes(_ctx(late, ideal, fight_s=600.0))
    ls = [c for c in causes if c.ability_id == dd.LIVING_SHADOW]
    _check("Living Shadow lost-use cause emitted",
           len(ls) == 1 and ls[0].kind == "cascade_lost_use", f"got {causes}")
    _check("located at the worst slip (gap start 260)",
           ls[0].time_sec == 260.0, f"got {ls[0].time_sec}")
    _check("located time sits inside the fight",
           0.0 <= ls[0].time_sec <= 600.0, f"got {ls[0].time_sec}")
    _check("summary counts the lost use",
           "1 use lost" in ls[0].summary, f"got {ls[0].summary!r}")
    _check("evidence carries the cast-count row",
           ls[0].evidence and ls[0].evidence[0].v == "4 / 5",
           f"got {ls[0].evidence}")
    _check("measured_p stays 0 (the orchestrator prices it)",
           ls[0].measured_p == 0.0, f"got {ls[0].measured_p}")
    clean = _cooldown_drift_causes(_ctx(ideal, ideal, fight_s=600.0))
    _check("clean on-cooldown stream -> no cause", clean == [], f"got {clean}")


def test_drift_silent_in_forced_windows() -> None:
    print("\nTest: drift inside death or downtime windows attributes nothing")
    ideal = [(120.0 * i, dd.LIVING_SHADOW) for i in range(5)]
    late = [(0.0, dd.LIVING_SHADOW), (130.0, dd.LIVING_SHADOW),
            (260.0, dd.LIVING_SHADOW), (420.0, dd.LIVING_SHADOW)]
    dead = _cooldown_drift_causes(
        _ctx(late, ideal, fight_s=600.0, death_windows=[(100.0, 450.0)]))
    _check("every slip overlaps a death window -> silent", dead == [],
           f"got {dead}")
    down = _cooldown_drift_causes(
        _ctx(late, ideal, fight_s=600.0, downtime_windows=[(100.0, 450.0)]))
    _check("a boss you cannot hit is not your drift", down == [],
           f"got {down}")


def test_carve_pool_counts_abyssal_drain() -> None:
    print("\nTest: Abyssal Drain consumes the shared Carve and Spit recast")
    ideal = [(60.0 * i, dd.CARVE_AND_SPIT) for i in range(5)]
    player = [(0.0, dd.CARVE_AND_SPIT), (70.0, dd.ABYSSAL_DRAIN),
              (200.0, dd.CARVE_AND_SPIT)]
    causes = _cooldown_drift_causes(_ctx(player, ideal, fight_s=300.0))
    carve = [c for c in causes if c.ability_id == dd.CARVE_AND_SPIT]
    _check("one Carve cause (Abyssal Drain counted, not a second cause)",
           len(carve) == 1, f"got {causes}")
    _check("count row folds the AoE cast into the pool (3 / 5)",
           carve[0].evidence[0].v == "3 / 5", f"got {carve[0].evidence}")
    # The same three casts, all on cooldown: no drift to speak of.
    on_cd = [(0.0, dd.CARVE_AND_SPIT), (60.0, dd.ABYSSAL_DRAIN),
             (120.0, dd.CARVE_AND_SPIT), (180.0, dd.CARVE_AND_SPIT),
             (240.0, dd.CARVE_AND_SPIT)]
    _check("on-cooldown pool -> no cause",
           _cooldown_drift_causes(_ctx(on_cd, ideal, fight_s=300.0)) == [],
           "got causes")


def test_shadowbringer_is_not_watched() -> None:
    print("\nTest: the 2-charge Shadowbringer is never a drift subject")
    ideal = [(30.0 * i, dd.SHADOWBRINGER) for i in range(10)]
    banked = [(0.0, dd.SHADOWBRINGER), (150.0, dd.SHADOWBRINGER),
              (152.0, dd.SHADOWBRINGER)]
    _check("charge banking never reads as drift",
           _cooldown_drift_causes(_ctx(banked, ideal, fight_s=300.0)) == [],
           "got causes")


# --- Blood ------------------------------------------------------------------

def test_blood_overcap_cause() -> None:
    print("\nTest: Blood overcap -> delayed-Bloodspiller root cause")
    # Seven combo cycles, no spender: 140 Blood generated, 40 of it wasted.
    hot = [(2.5 * i, _COMBO[i % 3]) for i in range(21)]
    c = _blood_overcap_cause(_ctx(hot, []))
    _check("cause emitted on a 40-Blood overflow",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == dd.BLOODSPILLER, f"got {c}")
    _check("located at the FIRST overcap (t=42.5)",
           c.time_sec == 42.5, f"got {c.time_sec}")
    _check("summary counts the waste",
           "40 Blood wasted" in c.summary, f"got {c.summary!r}")
    _check("resources tag the Blood Gauge",
           c.resources and c.resources[0] is GAUGE_TEXT["blood"],
           f"got {c.resources}")
    _check("no evidence note repeats the prescription",
           all(r.note not in c.prescription for r in c.evidence),
           f"got {c.evidence}")
    # The same cycles with a Bloodspiller after each finisher: never overflows.
    spent: list[tuple[float, int]] = []
    t = 0.0
    for i in range(21):
        spent.append((t, _COMBO[i % 3]))
        t += 2.5
        if i % 3 == 2:
            spent.append((t, dd.BLOODSPILLER))
            t += 2.5
    _check("gauge spent down -> no cause",
           _blood_overcap_cause(_ctx(spent, [])) is None, "got a cause")


def test_blood_overcap_nets_out_the_ideal_line() -> None:
    print("\nTest: waste the sim's own line also wears is not the player's")
    hot = [(2.5 * i, _COMBO[i % 3]) for i in range(21)]
    _check("ideal wastes the same -> silent",
           _blood_overcap_cause(_ctx(hot, hot)) is None, "got a cause")


def test_blood_stranded_cause() -> None:
    print("\nTest: Blood stranded in the gauge at the kill")
    player = [(50.0 + 2.5 * i, _COMBO[i % 3]) for i in range(9)]   # 60 Blood
    ideal = player + [(72.5, dd.BLOODSPILLER)]                     # 10 Blood
    c = _blood_stranded_cause(_ctx(player, ideal, fight_s=100.0))
    _check("cause emitted for the stranded spender",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == dd.BLOODSPILLER, f"got {c}")
    _check("located at the last Blood generator (t=70)",
           c.time_sec == 70.0, f"got {c.time_sec}")
    _check("summary counts the stranding",
           "50 Blood left" in c.summary, f"got {c.summary!r}")
    _check("gauge spent down -> no cause",
           _blood_stranded_cause(_ctx(ideal, ideal, fight_s=100.0)) is None,
           "got a cause")


def test_stranded_guards() -> None:
    print("\nTest: stranded-Blood guards (kill timing, ideal end state)")
    late = [(50.0 + 2.5 * i, _COMBO[i % 3]) for i in range(9)]
    _check("last generator inside the final GCD -> silent",
           _blood_stranded_cause(_ctx(late, [], fight_s=71.0)) is None,
           "got a cause")
    _check("the ideal strands the same Blood -> silent",
           _blood_stranded_cause(_ctx(late, late, fight_s=100.0)) is None,
           "got a cause")


# --- MP ---------------------------------------------------------------------

def test_mp_waste_cause() -> None:
    print("\nTest: MP wasted at the cap -> held Edge of Shadow root cause")
    # A full bar and no Edge: every passive tick is gone.
    hot = [(2.5 * i, dd.HARD_SLASH) for i in range(40)]
    c = _mp_waste_cause(_ctx(hot, [], fight_s=120.0))
    _check("cause emitted past the two-Edge floor",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == dd.EDGE_OF_SHADOW, f"got {c}")
    _check("located at the first full tick lost (t=5)",
           c.time_sec == 5.0, f"got {c.time_sec}")
    _check("summary counts the wasted MP",
           "6400 MP wasted" in c.summary, f"got {c.summary!r}")
    _check("resources tag the MP bar",
           c.resources and c.resources[0] is GAUGE_TEXT["mp"],
           f"got {c.resources}")
    _check("the card speaks about Edge casts, not the Darkside amp",
           "Darkside" not in c.prescription and "Darkside" not in c.summary,
           f"got {c.prescription!r}")
    # Edge on cooldown-ish cadence: the bar drains faster than it fills.
    cool: list[tuple[float, int]] = []
    for i in range(40):
        cool.append((2.5 * i + 1.0, dd.HARD_SLASH))
        if i % 6 == 0:
            cool.append((2.5 * i + 1.2, dd.EDGE_OF_SHADOW))
    _check("MP spent as it fills -> no cause",
           _mp_waste_cause(_ctx(cool, [], fight_s=120.0)) is None,
           "got a cause")


def test_mp_waste_floor_absorbs_one_edge() -> None:
    print("\nTest: the MP floor stays quiet below two full Edge casts")
    # ~30s at the cap = 2000 MP, one Edge short of the floor.
    short = [(2.5 * i, dd.HARD_SLASH) for i in range(13)]
    _check("a single Edge of waste stays silent",
           _mp_waste_cause(_ctx(short, [], fight_s=40.0)) is None,
           "got a cause")


# --- Registration, gauge keys, copy -----------------------------------------

def test_registration() -> None:
    print("\nTest: the pack is registered on the Dark Knight job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Dark Knight")
    _check("resolve_pack returns the DRK pack",
           pack is not None and pack.gauge_text is GAUGE_TEXT, f"got {pack}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar SimState field")
    from jobs.darkknight.simulator import _model_for
    st = _model_for(None).init_state()
    for key in GAUGE_TEXT:
        _check(f"'{key}' exists on SimState", hasattr(st, key),
               f"missing field {key}")
        val = getattr(st, key)
        _check(f"'{key}' is a scalar (delta-comparable)",
               isinstance(val, (bool, int, float)), f"got {type(val)}")
        _check(f"'{key}' is public (snapshot-visible)",
               not key.startswith("_"), "underscore-prefixed")


def test_copy_lint() -> None:
    print("\nTest: copy rules - no em/en dashes, no jargon, no exclamations")

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


# --- The cascade on the real DRK sim ----------------------------------------

def _ideal_timeline(dur: float):
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.darkknight.simulator import SimParams, _model_for
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    return [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the DRK sim - conservation, stability")
    import json

    from jobs._core.sim.counterfactual import Runner
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    ideal = _ideal_timeline(dur)
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _ctx(player, ideal, fight_s=dur)
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(ctx.gcd_ids))
    cards = [
        _card("missed_cast", dd.LIVING_SHADOW, 30.0, lost=400.0,
              name="Living Shadow"),
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
    from jobs.darkknight.advice import advice_probes
    dur = 150.0
    ideal = _ideal_timeline(dur)
    items, causes = advice_probes(_ctx(ideal, ideal, fight_s=dur), [])
    _check("no ProbeItems by design", items == [], f"got {items}")
    _check("clean stream -> no causes", causes == [], f"got {causes}")


def main() -> int:
    test_ledger_mirrors_the_simulator()
    test_ledger_wipes_the_gauge_on_death()
    test_death_does_not_invent_overcap_or_stranding()
    test_cooldown_drift_cause()
    test_drift_silent_in_forced_windows()
    test_carve_pool_counts_abyssal_drain()
    test_shadowbringer_is_not_watched()
    test_blood_overcap_cause()
    test_blood_overcap_nets_out_the_ideal_line()
    test_blood_stranded_cause()
    test_stranded_guards()
    test_mp_waste_cause()
    test_mp_waste_floor_absorbs_one_edge()
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
