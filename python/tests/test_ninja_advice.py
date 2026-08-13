"""Unit tests for the Ninja deep-advice pack (jobs/ninja/advice.py).

Covers each RootCause producer (an emitting synthetic stream + a clean stream
that stays silent), the AdvicePack registration, GAUGE_TEXT key validity
against the real sim state, the user-facing copy rules, and the cascade
conservation smoke on the NIN simulator.

Run from python/:  python tests/test_ninja_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.ninja import data as nd
from jobs.ninja.advice import (
    GAUGE_TEXT, TEXT, _charge_cap_cause, _drift_causes, _ninki_overcap_cause,
    _ninki_stranded_cause, _walk_ninki, advice_probes,
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


def _nin_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             downtime=None, deaths=None):
    from jobs._core.advice import AdviceContext
    gcds = frozenset(nd.POTENCIES) - nd.OGCD_IDS
    return AdviceContext(
        job="Ninja", data=nd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.ninja.simulator", runner=runner, gcd_ids=gcds,
        gauge_text=dict(GAUGE_TEXT))


# --- Registration / allowlist / copy ----------------------------------------

def test_registration_returns_pack() -> None:
    print("\nTest: the registry serves the NIN pack with its gauge glossary")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Ninja")
    _check("pack registered", pack is not None, "got None")
    _check("gauge_text is the NIN glossary", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text!r}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a real SimState scalar field")
    from jobs.ninja.simulator import _model_for
    state = _model_for(None).init_state()
    for k in sorted(GAUGE_TEXT):
        _check(f"state has '{k}'", hasattr(state, k),
               f"SimState lacks {k!r}")
        val = getattr(state, k)
        _check(f"'{k}' is a public scalar", isinstance(val, (int, float, bool)),
               f"got {type(val)}")


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no strict/lenient jargon)")

    def _walk(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        strings.extend(s for s in (gt.label, gt.short, gt.over_note,
                                   gt.under_note) if s)
    _check("some copy collected", len(strings) > 10, f"got {len(strings)}")
    for s in strings:
        _check(f"no em dash in {s[:30]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:30]!r}", "–" not in s, s)
        low = s.lower()
        _check(f"no strict/lenient in {s[:30]!r}",
               "strict" not in low and "lenient" not in low, s)


# --- Cooldown drift ----------------------------------------------------------

def test_drift_cause_emits() -> None:
    print("\nTest: Kunai's Bane drift -> lost-use root cause (Suiton framing)")
    ideal = [(60.0 * i, nd.KUNAIS_BANE) for i in range(5)]
    late = [(90.0 * i, nd.KUNAIS_BANE) for i in range(3)]   # 30s over per gap
    causes = _drift_causes(_nin_ctx(late, ideal, fight_s=300.0))
    kb = [c for c in causes if c.ability_id == nd.KUNAIS_BANE]
    _check("KB lost-use cause emitted",
           len(kb) == 1 and kb[0].kind == "cascade_lost_use",
           f"got {causes}")
    _check("summary counts the lost uses", "2 uses lost" in kb[0].summary,
           f"got {kb[0].summary!r}")
    _check("KB prescription names the Shadow Walker feed",
           "Shadow Walker" in kb[0].prescription,
           f"got {kb[0].prescription!r}")
    _check("located inside the fight",
           0.0 <= kb[0].time_sec <= 300.0, f"got {kb[0].time_sec}")
    _check("evidence carries the count row",
           kb[0].evidence and kb[0].evidence[0].v == "3 / 5",
           f"got {kb[0].evidence}")


def test_drift_cause_silent_when_clean() -> None:
    print("\nTest: on-cooldown stream -> no drift cause")
    ideal = [(60.0 * i, nd.KUNAIS_BANE) for i in range(5)]
    on_cd = [(60.0 * i, nd.KUNAIS_BANE) for i in range(5)]
    _check("clean stream silent",
           _drift_causes(_nin_ctx(on_cd, ideal, fight_s=300.0)) == [],
           "got causes")


def test_drift_forgives_downtime_and_deaths() -> None:
    print("\nTest: drift ledger forgives downtime; death-window gaps skipped")
    ideal = [(60.0 * i, nd.KUNAIS_BANE) for i in range(5)]
    late = [(90.0 * i, nd.KUNAIS_BANE) for i in range(3)]
    dt = [(60.0, 90.0), (150.0, 180.0)]      # exactly the 30s over per gap
    _check("downtime-explained gaps stay silent",
           _drift_causes(_nin_ctx(late, ideal, fight_s=300.0,
                                  downtime=dt)) == [],
           "got causes")
    deaths = [(80.0, 95.0)]                  # touches both gaps
    _check("death-window gaps are skipped",
           _drift_causes(_nin_ctx(late, ideal, fight_s=300.0,
                                  deaths=deaths)) == [],
           "got causes")


# --- Ninki overcap -----------------------------------------------------------

def test_ninki_overcap_counts_bunshin_mirrors() -> None:
    print("\nTest: Ninki ledger overflows only when Bunshin mirrors count")
    # Bunshin arms 5 mirrors; 20 Spinning Edges then generate 100 (table) + 25
    # (mirrors) = 125 -> 25 overflow. Without the Bunshin cast the table gain
    # alone is exactly 100: no overflow, so an emit PROVES mirror crediting.
    edges = [(2.0 + 2.0 * i, nd.SPINNING_EDGE) for i in range(20)]
    with_bunshin = [(0.0, nd.BUNSHIN)] + edges
    c = _ninki_overcap_cause(_nin_ctx(with_bunshin, [], fight_s=60.0))
    _check("cause emitted on the mirror-fed overflow",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == nd.BHAVACAKRA, f"got {c}")
    _check("summary carries the wasted total", "25 wasted" in c.summary,
           f"got {c.summary!r}")
    _check("located at the first meaningful overflow", c.time_sec == 32.0,
           f"got {c.time_sec}")
    _check("ninki resource tag attached",
           c.resources and c.resources[0].label == "Ninki",
           f"got {c.resources}")
    _check("no overflow without the mirrors",
           _ninki_overcap_cause(_nin_ctx(edges, [], fight_s=60.0)) is None,
           "got a cause")


def test_ninki_ledger_expires_bunshin_mirrors() -> None:
    print("\nTest: Bunshin mirrors stop crediting Ninki after the 30s window")
    # Bunshin at 0, the mirrored weaponskills only at 60s+: the stacks are long
    # gone, so the ledger must count the table gain alone (5 x 5 = 25), not
    # phantom mirror Ninki (which would read 50 and invent overcap).
    casts = [(0.0, nd.BUNSHIN)] + [(60.0 + 2.0 * i, nd.SPINNING_EDGE)
                                   for i in range(5)]
    _ovf, final, _last, _cross = _walk_ninki(casts)
    _check("expired mirrors grant nothing", final == 25, f"got {final}")
    inside = [(0.0, nd.BUNSHIN)] + [(2.0 + 2.0 * i, nd.SPINNING_EDGE)
                                    for i in range(5)]
    _ovf, final_in, _last, _cross = _walk_ninki(inside)
    _check("mirrors inside the window still grant", final_in == 50,
           f"got {final_in}")


def test_producers_silent_on_the_sims_own_line() -> None:
    print("\nTest: the sim's own rotation is never carded (clean-play floor)")
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.ninja.simulator import SimParams, _model_for
    # NIN pools Ninki into burst by design, so the ideal line itself spills a
    # little on a long fight; the overcap ledger must measure against that
    # line, not against zero, or clean play gets a card.
    for dur in (300.0, 420.0):
        params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
        tl, _aux = engine.run_rotation(_model_for(None), dur, [], params)
        ideal = [(t, a) for t, a in tl if a != TINCTURE_ACTION_ID]
        ctx = _nin_ctx(ideal, ideal, fight_s=dur)
        _check(f"{dur:.0f}s: no drift cause", _drift_causes(ctx) == [],
               f"got {[c.summary for c in _drift_causes(ctx)]}")
        ov = _ninki_overcap_cause(ctx)
        _check(f"{dur:.0f}s: no Ninki overcap cause", ov is None,
               f"got {ov and ov.summary}")
        ch = _charge_cap_cause(ctx)
        _check(f"{dur:.0f}s: no charge-cap cause", ch is None,
               f"got {ch and ch.summary}")
        st = _ninki_stranded_cause(ctx)
        _check(f"{dur:.0f}s: no stranded cause", st is None,
               f"got {st and st.summary}")


def test_ninki_overcap_silent_when_spent() -> None:
    print("\nTest: a spent gauge never overcaps")
    casts = ([(2.0 * i, nd.ARMOR_CRUSH) for i in range(5)]     # 75 Ninki
             + [(11.0, nd.BHAVACAKRA)])                        # -> 25
    _check("no cause when the gauge never overflows",
           _ninki_overcap_cause(_nin_ctx(casts, [], fight_s=60.0)) is None,
           "got a cause")


# --- Mudra charge pool at cap ------------------------------------------------

def test_charge_cap_emits() -> None:
    print("\nTest: capped mudra pool -> charge-economy root cause")
    # No prepull -> the pool seeds full. TEN at 2.0 spends one; the pool caps
    # again at 22.0 and sits full until the CHI-opened sequence at 150 (the
    # shared-pool rule), then caps at 170 until the 200s end.
    casts = [(2.0, nd.TEN), (150.0, nd.CHI)]
    c = _charge_cap_cause(_nin_ctx(casts, [], fight_s=200.0))
    _check("cause emitted", c is not None and c.kind == "cascade_burst"
           and c.ability_id == nd.RAITON, f"got {c}")
    _check("located at the first long capped stretch", c.time_sec == 22.0,
           f"got {c.time_sec}")
    _check("summary counts the lost charges", "8.0 charges" in c.summary,
           f"got {c.summary!r}")


def test_charge_cap_seeds_from_prepull() -> None:
    print("\nTest: a pre-pull mudra sequence seeds the sim's opener charges")
    from jobs.ninja.simulator import OPENER_CHARGES
    _check("opener seed below the cap", OPENER_CHARGES < 2.0,
           f"got {OPENER_CHARGES}")
    casts = [(-6.0, nd.TEN), (-5.5, nd.CHI_FREE), (-5.0, nd.JIN_FREE),
             (30.0, nd.TEN)]
    c = _charge_cap_cause(_nin_ctx(casts, [], fight_s=60.0))
    _check("cause emitted from the seeded walk", c is not None, "got None")
    # Seeded at 1.3, the pool caps at (2.0 - 1.3) * 20 = 14.0, not at 0.0.
    _check("cap time honors the 1.3 seed", c.time_sec == 14.0,
           f"got {c.time_sec}")


def test_charge_cap_silent_when_spent_or_downtime() -> None:
    print("\nTest: a working pool stays silent; downtime cap time is forgiven")
    busy = [(15.0 * i, nd.TEN) for i in range(14)]
    _check("regular spending never caps",
           _charge_cap_cause(_nin_ctx(busy, [], fight_s=210.0)) is None,
           "got a cause")
    idle = [(2.0, nd.TEN)]
    dt = [(22.0, 100.0)]
    _check("capped time inside downtime is forgiven",
           _charge_cap_cause(_nin_ctx(idle, [], fight_s=100.0,
                                      downtime=dt)) is None,
           "got a cause")


def test_charge_cap_never_locates_inside_a_forgiven_window() -> None:
    print("\nTest: the charge card points at spendable time, not downtime")
    # The pool caps at 22.0, but the boss is gone until 60.0: the card must
    # locate at the re-engage, never at a moment with nothing to hit.
    casts = [(2.0, nd.TEN)]
    dt = [(22.0, 60.0)]
    c = _charge_cap_cause(_nin_ctx(casts, [], fight_s=200.0, downtime=dt))
    _check("cause still emitted on the uptime remainder", c is not None,
           "got None")
    _check("located at the re-engage", c.time_sec == 60.0, f"got {c.time_sec}")
    _check("located time is outside every downtime window",
           not any(s <= c.time_sec < e for s, e in dt), f"got {c.time_sec}")
    deaths = [(22.0, 60.0)]
    c2 = _charge_cap_cause(_nin_ctx(casts, [], fight_s=200.0, deaths=deaths))
    _check("death time is cut out the same way",
           c2 is not None and c2.time_sec == 60.0,
           f"got {c2 and c2.time_sec}")


# --- Ninki stranded ----------------------------------------------------------

def test_ninki_stranded_emits() -> None:
    print("\nTest: a full spender dead in the gauge at the kill")
    casts = [(2.0 * i, nd.ARMOR_CRUSH) for i in range(6)]      # 90 Ninki
    c = _ninki_stranded_cause(_nin_ctx(casts, [], fight_s=60.0))
    _check("cause emitted", c is not None
           and c.kind == "cascade_lost_use"
           and c.ability_id == nd.BHAVACAKRA, f"got {c}")
    _check("summary carries the stranded amount",
           "90 Ninki left" in c.summary, f"got {c.summary!r}")
    _check("located at the last Ninki gain", c.time_sec == 10.0,
           f"got {c.time_sec}")
    _check("prescription prices the missed spender",
           "~720p" in c.prescription, f"got {c.prescription!r}")


def test_ninki_stranded_silent_cases() -> None:
    print("\nTest: stranded stays silent when spent or only just crossed 50")
    spent = ([(2.0 * i, nd.ARMOR_CRUSH) for i in range(6)]
             + [(12.0, nd.BHAVACAKRA)])                        # -> 40
    _check("spent gauge silent",
           _ninki_stranded_cause(_nin_ctx(spent, [], fight_s=60.0)) is None,
           "got a cause")
    late_cross = [(52.0 + 2.0 * i, nd.ARMOR_CRUSH) for i in range(4)]
    _check("a gauge that crossed 50 in the final GCDs stays silent",
           _ninki_stranded_cause(_nin_ctx(late_cross, [],
                                          fight_s=60.0)) is None,
           "got a cause")


def test_ninki_stranded_silent_when_dead_or_targetless() -> None:
    print("\nTest: no stranded card for a gauge held while dead or targetless")
    # 90 Ninki by 10.0 (the gauge crosses 50 at 6.0); the player then spends
    # the rest of the pull dead, or with no boss up. Neither is a spender they
    # could have pressed, and the death card already prices the death.
    casts = [(2.0 * i, nd.ARMOR_CRUSH) for i in range(6)]
    _check("dead for the tail stays silent",
           _ninki_stranded_cause(_nin_ctx(casts, [], fight_s=60.0,
                                          deaths=[(8.0, 60.0)])) is None,
           "got a cause")
    _check("no target for the tail stays silent",
           _ninki_stranded_cause(_nin_ctx(casts, [], fight_s=60.0,
                                          downtime=[(8.0, 60.0)])) is None,
           "got a cause")
    _check("the same gauge on a live boss still cards",
           _ninki_stranded_cause(_nin_ctx(casts, [], fight_s=60.0)) is not None,
           "got None")


# --- Probe entry point -------------------------------------------------------

def test_advice_probes_shape_and_order() -> None:
    print("\nTest: advice_probes returns ([], causes) in priority order")
    ideal = [(60.0 * i, nd.KUNAIS_BANE) for i in range(5)]
    casts = ([(90.0 * i, nd.KUNAIS_BANE) for i in range(3)]    # KB drift
             + [(2.0 * i, nd.ARMOR_CRUSH) for i in range(6)])  # 90 stranded
    items, causes = advice_probes(_nin_ctx(casts, ideal, fight_s=300.0), [])
    _check("no probe items (causes only)", items == [], f"got {items}")
    _check("drift cause leads, stranded trails",
           len(causes) >= 2 and causes[0].ability_id == nd.KUNAIS_BANE
           and causes[-1].kind == "cascade_lost_use"
           and causes[-1].ability_id == nd.BHAVACAKRA,
           f"got {[(c.kind, c.ability_id) for c in causes]}")
    _check("all causes carry measured_p == 0",
           all(c.measured_p == 0.0 for c in causes),
           f"got {[c.measured_p for c in causes]}")
    _check("all cause times inside the fight",
           all(0.0 <= c.time_sec <= 300.0 for c in causes),
           f"got {[c.time_sec for c in causes]}")


# --- Cascade conservation smoke ---------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on NIN — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.ninja.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 240.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _nin_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", nd.RAITON, 30.0, lost=400.0, name="Raiton"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live1)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("probe prescriptions merged into the cards in place",
           any(c.get("prescription") for c in live1),
           f"got {[c.get('prescription') for c in live1]}")
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    _check("examined payload produced", ex is not None, "got None")
    orig_sum = round(sum(c["lostPotency"] for c in cards), 1)
    new_sum = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("top-level sum conserved to the cent",
           abs(new_sum - orig_sum) <= 0.25,
           f"{new_sum} vs {orig_sum}")
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
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys ⊆ original card keys", item_keys <= card_keys,
           f"extra: {item_keys - card_keys}")


def main() -> int:
    test_registration_returns_pack()
    test_gauge_keys_are_real_state_fields()
    test_copy_lint()
    test_drift_cause_emits()
    test_drift_cause_silent_when_clean()
    test_drift_forgives_downtime_and_deaths()
    test_ninki_overcap_counts_bunshin_mirrors()
    test_ninki_ledger_expires_bunshin_mirrors()
    test_producers_silent_on_the_sims_own_line()
    test_ninki_overcap_silent_when_spent()
    test_charge_cap_emits()
    test_charge_cap_seeds_from_prepull()
    test_charge_cap_silent_when_spent_or_downtime()
    test_charge_cap_never_locates_inside_a_forgiven_window()
    test_ninki_stranded_emits()
    test_ninki_stranded_silent_cases()
    test_ninki_stranded_silent_when_dead_or_targetless()
    test_advice_probes_shape_and_order()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
