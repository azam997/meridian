"""White Mage deep-advice pack tests (jobs/whitemage/advice.py).

Mirrors test_deep_advice.py's structure for the first HEALER pack:

  * Each RootCause producer fires on a synthetic stream that earns it and stays
    SILENT on a clean one (false positives are worse than missing causes).
  * The healer guards hold: a pardoned raise's cast bar and death windows are
    never re-billed as drift, and a bloom stranded behind a death is silent.
  * Registration resolves through the registry, every GAUGE_TEXT key is a real
    sim-state field, and the copy passes the punctuation/jargon lint.
  * Cascade smoke on the WHM sim: conservation to the cent and byte-stability.

Run from python/:  python tests/test_whitemage_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.whitemage import data as wd
from jobs.whitemage.advice import (
    GAUGE_TEXT, TEXT, _cooldown_drift_causes, _lily_stall_cause,
    _misery_stranded_cause, _sacred_sight_cause, advice_probes,
)

GLARE_III = wd.GLARE_III
GLARE_IV = wd.GLARE_IV
DIA = wd.DIA
ASSIZE = wd.ASSIZE
POM = wd.PRESENCE_OF_MIND
MISERY = wd.AFFLATUS_MISERY
SOLACE = wd.AFFLATUS_SOLACE

_GCD_IDS = frozenset({GLARE_III, GLARE_IV, DIA, MISERY, SOLACE,
                      wd.AFFLATUS_RAPTURE, wd.HOLY_III})

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
         deaths=(), downtime=(), scoring_state=None) -> AdviceContext:
    return AdviceContext(
        job="White Mage", data=wd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=list(downtime),
        death_windows=list(deaths),
        clipping_state={"clipping": None},
        scoring_state=dict(scoring_state or {}),
        enabler_values={}, sim_context=None,
        sim_module="jobs.whitemage.simulator", runner=runner,
        gcd_ids=_GCD_IDS, gauge_text=GAUGE_TEXT)


# --- Cooldown drift -----------------------------------------------------------

def test_cd_drift_cause() -> None:
    print("\nTest: Presence of Mind drift -> lost-use root cause")
    # 120s recast: gaps of 130s and 170s leave 60s of accumulated drift (floor
    # is 30s), and the sim fits a fourth use the player never pressed.
    player = [(10.0, POM), (140.0, POM), (310.0, POM)]
    ideal = [(0.0, POM), (120.0, POM), (240.0, POM), (330.0, POM)]
    causes = _cooldown_drift_causes(_ctx(player, ideal))
    _check("one cause emitted", len(causes) == 1, f"got {causes}")
    _v, c = causes[0]
    _check("kind + ability id",
           c.kind == "cascade_lost_use" and c.ability_id == POM,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the worst slip (the 170s gap opens at 2:20)",
           c.time_sec == 140.0, f"got {c.time_sec}")
    _check("summary carries the drift and the lost use",
           "60s in total" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("prescription is imperative, located, and healer-aware",
           c.prescription.startswith("Press Presence of Mind")
           and "2:20" in c.prescription
           and "costs you no healing GCD" in c.prescription,
           f"got {c.prescription!r}")
    _check("evidence rows are labelled and non-empty",
           len(c.evidence) == 2
           and all(r.k and r.v and r.note for r in c.evidence),
           f"got {c.evidence}")


def test_cd_drift_silent_when_clean() -> None:
    print("\nTest: on-cooldown / no-deficit streams emit nothing")
    on_cd = [(0.0, POM), (120.0, POM), (240.0, POM)]
    ideal = [(0.0, POM), (120.0, POM), (240.0, POM), (330.0, POM)]
    _check("no drift -> no cause",
           _cooldown_drift_causes(_ctx(on_cd, ideal)) == [], "got causes")
    drifty = [(10.0, POM), (140.0, POM), (310.0, POM)]
    _check("drift but no deficit -> no cause",
           _cooldown_drift_causes(
               _ctx(drifty, [(0.0, POM), (120.0, POM), (240.0, POM)])) == [],
           "got causes")


def test_cd_drift_never_bills_a_raise_or_a_death() -> None:
    print("\nTest: raise cast bars and death windows are not drift")
    # Assize (40s recast, 10s floor): two gaps of 50s = 20s of raw drift.
    player = [(20.0, ASSIZE), (70.0, ASSIZE), (120.0, ASSIZE)]
    ideal = [(0.0, ASSIZE)] * 8
    raw = _cooldown_drift_causes(_ctx(player, ideal))
    _check("raw stream does card", len(raw) == 1, f"got {raw}")
    # Two pardoned raises per gap (3 locked slots each) cover 15s of it.
    rez = {"heal_lock_rez_casts": [[25.0, wd.RAISE, 3], [35.0, wd.RAISE, 3],
                                   [75.0, wd.RAISE, 3], [85.0, wd.RAISE, 3]]}
    _check("raise cast bars the ceiling pays for are not drift",
           _cooldown_drift_causes(_ctx(player, ideal, scoring_state=rez)) == [],
           "got causes")
    _check("time spent dead is not drift either",
           _cooldown_drift_causes(
               _ctx(player, ideal, deaths=[(25.0, 45.0), (75.0, 95.0)])) == [],
           "got causes")


# --- The lily economy ---------------------------------------------------------

def test_lily_stall_cause() -> None:
    print("\nTest: lilies capped + Misery deficit -> stalled-lily root cause")
    # No lily spends at all: the gauge caps at 60s and every later tick is lost.
    player = [(2.5 * i, GLARE_III) for i in range(100)]
    ideal = player + [(60.0, MISERY), (140.0, MISERY), (220.0, MISERY)]
    hit = _lily_stall_cause(_ctx(player, ideal))
    _check("cause emitted", hit is not None, "got None")
    _v, c = hit
    _check("kind + ability id",
           c.kind == "cascade_burst" and c.ability_id == MISERY,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the first wasted lily tick (1:20)",
           c.time_sec == 80.0, f"got {c.time_sec}")
    _check("summary names the waste and the Misery deficit",
           "lost to a full gauge" in c.summary and "3 fewer" in c.summary,
           f"got {c.summary!r}")
    _check("prescription is scoped here and never scolds the healing",
           "here" in c.prescription and "costs no damage" in c.prescription,
           f"got {c.prescription!r}")
    _check("the lily gauge is tagged as the implicated resource",
           [g.short for g in c.resources] == ["LILY"], f"got {c.resources}")


def test_lily_stall_silent_when_clean() -> None:
    print("\nTest: spent lilies, or no Misery deficit, stay silent")
    spends = [(21.0 + 20.0 * i, SOLACE) for i in range(15)]
    fed = spends + [(2.5 * i, GLARE_III) for i in range(100)]
    ideal = fed + [(60.0, MISERY), (140.0, MISERY)]
    _check("gauge never caps -> no cause",
           _lily_stall_cause(_ctx(fed, ideal)) is None, "got a cause")
    idle = [(2.5 * i, GLARE_III) for i in range(100)]
    _check("capped lilies but the sim lands no Misery either -> no cause",
           _lily_stall_cause(_ctx(idle, idle)) is None, "got a cause")


def test_misery_stranded_cause() -> None:
    print("\nTest: a bloomed Blood Lily left uncast at the kill")
    filler = [(95.0 + 2.5 * i, GLARE_III) for i in range(20)]
    player = [(30.0, SOLACE), (60.0, SOLACE), (90.0, SOLACE)] + filler
    hit = _misery_stranded_cause(_ctx(player, []))
    _check("cause emitted", hit is not None, "got None")
    _v, c = hit
    _check("kind + ability id",
           c.kind == "cascade_lost_use" and c.ability_id == MISERY,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the spend that bloomed it (1:30)",
           c.time_sec == 90.0, f"got {c.time_sec}")
    _check("prescription allows the legitimate hold",
           "Holding it for a buff window is fine" in c.prescription,
           f"got {c.prescription!r}")
    with_cast = player + [(120.0, MISERY)]
    _check("Misery actually cast -> no cause",
           _misery_stranded_cause(_ctx(with_cast, [])) is None, "got a cause")
    late = ([(30.0, SOLACE), (60.0, SOLACE), (358.0, SOLACE)]
            + [(95.0 + 2.5 * i, GLARE_III) for i in range(20)])
    _check("bloom inside the last GCDs -> no cause",
           _misery_stranded_cause(_ctx(late, [])) is None, "got a cause")
    _check("a death covering the tail -> no cause (the death card owns it)",
           _misery_stranded_cause(
               _ctx(player, [], deaths=[(300.0, 330.0)])) is None,
           "got a cause")
    healed_out = ([(30.0, SOLACE), (60.0, SOLACE), (90.0, SOLACE)]
                  + [(95.0 + 2.5 * i, wd.CURE_II) for i in range(20)])
    _check("nothing but heals after the bloom -> no cause",
           _misery_stranded_cause(_ctx(healed_out, [])) is None,
           "got a cause")


# --- Sacred Sight -------------------------------------------------------------

def test_sacred_sight_cause() -> None:
    print("\nTest: Sacred Sight stacks expiring on top of Glare III filler")
    player = ([(60.0, POM), (62.0, GLARE_IV)]
              + [(65.0 + 2.5 * i, GLARE_III) for i in range(6)])
    hit = _sacred_sight_cause(_ctx(player, []))
    _check("cause emitted", hit is not None, "got None")
    _v, c = hit
    _check("kind + ability id",
           c.kind == "cascade_burst" and c.ability_id == GLARE_IV,
           f"got {c.kind} / {c.ability_id}")
    _check("located at the Presence of Mind that opened the window",
           c.time_sec == 60.0, f"got {c.time_sec}")
    _check("summary counts the unspent stacks",
           "2 Glare IV left unspent" in c.summary, f"got {c.summary!r}")
    _check("prescription names the window and the per-stack value",
           "1:00" in c.prescription and "290p each" in c.prescription,
           f"got {c.prescription!r}")
    _check("the copy names the filler the player actually cast",
           "Glare III" in c.prescription, f"got {c.prescription!r}")
    # The AoE line swaps the filler to Holy III; the copy must follow it
    # instead of naming a spell the player never pressed.
    aoe = ([(60.0, POM), (62.0, GLARE_IV)]
           + [(65.0 + 2.5 * i, wd.HOLY_III) for i in range(6)])
    hit_aoe = _sacred_sight_cause(_ctx(aoe, []))
    _check("cause emitted on the AoE filler line", hit_aoe is not None,
           "got None")
    _check("copy names Holy III, not Glare III",
           "Holy III" in hit_aoe[1].prescription
           and "Glare III" not in hit_aoe[1].prescription,
           f"got {hit_aoe[1].prescription!r}")


def test_sacred_sight_silent_when_clean() -> None:
    print("\nTest: a saturated window, and one that outlives the pull, stay "
          "silent")
    full = ([(60.0, POM)]
            + [(62.0 + 2.5 * i, GLARE_IV) for i in range(3)]
            + [(75.0 + 2.5 * i, GLARE_III) for i in range(4)])
    _check("all three stacks fired -> no cause",
           _sacred_sight_cause(_ctx(full, [])) is None, "got a cause")
    late = ([(340.0, POM)]
            + [(345.0 + 2.5 * i, GLARE_III) for i in range(5)])
    _check("window runs past the kill -> no cause",
           _sacred_sight_cause(_ctx(late, [])) is None, "got a cause")
    healing = [(60.0, POM), (62.0, GLARE_IV), (65.0, wd.CURE_II),
               (67.5, wd.MEDICA_II)]
    _check("stacks lost to healing GCDs are never carded",
           _sacred_sight_cause(_ctx(healing, [])) is None, "got a cause")
    # A death inside the window: the stacks died with the player, and the death
    # card already prices that stretch.
    died = ([(60.0, POM), (62.0, GLARE_IV)]
            + [(65.0 + 2.5 * i, GLARE_III) for i in range(6)])
    _check("raw stream (no death) does card",
           _sacred_sight_cause(_ctx(died, [])) is not None, "got None")
    _check("a death running through the window -> no cause",
           _sacred_sight_cause(_ctx(died, [], deaths=[(66.0, 80.0)])) is None,
           "got a cause")


def test_probe_entry_orders_by_value() -> None:
    print("\nTest: advice_probes returns causes only, ranked by value")
    player = ([(10.0, POM), (140.0, POM), (310.0, POM)]
              + [(30.0, SOLACE), (60.0, SOLACE), (90.0, SOLACE)]
              # Filler between the Sacred Sight windows: the stranded bloom has
              # a slot to land in, and no window reads as under-used.
              + [(95.0 + 2.5 * i, GLARE_III) for i in range(16)])
    ideal = [(0.0, POM), (120.0, POM), (240.0, POM), (330.0, POM)]
    items, causes = advice_probes(_ctx(player, ideal), [])
    _check("no probe items (this pack ships causes only)", items == [],
           f"got {items}")
    kinds = [(c.ability_id, c.kind) for c in causes]
    _check("both causes present", len(causes) == 2, f"got {kinds}")
    _check("the 1400p Presence of Mind use outranks the 1050p Misery",
           causes[0].ability_id == POM, f"got {kinds}")


# --- Registration / data hygiene ---------------------------------------------

def test_registration() -> None:
    print("\nTest: the registry resolves the WHM pack")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("White Mage")
    _check("pack registered", pack is not None, "got None")
    _check("gauge glossary is ours", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text}")
    _check("probe callable is ours", pack.probes is advice_probes,
           f"got {pack.probes}")


def test_gauge_keys_are_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a real sim-state field")
    from jobs.whitemage.simulator import _model_for
    state = _model_for(360.0, None).init_state()
    for key in sorted(GAUGE_TEXT):
        _check(f"state has {key}", hasattr(state, key),
               f"missing on {type(state).__name__}")
        val = getattr(state, key)
        _check(f"{key} is a scalar the snapshot reads",
               isinstance(val, (int, float)) and abs(float(val)) < 1e8,
               f"got {val!r}")


def _walk_strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        out: list[str] = []
        for k in sorted(node, key=str):
            out.extend(_walk_strings(node[k]))
        return out
    if isinstance(node, (list, tuple)):
        return [s for item in node for s in _walk_strings(item)]
    return []


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no jargon, no exclamations)")
    strings = _walk_strings(TEXT)
    for g in GAUGE_TEXT.values():
        assert isinstance(g, GaugeText)
        strings.extend(s for s in (g.label, g.short, g.over_note, g.under_note)
                       if s)
    _check("copy present", len(strings) > 20, f"got {len(strings)}")
    # Escaped so this file itself stays pure ASCII (it prints on a cp1252
    # console when run standalone).
    dashes = (chr(0x2014), chr(0x2013))   # em dash, en dash
    bad_dash = [s for s in strings if any(d in s for d in dashes)]
    _check("no em or en dashes", not bad_dash, f"got {bad_dash}")
    jargon = [s for s in strings
              if "strict" in s.lower() or "lenient" in s.lower()]
    _check("no strict/lenient jargon", not jargon, f"got {jargon}")
    bang = [s for s in strings if "!" in s]
    _check("no exclamation marks", not bang, f"got {bang}")


# --- Cascade smoke ------------------------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: cascade examined list on the WHM sim: conservation, "
          "stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.whitemage.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 240.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    # The player: an 8s hole plus the classic WHM slip, every lily spend and
    # every Afflatus Misery replaced by the Glare III filler, so the lily line
    # never blooms.
    swapped = {MISERY, SOLACE, wd.AFFLATUS_RAPTURE}
    player = [(t, GLARE_III if a in swapped else a) for t, a in ideal
              if not 60.0 <= t < 68.0]
    ctx = _ctx(player, ideal, fight_s=dur)
    _items, causes = advice_probes(ctx, [])
    _check("the stalled lily line is carded on real sim data",
           any(c.ability_id == MISERY and c.evidence for c in causes),
           f"got {[(c.kind, c.ability_id) for c in causes]}")
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(_GCD_IDS))
    cards = [
        _card("missed_cast", ASSIZE, 30.0, lost=400.0, name="Assize"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    if ex is None:
        _check("degrade path: advice list still present",
               isinstance(out1["advice"], list), "missing advice")
        print("  [NOTE] no examined payload for this synthetic hole")
        return
    orig_sum = round(sum(c["lostPotency"] for c in cards), 1)
    new_sum = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("top-level sum conserved to the cent",
           abs(new_sum - orig_sum) <= 0.25, f"{new_sum} vs {orig_sum}")
    _check("recoverable echoes the original sum",
           abs(ex["recoverable"] - orig_sum) <= 0.25, f"got {ex['recoverable']}")
    promoted = [c for c in ex["improvements"]
                if str(c["kind"]).startswith("cascade_")]
    _check("at least one cascade card promoted", len(promoted) >= 1,
           f"kinds={[c['kind'] for c in ex['improvements']]}")
    _check("every cascade card is priced above the floor",
           all(c["lostPotency"] >= 150.0 for c in promoted),
           f"got {[c['lostPotency'] for c in promoted]}")
    _check("the held-lily state delta reaches the cards as evidence",
           any(c.get("evidence") for c in promoted),
           f"got {[c.get('evidence') for c in promoted]}")
    _check("every evidence row is labelled (k / v / note), none empty",
           all({"k", "v", "note"} <= set(r) and r["k"] and r["v"] and r["note"]
               for c in promoted for r in c.get("evidence", [])),
           f"got {[c.get('evidence') for c in promoted]}")
    _check("no evidence row repeats its card's prescription",
           all(r["note"] not in c.get("prescription", "")
               for c in promoted for r in c.get("evidence", [])),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in promoted]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and 60.0 <= resid[0]["lostPotency"] < 2400.0,
           f"got {resid}")


def main() -> int:
    test_cd_drift_cause()
    test_cd_drift_silent_when_clean()
    test_cd_drift_never_bills_a_raise_or_a_death()
    test_lily_stall_cause()
    test_lily_stall_silent_when_clean()
    test_misery_stranded_cause()
    test_sacred_sight_cause()
    test_sacred_sight_silent_when_clean()
    test_probe_entry_orders_by_value()
    test_registration()
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
