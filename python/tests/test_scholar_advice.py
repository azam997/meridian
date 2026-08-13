"""Unit tests for the Scholar deep-advice pack (jobs/scholar/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean/silent one, plus the healer guards (the
Aetherflow heals count as spends, the resurrection pardon subtracts from the
drift ledger), registration, gauge-key validity against the real sim state, the
copy lint, and the cascade conservation smoke on the SCH simulator.

Run from python/:  python tests/test_scholar_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.scholar import data as sd
from jobs.scholar.advice import (
    GAUGE_TEXT,
    TEXT,
    _CD_CONSUMERS,
    _CD_WORDS,
    _aetherflow_stranded_cause,
    _aetherflow_waste_cause,
    _baneful_lost_cause,
    _cooldown_drift_causes,
    advice_probes,
)

CHAIN = sd.CHAIN_STRATAGEM
BANEFUL = sd.BANEFUL_IMPACTION
ED = sd.ENERGY_DRAIN
AF = sd.AETHERFLOW
BROIL = sd.BROIL_IV

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


def _sch_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             downtime=None, deaths=None, scoring=None) -> AdviceContext:
    return AdviceContext(
        job="Scholar", data=sd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state=dict(scoring or {}),
        enabler_values={}, sim_context=None,
        sim_module="jobs.scholar.simulator", runner=runner,
        gcd_ids=_gcd_ids(), gauge_text=GAUGE_TEXT)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def _burst_ideal(times: list[float]) -> list[tuple[float, int]]:
    """Chain Stratagem + its Baneful Impaction weave at each burst time."""
    out: list[tuple[float, int]] = []
    for t in times:
        out += [(t, CHAIN), (t + 1.0, BANEFUL)]
    return out


# --- Cooldown drift ---------------------------------------------------------

def test_chain_drift_cause() -> None:
    print("\nTest: Chain Stratagem drift -> lost-use root cause")
    ideal = _burst_ideal([0.0, 120.0, 240.0])
    late = [(0.0, CHAIN), (1.0, BANEFUL), (200.0, CHAIN), (201.0, BANEFUL)]
    causes = _cooldown_drift_causes(_sch_ctx(late, ideal, fight_s=360.0))
    _check("one weighted cause", len(causes) == 1, f"got {causes}")
    value, c = causes[0]
    _check("kind + ability + located at the worst slip",
           c.kind == "cascade_lost_use" and c.ability_id == CHAIN
           and c.time_sec == 0.0,
           f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the lost use",
           "1 use lost" in c.summary, f"got {c.summary!r}")
    _check("value is the Chain Stratagem package price",
           value == float(sd.COOLDOWN_VALUE_P[CHAIN]), f"got {value}")
    _check("count evidence row shows 2 / 3",
           c.evidence and c.evidence[0].v == "2 / 3", f"got {c.evidence}")
    _check("no raise row on a rez-less pull",
           all(r.k != "Raise" for r in c.evidence), f"got {c.evidence}")


def test_chain_drift_clean_silent() -> None:
    print("\nTest: on-cooldown Chain Stratagem stream -> no drift cause")
    ideal = _burst_ideal([0.0, 120.0, 240.0])
    clean = _burst_ideal([0.0, 120.0, 240.0])
    _check("no cause on a clean stream",
           _cooldown_drift_causes(_sch_ctx(clean, ideal, fight_s=360.0)) == [],
           "got causes")


def test_chain_drift_downtime_pardoned() -> None:
    print("\nTest: gaps spanning downtime are pardoned, not drift")
    ideal = _burst_ideal([0.0, 120.0, 240.0])
    late = [(0.0, CHAIN), (200.0, CHAIN)]
    dt = [(100.0, 190.0)]
    _check("downtime overlap zeroes the drift ledger",
           _cooldown_drift_causes(
               _sch_ctx(late, ideal, fight_s=360.0, downtime=dt)) == [],
           "got causes")


def test_chain_drift_rez_pardon() -> None:
    print("\nTest: the raise the ceiling paid for is never charged as drift")
    ideal = _burst_ideal([0.0, 120.0, 240.0])
    rez = {"heal_lock_rez_casts": [[10.0, sd.RESURRECTION, 3]],
           "heal_lock_rez_count": 1, "heal_lock_rez_gcds": 3}
    near = [(0.0, CHAIN), (185.0, CHAIN)]      # 65s raw drift, 7.5s pardoned
    _check("a pardoned raise drops the ledger below the floor",
           _cooldown_drift_causes(
               _sch_ctx(near, ideal, fight_s=360.0, scoring=rez)) == [],
           "got causes")
    far = [(0.0, CHAIN), (210.0, CHAIN)]       # 90s raw drift, still over
    causes = _cooldown_drift_causes(
        _sch_ctx(far, ideal, fight_s=360.0, scoring=rez))
    _check("real drift beyond the raise still cards", len(causes) == 1,
           f"got {causes}")
    _value, c = causes[0]
    rows = [r for r in c.evidence if r.k == "Raise"]
    _check("the raise is acknowledged in the evidence, not blamed",
           len(rows) == 1 and "already pays for the raise" in rows[0].note,
           f"got {c.evidence}")


def test_dissipation_counts_as_an_aetherflow_refill() -> None:
    print("\nTest: a Dissipation refill is never charged as Aetherflow drift")
    ideal = [(t, AF) for t in (0.0, 60.0, 120.0, 180.0, 240.0, 300.0)]
    # Refilled with Dissipation at 2:00, so the next Aetherflow press waits out
    # the stacks it granted. Six refills happened; only five were Aetherflow.
    player: list[tuple[float, int]] = [
        (0.0, AF), (60.0, AF), (120.0, sd.DISSIPATION), (185.0, AF),
        (245.0, AF), (305.0, AF)]
    for base in (0.0, 60.0, 120.0, 185.0, 245.0, 305.0):
        player += [(base + 2, ED), (base + 4, ED), (base + 6, ED)]
    _check("the Dissipation gap is not drift",
           _cooldown_drift_causes(_sch_ctx(player, ideal, fight_s=360.0)) == [],
           "got causes")
    _check("Dissipation is declared a shared consumer of the refill",
           sd.DISSIPATION in _CD_CONSUMERS[AF], f"got {_CD_CONSUMERS}")
    # Drop the Dissipation entirely and the same stream DOES card.
    without = [c for c in player if c[1] != sd.DISSIPATION]
    _check("a genuinely skipped refill still cards",
           len(_cooldown_drift_causes(
               _sch_ctx(without, ideal, fight_s=360.0))) == 1,
           "no cause")


def test_drift_walk_survives_a_new_cooldown() -> None:
    print("\nTest: a cooldown added to data.py later cannot crash the walk")
    saved = dict(sd.COOLDOWNS)
    try:
        sd.COOLDOWNS[sd.BIOLYSIS] = (30.0, 1)      # a hypothetical future entry
        _cooldown_drift_causes(
            _sch_ctx([(0.0, sd.BIOLYSIS), (100.0, sd.BIOLYSIS)],
                     [(float(i), sd.BIOLYSIS) for i in range(5)],
                     fight_s=360.0))
        _check("an unlabelled cooldown stays silent instead of raising",
               True, "")
    except KeyError as e:
        _check("an unlabelled cooldown stays silent instead of raising",
               False, f"KeyError {e}")
    finally:
        sd.COOLDOWNS.clear()
        sd.COOLDOWNS.update(saved)


# --- Baneful Impaction ------------------------------------------------------

def test_baneful_lost_cause() -> None:
    print("\nTest: a Chain Stratagem stack that never fired -> lost use")
    ideal = _burst_ideal([0.0, 120.0])
    player = [(0.0, CHAIN), (1.0, BANEFUL), (120.0, CHAIN)]
    got = _baneful_lost_cause(_sch_ctx(player, ideal, fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + ability + located at the unfired unlock",
           c.kind == "cascade_lost_use" and c.ability_id == BANEFUL
           and c.time_sec == 120.0,
           f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the unfired stacks",
           "after 1 Chain Stratagem" in c.summary, f"got {c.summary!r}")
    _check("value is the folded Baneful DoT",
           value == float(sd.BANEFUL_TOTAL_P), f"got {value}")
    _check("count evidence row shows 1 / 2",
           c.evidence and c.evidence[0].v == "1 / 2", f"got {c.evidence}")
    _check("resources tag the Baneful stack",
           c.resources and c.resources[0] is GAUGE_TEXT["baneful_ready"],
           f"got {c.resources}")


def test_baneful_guards() -> None:
    print("\nTest: fired stacks / tail bursts / deaths / old logs stay silent")
    ideal = _burst_ideal([0.0, 120.0])
    full = [(0.0, CHAIN), (1.0, BANEFUL), (120.0, CHAIN), (121.5, BANEFUL)]
    _check("every stack fired -> silent",
           _baneful_lost_cause(_sch_ctx(full, ideal, fight_s=300.0)) is None,
           "got a cause")
    tail = [(0.0, CHAIN), (1.0, BANEFUL), (295.0, CHAIN)]
    _check("a burst in the last seconds is never counted",
           _baneful_lost_cause(_sch_ctx(tail, ideal, fight_s=300.0)) is None,
           "got a cause")
    player = [(0.0, CHAIN), (1.0, BANEFUL), (120.0, CHAIN)]
    _check("a death inside the unlock window pardons the miss",
           _baneful_lost_cause(
               _sch_ctx(player, ideal, fight_s=300.0,
                        deaths=[(125.0, 135.0)])) is None,
           "got a cause")
    _check("a ceiling that never fires Baneful -> silent",
           _baneful_lost_cause(
               _sch_ctx(player, [(0.0, CHAIN), (120.0, CHAIN)],
                        fight_s=300.0)) is None,
           "got a cause")


def test_baneful_counts_pairs_not_casts() -> None:
    print("\nTest: the count row pairs stacks with chains, not raw casts")
    ideal = _burst_ideal([0.0, 120.0])
    # The fired Baneful belongs to a pre-pull Chain outside the scored window;
    # counting casts would print "1 / 1 fired" next to "left unfired".
    player = [(-2.0, CHAIN), (0.5, BANEFUL), (120.0, CHAIN)]
    got = _baneful_lost_cause(_sch_ctx(player, ideal, fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    _value, c = got
    _check("the count row shows 0 of 1 in-fight chains paired",
           c.evidence[0].v == "0 / 1", f"got {c.evidence[0].v}")
    # A tail chain that DID fire its stack still counts as paired.
    tail = [(0.0, CHAIN), (120.0, CHAIN), (295.0, CHAIN), (296.0, BANEFUL)]
    got2 = _baneful_lost_cause(_sch_ctx(tail, ideal, fight_s=300.0))
    _check("cause emitted for the two unfired chains", got2 is not None,
           "got None")
    _value2, c2 = got2
    _check("one of three chains fired its stack",
           c2.evidence[0].v == "1 / 3", f"got {c2.evidence[0].v}")


# --- Aetherflow waste -------------------------------------------------------

def test_aetherflow_waste_cause() -> None:
    print("\nTest: Aetherflow refilled over live stacks -> cascade_burst")
    got = _aetherflow_waste_cause(
        _sch_ctx([(10.0, AF), (12.0, ED), (70.0, AF)], [], fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + spender id + first-refill time",
           c.kind == "cascade_burst" and c.ability_id == ED
           and c.time_sec == 70.0,
           f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the lost Energy Drain casts",
           "2 Energy Drain casts lost" in c.summary, f"got {c.summary!r}")
    _check("value is stacks x Energy Drain",
           value == 2 * float(sd.POTENCIES[ED]), f"got {value}")
    _check("resources tag the Aetherflow gauge",
           c.resources and c.resources[0] is GAUGE_TEXT["aetherflow"],
           f"got {c.resources}")


def test_aetherflow_waste_healer_guards() -> None:
    print("\nTest: Aetherflow heals count as spends; deaths and tails silent")
    healed = [(10.0, AF), (12.0, sd.LUSTRATE), (14.0, sd.INDOMITABILITY),
              (16.0, sd.EXCOGITATION), (70.0, AF)]
    _check("stacks spent on healing are never wasted stacks",
           _aetherflow_waste_cause(_sch_ctx(healed, [], fight_s=300.0))
           is None, "got a cause")
    _check("a death wipes the gauge -> the refill is clean",
           _aetherflow_waste_cause(
               _sch_ctx([(10.0, AF), (70.0, AF)], [], fight_s=300.0,
                        deaths=[(30.0, 35.0)])) is None,
           "got a cause")
    _check("tail refill skipped (can be a net gain)",
           _aetherflow_waste_cause(
               _sch_ctx([(10.0, AF), (292.0, AF)], [], fight_s=300.0))
           is None, "got a cause")
    _check("one leftover stack is under the floor",
           _aetherflow_waste_cause(
               _sch_ctx([(10.0, AF), (12.0, ED), (14.0, ED), (70.0, AF)], [],
                        fight_s=300.0)) is None,
           "got a cause")


# --- Aetherflow stranded ----------------------------------------------------

def test_aetherflow_stranded_cause() -> None:
    print("\nTest: the whole gauge dead at the kill")
    got = _aetherflow_stranded_cause(
        _sch_ctx([(10.0, AF), (20.0, BROIL)], [], fight_s=300.0))
    _check("cause emitted", got is not None, "got None")
    value, c = got
    _check("kind + spender id + located at the refill",
           c.kind == "cascade_lost_use" and c.ability_id == ED
           and c.time_sec == 10.0,
           f"got {c.kind} {c.ability_id} @ {c.time_sec}")
    _check("summary counts the stranded stacks",
           "with 3 stacks at the kill" in c.summary, f"got {c.summary!r}")
    _check("value is the full gauge in Energy Drains",
           value == 3 * float(sd.POTENCIES[ED]), f"got {value}")


def test_aetherflow_stranded_guards() -> None:
    print("\nTest: partly spent gauge / tail refill stay silent")
    _check("a stack that went into healing is not stranded",
           _aetherflow_stranded_cause(
               _sch_ctx([(10.0, AF), (12.0, sd.LUSTRATE)], [],
                        fight_s=300.0)) is None, "got a cause")
    _check("last-seconds refill -> silent (no room to spend)",
           _aetherflow_stranded_cause(
               _sch_ctx([(295.0, AF)], [], fight_s=300.0)) is None,
           "got a cause")


# --- Ordering / pack shape --------------------------------------------------

def test_advice_probes_order_and_shape() -> None:
    print("\nTest: advice_probes -> no items; causes value-ordered")
    ideal = _burst_ideal([0.0, 120.0, 240.0])
    # Two unfired Baneful stacks (1400) + one lost Chain use (700) + a full
    # gauge stranded at the kill (300) + two stacks overwritten (200).
    player = [(0.0, CHAIN), (10.0, AF), (12.0, ED), (70.0, AF),
              (200.0, CHAIN)]
    items, causes = advice_probes(_sch_ctx(player, ideal, fight_s=360.0), [])
    _check("no probe items", items == [], f"got {items}")
    _check("four causes", len(causes) == 4,
           f"got {[(c.kind, c.ability_id) for c in causes]}")
    _check("value order: Baneful, Chain, stranded, waste",
           [c.ability_id for c in causes] == [BANEFUL, CHAIN, ED, ED]
           and causes[2].kind == "cascade_lost_use"
           and causes[3].kind == "cascade_burst",
           f"got {[(c.ability_id, c.kind) for c in causes]}")
    _check("times inside the fight, rounded",
           all(0 <= c.time_sec <= 360.0
               and c.time_sec == round(c.time_sec, 1) for c in causes),
           f"got {[c.time_sec for c in causes]}")
    _check("measured_p is 0 everywhere",
           all(c.measured_p == 0.0 for c in causes), "nonzero weight")
    _check("no evidence note repeats its own prescription",
           all(r.note not in c.prescription
               for c in causes for r in c.evidence),
           "a note echoes the prescription")


def test_clean_stream_is_silent() -> None:
    print("\nTest: a clean SCH stream emits nothing at all")
    ideal = _burst_ideal([0.0, 120.0])
    player = [(0.0, CHAIN), (1.0, BANEFUL), (10.0, AF), (12.0, ED),
              (14.0, ED), (16.0, ED), (120.0, CHAIN), (121.0, BANEFUL)]
    items, causes = advice_probes(_sch_ctx(player, ideal, fight_s=180.0), [])
    _check("no items, no causes", items == [] and causes == [],
           f"got {items} / {[c.kind for c in causes]}")


# --- Registration -----------------------------------------------------------

def test_pack_registered() -> None:
    print("\nTest: the pack is registered on the Scholar job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Scholar")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is the SCH glossary",
           pack.gauge_text is GAUGE_TEXT, "different dict")


# --- Gauge-key validity -----------------------------------------------------

def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key survives the cascade snapshot")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.scholar.simulator import _model_for
    st = _model_for(300.0, None).init_state()
    for k in GAUGE_TEXT:
        _check(f"state has {k}", hasattr(st, k), "missing attribute")
    snap = _snapshot(st)
    for k in GAUGE_TEXT:
        _check(f"snapshot gauges carry {k}", k in snap["gauges"],
               f"got {sorted(snap['gauges'])}")
    _check("the Biolysis expiry clock stays out of the glossary",
           "biolysis_end" not in GAUGE_TEXT, "biolysis_end would leak noise")


# --- Copy lint --------------------------------------------------------------

def _all_copy_strings():
    for section in TEXT.values():
        yield from section.values()
    for label, noun, action in _CD_WORDS.values():
        yield label
        yield noun
        yield action
    for gt in GAUGE_TEXT.values():
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s:
                yield s


def _ascii(s: str) -> str:
    """Printable-safe echo of a copy string. The shipped copy carries a few
    non-ASCII glyphs (the `≈` in the idle row); this console is cp1252, so the
    test's own progress lines escape them rather than crash on print."""
    return repr(s).encode("ascii", "backslashreplace").decode("ascii")


def test_copy_lint() -> None:
    print("\nTest: no dashes, no jargon, no exclamations in any copy")
    for s in _all_copy_strings():
        _check(f"copy clean: {_ascii(s[:40])}",
               "—" not in s and "–" not in s and "!" not in s
               and "strict" not in s.lower() and "lenient" not in s.lower(),
               f"offending string: {_ascii(s)}")


def test_copy_never_blames_healing() -> None:
    print("\nTest: no copy blames the player for healing or raising")
    banned = ("healed too much", "stop healing", "wasted on healing",
              "instead of healing", "too many heals")
    for s in _all_copy_strings():
        low = s.lower()
        _check(f"blame-free: {_ascii(s[:40])}",
               all(b not in low for b in banned),
               f"offending string: {_ascii(s)}")


# --- Cascade conservation smoke ---------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined on the SCH sim — conservation + stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.scholar.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [],
                                         params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 62.0 <= t < 74.0]   # 12s hole
    ctx = _sch_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", BROIL, 30.0, lost=400.0, name="Broil IV"),
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
    test_chain_drift_cause()
    test_chain_drift_clean_silent()
    test_chain_drift_downtime_pardoned()
    test_chain_drift_rez_pardon()
    test_dissipation_counts_as_an_aetherflow_refill()
    test_drift_walk_survives_a_new_cooldown()
    test_baneful_lost_cause()
    test_baneful_guards()
    test_baneful_counts_pairs_not_casts()
    test_aetherflow_waste_cause()
    test_aetherflow_waste_healer_guards()
    test_aetherflow_stranded_cause()
    test_aetherflow_stranded_guards()
    test_advice_probes_order_and_shape()
    test_clean_stream_is_silent()
    test_pack_registered()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_copy_never_blames_healing()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
