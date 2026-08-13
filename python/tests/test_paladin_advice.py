"""Unit tests for the Paladin deep-advice pack (jobs/paladin/advice.py).

Covers the three RootCause producers (emit + clean-stream silence), the
registry wiring, the GAUGE_TEXT allowlist against the real sim state, the
copy rules, and the cascade conservation smoke on the PLD simulator.

Run from python/:  python tests/test_paladin_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, AdvicePack
from jobs.paladin import data as pd
from jobs.paladin.advice import (
    GAUGE_TEXT, TEXT, _chain_cut_cause, _cooldown_drift_causes,
    _proc_overwrite_cause, advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _pld_ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
             deaths=None, downtime=None) -> AdviceContext:
    gcds = frozenset(pd.POTENCIES) - pd.OGCD_IDS
    return AdviceContext(
        job="Paladin", data=pd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.paladin.simulator", runner=runner, gcd_ids=gcds,
        gauge_text=dict(GAUGE_TEXT))


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


# --- Cooldown drift ---------------------------------------------------------

def test_cd_drift_emits_on_late_fof() -> None:
    print("\nTest: Fight or Flight drift with a lost use emits a cause")
    ideal = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(5)]
    late = [(75.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(4)]   # 15s over/gap
    causes = _cooldown_drift_causes(_pld_ctx(late, ideal))
    _check("one FoF cause", len(causes) == 1
           and causes[0].ability_id == pd.FIGHT_OR_FLIGHT,
           f"got {[(c.ability_id, c.kind) for c in causes]}")
    c = causes[0]
    _check("kind is cascade_lost_use", c.kind == "cascade_lost_use",
           f"got {c.kind}")
    _check("located inside the fight", 0.0 <= c.time_sec <= 300.0,
           f"got {c.time_sec}")
    _check("summary names the button and the lost use",
           "Fight or Flight" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")
    _check("evidence rows present", len(c.evidence) == 2,
           f"got {c.evidence}")


def test_cd_drift_silent_when_clean() -> None:
    print("\nTest: on-cooldown stream emits no drift cause")
    ideal = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(5)]
    on_cd = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(5)]
    _check("no cause on the clean stream",
           _cooldown_drift_causes(_pld_ctx(on_cd, ideal)) == [], "got causes")
    # Deficit without accumulated drift (a short fight artifact) stays silent.
    four = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(4)]
    _check("deficit alone (no drift) stays silent",
           _cooldown_drift_causes(_pld_ctx(four, ideal)) == [], "got causes")


def test_cd_drift_skips_death_gaps() -> None:
    print("\nTest: a drift gap overlapping a death window is not blamed")
    ideal = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(5)]
    late = [(0.0, pd.FIGHT_OR_FLIGHT), (100.0, pd.FIGHT_OR_FLIGHT)]
    hot = _cooldown_drift_causes(_pld_ctx(late, ideal))
    _check("the 40s slip alone does emit", len(hot) == 1, f"got {hot}")
    dead = _cooldown_drift_causes(
        _pld_ctx(late, ideal, deaths=[(10.0, 70.0)]))
    _check("the same slip inside a death window stays silent",
           dead == [], f"got {dead}")


def test_cd_drift_discounts_downtime() -> None:
    print("\nTest: downtime inside a gap is not counted as chosen idle time")
    ideal = [(60.0 * i, pd.FIGHT_OR_FLIGHT) for i in range(4)]
    # One use behind, but the 90s gap is mostly a 55s boss absence.
    player = [(0.0, pd.FIGHT_OR_FLIGHT), (90.0, pd.FIGHT_OR_FLIGHT),
              (150.0, pd.FIGHT_OR_FLIGHT)]
    _check("no drift blamed on a gap the boss was gone for",
           _cooldown_drift_causes(
               _pld_ctx(player, ideal, downtime=[(30.0, 85.0)])) == [],
           "got causes")
    _check("the same gap with the boss present does emit",
           len(_cooldown_drift_causes(_pld_ctx(player, ideal))) == 1,
           "expected one cause")


def test_cd_drift_legacy_requiescat_counts() -> None:
    print("\nTest: Requiescat casts consume the Imperator slot (older logs)")
    ideal = [(60.0 * i, pd.IMPERATOR) for i in range(5)]
    legacy = [(60.0 * i, pd.REQUIESCAT) for i in range(5)]
    causes = _cooldown_drift_causes(_pld_ctx(legacy, ideal))
    _check("no false Imperator drift on a Requiescat log",
           all(c.ability_id != pd.IMPERATOR for c in causes),
           f"got {[(c.ability_id, c.summary) for c in causes]}")


# --- Royal Authority proc overwrite -----------------------------------------

def _combo(t0: float) -> list[tuple[float, int]]:
    return [(t0, pd.FAST_BLADE), (t0 + 2.5, pd.RIOT_BLADE),
            (t0 + 5.0, pd.ROYAL_AUTHORITY)]


def test_proc_overwrite_emits() -> None:
    print("\nTest: Royal Authority over pending chain + Divine Might emits")
    stream = _combo(0.0) + [(7.5, pd.ATONEMENT)] + _combo(10.0)
    # At the 15.0 Royal: Supplication pending (2 chain steps) + Divine Might.
    c = _proc_overwrite_cause(_pld_ctx(stream, []))
    _check("cause emitted", c is not None, "got None")
    _check("kind is cascade_burst", c.kind == "cascade_burst", f"got {c.kind}")
    _check("anchored on Royal Authority",
           c.ability_id == pd.ROYAL_AUTHORITY, f"got {c.ability_id}")
    _check("located at the first overwrite", c.time_sec == 15.0,
           f"got {c.time_sec}")
    _check("summary counts the 3 procs", "3 unspent procs" in c.summary,
           f"got {c.summary!r}")
    _check("resources tag the implicated procs", len(c.resources) >= 1,
           f"got {c.resources}")
    _check("evidence rows are k/v/note shaped",
           all(r.k and r.v for r in c.evidence), f"got {c.evidence}")
    _check("no evidence note repeats the prescription",
           all(r.note not in c.prescription for r in c.evidence),
           f"got {c.prescription!r} / {c.evidence}")


def test_proc_overwrite_silent_when_spent() -> None:
    print("\nTest: a cleanly spent chain emits no overwrite cause")
    stream = (_combo(0.0)
              + [(7.5, pd.ATONEMENT), (10.0, pd.SUPPLICATION),
                 (12.5, pd.SEPULCHRE), (15.0, pd.HOLY_SPIRIT)]
              + _combo(17.5))
    _check("no cause", _proc_overwrite_cause(_pld_ctx(stream, [])) is None,
           "got a cause")


def test_proc_overwrite_below_floor_silent() -> None:
    print("\nTest: a single Divine Might overwrite stays under the floor")
    stream = (_combo(0.0)
              + [(7.5, pd.ATONEMENT), (10.0, pd.SUPPLICATION),
                 (12.5, pd.SEPULCHRE)]           # chain spent, DM kept
              + _combo(15.0))                    # only DM (500p) overwritten
    _check("500p face potency stays silent",
           _proc_overwrite_cause(_pld_ctx(stream, [])) is None, "got a cause")


def test_proc_overwrite_expires_on_its_own() -> None:
    print("\nTest: procs and the combo run out after 30s, so no phantom loss")
    # A boss jump between two clean combos: the chain and Divine Might expired
    # long before the second Royal Authority, which overwrote nothing.
    jumped = _combo(0.0) + _combo(50.0)
    _check("a 45s gap expires the procs before the next Royal Authority",
           _proc_overwrite_cause(
               _pld_ctx(jumped, [], downtime=[(6.0, 48.0)])) is None,
           "got a cause")
    # The combo itself lapses too, so the Royal Authority after a stale Riot
    # Blade grants nothing that a later one could overwrite.
    broken = [(0.0, pd.FAST_BLADE), (40.0, pd.RIOT_BLADE),
              (42.5, pd.ROYAL_AUTHORITY)] + _combo(50.0)
    _check("an uncombo'd Royal Authority grants nothing",
           _proc_overwrite_cause(_pld_ctx(broken, [])) is None, "got a cause")
    # Same shape inside the window: the grant is real, so the overwrite is too.
    tight = [(0.0, pd.FAST_BLADE), (2.5, pd.RIOT_BLADE),
             (5.0, pd.ROYAL_AUTHORITY)] + _combo(12.5)
    live = _proc_overwrite_cause(_pld_ctx(tight, []))
    _check("the in-window version still emits", live is not None
           and live.time_sec == 17.5, f"got {live}")


def test_proc_overwrite_resets_on_death() -> None:
    print("\nTest: procs drop on death, so no phantom overwrite after one")
    stream = _combo(0.0) + [(7.5, pd.ATONEMENT)] + _combo(25.0)
    live = _proc_overwrite_cause(_pld_ctx(stream, []))
    _check("without the death the overwrite fires", live is not None,
           "got None")
    dead = _proc_overwrite_cause(
        _pld_ctx(stream, [], deaths=[(9.0, 20.0)]))
    _check("with a death between, the ledger resets and stays silent",
           dead is None, f"got {dead}")


# --- Confiteor chain cut at the kill ----------------------------------------

_FULL_CHAIN = [(10.0, pd.IMPERATOR), (12.5, pd.CONFITEOR),
               (15.0, pd.BLADE_OF_FAITH), (17.5, pd.BLADE_OF_TRUTH),
               (20.0, pd.BLADE_OF_VALOR), (20.6, pd.BLADE_OF_HONOR)]


def test_chain_cut_emits() -> None:
    print("\nTest: a kill-cut Confiteor chain emits a stranded cause")
    player = [(90.0, pd.IMPERATOR), (92.5, pd.CONFITEOR),
              (95.0, pd.BLADE_OF_FAITH)]
    c = _chain_cut_cause(_pld_ctx(player, _FULL_CHAIN, fight_s=100.0))
    _check("cause emitted", c is not None, "got None")
    _check("kind is cascade_lost_use", c.kind == "cascade_lost_use",
           f"got {c.kind}")
    _check("anchored on Imperator", c.ability_id == pd.IMPERATOR,
           f"got {c.ability_id}")
    _check("located at the last Imperator", c.time_sec == 90.0,
           f"got {c.time_sec}")
    # Remaining hits: Blade of Truth + Valor + Honor = 880 + 1000 + 1000.
    _check("summary carries the uncast potency", "2880" in c.summary,
           f"got {c.summary!r}")
    _check("prescription counts the 3 uncast hits", "3 hits" in c.prescription,
           f"got {c.prescription!r}")


def test_chain_cut_silent_cases() -> None:
    print("\nTest: chain-cut silence (ideal also cut / completed / death)")
    cut = [(90.0, pd.IMPERATOR), (92.5, pd.CONFITEOR)]
    _check("silent when the sim's own chain is cut by the fight end",
           _chain_cut_cause(_pld_ctx(cut, cut, fight_s=100.0)) is None,
           "got a cause")
    done = [(t + 80.0, a) for t, a in _FULL_CHAIN]
    _check("silent when the player completed the chain",
           _chain_cut_cause(_pld_ctx(done, _FULL_CHAIN, fight_s=102.0))
           is None, "got a cause")
    _check("silent when a death follows the last Imperator",
           _chain_cut_cause(_pld_ctx(cut, _FULL_CHAIN, fight_s=100.0,
                                     deaths=[(94.0, 100.0)])) is None,
           "got a cause")
    _check("silent with no Imperator at all",
           _chain_cut_cause(_pld_ctx([(5.0, pd.FAST_BLADE)], _FULL_CHAIN,
                                     fight_s=100.0)) is None, "got a cause")


def test_chain_cut_silent_when_the_chain_had_room() -> None:
    print("\nTest: a chain dropped mid-fight is not a kill-cut chain")
    # Imperator at 1:00 of a 5:00 fight, chain never continued: the fight
    # had four more minutes of room, so this is a dropped chain (its own
    # missed-cast cards), not a cut one.
    player = ([(60.0, pd.IMPERATOR)] + _combo(70.0) + _combo(80.0))
    _check("silent when the kill came long after the last Imperator",
           _chain_cut_cause(_pld_ctx(player, _FULL_CHAIN, fight_s=300.0))
           is None, "got a cause")
    # A Blade of Honor left pending at 0:20 of the same fight is likewise not a
    # kill-cut story.
    dropped = [(10.0, pd.IMPERATOR), (12.5, pd.CONFITEOR),
               (15.0, pd.BLADE_OF_FAITH), (17.5, pd.BLADE_OF_TRUTH),
               (20.0, pd.BLADE_OF_VALOR)] + _combo(100.0)
    _check("silent on a mid-fight pending finisher",
           _chain_cut_cause(_pld_ctx(dropped, _FULL_CHAIN, fight_s=300.0))
           is None, "got a cause")


# --- Aggregator + registration ---------------------------------------------

def test_advice_probes_order_and_no_items() -> None:
    print("\nTest: advice_probes emits no ProbeItems; cause order is stable")
    player = (_combo(0.0) + [(7.5, pd.ATONEMENT)] + _combo(10.0)
              + [(90.0, pd.IMPERATOR), (92.5, pd.CONFITEOR),
                 (95.0, pd.BLADE_OF_FAITH)])
    ideal = _FULL_CHAIN
    items, causes = advice_probes(_pld_ctx(player, ideal, fight_s=100.0), [])
    _check("no ProbeItems", items == [], f"got {items}")
    _check("overwrite then chain cut, in priority order",
           [c.kind for c in causes] == ["cascade_burst", "cascade_lost_use"],
           f"got {[(c.kind, c.ability_id) for c in causes]}")
    _check("every cause sits inside the fight",
           all(0.0 <= c.time_sec <= 100.0 for c in causes),
           f"got {[c.time_sec for c in causes]}")


def test_registered_pack() -> None:
    print("\nTest: the registry serves the PLD pack with our gauge glossary")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Paladin")
    _check("pack registered", isinstance(pack, AdvicePack), f"got {pack}")
    _check("gauge_text is ours", pack.gauge_text == GAUGE_TEXT,
           f"got {pack.gauge_text}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.paladin.simulator import _model_for
    state = _model_for(None).init_state()
    snap = _snapshot(state)
    for key in GAUGE_TEXT:
        _check(f"{key} is a state attribute", hasattr(state, key),
               f"missing on {type(state).__name__}")
        _check(f"{key} lands in the snapshot gauges", key in snap["gauges"],
               f"gauges={sorted(snap['gauges'])}")


def test_copy_rules() -> None:
    print("\nTest: copy lint (no dashes, no strict/lenient jargon)")

    def _strings():
        for section in TEXT.values():
            yield from section.values()
        for gt in GAUGE_TEXT.values():
            for s in (gt.label, gt.short, gt.over_note, gt.under_note):
                if s:
                    yield s

    for s in _strings():
        _check(f"no em/en dash in {s[:40]!r}",
               "—" not in s and "–" not in s, f"got {s!r}")
        _check(f"no strict/lenient jargon in {s[:40]!r}",
               "strict" not in s.lower() and "lenient" not in s.lower(),
               f"got {s!r}")
        _check(f"no exclamation in {s[:40]!r}", "!" not in s, f"got {s!r}")


# --- Cascade conservation smoke on the PLD simulator ------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on PLD — conservation and stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.paladin.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _pld_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", pd.ROYAL_AUTHORITY, 30.0, lost=400.0,
              name="Royal Authority"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    _check("advice list present", isinstance(out1["advice"], list), "missing")
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
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank but kept its floor",
           len(resid) == 1 and 60.0 <= resid[0]["lostPotency"] < 2400.0,
           f"got {resid}")
    _check("no evidence row repeats its card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in cascade for r in c.get("evidence", [])),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")


def main() -> int:
    test_cd_drift_emits_on_late_fof()
    test_cd_drift_silent_when_clean()
    test_cd_drift_skips_death_gaps()
    test_cd_drift_discounts_downtime()
    test_cd_drift_legacy_requiescat_counts()
    test_proc_overwrite_emits()
    test_proc_overwrite_silent_when_spent()
    test_proc_overwrite_below_floor_silent()
    test_proc_overwrite_expires_on_its_own()
    test_proc_overwrite_resets_on_death()
    test_chain_cut_emits()
    test_chain_cut_silent_cases()
    test_chain_cut_silent_when_the_chain_had_room()
    test_advice_probes_order_and_no_items()
    test_registered_pack()
    test_gauge_keys_are_real_state_fields()
    test_copy_rules()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
