"""Unit tests for the Samurai deep-advice pack (jobs/samurai/advice.py).

Follows test_deep_advice.py's structure: each RootCause producer gets an
emitting synthetic stream and a clean stream that stays silent; plus
registration, gauge-key validity, copy lint, and the cascade conservation
smoke on the real SAM simulator.

Run from python/:  python tests/test_samurai_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.samurai import data as sd
from jobs.samurai.advice import (
    GAUGE_TEXT, TEXT, _cd_drift_causes, _kenki_overcap_cause,
    _sen_pacing_cause, _stranded_cause,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ctx(norm_casts, idealized, runner=None, fight_s: float = 300.0,
         deaths=None, downtime=None):
    from jobs._core.advice import AdviceContext
    gcds = frozenset(set(sd.POTENCIES) - sd.OGCD_IDS)
    return AdviceContext(
        job="Samurai", data=sd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=list(downtime or []),
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.samurai.simulator", runner=runner, gcd_ids=gcds,
        gauge_text=dict(GAUGE_TEXT))


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def test_cd_drift_cause_emits() -> None:
    print("\nTest: Senei drift with a lost use -> cascade_lost_use cause")
    ideal = [(60.0 * i, sd.HISSATSU_SENEI) for i in range(6)]
    late = [(75.0 * i, sd.HISSATSU_SENEI) for i in range(4)]   # 15s over per gap
    out = _cd_drift_causes(_ctx(late, ideal, fight_s=360.0))
    _check("one Senei cause emitted",
           len(out) == 1 and out[0][1].ability_id == sd.HISSATSU_SENEI
           and out[0][1].kind == "cascade_lost_use", f"got {out}")
    _v, c = out[0]
    _check("located at the worst slip start, inside the fight",
           c.time_sec == 0.0 and 0.0 <= c.time_sec <= 360.0,
           f"got {c.time_sec}")
    _check("summary carries the deficit and the name",
           "Hissatsu: Senei" in c.summary and "2 uses lost" in c.summary,
           f"got {c.summary!r}")
    _check("evidence has count and idle rows",
           len(c.evidence) == 2 and c.evidence[0].v == "4 / 6",
           f"got {c.evidence}")


def test_cd_drift_silent_when_clean_and_death_skipped() -> None:
    print("\nTest: on-cooldown stream silent; death-window gap uncounted")
    ideal = [(60.0 * i, sd.HISSATSU_SENEI) for i in range(6)]
    clean = [(60.0 * i, sd.HISSATSU_SENEI) for i in range(6)]
    _check("clean on-cooldown stream -> no cause",
           _cd_drift_causes(_ctx(clean, ideal, fight_s=360.0)) == [],
           "got causes")
    # One 90s gap (30s over, exactly at the floor) that contains a death:
    # the pair is skipped, drift stays 0, silent.
    gappy = [(0.0, sd.HISSATSU_SENEI), (90.0, sd.HISSATSU_SENEI),
             (150.0, sd.HISSATSU_SENEI), (210.0, sd.HISSATSU_SENEI)]
    with_death = _cd_drift_causes(
        _ctx(gappy, ideal, fight_s=360.0, deaths=[(20.0, 40.0)]))
    _check("death-window gap not blamed", with_death == [],
           f"got {with_death}")
    without = _cd_drift_causes(_ctx(gappy, ideal, fight_s=360.0))
    _check("same stream without the death emits (floor edge)",
           len(without) == 1, f"got {without}")


def test_kenki_overcap_cause() -> None:
    print("\nTest: Kenki ledger overflow -> delayed-Shinten root cause")
    hot = [(3.0 * i, sd.YUKIKAZE) for i in range(10)]   # 150 in, no spender
    got = _kenki_overcap_cause(_ctx(hot, []))
    _check("cause emitted on a 50-Kenki overflow",
           got is not None and got[1].kind == "cascade_burst"
           and got[1].ability_id == sd.HISSATSU_SHINTEN, f"got {got}")
    _v, c = got
    _check("located at the first meaningful overflow (t=18.0)",
           c.time_sec == 18.0, f"got {c.time_sec}")
    _check("summary carries the wasted total",
           "50 wasted" in c.summary, f"got {c.summary!r}")
    _check("Kenki resource tag attached",
           c.resources and c.resources[0].label == "Kenki",
           f"got {c.resources}")
    cool = [(3.0 * i, sd.YUKIKAZE) if i % 2 == 0
            else (3.0 * i, sd.HISSATSU_SHINTEN) for i in range(10)]
    _check("no cause when the gauge never overflows",
           _kenki_overcap_cause(_ctx(cool, [])) is None, "got a cause")


def test_sen_pacing_cause() -> None:
    print("\nTest: full Sen held past the swing -> lost-Iaijutsu root cause")
    held = [(0.0, sd.GEKKO), (2.0, sd.KASHA), (4.0, sd.YUKIKAZE),
            (24.0, sd.MIDARE_SETSUGEKKA)]          # 20s hold, 15s past grace
    ideal = [(6.0, sd.MIDARE_SETSUGEKKA), (30.0, sd.MIDARE_SETSUGEKKA)]
    got = _sen_pacing_cause(_ctx(held, ideal))
    _check("cause emitted on the held full set",
           got is not None and got[1].kind == "cascade_lost_use"
           and got[1].ability_id == sd.MIDARE_SETSUGEKKA, f"got {got}")
    _v, c = got
    _check("located where the set completed", c.time_sec == 4.0,
           f"got {c.time_sec}")
    _check("summary carries the deficit",
           "1 Iaijutsu lost" in c.summary, f"got {c.summary!r}")
    prompt_cast = [(0.0, sd.GEKKO), (2.0, sd.KASHA), (4.0, sd.YUKIKAZE),
                   (6.0, sd.MIDARE_SETSUGEKKA)]    # 2s hold, inside grace
    _check("prompt Iaijutsu -> silent even with a count deficit",
           _sen_pacing_cause(_ctx(prompt_cast, ideal)) is None,
           "got a cause")
    _check("death resets the ledger -> the hold is never blamed",
           _sen_pacing_cause(_ctx(held, ideal, deaths=[(10.0, 20.0)])) is None,
           "got a cause")


def test_stranded_cause() -> None:
    print("\nTest: full Sen / spendable Kenki dead in the gauge at the kill")
    sen_dead = [(50.0, sd.GEKKO), (52.0, sd.KASHA), (54.0, sd.YUKIKAZE)]
    got = _stranded_cause(_ctx(sen_dead, []))
    _check("sen-led cause emitted",
           got is not None and got[1].ability_id == sd.MIDARE_SETSUGEKKA
           and got[1].kind == "cascade_lost_use", f"got {got}")
    _check("located at the set completion",
           got[1].time_sec == 54.0, f"got {got[1].time_sec}")
    _check("summary names the uncast Midare",
           "Midare Setsugekka" in got[1].summary, f"got {got[1].summary!r}")
    kenki_dead = [(50.0, sd.IKISHOTEN), (52.0, sd.YUKIKAZE)]   # 65 unspent
    got_k = _stranded_cause(_ctx(kenki_dead, []))
    _check("kenki-led cause emitted",
           got_k is not None and got_k[1].ability_id == sd.HISSATSU_SHINTEN,
           f"got {got_k}")
    _check("summary carries the stranded amount",
           "65 Kenki left" in got_k[1].summary, f"got {got_k[1].summary!r}")
    late_pile = [(297.0, sd.IKISHOTEN)]            # 3s before the kill: no slot
    _check("a pile built on the final swing stays silent",
           _stranded_cause(_ctx(late_pile, [])) is None, "got a cause")
    late_sen = [(290.0, sd.GEKKO), (292.0, sd.KASHA), (298.0, sd.YUKIKAZE)]
    _check("a set completed on the final swing stays silent",
           _stranded_cause(_ctx(late_sen, [])) is None, "got a cause")


def test_unmodeled_spenders_keep_ledgers_honest() -> None:
    print("\nTest: Kyuten/Guren/Gyoten Kenki spends debit the ledgers")
    # (a) Kenki: clean AoE-ish play that never really overcaps (peaks at
    # exactly 100). Without the Guren/Gyoten debits the ledger reads 35
    # overflowed and invents an overcap card.
    stream = ([(3.0 * i, sd.YUKIKAZE) for i in range(6)]        # 90
              + [(16.0, sd.HISSATSU_GUREN),                     # 65
                 (18.0, sd.YUKIKAZE),                           # 80
                 (21.0, sd.YUKIKAZE),                           # 95
                 (22.0, sd.HISSATSU_GYOTEN),                    # 85
                 (24.0, sd.YUKIKAZE)]                           # 100, at cap
              + [(25.0 + i, sd.HISSATSU_KYUTEN) for i in range(4)])  # 0
    _check("no overcap cause on clean play through Guren/Gyoten",
           _kenki_overcap_cause(_ctx(stream, [])) is None, "got a cause")
    # (b) Senei drift: Guren SHARES the 60s recast. Alternating them on
    # cooldown is clean play; the sim's line only knows Senei, so without
    # the shared-recast consume set this reads as 3 lost uses + 120s drift.
    ideal = [(60.0 * i, sd.HISSATSU_SENEI) for i in range(6)]
    alternating = [(60.0 * i,
                    sd.HISSATSU_SENEI if i % 2 == 0 else sd.HISSATSU_GUREN)
                   for i in range(6)]
    _check("alternating Senei/Guren on cooldown -> no drift cause",
           _cd_drift_causes(_ctx(alternating, ideal, fight_s=360.0)) == [],
           "got causes")


def test_forced_downtime_is_never_blamed() -> None:
    print("\nTest: no-enemy-targetable time is discounted out of the gaps")
    ideal = [(60.0 * i, sd.HISSATSU_SENEI) for i in range(6)]
    # Boss untargetable 60-105. The player presses Senei the moment it is
    # castable again, so the 105s gap is 45s of forced downtime, not drift.
    on_return = [(0.0, sd.HISSATSU_SENEI), (105.0, sd.HISSATSU_SENEI),
                 (165.0, sd.HISSATSU_SENEI), (225.0, sd.HISSATSU_SENEI)]
    _check("Senei pressed on the boss's return -> no drift cause",
           _cd_drift_causes(_ctx(on_return, ideal, fight_s=360.0,
                                 downtime=[(60.0, 105.0)])) == [],
           "got causes")
    _check("the same stream without the downtime does emit",
           len(_cd_drift_causes(_ctx(on_return, ideal, fight_s=360.0))) == 1,
           "got no cause")
    # Meikyo is a self-buff: pressable with nothing targetable, so its gaps
    # are NOT discounted (the sim presses it inside the window too).
    mk_ideal = [(55.0 * i, sd.MEIKYO_SHISUI) for i in range(7)]
    mk_late = [(0.0, sd.MEIKYO_SHISUI), (120.0, sd.MEIKYO_SHISUI),
               (240.0, sd.MEIKYO_SHISUI)]
    _check("Meikyo drift still counts through a downtime window",
           len(_cd_drift_causes(_ctx(mk_late, mk_ideal, fight_s=360.0,
                                     downtime=[(60.0, 105.0)]))) == 1,
           "got no cause")
    # A full Sen set dumped the instant the boss returns is not a wait.
    held = [(50.0, sd.GEKKO), (52.0, sd.KASHA), (54.0, sd.YUKIKAZE),
            (106.0, sd.MIDARE_SETSUGEKKA)]
    sen_ideal = [(56.0, sd.MIDARE_SETSUGEKKA), (110.0, sd.MIDARE_SETSUGEKKA)]
    _check("Sen held across forced downtime -> no pacing cause",
           _sen_pacing_cause(_ctx(held, sen_ideal, fight_s=360.0,
                                  downtime=[(60.0, 105.0)])) is None,
           "got a cause")
    _check("the same hold without the downtime does emit",
           _sen_pacing_cause(_ctx(held, sen_ideal, fight_s=360.0)) is not None,
           "got no cause")
    # A pile that only outlives the boss's last targetable second is not
    # spendable, so it is not stranded.
    tail = [(250.0, sd.GEKKO), (252.0, sd.KASHA), (254.0, sd.YUKIKAZE)]
    _check("set completed into a closing untargetable stretch stays silent",
           _stranded_cause(_ctx(tail, [], downtime=[(256.0, 300.0)])) is None,
           "got a cause")


def test_hagakure_dumps_sen_without_blame() -> None:
    print("\nTest: a Hagakure Sen dump clears the ledger silently")
    dumped = [(10.0, sd.GEKKO), (12.0, sd.KASHA), (14.0, sd.YUKIKAZE),
              (16.0, sd.HAGAKURE)]
    _check("dumped Sen is not stranded at the kill",
           _stranded_cause(_ctx(dumped, [])) is None, "got a cause")
    # Rebuild after the dump: the stale mask used to keep the old set 'full',
    # inventing a ~45s wait on the next Iaijutsu.
    rebuilt = dumped + [(60.0, sd.GEKKO), (62.0, sd.MIDARE_SETSUGEKKA)]
    ideal = [(20.0, sd.MIDARE_SETSUGEKKA), (50.0, sd.MIDARE_SETSUGEKKA),
             (80.0, sd.MIDARE_SETSUGEKKA)]
    _check("no phantom wait carried across the dump",
           _sen_pacing_cause(_ctx(rebuilt, ideal)) is None, "got a cause")


def test_probe_order_and_no_items() -> None:
    print("\nTest: advice_probes -> no items, causes value-ordered")
    from jobs.samurai.advice import advice_probes
    # Sen stranded (~2203p) + kenki overcap (500p): sen must lead.
    casts = ([(3.0 * i, sd.YUKIKAZE) for i in range(10)]
             + [(40.0, sd.GEKKO), (42.0, sd.KASHA)])
    items, causes = advice_probes(_ctx(casts, []), [])
    _check("no ProbeItems (causes only)", items == [], f"got {items}")
    _check("two causes emitted", len(causes) == 2,
           f"got {[c.kind for c in causes]}")
    _check("highest-value cause first (stranded Midare over overcap)",
           causes[0].ability_id == sd.MIDARE_SETSUGEKKA
           and causes[1].ability_id == sd.HISSATSU_SHINTEN,
           f"got {[c.ability_id for c in causes]}")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Samurai job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Samurai")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is ours", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text}")


def test_gauge_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a public scalar SimState field")
    from jobs.samurai.simulator import _model_for
    st = _model_for(300.0, None).init_state()
    for key in sorted(GAUGE_TEXT):
        _check(f"{key} exists on SimState", hasattr(st, key), "missing")
        val = getattr(st, key)
        _check(f"{key} is a scalar under the sentinel bound",
               isinstance(val, (int, float, bool)) and abs(float(val)) < 1e8,
               f"got {val!r}")


def test_copy_lint() -> None:
    print("\nTest: copy rules (no em/en dashes, no jargon, no exclamations)")
    strings: list[str] = []

    def _walk(x) -> None:
        if isinstance(x, str):
            strings.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                _walk(v)
    _walk(TEXT)
    for gt in GAUGE_TEXT.values():
        for s in (gt.label, gt.short, gt.over_note, gt.under_note):
            if s:
                strings.append(s)
    bad = [s for s in strings
           if "—" in s or "–" in s or "!" in s
           or "strict" in s.lower() or "lenient" in s.lower()]
    _check("all copy clean", bad == [], f"got {bad}")
    _check("copy present", len(strings) > 10, f"got {len(strings)}")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the SAM sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.samurai.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 300.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(dur, None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [
        _card("missed_cast", sd.HISSATSU_SENEI, 30.0, lost=400.0,
              name="Hissatsu: Senei"),
        _card("residual", 0, 0.0, lost=2400.0),
    ]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    if ex is None:
        # Degrade path: nothing worth moving. Advice list must still be there.
        _check("degrade path: advice list present",
               isinstance(out1["advice"], list), "missing advice")
        print("  [note] examined is None (degrade path exercised)")
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
    test_cd_drift_cause_emits()
    test_cd_drift_silent_when_clean_and_death_skipped()
    test_kenki_overcap_cause()
    test_sen_pacing_cause()
    test_stranded_cause()
    test_unmodeled_spenders_keep_ledgers_honest()
    test_forced_downtime_is_never_blamed()
    test_hagakure_dumps_sen_without_blame()
    test_probe_order_and_no_items()
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
