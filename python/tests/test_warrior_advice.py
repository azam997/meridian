"""Unit tests for the Warrior deep-advice pack (jobs/warrior/advice.py).

Covers each RootCause producer (emit + silent), the registration seam, the
GAUGE_TEXT allowlist keys against the real SimState, the copy rules, and the
cascade conservation smoke on the WAR simulator.

Run from python/:  python tests/test_warrior_advice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.advice import AdviceContext, GaugeText
from jobs.warrior import data as wd
from jobs.warrior.advice import (
    GAUGE_TEXT, TEXT, _beast_overcap_cause, _beast_stranded_cause,
    _cooldown_drift_causes, _infuriate_banked_cause, advice_probes,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _war_ctx(norm_casts, idealized, runner=None, fight_s: float = 400.0,
             deaths=None) -> AdviceContext:
    gcds = frozenset(wd.POTENCIES) - wd.OGCD_IDS
    return AdviceContext(
        job="Warrior", data=wd.JOB_DATA,
        norm_casts=list(norm_casts), idealized=list(idealized),
        fight_duration_s=fight_s, downtime_windows=[],
        death_windows=list(deaths or []),
        clipping_state={"clipping": None}, scoring_state={},
        enabler_values={}, sim_context=None,
        sim_module="jobs.warrior.simulator", runner=runner, gcd_ids=gcds)


def _card(kind: str, aid: int, t: float, lost: float = 500.0,
          name: str = "") -> dict:
    return {"kind": kind, "abilityId": aid, "abilityName": name,
            "timeSec": t, "lostPotency": lost, "summary": "x"}


def _combo(t0: float, step: float = 2.5) -> list[tuple[float, int]]:
    """One full ST combo (HS -> Maim -> Storm's Path), +30 gauge total."""
    return [(t0, wd.HEAVY_SWING), (t0 + step, wd.MAIM),
            (t0 + 2 * step, wd.STORMS_PATH)]


def test_cd_drift_emits() -> None:
    print("\nTest: Inner Release drift -> lost-use root cause")
    ideal = [(60.0 * i, wd.INNER_RELEASE) for i in range(6)]
    late = [(95.0 * i + 2.0, wd.INNER_RELEASE) for i in range(4)]
    got = _cooldown_drift_causes(_war_ctx(late, ideal))
    _check("one weighted cause", len(got) == 1, f"got {len(got)}")
    value, cause = got[0]
    _check("kind + ability", cause.kind == "cascade_lost_use"
           and cause.ability_id == wd.INNER_RELEASE, f"got {cause}")
    _check("value = deficit x per-use",
           value == 2 * wd.COOLDOWN_VALUE_P[wd.INNER_RELEASE],
           f"got {value}")
    _check("located inside the fight at the worst slip",
           0.0 <= cause.time_sec <= 400.0 and cause.time_sec == 2.0,
           f"got {cause.time_sec}")
    _check("summary counts the lost uses",
           "2 uses lost" in cause.summary, f"got {cause.summary!r}")
    _check("evidence rows carry counts + idle",
           len(cause.evidence) == 2 and cause.evidence[0].v == "4 / 6",
           f"got {cause.evidence}")


def test_cd_drift_silent_when_clean() -> None:
    print("\nTest: on-cooldown Inner Release stream stays silent")
    ideal = [(60.0 * i, wd.INNER_RELEASE) for i in range(6)]
    clean = [(60.0 * i, wd.INNER_RELEASE) for i in range(6)]
    _check("no cause on the clean stream",
           _cooldown_drift_causes(_war_ctx(clean, ideal)) == [], "got causes")
    # Deficit without accumulated drift (uses simply cut off by fight end)
    # also stays silent: the gaps are on-recast.
    short = [(60.0 * i, wd.INNER_RELEASE) for i in range(4)]
    _check("deficit without drift stays silent",
           _cooldown_drift_causes(_war_ctx(short, ideal)) == [], "got causes")


def test_cd_drift_orogeny_shares_upheaval() -> None:
    print("\nTest: Orogeny consumes Upheaval's shared recast (no fake drift)")
    ideal = [(30.0 * i, wd.UPHEAVAL) for i in range(10)]
    player = [(30.0 * i, wd.UPHEAVAL if i % 2 == 0 else wd.OROGENY)
              for i in range(10)]
    _check("alternating Upheaval/Orogeny on rate -> silent",
           _cooldown_drift_causes(_war_ctx(player, ideal)) == [],
           "got causes")


def test_infuriate_banked_mirrors_cdr() -> None:
    print("\nTest: Infuriate pool ledger mirrors the Fell Cleave CDR")
    # Player spends both charges early (t=5, t=10), then casts Fell Cleave
    # every 5s from 15..115. WITH the 5s-per-weaponskill refund mirrored, the
    # pool refills to 2 charges at t=70 and freezes for the last 50s (plus
    # the 5s opener stretch) -> 55s frozen >= the 30s floor -> cause. Without
    # the CDR the pool would not refill before the 120s fight ends and the
    # ledger would stay silent — emission here proves the mirror.
    fc = [(15.0 + 5.0 * k, wd.FELL_CLEAVE) for k in range(21)]
    player = [(5.0, wd.INFURIATE), (10.0, wd.INFURIATE)] + fc
    ideal = [(20.0 * i, wd.INFURIATE) for i in range(4)]
    got = _infuriate_banked_cause(_war_ctx(player, ideal, fight_s=120.0))
    _check("cause emitted (CDR mirrored)", got is not None, "got None")
    value, cause = got
    _check("kind + ability", cause.kind == "cascade_lost_use"
           and cause.ability_id == wd.INFURIATE, f"got {cause}")
    _check("located where the pool fills (t~70 only via the CDR)",
           69.5 <= cause.time_sec <= 70.5, f"got {cause.time_sec}")
    _check("value = deficit x per-use",
           value == 2 * wd.COOLDOWN_VALUE_P[wd.INFURIATE], f"got {value}")
    _check("summary names the full pool",
           "2 charges" in cause.summary and "lost" in cause.summary,
           f"got {cause.summary!r}")


def test_infuriate_banked_silent_when_spent() -> None:
    print("\nTest: spending near-full Infuriate keeps the ledger silent")
    # Same CDR-heavy stream, but the player spends the pool as it fills
    # (t=75, t=110): every full stretch stays short, total ~15s < 30s floor.
    # The deficit gate is NOT the reason (ideal 5 vs player 4 keeps it >= 1).
    fc = [(15.0 + 5.0 * k, wd.FELL_CLEAVE) for k in range(21)]
    player = ([(5.0, wd.INFURIATE), (10.0, wd.INFURIATE),
               (75.0, wd.INFURIATE), (110.0, wd.INFURIATE)] + fc)
    ideal = [(20.0 * i, wd.INFURIATE) for i in range(5)]
    got = _infuriate_banked_cause(_war_ctx(player, ideal, fight_s=120.0))
    _check("silent when the pool is spent on time", got is None, f"got {got}")
    # No deficit -> silent regardless of banking.
    banked = [(5.0, wd.INFURIATE), (10.0, wd.INFURIATE)] + fc
    even = [(0.0, wd.INFURIATE), (30.0, wd.INFURIATE)]
    _check("no deficit -> silent",
           _infuriate_banked_cause(
               _war_ctx(banked, even, fight_s=120.0)) is None, "got a cause")


def test_beast_overcap_emits() -> None:
    print("\nTest: Beast Gauge overflow -> delayed-Fell-Cleave root cause")
    # Eight combos, no spender: +30 each; overflow starts on combo 4's
    # finisher and accumulates 140 total.
    player: list[tuple[float, int]] = []
    for i in range(8):
        player += _combo(7.5 * i)
    got = _beast_overcap_cause(_war_ctx(player, []))
    _check("cause emitted", got is not None, "got None")
    value, cause = got
    _check("kind + ability (the spender)", cause.kind == "cascade_burst"
           and cause.ability_id == wd.FELL_CLEAVE, f"got {cause}")
    _check("summary totals the wasted gauge",
           "140 gauge wasted" in cause.summary, f"got {cause.summary!r}")
    _check("located at the first meaningful overflow (combo 4 finisher)",
           cause.time_sec == 27.5, f"got {cause.time_sec}")
    _check("beast gauge tagged as the implicated resource",
           cause.resources and cause.resources[0].label == "Beast Gauge",
           f"got {cause.resources}")
    _check("value prices the overflow",
           abs(value - 140 * wd.BEAST_VALUE_P_PER_UNIT) < 1e-9,
           f"got {value}")


def test_beast_overcap_mirrors_inner_release_free_cost() -> None:
    print("\nTest: free IR Fell Cleaves spend no gauge in the ledger")
    # Two Infuriates fill the gauge to exactly 100 (no overflow), Inner
    # Release arms 3 free Fell Cleaves (no gauge spent), then a finisher
    # (+20 over) and another Infuriate (+50 over) overflow 70 total. If the
    # ledger wrongly charged the free casts 50 each, the gauge would sit at
    # 0 and nothing would overflow — emission proves the IR mirror.
    player = [
        (0.0, wd.INFURIATE), (2.0, wd.INFURIATE),
        (5.0, wd.INNER_RELEASE),
        (7.5, wd.FELL_CLEAVE), (10.0, wd.FELL_CLEAVE),
        (12.5, wd.FELL_CLEAVE),
        (15.0, wd.STORMS_PATH),
        (17.0, wd.INFURIATE),
    ]
    got = _beast_overcap_cause(_war_ctx(player, []))
    _check("cause emitted (IR free-cost mirrored)", got is not None,
           "got None")
    _value, cause = got
    _check("first overflow at the finisher after the free casts",
           cause.time_sec == 15.0, f"got {cause.time_sec}")


def test_beast_overcap_silent_when_clean() -> None:
    print("\nTest: a spending rotation never overflows -> silent")
    player: list[tuple[float, int]] = []
    t = 0.0
    for _ in range(4):
        player += _combo(t) + _combo(t + 7.5)          # +60 gauge
        player.append((t + 15.0, wd.FELL_CLEAVE))      # spend 50 back down
        t += 17.5
    _check("no cause", _beast_overcap_cause(_war_ctx(player, [])) is None,
           "got a cause")


def test_beast_overcap_death_windows_excluded() -> None:
    print("\nTest: overflow inside a death window attributes nothing")
    player: list[tuple[float, int]] = []
    for i in range(8):
        player += _combo(7.5 * i)
    got = _beast_overcap_cause(
        _war_ctx(player, [], deaths=[(0.0, 400.0)]))
    _check("silent when every overflow is inside a death window",
           got is None, f"got {got}")


def test_beast_stranded() -> None:
    print("\nTest: 60 Beast dead at the kill -> stranded cause; spent -> none")
    player = _combo(0.0) + _combo(7.5)                 # 60 gauge at the end
    got = _beast_stranded_cause(_war_ctx(player, [], fight_s=20.0))
    _check("cause emitted", got is not None, "got None")
    value, cause = got
    _check("kind + ability", cause.kind == "cascade_lost_use"
           and cause.ability_id == wd.FELL_CLEAVE, f"got {cause}")
    _check("located at the last gauge builder",
           cause.time_sec == 12.5, f"got {cause.time_sec}")
    _check("summary names the stranded gauge",
           "left with 60" in cause.summary, f"got {cause.summary!r}")
    _check("value prices only the spendable 50",
           abs(value - 50 * wd.BEAST_VALUE_P_PER_UNIT) < 1e-9,
           f"got {value}")
    spent = player + [(15.0, wd.FELL_CLEAVE)]          # down to 10
    _check("spent gauge -> no cause",
           _beast_stranded_cause(_war_ctx(spent, [], fight_s=20.0)) is None,
           "got a cause")


def test_beast_stranded_silent_when_spender_follows_builder() -> None:
    print("\nTest: paid spend after the last builder keeps the ledger silent")
    # Build to 100 (two Infuriates), then a paid Fell Cleave at 105 leaves 50
    # in the gauge at the kill. A Fell Cleave only subtracts 50, so 50+ can
    # survive a spend that came AFTER the last builder; the evidence line
    # claims "no spender after", which would be false here -> stay silent.
    player = [(95.0, wd.INFURIATE), (100.0, wd.INFURIATE),
              (105.0, wd.FELL_CLEAVE)]
    got = _beast_stranded_cause(_war_ctx(player, [], fight_s=110.0))
    _check("silent when a spender follows the last builder", got is None,
           f"got {got}")
    # A FREE Inner Release Fell Cleave after the last builder spends stacks,
    # not gauge, so the gauge genuinely sat unspent -> the cause still fires.
    player_ir = [(90.0, wd.INFURIATE), (95.0, wd.INFURIATE),
                 (100.0, wd.INNER_RELEASE), (105.0, wd.FELL_CLEAVE)]
    got_ir = _beast_stranded_cause(_war_ctx(player_ir, [], fight_s=110.0))
    _check("free IR cast after the builder does not silence it",
           got_ir is not None and got_ir[1].time_sec == 95.0,
           f"got {got_ir}")


def test_advice_probes_order_and_determinism() -> None:
    print("\nTest: advice_probes -> no items, causes by descending value")
    # Fire IR drift (2 x 1500) and the stranded gauge (50 x 5.6 = 280).
    ideal = [(60.0 * i, wd.INNER_RELEASE) for i in range(6)]
    player = ([(95.0 * i + 2.0, wd.INNER_RELEASE) for i in range(4)]
              + _combo(360.0) + _combo(370.0))
    ctx = _war_ctx(player, ideal)
    items, causes = advice_probes(ctx, [])
    _check("no probe items (WAR ships causes only)", items == [],
           f"got {items}")
    _check("both causes present", len(causes) == 2,
           f"got {[c.kind for c in causes]}")
    _check("highest lost value leads (IR drift before stranded gauge)",
           causes[0].ability_id == wd.INNER_RELEASE
           and causes[1].ability_id == wd.FELL_CLEAVE,
           f"got {[c.ability_id for c in causes]}")
    again = advice_probes(_war_ctx(player, ideal), [])
    _check("deterministic across two runs", (items, causes) == again,
           "runs differ")


def test_registration() -> None:
    print("\nTest: the pack is registered on the Warrior job")
    from sidecar.advice import resolve_pack
    pack = resolve_pack("Warrior")
    _check("pack resolves", pack is not None, "got None")
    _check("gauge_text is the WAR glossary", pack.gauge_text is GAUGE_TEXT,
           f"got {pack.gauge_text}")


def test_gauge_text_keys_are_real_state_fields() -> None:
    print("\nTest: every GAUGE_TEXT key is a real SimState scalar field")
    from jobs.warrior.simulator import _model_for
    state = _model_for(None).init_state()
    for key in GAUGE_TEXT:
        val = getattr(state, key, None)
        _check(f"'{key}' exists on SimState and is scalar",
               hasattr(state, key) and isinstance(val, (bool, int, float)),
               f"got {val!r}")


def test_copy_rules() -> None:
    print("\nTest: copy lint (no em/en dashes, no strict/lenient jargon)")

    def _walk(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)

    strings = list(_walk(TEXT))
    for gt in GAUGE_TEXT.values():
        _check("GaugeText entries are GaugeText", isinstance(gt, GaugeText),
               f"got {type(gt)}")
        strings += [s for s in (gt.label, gt.short, gt.over_note,
                                gt.under_note) if s]
    _check("some strings collected", len(strings) > 10, f"got {len(strings)}")
    for s in strings:
        # ascii() keeps the console-print safe on cp1252 Windows consoles
        # (the idle_note's U+2248 would crash a plain {s!r} print).
        _check(f"no em/en dash in {ascii(s[:40])}",
               "—" not in s and "–" not in s, f"got {ascii(s)}")
        _check(f"no jargon in {ascii(s[:40])}",
               "strict" not in s.lower() and "lenient" not in s.lower(),
               f"got {ascii(s)}")
        _check(f"no exclamation in {ascii(s[:40])}", "!" not in s,
               f"got {ascii(s)}")


def test_examined_conservation_and_stability() -> None:
    print("\nTest: v2 examined list on the WAR sim — conservation, stability")
    import json

    from jobs._core.sim import engine
    from jobs._core.sim.counterfactual import Runner
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.warrior.simulator import SimParams, _model_for
    from sidecar.advice import compute_advice_v2

    dur = 180.0
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, dur),))
    timeline, _aux = engine.run_rotation(_model_for(None), dur, [], params)
    ideal = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    player = [(t, a) for t, a in ideal if not 60.0 <= t < 66.0]   # 6s hole
    ctx = _war_ctx(player, ideal, fight_s=dur)
    runner = Runner(ctx.sim_module, dur, (), None, player,
                    gcd_ids=sorted(ctx.gcd_ids))
    ctx.runner = runner
    cards = [_card("residual", 0, 0.0, lost=2400.0)]
    out1 = compute_advice_v2(ctx, [dict(c) for c in cards])
    out2 = compute_advice_v2(ctx, [dict(c) for c in cards])
    _check("byte-stable across two runs",
           json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True),
           "runs differ")
    ex = out1["examined"]
    _check("advice list present", isinstance(out1["advice"], list), "missing")
    if ex is None:
        # Degrade path exercised (nothing worth moving) — honest but report it.
        print("  NOTE: examined is None (degrade path); conservation trivially holds")
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
               for c in cascade for r in c.get("evidence", []) if r["note"]),
           f"got {[(c.get('prescription'), c.get('evidence')) for c in cascade]}")
    resid = [c for c in ex["improvements"] if c["kind"] == "residual"]
    _check("residual shrank but kept its floor",
           len(resid) == 1 and 60.0 <= resid[0]["lostPotency"] < 2400.0,
           f"got {resid}")


def main() -> int:
    test_cd_drift_emits()
    test_cd_drift_silent_when_clean()
    test_cd_drift_orogeny_shares_upheaval()
    test_infuriate_banked_mirrors_cdr()
    test_infuriate_banked_silent_when_spent()
    test_beast_overcap_emits()
    test_beast_overcap_mirrors_inner_release_free_cost()
    test_beast_overcap_silent_when_clean()
    test_beast_overcap_death_windows_excluded()
    test_beast_stranded()
    test_beast_stranded_silent_when_spender_follows_builder()
    test_advice_probes_order_and_determinism()
    test_registration()
    test_gauge_text_keys_are_real_state_fields()
    test_copy_rules()
    test_examined_conservation_and_stability()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
