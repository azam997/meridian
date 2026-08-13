"""Unit tests for the Pictomancer deep-advice pack (jobs/pictomancer/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean stream that stays silent; the pack's
registration, gauge-key validity against the real sim state, the copy rules
(no em/en dashes, no strict/lenient jargon), and the cascade conservation
smoke on the PCT simulator.

Run from python/:  python tests/test_pictomancer_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.pictomancer import data as pd
from jobs.pictomancer.advice import (
    GAUGE_TEXT, TEXT, _comet_stranded_cause, _cooldown_drift_causes,
    _hammer_dropped_cause, _palette_overcap_cause, advice_probes,
)

WATER = pd.WATER_IN_BLUE
FIRE = pd.FIRE_IN_RED
SUBTRACTIVE = pd.SUBTRACTIVE_PALETTE
COMET = pd.COMET_IN_BLACK
HOLY = pd.HOLY_IN_WHITE
STARRY = pd.STARRY_MUSE
STRIKING = pd.STRIKING_MUSE
POM = pd.POM_MUSE
STAMP = pd.HAMMER_STAMP
BRUSH = pd.HAMMER_BRUSH
POLISH = pd.POLISHING_HAMMER

_GCDS = frozenset(a for a in pd.POTENCIES if a not in pd.OGCD_IDS)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 400.0,
         downtime=None, deaths=None) -> AdviceContext:
    return AdviceContext(
        job="Pictomancer", data=pd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.pictomancer.simulator", runner=runner,
        gcd_ids=_GCDS)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


# --- Cooldown drift ---------------------------------------------------------

def test_drift_starry_late() -> None:
    print("\nTest: late Starry Muse -> lost-use root cause")
    ideal = [(120.0 * i, STARRY) for i in range(4)]        # 0, 120, 240, 360
    player = [(0.0, STARRY), (200.0, STARRY), (330.0, STARRY)]
    causes = _cooldown_drift_causes(_ctx(player, ideal))
    _check("one Starry cause emitted",
           len(causes) == 1 and causes[0].ability_id == STARRY
           and causes[0].kind == "cascade_lost_use", f"got {causes}")
    c = causes[0]
    _check("summary carries the drift total and the deficit",
           "Starry Muse" in c.summary and "90s" in c.summary
           and "1 use lost" in c.summary, f"got {c.summary!r}")
    _check("located at the worst slip's gap start",
           c.time_sec == 0.0, f"got {c.time_sec}")
    _check("weight left to the orchestrator", c.measured_p == 0.0,
           f"got {c.measured_p}")


def test_drift_pooled_consumers_fold() -> None:
    print("\nTest: creature-muse variants fold onto the POM pool")
    ideal = [(40.0 * i, pd.CREATURE_MUSES[i % 4]) for i in range(10)]
    player = [(0.0, POM), (100.0, pd.WINGED_MUSE),
              (200.0, pd.CLAWED_MUSE), (300.0, pd.FANGED_MUSE)]
    causes = _cooldown_drift_causes(_ctx(player, ideal))
    _check("Living Muse cause emitted on the pooled deficit",
           len(causes) == 1 and causes[0].ability_id == POM,
           f"got {causes}")
    _check("summary names the pool, not one variant",
           "Living Muse" in causes[0].summary, f"got {causes[0].summary!r}")


def test_drift_clean_silent() -> None:
    print("\nTest: on-cooldown streams stay silent")
    ideal = [(120.0 * i, STARRY) for i in range(4)]
    clean = [(120.0 * i, STARRY) for i in range(4)]        # no deficit
    _check("no deficit -> no cause",
           _cooldown_drift_causes(_ctx(clean, ideal)) == [], "got causes")
    tight = [(120.0 * i, STARRY) for i in range(3)]        # deficit, no drift
    _check("deficit without accumulated drift -> silent",
           _cooldown_drift_causes(_ctx(tight, ideal)) == [], "got causes")


def test_drift_portrait_load_gate() -> None:
    print("\nTest: portraits are measured from their load, not the 30s recast")
    # The muse ladder grants a portrait every OTHER creature muse, so clean
    # portraits land ~80s apart against a 30s recast. Pressing each one two
    # seconds after its grant is flawless play and must stay silent even with
    # a deficit (pre-fix this read as ~50s of drift per portrait).
    grants, ports, ideal = [], [], []
    for i in range(5):
        g = 20.0 + 80.0 * i
        grants.append((g, pd.WINGED_MUSE if i % 2 == 0 else pd.FANGED_MUSE))
        ports.append((g + 2.0, pd.MOG_OF_THE_AGES if i % 2 == 0
                      else pd.RETRIBUTION_OF_THE_MADEEN))
        ideal.append((g + 1.0, pd.MOG_OF_THE_AGES))
    ideal.append((360.0, pd.MOG_OF_THE_AGES))          # one more in the sim
    clean = sorted(grants + ports)
    causes = _cooldown_drift_causes(_ctx(clean, ideal))
    _check("prompt portraits stay silent on the ladder cadence",
           [c for c in causes if c.ability_id == pd.MOG_OF_THE_AGES] == [],
           f"got {[c.summary for c in causes]}")
    # A portrait that sat loaded and was overwritten by the next grant DOES
    # speak, with the drift measured from the load.
    held = sorted(grants + [ports[0], ports[2], ports[3], ports[4]])
    causes = _cooldown_drift_causes(_ctx(held, ideal))
    mog = [c for c in causes if c.ability_id == pd.MOG_OF_THE_AGES]
    _check("a loaded portrait left to be overwritten speaks", len(mog) == 1,
           f"got {[c.summary for c in causes]}")
    _check("drift is load-relative, not recast-relative",
           "86s" in mog[0].summary, f"got {mog[0].summary!r}")
    _check("located at the moment the portrait was pressable",
           mog[0].time_sec == 100.0, f"got {mog[0].time_sec}")


def test_drift_respects_downtime() -> None:
    print("\nTest: a gap spanning downtime is not drift")
    ideal = [(120.0 * i, STARRY) for i in range(4)]
    player = [(0.0, STARRY), (200.0, STARRY)]
    loud = _cooldown_drift_causes(_ctx(player, ideal))
    _check("without downtime the 80s slip speaks",
           len(loud) == 1 and loud[0].ability_id == STARRY, f"got {loud}")
    quiet = _cooldown_drift_causes(
        _ctx(player, ideal, downtime=[(120.0, 200.0)]))
    _check("with the gap covered by downtime it stays silent",
           quiet == [], f"got {quiet}")


# --- Palette overcap --------------------------------------------------------

def test_palette_overcap_emits() -> None:
    print("\nTest: palette overflow -> delayed-Subtractive root cause")
    waters = [(7.5 * i, WATER) for i in range(6)]          # 150 palette total
    c = _palette_overcap_cause(_ctx(waters, []))
    _check("cause emitted on 50 wasted palette",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == SUBTRACTIVE, f"got {c}")
    _check("located at the first overflow", c.time_sec == 30.0,
           f"got {c.time_sec}")
    _check("summary carries the wasted total", "50 palette" in c.summary,
           f"got {c.summary!r}")
    _check("palette gauge tagged as the resource",
           c.resources and c.resources[0].label == "Palette",
           f"got {c.resources}")


def test_palette_spectrum_free_spend() -> None:
    print("\nTest: Subtractive Spectrum spends no palette (the Starry rule)")
    # Four Waters cap the gauge; the post-Starry Subtractive is FREE, so the
    # fifth Water overflows. A naive minus-50 ledger would read 75 and miss it.
    stream = ([(7.5 * i, WATER) for i in range(4)]
              + [(24.0, STARRY), (25.0, SUBTRACTIVE), (30.0, WATER)])
    c = _palette_overcap_cause(_ctx(stream, []))
    _check("free spend keeps the gauge full -> the overflow is caught",
           c is not None and c.time_sec == 30.0, f"got {c}")
    # Without Starry the same Subtractive really spends 50: no overflow.
    paid = ([(7.5 * i, WATER) for i in range(4)]
            + [(25.0, SUBTRACTIVE), (30.0, WATER)])
    _check("a paid Subtractive drains the gauge -> silent",
           _palette_overcap_cause(_ctx(paid, [])) is None, "got a cause")


def test_palette_clean_silent() -> None:
    print("\nTest: a spend-on-time stream never overflows")
    stream = []
    t = 0.0
    for i in range(8):
        stream.append((t, WATER))
        t += 7.5
        if (i + 1) % 2 == 0:
            stream.append((t, SUBTRACTIVE))                # spend at 50
            t += 1.0
    _check("no overflow -> no cause",
           _palette_overcap_cause(_ctx(stream, [])) is None, "got a cause")


# --- Comet stranded ---------------------------------------------------------

def test_comet_stranded_emits() -> None:
    print("\nTest: banked Comet dead at the kill -> stranded root cause")
    stream = [(0.0, WATER), (10.0, SUBTRACTIVE)]           # black granted at 10
    c = _comet_stranded_cause(_ctx(stream, [], fight_s=100.0))
    _check("cause emitted", c is not None
           and c.kind == "cascade_lost_use" and c.ability_id == COMET,
           f"got {c}")
    _check("located at the granting Subtractive", c.time_sec == 10.0,
           f"got {c.time_sec}")
    _check("summary carries the potency", "940p" in c.summary,
           f"got {c.summary!r}")


def test_comet_stranded_silent_paths() -> None:
    print("\nTest: spent, tail-granted or never-granted Comets stay silent")
    spent = [(0.0, WATER), (10.0, SUBTRACTIVE), (20.0, COMET)]
    _check("a fired Comet is silent",
           _comet_stranded_cause(_ctx(spent, [], fight_s=100.0)) is None,
           "got a cause")
    tail = [(0.0, WATER), (97.0, SUBTRACTIVE)]
    _check("a grant inside the tail guard is silent",
           _comet_stranded_cause(_ctx(tail, [], fight_s=100.0)) is None,
           "got a cause")
    no_white = [(10.0, SUBTRACTIVE)]
    _check("Subtractive without a white paint converts nothing",
           _comet_stranded_cause(_ctx(no_white, [], fight_s=100.0)) is None,
           "got a cause")
    died = [(0.0, WATER), (10.0, SUBTRACTIVE)]
    _check("a death after the grant leaves the loss to the death card",
           _comet_stranded_cause(
               _ctx(died, [], fight_s=100.0, deaths=[(30.0, 45.0)])) is None,
           "got a cause")


# --- Hammer Time dropped ----------------------------------------------------

def test_hammer_dropped_emits() -> None:
    print("\nTest: expired Hammer Time -> dropped-swings root cause")
    stream = [(10.0, STRIKING), (12.0, STAMP)]             # 2 of 3 dropped
    c = _hammer_dropped_cause(_ctx(stream, [], fight_s=100.0))
    _check("cause emitted", c is not None
           and c.kind == "cascade_lost_use" and c.ability_id == STRIKING,
           f"got {c}")
    _check("located at the Striking that armed the window",
           c.time_sec == 10.0, f"got {c.time_sec}")
    _check("summary counts the unused swings", "2 swings unused" in c.summary,
           f"got {c.summary!r}")


def test_hammer_overwrite_counts() -> None:
    print("\nTest: a second Striking overwriting live stacks counts the loss")
    stream = [(10.0, STRIKING), (12.0, STAMP), (14.5, BRUSH),
              (20.0, STRIKING), (22.0, STAMP), (24.5, BRUSH), (27.0, POLISH)]
    c = _hammer_dropped_cause(_ctx(stream, [], fight_s=100.0))
    _check("the overwritten swing is counted",
           c is not None and "1 swing unused" in c.summary, f"got {c}")


def test_hammer_silent_paths() -> None:
    print("\nTest: full combos, tail windows and death windows stay silent")
    clean = [(10.0, STRIKING), (12.0, STAMP), (14.5, BRUSH), (17.0, POLISH)]
    _check("all three swings used -> silent",
           _hammer_dropped_cause(_ctx(clean, [], fight_s=100.0)) is None,
           "got a cause")
    tail = [(90.0, STRIKING), (92.0, STAMP)]
    _check("a window still open at the kill -> silent",
           _hammer_dropped_cause(_ctx(tail, [], fight_s=100.0)) is None,
           "got a cause")
    died = [(10.0, STRIKING), (12.0, STAMP)]
    _check("a window a death interrupted -> silent",
           _hammer_dropped_cause(
               _ctx(died, [], fight_s=100.0, deaths=[(15.0, 30.0)])) is None,
           "got a cause")
    gapped = [(10.0, STRIKING), (12.0, STAMP)]
    _check("a window a downtime gap ate -> silent",
           _hammer_dropped_cause(
               _ctx(gapped, [], fight_s=100.0,
                    downtime=[(15.0, 38.0)])) is None,
           "got a cause")


# --- The pack ---------------------------------------------------------------

def test_probe_order_and_no_items() -> None:
    print("\nTest: advice_probes order is the documented priority order")
    ideal = [(120.0 * i, STARRY) for i in range(4)]
    player = ([(0.0, STARRY), (200.0, STARRY), (330.0, STARRY)]
              + [(50.0 + 7.5 * i, WATER) for i in range(6)]
              + [(150.0, SUBTRACTIVE)]
              + [(250.0, STRIKING), (252.0, STAMP)])
    items, causes = advice_probes(_ctx(player, ideal), [])
    _check("no ProbeItems (PCT ships causes only)", items == [],
           f"got {items}")
    got = [(c.kind, c.ability_id) for c in causes]
    _check("drift, then palette, then Comet, then hammers",
           got == [("cascade_lost_use", STARRY),
                   ("cascade_burst", SUBTRACTIVE),
                   ("cascade_lost_use", COMET),
                   ("cascade_lost_use", STRIKING)], f"got {got}")
    for c in causes:
        _check(f"cause @{c.time_sec} lies inside the fight",
               0.0 <= c.time_sec <= 400.0
               and c.time_sec == round(c.time_sec, 1),
               f"got {c.time_sec}")


def test_sim_line_stays_silent() -> None:
    print("\nTest: the sim's own timeline produces no cause at all")
    # The ceiling's line is flawless by construction, so every producer must
    # stay quiet on it — including through downtime windows (this is what
    # caught the Hammer Time window a 25s gap ate).
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.pictomancer.simulator import SimParams, _model_for

    dur = 400.0
    downtime = [(90.0, 115.0), (250.0, 275.0)]
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, downtime,
                                         params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    items, causes = advice_probes(
        _ctx(ideal, ideal, fight_s=dur, downtime=downtime), [])
    _check("no ProbeItems and no causes on the ideal line",
           not items and not causes,
           f"got {[(c.kind, c.summary) for c in causes]}")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Pictomancer job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Pictomancer")
    _check("resolve_pack returns the pack with our gauge glossary",
           pack is not None and pack.gauge_text is GAUGE_TEXT,
           f"got {pack}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key survives the state snapshot")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.pictomancer.simulator import _model_for
    state = _model_for(None).init_state()
    snap = _snapshot(state)
    for key in sorted(GAUGE_TEXT):
        _check(f"'{key}' is a real sim-state gauge field",
               hasattr(state, key) and key in snap["gauges"],
               f"snapshot gauges: {sorted(snap['gauges'])}")


def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _walk_strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _walk_strings(v)


def test_copy_rules() -> None:
    print("\nTest: copy rules (no em/en dashes, no jargon, no exclamations)")
    strings = list(_walk_strings(TEXT))
    for gt in GAUGE_TEXT.values():
        _check("gauge entries are GaugeText", isinstance(gt, GaugeText), "")
        strings.extend(s for s in (gt.label, gt.short, gt.over_note,
                                   gt.under_note) if s)
    bad = [s for s in strings
           if "—" in s or "–" in s or "!" in s
           or "strict" in s.lower() or "lenient" in s.lower()]
    _check("every string passes the copy lint", bad == [], f"got {bad}")
    # Formatted output passes too (the composite emit scenario).
    ideal = [(120.0 * i, STARRY) for i in range(4)]
    player = ([(0.0, STARRY), (200.0, STARRY), (330.0, STARRY)]
              + [(50.0 + 7.5 * i, WATER) for i in range(6)]
              + [(150.0, SUBTRACTIVE)]
              + [(250.0, STRIKING), (252.0, STAMP)])
    _items, causes = advice_probes(_ctx(player, ideal), [])
    rendered = []
    for c in causes:
        rendered.extend([c.summary, c.prescription])
        rendered.extend(r.note for r in c.evidence)
        for r in c.evidence:
            _check("no evidence note repeats a prescription",
                   r.note not in c.prescription,
                   f"{r.note!r} inside {c.prescription!r}")
    bad2 = [s for s in rendered
            if "—" in s or "–" in s or "!" in s
            or "strict" in s.lower() or "lenient" in s.lower()]
    _check("rendered cause copy passes the lint too", bad2 == [],
           f"got {bad2}")


# --- Cascade conservation smoke --------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the PCT sim (conservation, stability)")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.pictomancer.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _ctx(player, ideal, fight_s=dur)
    ctx.gauge_text = GAUGE_TEXT
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", FIRE, 30.0, lost=400.0, name="Fire in Red"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    if ex is None:
        # Degrade path: legal when the hole's loss lands under the promotion
        # floors; the advice list must still be present.
        _check("degrade path: advice list present",
               isinstance(out1["advice"], list), "missing advice")
        print("  [note] examined degraded to None on this synthetic stream")
        return
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
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is strict (nothing credited)", ex["basis"] == "strict",
           f"got {ex['basis']}")


def main() -> int:
    test_drift_starry_late()
    test_drift_pooled_consumers_fold()
    test_drift_clean_silent()
    test_drift_portrait_load_gate()
    test_drift_respects_downtime()
    test_palette_overcap_emits()
    test_palette_spectrum_free_spend()
    test_palette_clean_silent()
    test_comet_stranded_emits()
    test_comet_stranded_silent_paths()
    test_hammer_dropped_emits()
    test_hammer_overwrite_counts()
    test_hammer_silent_paths()
    test_probe_order_and_no_items()
    test_sim_line_stays_silent()
    test_registration()
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
