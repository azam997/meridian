"""Unit tests for the Summoner deep-advice pack (jobs/summoner/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean/silent one, plus registration, gauge-key
validity against the real sim state, the copy lint, and the cascade
conservation smoke on the SMN simulator.

Run from python/:  python tests/test_summoner_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.summoner import data as sd
from jobs.summoner.advice import (
    GAUGE_TEXT,
    TEXT,
    _CD_WORDS,
    _PRIMAL_WORDS,
    _aetherflow_overcap_cause,
    _aetherflow_stranded_cause,
    _cooldown_drift_causes,
    _gem_waste_cause,
    advice_probes,
)

SOLAR = sd.SUMMON_SOLAR_BAHAMUT
ED = sd.ENERGY_DRAIN
NEC = sd.NECROTIZE

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _gcd_ids() -> frozenset[int]:
    return frozenset(a for a in sd.POTENCIES
                     if a not in sd.OGCD_IDS and a not in sd.DEFENSIVE_IDS)


def _smn_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             downtime=None, deaths=None) -> AdviceContext:
    return AdviceContext(
        job="Summoner", data=sd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.summoner.simulator", runner=runner,
        gcd_ids=_gcd_ids(), gauge_text=GAUGE_TEXT)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def _demis(times: list[float]) -> list[tuple[float, int]]:
    """Demi casts at the given times, cycling the real order."""
    return [(t, sd.DEMI_CYCLE[i % 4]) for i, t in enumerate(times)]


# --- Cooldown drift ---------------------------------------------------------

def test_demi_drift_cause() -> None:
    print("\nTest: demi-pool drift -> lost-window root cause")
    ideal = _demis([1.0, 61.0, 121.0, 181.0, 241.0])       # 5 windows fit
    late = _demis([1.0, 76.0, 151.0, 226.0])               # 15s over per gap
    causes = _cooldown_drift_causes(_smn_ctx(late, ideal, fight_s=300.0))
    _check("one weighted cause", len(causes) == 1, f"got {causes}")
    value, c = causes[0]
    _check("kind + pool key + located at the worst slip",
           c.kind == "cascade_lost_use" and c.ability_id == SOLAR
           and c.time_sec == 1.0, f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the lost window",
           "1 window lost" in c.summary, f"got {c.summary!r}")
    _check("value is the demi package price",
           value == float(sd.COOLDOWN_VALUE_P[SOLAR]), f"got {value}")
    _check("count evidence row shows 4 / 5",
           c.evidence and c.evidence[0].v == "4 / 5", f"got {c.evidence}")


def test_demi_drift_clean_silent() -> None:
    print("\nTest: on-cadence demi stream -> no drift cause")
    ideal = _demis([1.0, 61.0, 121.0, 181.0, 241.0])
    clean = _demis([1.0, 61.0, 121.0, 181.0, 241.0])
    _check("no cause on a clean stream",
           _cooldown_drift_causes(_smn_ctx(clean, ideal, fight_s=300.0)) == [],
           "got causes")


def test_demi_drift_downtime_pardoned() -> None:
    print("\nTest: gaps spanning downtime are pardoned, not drift")
    ideal = _demis([1.0, 61.0, 121.0, 181.0, 241.0])
    late = _demis([1.0, 76.0, 151.0, 226.0])
    dt = [(61.0, 76.0), (136.0, 151.0), (211.0, 226.0)]
    _check("downtime overlap zeroes the drift ledger",
           _cooldown_drift_causes(
               _smn_ctx(late, ideal, fight_s=300.0, downtime=dt)) == [],
           "got causes")


# --- Gem waste --------------------------------------------------------------

def test_gem_waste_cause() -> None:
    print("\nTest: gems overwritten by the next demi -> lost-phase cause")
    player = [(1.0, SOLAR), (20.0, sd.SUMMON_GARUDA_II),
              (65.0, sd.SUMMON_BAHAMUT)]     # ruby + topaz still held
    got = _gem_waste_cause(_smn_ctx(player, [], fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + located at the overwriting demi",
           c.kind == "cascade_lost_use" and c.time_sec == 65.0,
           f"got {c.kind} @ {c.time_sec}")
    _check("lead ability is the highest-value wasted phase (Ifrit)",
           c.ability_id == sd.SUMMON_IFRIT_II, f"got {c.ability_id}")
    _check("summary counts both phases",
           "2 primal phases lost" in c.summary, f"got {c.summary!r}")
    _check("value sums the wasted phases",
           value == float(sd.PRIMAL_PHASE_VALUE_P[sd.SUMMON_IFRIT_II]
                          + sd.PRIMAL_PHASE_VALUE_P[sd.SUMMON_TITAN_II]),
           f"got {value}")
    _check("evidence names the phases",
           c.evidence and "Ifrit and Titan" in c.evidence[0].v,
           f"got {c.evidence}")
    _check("resources tag the wasted gems",
           c.resources and c.resources[0] is GAUGE_TEXT["ruby_gem"],
           f"got {c.resources}")


def test_gem_waste_clean_and_guards() -> None:
    print("\nTest: full phases / tail holds / deaths stay silent")
    full = [(1.0, SOLAR), (17.0, sd.SUMMON_GARUDA_II),
            (30.0, sd.SUMMON_IFRIT_II), (45.0, sd.SUMMON_TITAN_II),
            (65.0, sd.SUMMON_BAHAMUT)]
    _check("all three phases run -> silent",
           _gem_waste_cause(_smn_ctx(full, [], fight_s=300.0)) is None,
           "got a cause")
    tail = [(1.0, SOLAR), (20.0, sd.SUMMON_GARUDA_II)]   # 2 gems die at kill
    _check("tail-held gems are never counted",
           _gem_waste_cause(_smn_ctx(tail, [], fight_s=60.0)) is None,
           "got a cause")
    player = [(1.0, SOLAR), (20.0, sd.SUMMON_GARUDA_II),
              (65.0, sd.SUMMON_BAHAMUT)]
    _check("a death inside the cycle pardons the waste",
           _gem_waste_cause(_smn_ctx(player, [], fight_s=300.0,
                                     deaths=[(30.0, 40.0)])) is None,
           "got a cause")


# --- Aetherflow overcap -----------------------------------------------------

def test_aetherflow_overcap_cause() -> None:
    print("\nTest: Energy Drain over live stacks -> cascade_burst")
    got = _aetherflow_overcap_cause(
        _smn_ctx([(10.0, ED), (70.0, ED)], [], fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + spender id + first-refill time",
           c.kind == "cascade_burst" and c.ability_id == ED
           and c.time_sec == 70.0, f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the lost Necrotize casts",
           "2 Necrotize casts lost" in c.summary, f"got {c.summary!r}")
    _check("value is stacks x Necrotize",
           value == 2 * sd.AETHERFLOW_GAUGE.value_p_per_unit, f"got {value}")
    _check("resources tag the aetherflow gauge",
           c.resources and c.resources[0] is GAUGE_TEXT["aetherflow"],
           f"got {c.resources}")


def test_aetherflow_overcap_guards() -> None:
    print("\nTest: spent stacks / deaths / tail refills stay silent")
    clean = [(10.0, ED), (12.0, NEC), (14.0, NEC), (70.0, ED)]
    _check("both stacks spent -> silent",
           _aetherflow_overcap_cause(_smn_ctx(clean, [], fight_s=300.0))
           is None, "got a cause")
    _check("a death wipes the gauge -> the refill is clean",
           _aetherflow_overcap_cause(
               _smn_ctx([(10.0, ED), (70.0, ED)], [], fight_s=300.0,
                        deaths=[(30.0, 35.0)])) is None,
           "got a cause")
    _check("tail refill skipped (can be a net gain)",
           _aetherflow_overcap_cause(
               _smn_ctx([(10.0, ED), (292.0, ED)], [], fight_s=300.0))
           is None, "got a cause")


# --- Aetherflow stranded ----------------------------------------------------

def test_aetherflow_stranded_cause() -> None:
    print("\nTest: a stack dead in the gauge at the kill")
    got = _aetherflow_stranded_cause(
        _smn_ctx([(10.0, ED), (12.0, NEC)], [], fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + spender id + located at the last Energy Drain",
           c.kind == "cascade_lost_use" and c.ability_id == NEC
           and c.time_sec == 10.0, f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the stranded stack",
           "1 stack at the kill" in c.summary, f"got {c.summary!r}")
    _check("value is one Necrotize",
           value == sd.AETHERFLOW_GAUGE.value_p_per_unit, f"got {value}")


def test_aetherflow_stranded_guards() -> None:
    print("\nTest: spent gauge / tail Energy Drain stay silent")
    _check("all spent -> silent",
           _aetherflow_stranded_cause(
               _smn_ctx([(10.0, ED), (12.0, NEC), (14.0, NEC)], [],
                        fight_s=300.0)) is None, "got a cause")
    _check("last-seconds Energy Drain -> silent (no room to spend)",
           _aetherflow_stranded_cause(
               _smn_ctx([(295.0, ED)], [], fight_s=300.0)) is None,
           "got a cause")


# --- Ordering / pack shape --------------------------------------------------

def test_advice_probes_order_and_shape() -> None:
    print("\nTest: advice_probes -> no items; causes value-ordered")
    ideal = _demis([1.0, 61.0, 121.0, 181.0, 241.0])
    # Demi drift (3800) + a stranded aetherflow stack (500) in one stream;
    # full primal phases inside every cycle so the gem ledger stays silent.
    phases: list[tuple[float, int]] = []
    for base in (17.0, 92.0, 167.0):
        phases += [(base, sd.SUMMON_GARUDA_II),
                   (base + 12.0, sd.SUMMON_IFRIT_II),
                   (base + 26.0, sd.SUMMON_TITAN_II)]
    player = (_demis([1.0, 76.0, 151.0, 226.0]) + phases
              + [(10.0, ED), (12.0, NEC)])
    items, causes = advice_probes(_smn_ctx(player, ideal, fight_s=300.0), [])
    _check("no probe items", items == [], f"got {items}")
    _check("two causes", len(causes) == 2, f"got {[c.kind for c in causes]}")
    _check("demi drift leads (highest value)",
           causes[0].ability_id == SOLAR and causes[1].ability_id == NEC,
           f"got {[c.ability_id for c in causes]}")
    _check("times inside the fight, rounded",
           all(0 <= c.time_sec <= 300.0
               and c.time_sec == round(c.time_sec, 1) for c in causes),
           f"got {[c.time_sec for c in causes]}")
    _check("measured_p is 0 everywhere",
           all(c.measured_p == 0.0 for c in causes), "nonzero weight")


# --- Registration -----------------------------------------------------------

def test_pack_registered() -> None:
    print("\nTest: the pack is registered on the Summoner job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Summoner")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is the SMN glossary",
           pack.gauge_text is GAUGE_TEXT, "different dict")


# --- Gauge-key validity -----------------------------------------------------

def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key survives the cascade snapshot")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.summoner.simulator import _model_for
    st = _model_for(None).init_state()
    for k in GAUGE_TEXT:
        _check(f"state has {k}", hasattr(st, k), "missing attribute")
    snap = _snapshot(st)
    for k in GAUGE_TEXT:
        _check(f"snapshot gauges carry {k}", k in snap["gauges"],
               f"got {sorted(snap['gauges'])}")


# --- Copy lint --------------------------------------------------------------

def _all_copy_strings():
    for section in TEXT.values():
        yield from section.values()
    for label, noun, action in _CD_WORDS.values():
        yield label
        yield noun
        yield action
    yield from _PRIMAL_WORDS.values()
    for gt in GAUGE_TEXT.values():
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s:
                yield s


def test_copy_lint() -> None:
    print("\nTest: no dashes, no jargon, no exclamations in any copy")
    for s in _all_copy_strings():
        _check(f"copy clean: {s[:40]!r}",
               "—" not in s and "–" not in s and "!" not in s
               and "strict" not in s.lower() and "lenient" not in s.lower(),
               f"offending string: {s!r}")


# --- Cascade conservation smoke ---------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined on the SMN sim — conservation + stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.summoner.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 62.0 <= t < 74.0]   # 12s hole
    ctx = _smn_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", sd.RUIN_III, 30.0, lost=400.0, name="Ruin III"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    _check("advice list present", isinstance(out1["advice"], list), "missing")
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
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")


def main() -> int:
    test_demi_drift_cause()
    test_demi_drift_clean_silent()
    test_demi_drift_downtime_pardoned()
    test_gem_waste_cause()
    test_gem_waste_clean_and_guards()
    test_aetherflow_overcap_cause()
    test_aetherflow_overcap_guards()
    test_aetherflow_stranded_cause()
    test_aetherflow_stranded_guards()
    test_advice_probes_order_and_shape()
    test_pack_registered()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
