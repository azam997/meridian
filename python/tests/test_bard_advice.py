"""Unit tests for the Bard deep-advice pack (jobs/bard/advice.py).

Follows tests/test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean/silent one; registration, gauge-key
validity against the real sim state, the copy lint, and the cascade
conservation smoke on the BRD simulator.

Run from python/:  python tests/test_bard_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, AdvicePack, GaugeText
from jobs.bard import data as bd
from jobs.bard.advice import (
    GAUGE_TEXT, SONG_TILE, TEXT, _cooldown_drift_causes, _song_cycle_cause,
)

EMPYREAL = bd.EMPYREAL_ARROW
SIDEWINDER = bd.SIDEWINDER
BARRAGE = bd.BARRAGE
WM, MB, AP = bd.WANDERERS_MINUET, bd.MAGES_BALLAD, bd.ARMYS_PAEON

_BRD_GCDS = frozenset({
    bd.BURST_SHOT, bd.REFULGENT_ARROW, bd.CAUSTIC_BITE, bd.STORMBITE,
    bd.IRON_JAWS, bd.APEX_ARROW, bd.BLAST_ARROW, bd.RESONANT_ARROW,
    bd.RADIANT_ENCORE, bd.LADONSBITE, bd.SHADOWBITE, bd.WIDE_VOLLEY,
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


def _brd_ctx(norm_casts, idealized, runner=None, fight_s: float = 200.0,
             downtime=(), deaths=()):
    return AdviceContext(
        job="Bard", data=bd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s,
        downtime_windows=list(downtime), death_windows=list(deaths),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.bard.simulator", runner=runner,
        gcd_ids=_BRD_GCDS, gauge_text=GAUGE_TEXT)


def test_cooldown_drift_emits() -> None:
    print("\nTest: Empyreal Arrow drift + lost uses -> root cause")
    ideal = [(15.0 * i, EMPYREAL) for i in range(10)]        # on cooldown
    late = [(25.0 * i + 1.0, EMPYREAL) for i in range(6)]    # 10s over per gap
    ctx = _brd_ctx(late, ideal, fight_s=160.0)
    pairs = _cooldown_drift_causes(ctx)
    _check("exactly one cause", len(pairs) == 1, f"got {len(pairs)}")
    value, c = pairs[0]
    _check("kind + ability", c.kind == "cascade_lost_use"
           and c.ability_id == EMPYREAL, f"got {c.kind} / {c.ability_id}")
    _check("weight is deficit x per-use value",
           value == 4 * bd.COOLDOWN_VALUE_P[EMPYREAL], f"got {value}")
    _check("located at the worst slip start, inside the fight",
           c.time_sec == 1.0 and 0 <= c.time_sec <= 160.0,
           f"got {c.time_sec}")
    _check("summary carries the idle total and the lost-use count",
           "sat idle 50s" in c.summary and "4 uses lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence: count row + idle row",
           len(c.evidence) == 2 and c.evidence[0].v == "6 / 10"
           and c.evidence[1].v == "50s",
           f"got {[(r.k, r.v) for r in c.evidence]}")
    _check("measured_p stays 0", c.measured_p == 0.0, f"got {c.measured_p}")


def test_cooldown_drift_clean_silent() -> None:
    print("\nTest: clean on-cooldown stream -> no drift cause")
    ideal = [(15.0 * i, EMPYREAL) for i in range(10)]
    on_cd = [(15.0 * i, EMPYREAL) for i in range(10)]
    _check("no cause on a clean stream",
           _cooldown_drift_causes(_brd_ctx(on_cd, ideal, fight_s=160.0)) == [],
           "got causes")
    # Deficit without accumulated drift (the fight just ended early for the
    # player's last use): still silent — the drift floor gates it.
    short = [(15.0 * i, EMPYREAL) for i in range(8)]
    _check("deficit without drift stays silent",
           _cooldown_drift_causes(_brd_ctx(short, ideal, fight_s=160.0)) == [],
           "got causes")


def test_cooldown_drift_downtime_not_blamed() -> None:
    print("\nTest: a gap covered by downtime is not drift")
    ideal = [(15.0 * i, EMPYREAL) for i in range(8)]
    # One 45s gap, but 30s of it is boss-untargetable: net over = 0.
    player = [(0.0, EMPYREAL), (45.0, EMPYREAL), (60.0, EMPYREAL),
              (75.0, EMPYREAL), (90.0, EMPYREAL)]
    ctx = _brd_ctx(player, ideal, fight_s=160.0, downtime=[(15.0, 45.0)])
    _check("downtime-covered gap stays silent",
           _cooldown_drift_causes(ctx) == [], "got causes")


def test_song_cycle_emits() -> None:
    print("\nTest: late song swaps + a lost song -> song-cycle cause")
    ideal = [(0.0, WM), (43.5, MB), (83.5, AP), (120.0, WM), (163.5, MB),
             (203.5, AP), (240.0, WM)]
    player = [(0.0, WM), (60.0, MB), (100.0, AP), (140.0, WM),
              (183.5, MB), (223.5, AP)]        # 16.5s late once, one song lost
    pair = _song_cycle_cause(_brd_ctx(player, ideal, fight_s=250.0))
    _check("cause emitted", pair is not None, "got None")
    value, c = pair
    _check("kind + the late song's id",
           c.kind == "cascade_lost_use" and c.ability_id == MB,
           f"got {c.kind} / {c.ability_id}")
    _check("weight is deficit x the late song's value",
           value == 1 * bd.COOLDOWN_VALUE_P[MB], f"got {value}")
    _check("located when the swap was due, inside the fight",
           c.time_sec == 43.5 and 0 <= c.time_sec <= 250.0,
           f"got {c.time_sec}")
    _check("summary carries the total slip and the lost-song count",
           "20s behind" in c.summary and "1 song lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence: song count row + slip row",
           len(c.evidence) == 2 and c.evidence[0].v == "6 / 7"
           and c.evidence[1].v == "20s",
           f"got {[(r.k, r.v) for r in c.evidence]}")
    _check("SONG resource tag attached",
           c.resources == [SONG_TILE], f"got {c.resources}")
    _check("the songs counter stays OFF the state-delta allowlist",
           "song_idx" not in GAUGE_TEXT, "song_idx allowlisted")


def test_song_cycle_clean_silent() -> None:
    print("\nTest: an on-schedule cycle (or no deficit) stays silent")
    ideal = [(0.0, WM), (43.5, MB), (83.5, AP), (120.0, WM)]
    on_time = [(0.0, WM), (43.5, MB), (83.5, AP), (120.0, WM)]
    _check("on-schedule stream -> None",
           _song_cycle_cause(_brd_ctx(on_time, ideal, fight_s=160.0)) is None,
           "got a cause")
    # A visible slip but NO lost song: the deficit gate keeps it silent (the
    # cost of a recoverable slip lives in the residual, not a lost-use card).
    slipped = [(0.0, WM), (60.0, MB), (100.0, AP), (140.0, WM)]
    _check("slip without a lost song -> None",
           _song_cycle_cause(_brd_ctx(slipped, ideal, fight_s=160.0)) is None,
           "got a cause")


def test_song_cycle_death_window_not_blamed() -> None:
    print("\nTest: a slip covered by a death window stays silent")
    ideal = [(0.0, WM), (43.5, MB), (83.5, AP)]
    player = [(0.0, WM), (63.5, MB)]           # 20s late, but dead throughout
    ctx = _brd_ctx(player, ideal, fight_s=120.0,
                   deaths=[(43.5, 63.5)])
    _check("death-covered slip -> None",
           _song_cycle_cause(ctx) is None, "got a cause")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Bard job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Bard")
    _check("resolve_pack returns an AdvicePack",
           isinstance(pack, AdvicePack), f"got {type(pack)}")
    _check("gauge_text is this module's glossary",
           pack.gauge_text is GAUGE_TEXT, "different object")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar sim-state field")
    from jobs.bard.simulator import _model_for
    state = _model_for(150.0, None).init_state()
    for key in GAUGE_TEXT:
        _check(f"'{key}' exists on SimState", hasattr(state, key),
               f"missing {key}")
        val = getattr(state, key)
        _check(f"'{key}' is a scalar below the sentinel cutoff",
               isinstance(val, (int, float, bool)) and abs(float(val)) < 1e8,
               f"got {val!r}")


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
    for gt in list(GAUGE_TEXT.values()) + [SONG_TILE]:
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


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the BRD sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.bard.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [],
                                         params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _brd_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", EMPYREAL, 30.0, lost=400.0,
              name="Empyreal Arrow"),
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
    # The in-place advice list still targets only original cards.
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys subset of original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")


def main() -> int:
    test_cooldown_drift_emits()
    test_cooldown_drift_clean_silent()
    test_cooldown_drift_downtime_not_blamed()
    test_song_cycle_emits()
    test_song_cycle_clean_silent()
    test_song_cycle_death_window_not_blamed()
    test_registration()
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
