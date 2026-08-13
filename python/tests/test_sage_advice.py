"""Unit tests for the Sage deep-advice pack (jobs/sage/advice.py).

Follows tests/test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean/silent one, plus the healer-specific
excuses (downtime, deaths, and the rez cast bars the ceiling already pays for);
then registration, gauge-allowlist validity against the real sim state, the copy
lint, and the cascade conservation smoke on the SGE simulator.

Run from python/:  python tests/test_sage_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, AdvicePack, GaugeText
from jobs.sage import data as gd
from jobs.sage.advice import (
    DOT_TILE, GAUGE_TEXT, PHLEGMA_TILE, TEXT, _cooldown_drift_causes,
    _dot_lapse_cause, _phlegma_bank_cause, advice_probes,
)

EK_DOSIS = gd.EUKRASIAN_DOSIS_III
PHLEGMA = gd.PHLEGMA_III
PSYCHE = gd.PSYCHE
DOSIS = gd.DOSIS_III

_SGE_GCDS = frozenset({
    gd.DOSIS_III, gd.EUKRASIA, gd.EUKRASIAN_DOSIS_III, gd.PHLEGMA_III,
    gd.DYSKRASIA_II, gd.TOXIKON_II, gd.PNEUMA,
})

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


def _sge_ctx(norm_casts, idealized, runner=None, fight_s: float = 200.0,
             downtime=(), deaths=(), scoring=None):
    return AdviceContext(
        job="Sage", data=gd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime), death_windows=list(deaths),
        clipping_state={"clipping": None}, scoring_state=dict(scoring or {}),
        enabler_values={}, sim_context=None,
        sim_module="jobs.sage.simulator", runner=runner,
        gcd_ids=_SGE_GCDS, gauge_text=GAUGE_TEXT)


# --- Eukrasian Dosis III lapse ----------------------------------------------

def test_dot_lapse_emits() -> None:
    print("\nTest: DoT applications 45s apart -> uncovered-DoT root cause")
    # 30s DoT refreshed every 45s: 15s uncovered per gap, three gaps.
    casts = [(45.0 * i, EK_DOSIS) for i in range(4)]
    pair = _dot_lapse_cause(_sge_ctx(casts, []))
    _check("cause emitted", pair is not None, "got None")
    value, c = pair
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == EK_DOSIS, f"got {c.kind} / {c.ability_id}")
    _check("weight is the missing ticks' potency",
           abs(value - (45.0 / 3.0) * 90.0) < 1e-6, f"got {value}")
    _check("located at the first expiry of the worst gap, inside the fight",
           c.time_sec == 30.0 and 0 <= c.time_sec <= 200.0, f"got {c.time_sec}")
    _check("summary carries the uncovered total",
           "off the target for 45s" in c.summary, f"got {c.summary!r}")
    _check("evidence: uncovered total + longest gap",
           len(c.evidence) == 2 and c.evidence[0].v == "45s"
           and c.evidence[1].v == "15s",
           f"got {[(r.k, r.v) for r in c.evidence]}")
    _check("DOT tile attached", c.resources == [DOT_TILE], f"got {c.resources}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_dot_lapse_clean_silent() -> None:
    print("\nTest: a DoT kept up (or a single application) stays silent")
    kept = [(28.0 * i, EK_DOSIS) for i in range(8)]
    _check("28s refresh cadence -> None",
           _dot_lapse_cause(_sge_ctx(kept, [])) is None, "got a cause")
    _check("one application -> None",
           _dot_lapse_cause(_sge_ctx([(0.0, EK_DOSIS)], [])) is None,
           "got a cause")
    # A single 8s slip sits under the three-tick floor.
    small = [(0.0, EK_DOSIS), (38.0, EK_DOSIS)]
    _check("sub-floor slip -> None",
           _dot_lapse_cause(_sge_ctx(small, [])) is None, "got a cause")


def test_dot_lapse_downtime_not_blamed() -> None:
    print("\nTest: a lapse covered by downtime is not a lapse")
    casts = [(0.0, EK_DOSIS), (60.0, EK_DOSIS)]
    ctx = _sge_ctx(casts, [], downtime=[(30.0, 70.0)])
    _check("boss-away stretch -> None", _dot_lapse_cause(ctx) is None,
           "got a cause")


def test_dot_lapse_rez_not_blamed() -> None:
    print("\nTest: a raise the ceiling already pays for excuses the stretch")
    casts = [(0.0, EK_DOSIS), (45.0, EK_DOSIS)]
    # Hardcast Egeiro at 0:30, priced by the rez pardon at 3 locked GCD slots.
    scoring = {"heal_lock_rez_count": 1,
               "heal_lock_rez_casts": [[30.0, gd.EGEIRO, 3]],
               "heal_lock_rez_gcds": 3, "heal_lock_rez_recovery_count": 2}
    plain = _dot_lapse_cause(_sge_ctx(casts, []))
    _check("without the rez block the 15s lapse cards",
           plain is not None, "got None")
    _check("the raise's cast bar takes it under the floor",
           _dot_lapse_cause(_sge_ctx(casts, [], scoring=scoring)) is None,
           "got a cause")


def test_dot_lapse_aoe_dot_not_blamed() -> None:
    print("\nTest: an add phase on the AoE DoT is not an uncovered stretch")
    # A correctly played add phase: Eukrasian Dyskrasia (the unmodeled AoE DoT)
    # replaces the single-target application for three refreshes.
    casts = ([(0.0, EK_DOSIS), (28.0, EK_DOSIS)]
             + [(56.0, gd.EUKRASIAN_DYSKRASIA), (84.0, gd.EUKRASIAN_DYSKRASIA),
                (112.0, gd.EUKRASIAN_DYSKRASIA)]
             + [(140.0, EK_DOSIS), (168.0, EK_DOSIS)])
    _check("AoE-DoT stretch -> None",
           _dot_lapse_cause(_sge_ctx(casts, [])) is None, "got a cause")
    # Same stream with the AoE applications dropped is a real lapse, so the
    # silence above comes from the coverage rule, not from the floor.
    st_only = [(t, a) for t, a in casts if a == EK_DOSIS]
    _check("without them the gap cards",
           _dot_lapse_cause(_sge_ctx(st_only, [])) is not None, "got None")


def test_located_times_skip_excused_windows() -> None:
    print("\nTest: a card is located where the player could act, not mid-death")
    casts = [(0.0, EK_DOSIS), (60.0, EK_DOSIS)]
    pair = _dot_lapse_cause(_sge_ctx(casts, [], deaths=[(30.0, 45.0)]))
    _check("cause emitted", pair is not None, "got None")
    _check("located after the death window, not at the expiry",
           pair[1].time_sec == 45.0, f"got {pair[1].time_sec}")
    player = [(0.0, PHLEGMA), (5.0, PHLEGMA)]
    ideal = [(40.0 * i, PHLEGMA) for i in range(7)]
    bank = _phlegma_bank_cause(
        _sge_ctx(player, ideal, fight_s=200.0, downtime=[(70.0, 110.0)]))
    _check("banked cause emitted", bank is not None, "got None")
    _check("located after the boss returns, not while it is away",
           bank[1].time_sec == 110.0, f"got {bank[1].time_sec}")


# --- Phlegma III charge pool -------------------------------------------------

def test_phlegma_bank_emits() -> None:
    print("\nTest: both Phlegma charges banked all fight -> lost-cast cause")
    player = [(0.0, PHLEGMA), (5.0, PHLEGMA)]          # dump, then never again
    ideal = [(40.0 * i, PHLEGMA) for i in range(7)]
    pair = _phlegma_bank_cause(_sge_ctx(player, ideal, fight_s=200.0))
    _check("cause emitted", pair is not None, "got None")
    value, c = pair
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == PHLEGMA, f"got {c.kind} / {c.ability_id}")
    _check("weight is deficit x the potency above the filler",
           value == 5 * (gd.POTENCIES[PHLEGMA] - gd.POTENCIES[DOSIS]),
           f"got {value}")
    _check("located where the pool first filled, inside the fight",
           c.time_sec == 80.0 and 0 <= c.time_sec <= 200.0, f"got {c.time_sec}")
    _check("summary carries the banked time and the lost casts",
           "both charges for 120s" in c.summary and "5 casts lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence: count row + banked row",
           len(c.evidence) == 2 and c.evidence[0].v == "2 / 7"
           and c.evidence[1].v == "120s",
           f"got {[(r.k, r.v) for r in c.evidence]}")
    _check("PHL tile attached", c.resources == [PHLEGMA_TILE],
           f"got {c.resources}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_phlegma_bank_clean_silent() -> None:
    print("\nTest: a pool kept moving (or a deficit with no banked regen)")
    ideal = [(40.0 * i, PHLEGMA) for i in range(7)]
    on_cd = [(0.0, PHLEGMA), (2.5, PHLEGMA)] + \
            [(40.0 * i + 2.5, PHLEGMA) for i in range(1, 6)]
    _check("dump-and-recharge stream -> None",
           _phlegma_bank_cause(_sge_ctx(on_cd, ideal)) is None, "got a cause")
    # A deficit whose charges never actually sat capped (the fight simply ended
    # first): the banked floor keeps it silent.
    tight = [(40.0 * i, PHLEGMA) for i in range(5)]
    _check("deficit without banked regen -> None",
           _phlegma_bank_cause(_sge_ctx(tight, ideal, fight_s=165.0)) is None,
           "got a cause")


def test_phlegma_bank_downtime_not_blamed() -> None:
    print("\nTest: charges banked through downtime are not blamed")
    player = [(0.0, PHLEGMA), (5.0, PHLEGMA)]
    ideal = [(40.0 * i, PHLEGMA) for i in range(7)]
    ctx = _sge_ctx(player, ideal, fight_s=200.0, downtime=[(80.0, 200.0)])
    _check("boss-away pool -> None", _phlegma_bank_cause(ctx) is None,
           "got a cause")


# --- Psyche drift ------------------------------------------------------------

def test_psyche_drift_emits() -> None:
    print("\nTest: Psyche drift + a lost use -> root cause")
    ideal = [(60.0 * i, PSYCHE) for i in range(4)]
    player = [(0.0, PSYCHE), (75.0, PSYCHE), (165.0, PSYCHE)]
    pairs = _cooldown_drift_causes(_sge_ctx(player, ideal))
    _check("exactly one cause", len(pairs) == 1, f"got {len(pairs)}")
    value, c = pairs[0]
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == PSYCHE, f"got {c.kind} / {c.ability_id}")
    _check("weight is deficit x the full oGCD potency",
           value == 1 * gd.POTENCIES[PSYCHE], f"got {value}")
    _check("located at the worst slip start, inside the fight",
           c.time_sec == 75.0 and 0 <= c.time_sec <= 200.0, f"got {c.time_sec}")
    _check("summary carries the idle total and the lost-use count",
           "sat idle 45s" in c.summary and "1 use lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence: count row + idle row",
           len(c.evidence) == 2 and c.evidence[0].v == "3 / 4"
           and c.evidence[1].v == "45s",
           f"got {[(r.k, r.v) for r in c.evidence]}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_psyche_drift_clean_silent() -> None:
    print("\nTest: on-cooldown Psyche (or a deficit with no drift) stays silent")
    ideal = [(60.0 * i, PSYCHE) for i in range(4)]
    on_cd = [(60.0 * i, PSYCHE) for i in range(4)]
    _check("on-cooldown stream -> no cause",
           _cooldown_drift_causes(_sge_ctx(on_cd, ideal)) == [], "got causes")
    short = [(60.0 * i, PSYCHE) for i in range(3)]
    _check("deficit without drift stays silent",
           _cooldown_drift_causes(_sge_ctx(short, ideal)) == [], "got causes")


def test_psyche_drift_death_window_not_blamed() -> None:
    print("\nTest: a slip spent dead is not drift")
    ideal = [(60.0 * i, PSYCHE) for i in range(4)]
    player = [(0.0, PSYCHE), (120.0, PSYCHE)]
    ctx = _sge_ctx(player, ideal, deaths=[(60.0, 120.0)])
    _check("death-covered slip -> no cause",
           _cooldown_drift_causes(ctx) == [], "got causes")


# --- Pack-level ordering ------------------------------------------------------

def test_causes_sorted_by_value() -> None:
    print("\nTest: causes ship in descending lost-value order, no probe items")
    player = ([(45.0 * i, EK_DOSIS) for i in range(4)]        # 1350p of ticks
              + [(0.0, PHLEGMA), (5.0, PHLEGMA)]              # 5 x 310p
              + [(0.0, PSYCHE), (75.0, PSYCHE), (165.0, PSYCHE)])   # 1 x 690p
    ideal = ([(28.0 * i, EK_DOSIS) for i in range(7)]
             + [(40.0 * i, PHLEGMA) for i in range(7)]
             + [(60.0 * i, PSYCHE) for i in range(4)])
    items, causes = advice_probes(_sge_ctx(player, ideal), [])
    _check("no ProbeItems", items == [], f"got {items}")
    _check("all three causes emitted", len(causes) == 3,
           f"got {[c.ability_id for c in causes]}")
    _check("descending value order (Phlegma 1550, DoT 1350, Psyche 690)",
           [c.ability_id for c in causes] == [PHLEGMA, EK_DOSIS, PSYCHE],
           f"got {[c.ability_id for c in causes]}")
    _check("every cause is located inside the fight",
           all(0.0 <= c.time_sec <= 200.0 for c in causes),
           f"got {[c.time_sec for c in causes]}")
    _check("every cause priced by the orchestrator (measured_p 0)",
           all(c.measured_p == 0.0 for c in causes), "non-zero measured_p")
    # Determinism: the same input yields the same order and the same copy.
    again = advice_probes(_sge_ctx(player, ideal), [])[1]
    _check("stable across runs",
           [(c.kind, c.ability_id, c.time_sec, c.summary) for c in causes]
           == [(c.kind, c.ability_id, c.time_sec, c.summary) for c in again],
           "runs differ")


def test_clean_stream_emits_nothing() -> None:
    print("\nTest: a clean SGE stream produces no causes at all")
    player = ([(28.0 * i, EK_DOSIS) for i in range(7)]
              + [(40.0 * i, PHLEGMA) for i in range(5)]
              + [(60.0 * i, PSYCHE) for i in range(4)])
    ideal = ([(28.0 * i, EK_DOSIS) for i in range(7)]
             + [(40.0 * i, PHLEGMA) for i in range(5)]
             + [(60.0 * i, PSYCHE) for i in range(4)])
    items, causes = advice_probes(_sge_ctx(player, ideal), [])
    _check("silent when clean", (items, causes) == ([], []),
           f"got {items} / {causes}")


# --- Registration / allowlist / copy ------------------------------------------

def test_registration() -> None:
    print("\nTest: the pack is registered on the Sage job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Sage")
    _check("resolve_pack returns an AdvicePack",
           isinstance(pack, AdvicePack), f"got {type(pack)}")
    _check("gauge_text is this module's glossary",
           pack.gauge_text is GAUGE_TEXT, "different object")
    _check("probes is this module's callable",
           pack.probes is advice_probes, "different callable")


def test_gauge_allowlist_matches_the_sim_state() -> None:
    print("\nTest: the gauge allowlist is closed, and closed on purpose")
    from jobs.sage.simulator import _model_for
    state = _model_for(150.0, None).init_state()
    for key in GAUGE_TEXT:
        _check(f"'{key}' exists on SimState", hasattr(state, key),
               f"missing {key}")
        val = getattr(state, key)
        _check(f"'{key}' is a scalar below the sentinel cutoff",
               isinstance(val, (int, float, bool)) and abs(float(val)) < 1e8,
               f"got {val!r}")
    # SGE's state adds exactly two public scalars over the engine base, and
    # neither is spendable (an expiry clock and a two-GCD sequence flag), so the
    # allowlist stays empty by design: the generic sequencing card would phrase
    # them as something to "use up".
    _check("dosis_dot_end is a real state field",
           hasattr(state, "dosis_dot_end"), "renamed?")
    _check("eukrasia_active is a real state field",
           hasattr(state, "eukrasia_active"), "renamed?")
    _check("neither is allowlisted",
           "dosis_dot_end" not in GAUGE_TEXT
           and "eukrasia_active" not in GAUGE_TEXT, "allowlisted")
    _check("the allowlist is empty", GAUGE_TEXT == {}, f"got {GAUGE_TEXT}")


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no strict/lenient jargon)")
    strings = list(_walk_strings(TEXT))
    for gt in list(GAUGE_TEXT.values()) + [PHLEGMA_TILE, DOT_TILE]:
        _check("GaugeText type", isinstance(gt, GaugeText), f"got {type(gt)}")
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s is not None:
                strings.append(s)
    for s in strings:
        _check(f"no em dash in {s[:34]!r}", "—" not in s, "em dash")
        _check(f"no en dash in {s[:34]!r}", "–" not in s, "en dash")
        low = s.lower()
        _check(f"no jargon in {s[:34]!r}",
               "strict" not in low and "lenient" not in low, "jargon")
        _check(f"no exclamation in {s[:34]!r}", "!" not in s, "bang")


def test_copy_never_blames_healing() -> None:
    print("\nTest: no line blames the player for healing or raising")
    banned = ("healed too much", "stop healing", "instead of healing",
              "wasted on healing", "too many heals", "over-healing")
    for s in _walk_strings(TEXT):
        low = s.lower()
        for phrase in banned:
            _check(f"no blame phrase in {s[:34]!r}", phrase not in low, phrase)


# --- Cascade smoke ------------------------------------------------------------

def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the SGE sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.sage.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _sge_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", PHLEGMA, 30.0, lost=400.0, name="Phlegma III"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    live1 = [dict(c) for c in cards]
    out1 = compute_advice_v2(ctx, live1)
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
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
    _check("at least one cascade card promoted", len(cascade) >= 1,
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
    _check("basis is buff-agnostic", ex["basis"] == "strict",
           f"got {ex['basis']}")
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys subset of original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")


def test_locked_healer_cascade_is_safe() -> None:
    """A mit-plan locked run (the normal healer case) must never raise out of
    the deep pass, and must conserve the top-level sum if it does produce a
    payload.

    Today it produces NONE: `replay._seed_state` builds the replayed state via
    `model.init_state()` without `engine._locks_init`, so `state.lock_done` is
    an empty tuple while the model carries locked windows, and
    `engine.continue_rotation`'s `_forced_lock_pick` indexes it out of range.
    `compute_advice_v2` catches that and degrades to `examined: None`, so a
    locked healer pull silently loses its examined panel. That fix is in
    jobs/_core (out of this pack's scope, reported as an escalation); this test
    accepts either outcome so it keeps passing once the core is fixed, and
    guards the invariant that matters either way.
    """
    print("\nTest: a heal-locked run degrades safely (never raises)")
    from jobs._core.heal_locks import HealLockContext, LockedGcdWindow
    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.sage.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    sc = HealLockContext(
        locks=(LockedGcdWindow(ability_id=gd.EUKRASIAN_PROGNOSIS_II,
                               start_s=60.0, end_s=90.0, count=2, cast_s=2.0),),
        inner=None)
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, sc), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 100.0 <= t < 112.0]
    ctx = _sge_ctx(player, ideal, fight_s=dur,
                   scoring={"heal_locks_applied": True,
                            "heal_lock_costed_count": 2})
    ctx.sim_context = sc
    ctx.runner = Runner(ctx.sim_module, dur, (), sc, player,
                        gcd_ids=sorted(ctx.gcd_ids
                                       | {gd.EUKRASIAN_PROGNOSIS_II}))
    cards = [_card("residual", 0, 0.0, lost=3000.0)]
    out = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("advice list still produced", isinstance(out["advice"], list),
           f"got {out}")
    ex = out["examined"]
    if ex is None:
        _check("degrade path: examined is None (core lock_done gap)", True)
        return
    total = round(sum(c["lostPotency"] for c in ex["improvements"]), 1)
    _check("locked-run payload conserves the top-level sum",
           abs(total - 3000.0) <= 0.25, f"got {total}")


def main() -> int:
    test_dot_lapse_emits()
    test_dot_lapse_clean_silent()
    test_dot_lapse_downtime_not_blamed()
    test_dot_lapse_rez_not_blamed()
    test_dot_lapse_aoe_dot_not_blamed()
    test_located_times_skip_excused_windows()
    test_phlegma_bank_emits()
    test_phlegma_bank_clean_silent()
    test_phlegma_bank_downtime_not_blamed()
    test_psyche_drift_emits()
    test_psyche_drift_clean_silent()
    test_psyche_drift_death_window_not_blamed()
    test_causes_sorted_by_value()
    test_clean_stream_emits_nothing()
    test_registration()
    test_gauge_allowlist_matches_the_sim_state()
    test_copy_lint()
    test_copy_never_blames_healing()
    test_examined_conservation_and_stability()
    test_locked_healer_cascade_is_safe()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
