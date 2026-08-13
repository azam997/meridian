"""Unit tests for the Viper deep-advice pack (jobs/viper/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean stream that stays silent; registration,
gauge-key validity against the real SimState, the copy lint (no em/en dashes,
no strict/lenient jargon), and the cascade conservation smoke on the VPR sim.

Run from python/:  python tests/test_viper_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext
from jobs.viper import data as vd
from jobs.viper.advice import (
    GAUGE_TEXT, TEXT,
    _coil_overcap_cause, _coils_stranded_cause, _ire_drift_cause,
    _offering_overcap_cause,
)

IRE = vd.SERPENTS_IRE
VW = vd.VICEWINDER
UF = vd.UNCOILED_FURY
RWK = vd.REAWAKEN
FIN = vd.FLANKSTING_STRIKE

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


_GCD_IDS = frozenset(a for a in vd.POTENCIES if a not in vd.OGCD_IDS)


def _vpr_ctx(norm_casts, idealized, runner=None, fight_s: float = 150.0,
             death_windows=None) -> AdviceContext:
    return AdviceContext(
        job="Viper", data=vd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=[],
        death_windows=list(death_windows or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.viper.simulator", runner=runner,
        gcd_ids=_GCD_IDS, gauge_text=GAUGE_TEXT)


def test_ire_drift_cause() -> None:
    print("\nTest: Serpent's Ire drift → lost-use root cause")
    ideal = [(2.0 + 120.0 * i, IRE) for i in range(5)]     # 5 uses fit
    late = [(2.0, IRE), (152.0, IRE), (302.0, IRE), (452.0, IRE)]
    c = _ire_drift_cause(_vpr_ctx(late, ideal, fight_s=520.0))
    _check("cause emitted on 90s of accumulated drift",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == IRE, f"got {c}")
    _check("located at the worst slip's gap start",
           c.time_sec == 2.0, f"got {c.time_sec}")
    _check("summary carries the lost-use count",
           "1 use lost" in c.summary, f"got {c.summary!r}")
    _check("time inside the fight", 0.0 <= c.time_sec <= 520.0,
           f"got {c.time_sec}")
    clean = _ire_drift_cause(_vpr_ctx(ideal, ideal, fight_s=520.0))
    _check("clean on-cooldown stream → no cause", clean is None,
           f"got {clean}")
    dead = _ire_drift_cause(_vpr_ctx(late, ideal, fight_s=520.0,
                                     death_windows=[(0.0, 520.0)]))
    _check("gaps inside death windows stay silent (death card owns them)",
           dead is None, f"got {dead}")


def test_offering_overcap_cause() -> None:
    print("\nTest: Serpent Offering overcap → delayed-Reawaken root cause")
    # 13 finishers (+10 each), no Reawaken: 130 built, 30 over the 100 cap.
    hot = [(2.5 * (i + 1), FIN) for i in range(13)]
    c = _offering_overcap_cause(_vpr_ctx(hot, []))
    _check("cause emitted on 30 wasted offering",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == RWK, f"got {c}")
    _check("located at the first overflow",
           c.time_sec == 27.5, f"got {c.time_sec}")
    _check("summary carries the wasted total",
           "30 offering wasted" in c.summary, f"got {c.summary!r}")
    # Free-Reawaken rule: a Reawaken under Ready to Reawaken spends nothing,
    # so the 50 offering carried through it still overcaps later.
    free = ([(0.0, IRE)]
            + [(2.5 * (i + 1), FIN) for i in range(5)]     # -> 50
            + [(15.0, RWK)]                                 # free, stays 50
            + [(17.5 + 2.5 * i, FIN) for i in range(8)])    # -> 130, 30 over
    cf = _offering_overcap_cause(_vpr_ctx(free, []))
    _check("free Reawaken spends no offering → the overflow is real",
           cf is not None and "30 offering wasted" in cf.summary,
           f"got {cf}")
    # Same stream with a PAID Reawaken (no Ire): 50 spent, nothing overcaps.
    paid = ([(2.5 * (i + 1), FIN) for i in range(5)]
            + [(15.0, RWK)]
            + [(17.5 + 2.5 * i, FIN) for i in range(8)])
    _check("paid Reawaken drains the gauge → no cause",
           _offering_overcap_cause(_vpr_ctx(paid, [])) is None,
           "got a cause")


def test_coil_overcap_cause() -> None:
    print("\nTest: Rattling Coil overcap → delayed-Uncoiled-Fury root cause")
    # Three Vicewinders bank the full 3-coil gauge; Serpent's Ire's +1 wastes.
    hot = [(0.0, VW), (40.0, VW), (80.0, VW), (100.0, IRE)]
    c = _coil_overcap_cause(_vpr_ctx(hot, []))
    _check("cause emitted on the wasted coil",
           c is not None and c.kind == "cascade_burst"
           and c.ability_id == UF, f"got {c}")
    _check("located at the overflowing gain",
           c.time_sec == 100.0, f"got {c.time_sec}")
    _check("summary counts one wasted coil",
           "1 coil wasted" in c.summary, f"got {c.summary!r}")
    clean = [(0.0, VW), (10.0, UF), (40.0, VW), (50.0, UF),
             (100.0, IRE), (110.0, UF)]
    _check("coils spent before the cap → no cause",
           _coil_overcap_cause(_vpr_ctx(clean, [])) is None, "got a cause")


def test_coils_stranded_cause() -> None:
    print("\nTest: Rattling Coils stranded at the kill → lost-use root cause")
    hot = [(250.0, VW), (260.0, VW)]
    c = _coils_stranded_cause(_vpr_ctx(hot, [], fight_s=300.0))
    _check("cause emitted on 2 coils dead in the gauge",
           c is not None and c.kind == "cascade_lost_use"
           and c.ability_id == UF, f"got {c}")
    _check("located at the last generator",
           c.time_sec == 260.0, f"got {c.time_sec}")
    _check("summary counts the stranded coils",
           "2 Rattling Coils left" in c.summary, f"got {c.summary!r}")
    clean = [(250.0, VW), (260.0, VW), (270.0, UF), (280.0, UF)]
    _check("coils spent before the kill → no cause",
           _coils_stranded_cause(_vpr_ctx(clean, [], fight_s=300.0)) is None,
           "got a cause")
    tail = [(298.0, VW)]
    _check("a coil gained in the final seconds was never spendable → silent",
           _coils_stranded_cause(_vpr_ctx(tail, [], fight_s=300.0)) is None,
           "got a cause")
    # Ideal-baseline rule: when the sim's own line banks a coil at the kill
    # (a final Vicewinder coil pair outvalues swapping to Uncoiled Fury),
    # a player matching that balance is NOT stranding.
    ideal_banks = [(250.0, VW), (270.0, UF), (293.0, VW)]
    _check("player end balance equal to the ideal's → silent",
           _coils_stranded_cause(_vpr_ctx(
               [(250.0, VW), (270.0, UF), (293.0, VW)], ideal_banks,
               fight_s=300.0)) is None, "got a cause")
    # ...but a genuine EXCESS over the ideal's end balance still speaks.
    excess = [(200.0, VW), (240.0, VW), (293.0, VW)]
    ce = _coils_stranded_cause(_vpr_ctx(excess, ideal_banks, fight_s=300.0))
    _check("2 coils over the ideal's end balance → cause (excess counted)",
           ce is not None and "2 Rattling Coils left" in ce.summary,
           f"got {ce}")


def test_clean_ideal_stream_is_silent() -> None:
    """The ultimate clean stream: the sim's own greedy timeline as the
    player. Every producer must stay silent (regression: the 300s greedy
    line banks one coil behind a final Vicewinder coil pair, which the old
    5s-tail rule carded as stranding)."""
    print("\nTest: all producers silent on the sim's own ideal timeline")
    from jobs._core.sim import engine
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.viper.simulator import SimParams, _model_for

    dur = 300.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    tl, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in tl if a != TINCTURE_ACTION_ID]
    ctx = _vpr_ctx(ideal, ideal, fight_s=dur)
    for label, fn in (("ire_drift", _ire_drift_cause),
                      ("offering_overcap", _offering_overcap_cause),
                      ("coil_overcap", _coil_overcap_cause),
                      ("coils_stranded", _coils_stranded_cause)):
        got = fn(ctx)
        _check(f"{label} silent on player == ideal", got is None,
               f"got {got.summary!r}" if got else "")


def test_death_resets_gauge_ledgers() -> None:
    print("\nTest: a death zeroes the ledgers (no phantom overcap/stranding)")
    # Offering: 60 banked, death, then a clean 80 rebuilt. The real gauge
    # never overcaps (death zeroed it); the unfixed ledger would read 140.
    banked = [(2.5 * (i + 1), FIN) for i in range(6)]
    rebuilt = [(25.0 + 2.5 * i, FIN) for i in range(8)]
    c = _offering_overcap_cause(_vpr_ctx(
        banked + rebuilt, [], death_windows=[(20.0, 25.0)]))
    _check("offering rebuilt after a death → no phantom overcap",
           c is None, f"got {c}")
    # A REAL overcap after the death still speaks (reset must not over-silence).
    big = [(25.0 + 2.5 * i, FIN) for i in range(13)]     # 130 built, 30 over
    c2 = _offering_overcap_cause(_vpr_ctx(
        banked + big, [], death_windows=[(20.0, 25.0)]))
    _check("real overcap after the death still emitted",
           c2 is not None and "30 offering wasted" in c2.summary,
           f"got {c2}")
    # Coils: 2 banked, death, then 3 rebuilt (exactly the cap, no waste).
    coil_stream = [(0.0, VW), (40.0, VW),
                   (60.0, VW), (100.0, VW), (140.0, VW)]
    c3 = _coil_overcap_cause(_vpr_ctx(
        coil_stream, [], fight_s=200.0, death_windows=[(50.0, 55.0)]))
    _check("coils rebuilt after a death → no phantom overcap",
           c3 is None, f"got {c3}")
    # Stranded: 2 coils banked, then the player dies and never casts again.
    # The bank died with them; the death card owns it, not a stranding card.
    c4 = _coils_stranded_cause(_vpr_ctx(
        [(0.0, VW), (40.0, VW)], [], fight_s=300.0,
        death_windows=[(100.0, 300.0)]))
    _check("coils that died with the player → no stranding card",
           c4 is None, f"got {c4}")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Viper job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Viper")
    _check("resolve_pack returns the VPR pack",
           pack is not None and pack.gauge_text is GAUGE_TEXT,
           f"got {pack}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a real SimState scalar field")
    from jobs._core.sim.counterfactual import _snapshot
    from jobs.viper.simulator import _model_for
    state = _model_for(None).init_state()
    for k in sorted(GAUGE_TEXT):
        _check(f"'{k}' is a SimState attribute", hasattr(state, k),
               f"missing on {type(state).__name__}")
    snap = _snapshot(state)
    for k in sorted(GAUGE_TEXT):
        _check(f"'{k}' survives the _snapshot gauge filter",
               k in snap["gauges"], f"gauges={sorted(snap['gauges'])}")


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)


def test_copy_lint() -> None:
    print("\nTest: copy rules — no em/en dashes, no strict/lenient jargon")
    strings = list(_walk_strings(TEXT))
    for gt in GAUGE_TEXT.values():
        strings.extend(s for s in (gt.label, gt.short, gt.over_note,
                                   gt.under_note) if s)
    _check("some copy to lint", len(strings) > 10, f"got {len(strings)}")
    for s in strings:
        _check(f"no em dash in {s[:40]!r}", "—" not in s, s)
        _check(f"no en dash in {s[:40]!r}", "–" not in s, s)
        low = s.lower()
        _check(f"no jargon in {s[:40]!r}",
               "strict" not in low and "lenient" not in low, s)


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the VPR sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.viper.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 150.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _vpr_ctx(player, ideal, fight_s=dur)
    ctx.runner = Runner(ctx.sim_module, dur, (), None, player,
                        gcd_ids=sorted(ctx.gcd_ids))
    cards = [
        {"kind": "missed_cast", "abilityId": VW, "abilityName": "Vicewinder",
         "timeSec": 30.0, "lostPotency": 400.0, "summary": "x"},
        {"kind": "residual", "abilityId": 0, "abilityName": "",
         "timeSec": 0.0, "lostPotency": 2400.0, "summary": "x"},
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
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank by exactly what moved",
           len(resid) == 1 and resid[0]["lostPotency"] < 2400.0
           and resid[0]["lostPotency"] >= 60.0, f"got {resid}")
    _check("basis is buff-agnostic potency", ex["basis"] == "strict",
           f"got {ex['basis']}")
    # The in-place advice list still targets only original cards.
    card_keys = {(c["kind"], c["abilityId"], round(c["timeSec"], 1))
                 for c in cards}
    item_keys = {(i["kind"], i["abilityId"], round(i["timeSec"], 1))
                 for i in out1["advice"]}
    _check("advice keys subset of original card keys",
           item_keys <= card_keys, f"extra: {item_keys - card_keys}")


def main() -> int:
    test_ire_drift_cause()
    test_offering_overcap_cause()
    test_coil_overcap_cause()
    test_coils_stranded_cause()
    test_clean_ideal_stream_is_silent()
    test_death_resets_gauge_ledgers()
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
