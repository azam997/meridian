"""Multi-target scoring & detection tests (no network).

Built up across the multi-target arc. Phase 1 covers the targetability-derived
candidate-window detection (>= 2 enemies simultaneously targetable) and the
disclaim threshold. Later phases append splash-math, ref-consensus, and the
>100% guard.

Run from python/:  python tests/test_multi_target.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import (  # noqa: E402
    fetch_multi_target_windows,
    is_multi_target_pull,
    simultaneous_targetable_windows,
)
from jobs._core.downtime_sources import (  # noqa: E402
    multi_target_consensus_from_refs,
)
from jobs._core.job import MELEE_DPS  # noqa: E402
from jobs._core.multi_target import (  # noqa: E402
    observed_multi_target_casts,
)


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ev(timestamp: int, target_id: int, targetable: int) -> dict:
    return {
        "timestamp": timestamp,
        "type": "targetabilityupdate",
        "sourceID": target_id,
        "targetID": target_id,
        "targetable": targetable,
    }


# --- simultaneous_targetable_windows (the sweep-line detector) --------------

def test_single_boss_no_windows() -> None:
    """One boss, no events => targetable the whole fight, coverage never
    reaches 2 => no multi-target windows. (The M12S single-target guard.)"""
    print()
    print("Test: single boss => no multi-target windows")
    out = simultaneous_targetable_windows(
        [], 0, 600_000, enemy_ids={28}, boss_ids={28})
    _check("no windows", out == [], f"got {out}")


def test_two_bosses_whole_fight() -> None:
    """Two co-bosses (both subType Boss, no events) => both targetable the
    whole fight => one window spanning it, peak 2. This is the M10S
    "Red Hot & Deep Blue" shape (two simultaneous bosses)."""
    print()
    print("Test: two co-bosses => one full-fight window")
    out = simultaneous_targetable_windows(
        [], 0, 100_000, enemy_ids={28, 29}, boss_ids={28, 29})
    _check("one window", len(out) == 1, f"got {out}")
    s, e, n = out[0]
    _check("spans the fight", abs(s) < 0.01 and abs(e - 100.0) < 0.01, f"({s},{e})")
    _check("peak 2 targets", n == 2, f"got {n}")


def test_add_phase_in_the_middle() -> None:
    """Boss always up; an add spawns at 30s and despawns at 70s => a single
    40s multi-target window, peak 2."""
    print()
    print("Test: mid-fight add phase => one ~40s window")
    evs = [_ev(30_000, 40, 1), _ev(70_000, 40, 0)]  # spawn then despawn
    out = simultaneous_targetable_windows(
        evs, 0, 120_000, enemy_ids={28, 40}, boss_ids={28})
    _check("one window", len(out) == 1, f"got {out}")
    s, e, n = out[0]
    _check("window ~[30,70]", abs(s - 30.0) < 0.01 and abs(e - 70.0) < 0.01,
           f"({s},{e})")
    _check("peak 2", n == 2, f"got {n}")


def test_pure_handoff_no_window() -> None:
    """Boss leaves and an add spawns to cover the gap, then despawns before
    the boss returns (a clean handoff). The two are NEVER simultaneously
    targetable => no multi-target window. (The M9S-style false-positive guard.)"""
    print()
    print("Test: boss<->add handoff => no simultaneous window")
    evs = [
        _ev(68_000, 28, 0),    # boss leaves
        _ev(68_300, 40, 1),    # add spawns
        _ev(118_000, 40, 0),   # add despawns
        _ev(119_000, 28, 1),   # boss returns
    ]
    out = simultaneous_targetable_windows(
        evs, 0, 200_000, enemy_ids={28, 40}, boss_ids={28})
    _check("no window (never overlap)", out == [], f"got {out}")


def test_brief_overlap_filtered() -> None:
    """A < min_window overlap (boss up, add flickers for 3s) is dropped as a
    handoff artifact rather than a real multi-target phase."""
    print()
    print("Test: sub-min_window overlap filtered")
    evs = [_ev(50_000, 40, 1), _ev(53_000, 40, 0)]  # 3s add, min_window=5s
    out = simultaneous_targetable_windows(
        evs, 0, 120_000, enemy_ids={28, 40}, boss_ids={28})
    _check("filtered to no window", out == [], f"got {out}")


def test_three_enemies_peak_count() -> None:
    """Boss + two adds all up together for a stretch => peak_n reports 3."""
    print()
    print("Test: three simultaneous enemies => peak 3")
    evs = [
        _ev(20_000, 40, 1), _ev(80_000, 40, 0),
        _ev(30_000, 41, 1), _ev(60_000, 41, 0),
    ]
    out = simultaneous_targetable_windows(
        evs, 0, 100_000, enemy_ids={28, 40, 41}, boss_ids={28})
    # >=2 coverage runs [20,80] (boss always up + at least one add).
    _check("one window", len(out) == 1, f"got {out}")
    s, e, n = out[0]
    _check("window ~[20,80]", abs(s - 20.0) < 0.01 and abs(e - 80.0) < 0.01,
           f"({s},{e})")
    _check("peak 3 (boss + both adds, [30,60])", n == 3, f"got {n}")


def test_non_boss_no_events_ignored() -> None:
    """A non-boss enemy that never flips (no events) carries no information
    and must NOT be counted — otherwise an environmental/anchor NPC would
    synthesize a phantom whole-fight multi-target window."""
    print()
    print("Test: non-boss enemy with no events is ignored")
    out = simultaneous_targetable_windows(
        [], 0, 100_000, enemy_ids={28, 99}, boss_ids={28})
    _check("no window (anchor NPC ignored)", out == [], f"got {out}")


def test_open_ended_add_no_phantom_overlap() -> None:
    """An add that spawns (`targetable=1`) and then vanishes WITHOUT ever
    logging a despawn `0` — many mechanic adds (M9S Vamp Fatale's Coffinmaker,
    Fatal Flails, Charnel Cells) emit only a spawn and then silently despawn —
    must NOT be assumed targetable to fight end. Otherwise it stacks onto the
    returning boss into a phantom whole-fight multi-target window. Mirrors
    `test_pure_handoff_no_window` but with the despawn event MISSING: the
    regression that mislabeled Vamp Fatale as a 3-target fight from ~1 min in."""
    print()
    print("Test: open-ended add (no despawn) doesn't extend to fight end")
    evs = [
        _ev(68_000, 28, 0),    # boss leaves
        _ev(68_300, 40, 1),    # add spawns — and never logs a despawn
        _ev(119_000, 28, 1),   # boss returns long after the add is gone
    ]
    out = simultaneous_targetable_windows(
        evs, 0, 520_000, enemy_ids={28, 40}, boss_ids={28})
    # The add's tail is capped near its spawn, so it's never simultaneously
    # targetable with the returned boss — no phantom 400s window.
    _check("no phantom whole-fight window", out == [], f"got {out}")


def test_repeated_spawn_add_window_localized() -> None:
    """An add that re-emits `targetable=1` several times without a despawn `0`
    (the Fatal Flail / Charnel Cell pattern) is targetable from its first spawn
    to a short grace past its LAST one — a window localized to roughly when the
    add was up, NOT extended all the way to fight end."""
    print()
    print("Test: repeated-spawn add => window localized, not to fight end")
    evs = [_ev(300_000, 46, 1), _ev(315_000, 46, 1), _ev(333_000, 46, 1)]
    out = simultaneous_targetable_windows(
        evs, 0, 520_000, enemy_ids={28, 46}, boss_ids={28})
    _check("one localized window", len(out) == 1, f"got {out}")
    s, e, n = out[0]
    _check("starts at first spawn (~300s)", abs(s - 300.0) < 0.01, f"{s}")
    _check("ends shortly after last spawn, far from fight end (520s)",
           333.0 < e < 350.0, f"{e}")
    _check("peak 2 (boss + add)", n == 2, f"{n}")


# --- is_multi_target_pull (the disclaim threshold) -------------------------

def test_disclaim_threshold() -> None:
    print()
    print("Test: is_multi_target_pull total-duration threshold")
    _check("empty => not multi-target", is_multi_target_pull([]) is False)
    _check("40s window => multi-target",
           is_multi_target_pull([(30.0, 70.0, 2)]) is True)
    _check("8s window => below threshold",
           is_multi_target_pull([(30.0, 38.0, 2)]) is False)
    _check("two 8s windows sum to 16s => multi-target",
           is_multi_target_pull([(10.0, 18.0, 2), (40.0, 48.0, 2)]) is True)


# --- fetch_multi_target_windows (end-to-end with a stub client) ------------

class _StubClient:
    def __init__(self, events: list[dict]):
        self._events = events

    def get_targetability_events(self, code, start, end):
        return [e for e in self._events if start <= e["timestamp"] <= end]


def test_fetch_end_to_end() -> None:
    print()
    print("Test: fetch_multi_target_windows — end-to-end via stub client")
    events = [_ev(30_000, 40, 1), _ev(70_000, 40, 0)]
    report_summary = {
        "masterData": {"actors": [
            {"id": 28, "subType": "Boss"},
            {"id": 40, "subType": "NPC"},
        ]},
    }
    fight = {"startTime": 0, "endTime": 120_000,
             "enemyNPCs": [{"id": 28}, {"id": 40}]}
    out = fetch_multi_target_windows(_StubClient(events), "abc",
                                     report_summary, fight)
    _check("one window", len(out) == 1, f"got {out}")
    _check("disclaimed (>15s)", is_multi_target_pull(out) is True)


def test_fetch_client_failure_empty() -> None:
    print()
    print("Test: fetch_multi_target_windows — client failure => []")

    class _Boom:
        def get_targetability_events(self, *a, **kw):
            raise RuntimeError("network")

    out = fetch_multi_target_windows(
        _Boom(), "abc", {"masterData": {"actors": []}},
        {"startTime": 0, "endTime": 100_000, "enemyNPCs": []})
    _check("empty on failure", out == [], f"got {out}")


# --- observed_multi_target_casts (packetID grouping) -----------------------

def _dmg(timestamp: int, ability_id: int, target_id: int,
         packet_id: int | None = None) -> dict:
    ev = {
        "timestamp": timestamp,
        "type": "calculateddamage",
        "abilityGameID": ability_id,
        "targetID": target_id,
    }
    if packet_id is not None:
        ev["packetID"] = packet_id
    return ev


class _DmgClient:
    def __init__(self, events: list[dict]):
        self._events = events

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        return list(self._events)


def test_packetid_grouping() -> None:
    """One cast hitting 3 enemies emits 3 events sharing a packetID => one
    (t, aid, 3) entry. A 1-target packet is dropped (no splash signal)."""
    print()
    print("Test: observed_multi_target_casts — packetID grouping")
    events = [
        # Cast A (packet 100): Spinning Scythe hits 3 enemies at ~10s.
        _dmg(10_000, 24376, 28, 100),
        _dmg(10_000, 24376, 40, 100),
        _dmg(10_050, 24376, 41, 100),
        # Cast B (packet 101): Slice hits 1 enemy at ~12s — single target.
        _dmg(12_000, 24373, 28, 101),
    ]
    fight = {"startTime": 0, "endTime": 100_000}
    out = observed_multi_target_casts(_DmgClient(events), "abc", fight,
                                      {"id": 7})
    _check("one multi-target cast", len(out) == 1, f"got {out}")
    t, aid, n = out[0]
    _check("ability 24376", aid == 24376, f"got {aid}")
    _check("3 targets", n == 3, f"got {n}")
    _check("time ~10s", abs(t - 10.0) < 0.1, f"got {t}")


def test_packetid_fallback_by_time() -> None:
    """With no packetID, hits of one cast (same ability, ~same time) still
    collapse to one packet via the (ability, timestamp-bucket) fallback."""
    print()
    print("Test: observed_multi_target_casts — packetID-absent fallback")
    events = [
        _dmg(20_000, 24384, 28),   # Guillotine, no packetID
        _dmg(20_030, 24384, 40),
        _dmg(20_060, 24384, 41),
    ]
    fight = {"startTime": 0, "endTime": 100_000}
    out = observed_multi_target_casts(_DmgClient(events), "abc", fight,
                                      {"id": 7})
    _check("one multi-target cast", len(out) == 1, f"got {out}")
    _check("3 targets via fallback", out[0][2] == 3, f"got {out}")


def test_observed_empty_on_failure() -> None:
    print()
    print("Test: observed_multi_target_casts — fetch failure => ()")

    class _Boom:
        def get_events(self, *a, **kw):
            raise RuntimeError("network")

    out = observed_multi_target_casts(
        _Boom(), "abc", {"startTime": 0, "endTime": 100_000}, {"id": 7})
    _check("empty on failure", out == (), f"got {out}")


# --- multi_target_consensus_from_refs --------------------------------------
# MELEE_DPS policy: min_ref_count=4, consensus_pct=0.75.

def _ref(*casts):
    """A ref's observed_multi_target_casts tuple."""
    return tuple(casts)


def test_consensus_min_ref_count_gate() -> None:
    print()
    print("Test: consensus — below min_ref_count => [] (can't confirm)")
    cand = [(0.0, 100.0, 2)]
    refs = [_ref((10.0, 24398, 2)) for _ in range(3)]  # 3 < min 4
    out = multi_target_consensus_from_refs(cand, refs, MELEE_DPS)
    _check("no windows (pool too small)", out == [], f"got {out}")


def test_consensus_confirms_window() -> None:
    print()
    print("Test: consensus — >=75% of refs cleaving confirms the window")
    cand = [(0.0, 100.0, 2)]
    # 5 refs, 4 cleave inside the window (80% >= 75%), 1 doesn't.
    refs = [_ref((10.0, 24398, 2)) for _ in range(4)] + [_ref()]
    out = multi_target_consensus_from_refs(cand, refs, MELEE_DPS)
    _check("one confirmed window", len(out) == 1, f"got {out}")
    _check("N = 2", out[0].target_count == 2, f"got {out[0]}")


def test_consensus_below_threshold_rejects() -> None:
    print()
    print("Test: consensus — <75% cleaving leaves the window unconfirmed")
    cand = [(0.0, 100.0, 2)]
    # 5 refs, only 2 cleave (40% < 75%).
    refs = [_ref((10.0, 24398, 2)) for _ in range(2)] + [_ref() for _ in range(3)]
    out = multi_target_consensus_from_refs(cand, refs, MELEE_DPS)
    _check("no confirmed window", out == [], f"got {out}")


def test_consensus_modal_n_capped_at_peak() -> None:
    print()
    print("Test: consensus — N is modal ref count, capped at window peak_n")
    cand = [(0.0, 100.0, 2)]   # peak_n = 2
    # Refs hit 3 targets, but the window only had 2 simultaneously targetable.
    refs = [_ref((10.0, 24398, 3)) for _ in range(4)]
    out = multi_target_consensus_from_refs(cand, refs, MELEE_DPS)
    _check("N capped at peak 2", out and out[0].target_count == 2, f"got {out}")


def test_consensus_only_casts_in_window_count() -> None:
    print()
    print("Test: consensus — casts outside the candidate window don't confirm it")
    cand = [(50.0, 60.0, 2)]
    # All 4 refs cleave, but at t=10 (outside [50,60]).
    refs = [_ref((10.0, 24398, 2)) for _ in range(4)]
    out = multi_target_consensus_from_refs(cand, refs, MELEE_DPS)
    _check("window not confirmed (casts elsewhere)", out == [], f"got {out}")


# --- _inject_multi_target crediting + the >100% guard ----------------------
# Integration test through the real Reaper job (splash table + simulator).

def _rpr_run(duration, delivered, idealized, mt_casts):
    from jobs import AspectResult, ModuleResult, Track, get_job
    get_job("Reaper")   # lazy-register the job (the sidecar's raw lookup won't)
    mr = ModuleResult(
        label="t", fight_duration_s=duration, downtime_windows=[],
        multi_target_windows=((0.0, duration, 2),),
        observed_multi_target_casts=tuple(mt_casts))
    mr.aspects["Scoring"] = AspectResult(
        name="Scoring", track=Track(name="Scoring", events=[]),
        state={"delivered_potency": float(delivered),
               "idealized_strict": float(idealized),
               "downtime_windows": [], "fight_duration_s": duration})
    return mr


def test_inject_credits_and_holds_invariant() -> None:
    print()
    print("Test: _inject_multi_target — credits splash, delivered <= ceiling")
    import sidecar.main as M
    COMMUNIO = 24398
    casts = [(float(t), COMMUNIO, 2) for t in range(50, 550, 60)]
    you = _rpr_run(600.0, delivered=100_000, idealized=110_000, mt_casts=casts)
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0, COMMUNIO, 2), (200.0, COMMUNIO, 2)]) for _ in range(5)]
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    _check("credited", st.get("multi_target_credited") is True, f"{st.get('multi_target_credited')}")
    _check("delivered rose", st["delivered_multitarget"] > 100_000,
           f"{st['delivered_multitarget']}")
    _check("ceiling rose", st["idealized_multitarget"] > 110_000,
           f"{st['idealized_multitarget']}")
    _check("delivered <= ceiling (>100% guard)",
           st["delivered_multitarget"] <= st["idealized_multitarget"] * 1.0005)
    _check("windows serialized with per-window splash",
           bool(st["multi_target_windows"])
           and "deliveredSplash" in st["multi_target_windows"][0])


def test_inject_guard_blocks_over_100() -> None:
    print()
    print("Test: _inject_multi_target — guard leaves run uncredited if >100%")
    import sidecar.main as M
    COMMUNIO = 24398
    # Player has a flood of multi-target Communios (synthetic), delivered already
    # right at the ceiling → credited delivered would exceed the ceiling, which
    # the idealized rotation's modest Communio count can't match. Must NOT credit.
    flood = [(float(t), COMMUNIO, 2) for t in range(40, 580, 8)]
    you = _rpr_run(600.0, delivered=109_900, idealized=110_000, mt_casts=flood)
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0, COMMUNIO, 2), (200.0, COMMUNIO, 2)]) for _ in range(5)]
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    _check("NOT credited (guard tripped)",
           st.get("multi_target_credited") is False, f"{st.get('multi_target_credited')}")


def test_inject_noop_without_consensus() -> None:
    print()
    print("Test: _inject_multi_target — no credit when refs don't confirm")
    import sidecar.main as M
    COMMUNIO = 24398
    you = _rpr_run(600.0, 100_000, 110_000, [(50.0, COMMUNIO, 2)])
    refs = [_rpr_run(600.0, 100_000, 110_000, []) for _ in range(5)]  # refs don't cleave
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    _check("uncredited (no consensus)", not st.get("multi_target_credited"))


# --- P3: floored N(t) schedule + AoE-only-job crediting --------------------

def test_per_window_deltas_sum_to_totals() -> None:
    """The per-window deny deltas must sum EXACTLY to the run's headline AoE totals,
    so denying every window reverts precisely to the single-target efficiency."""
    print()
    print("Test: per-window deltas sum to the headline totals (deny coherence)")
    import sidecar.main as M
    COMMUNIO = 24398
    casts = [(float(t), COMMUNIO, 2) for t in range(50, 550, 60)]
    you = _rpr_run(600.0, delivered=100_000, idealized=110_000, mt_casts=casts)
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0, COMMUNIO, 2), (200.0, COMMUNIO, 2)]) for _ in range(5)]
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    if not st.get("multi_target_credited"):
        _check("credited (precondition)", False, "not credited")
        return
    wins = st["multi_target_windows"]
    sum_dl = sum(w["deliveredSplash"] for w in wins)
    sum_cl = sum(w["ceilingSplash"] for w in wins)
    tot_dl = st["delivered_multitarget"] - st["delivered_potency"]
    tot_cl = st["idealized_multitarget"] - st["idealized_strict"]
    _check("Σ deliveredSplash == delivered delta",
           abs(sum_dl - tot_dl) <= 0.5, f"{sum_dl} vs {tot_dl}")
    _check("Σ ceilingSplash == ceiling delta",
           abs(sum_cl - tot_cl) <= 0.5, f"{sum_cl} vs {tot_cl}")


def test_per_ref_window_deltas_serialized() -> None:
    """Each serialized window carries every ref's own (delivered, ceiling)
    deltas in refs-array order plus the refs' average delivered splash — the
    frontend's crediting modes (cap at top-10 average / at player credited)
    recompute you AND the displayed ref efficiencies from these. Each ref's
    per-window deltas must sum to that ref's own headline totals, exactly like
    the player's do."""
    print()
    print("Test: per-ref window deltas serialized + sum to each ref's totals")
    import sidecar.main as M
    COMMUNIO = 24398
    casts = [(float(t), COMMUNIO, 2) for t in range(50, 550, 60)]
    you = _rpr_run(600.0, delivered=100_000, idealized=110_000, mt_casts=casts)
    # Refs with DIFFERENT cleave counts so the average is a real mean.
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0 + 10 * k, COMMUNIO, 2) for k in range(1 + i)])
            for i in range(5)]
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    if not st.get("multi_target_credited"):
        _check("credited (precondition)", False, "not credited")
        return
    wins = st["multi_target_windows"]
    for w in wins:
        _check("refDeliveredSplash has one entry per ref",
               len(w["refDeliveredSplash"]) == len(refs),
               f"{len(w['refDeliveredSplash'])} vs {len(refs)}")
        _check("refCeilingSplash has one entry per ref",
               len(w["refCeilingSplash"]) == len(refs),
               f"{len(w['refCeilingSplash'])} vs {len(refs)}")
        avg = sum(w["refDeliveredSplash"]) / len(refs)
        _check("refAvgDeliveredSplash is the mean of the per-ref deltas",
               abs(w["refAvgDeliveredSplash"] - avg) <= 0.5,
               f"{w['refAvgDeliveredSplash']} vs {avg}")
    for j, r in enumerate(refs):
        rst = r.aspects["Scoring"].state
        sum_dl = sum(w["refDeliveredSplash"][j] for w in wins)
        sum_cl = sum(w["refCeilingSplash"][j] for w in wins)
        tot_dl = rst["delivered_multitarget"] - rst["delivered_potency"]
        tot_cl = rst["idealized_multitarget"] - rst["idealized_strict"]
        _check(f"ref#{j} Σ delivered deltas == its headline delta",
               abs(sum_dl - tot_dl) <= 0.5, f"{sum_dl} vs {tot_dl}")
        _check(f"ref#{j} Σ ceiling deltas == its headline delta",
               abs(sum_cl - tot_cl) <= 0.5, f"{sum_cl} vs {tot_cl}")


def test_observed_reach_caps_construction() -> None:
    """`_observed_reach_caps` = per-ability max observed target count across
    you+refs, counting only casts inside the confirmed windows. The invariant
    that keeps it under-credit-safe: cap[aid] >= every delivered n of aid, since
    both read the same observed casts."""
    print()
    print("Test: _observed_reach_caps — per-ability max, in-window only")
    import sidecar.main as M
    from jobs._core.downtime_sources import MultiTargetWindow

    class _Run:
        def __init__(self, casts):
            self.observed_multi_target_casts = tuple(casts)

    windows = [MultiTargetWindow(start_s=0.0, end_s=100.0, target_count=3)]
    runs = [_Run([(10.0, 111, 2), (150.0, 111, 5)]),   # 2nd cast OUTSIDE the window
            _Run([(20.0, 111, 3), (30.0, 222, 2)])]
    caps = M._observed_reach_caps(windows, runs)
    _check("per-ability max across runs, in-window casts only",
           dict(caps) == {111: 3, 222: 2}, f"{caps}")
    _check("sorted hashable tuple", caps == tuple(sorted(dict(caps).items())),
           f"{caps}")
    _check("empty runs -> no caps", M._observed_reach_caps(windows, []) == ())


def test_capped_splash_ceiling_binds() -> None:
    """The observed-reach cap bounds the legacy free-splash ceiling: an ability
    observed hitting only 2 targets is credited at 2 even where the window's
    schedule says 3 — and the capped delta never drops below the single-target
    ceiling (>= 0)."""
    print()
    print("Test: _credit_multi_target_run — ability cap binds the splash ceiling")
    import sidecar.main as M
    from jobs import get_job
    from jobs._core.downtime_sources import MultiTargetWindow
    COMMUNIO = 24398
    data = get_job("Reaper").data
    sp = data.splash_potencies[COMMUNIO]
    windows = [MultiTargetWindow(start_s=0.0, end_s=100.0, target_count=3)]
    schedule = ((0.0, 100.0, 3),)

    class _FakeResult:
        def __init__(self):
            self.timeline = ((10.0, COMMUNIO), (50.0, COMMUNIO))
            self.delivered_potency = 1000.0

    class _FakeSim:
        def simulate(self, duration, downtime, sim_context=None):
            return _FakeResult()   # same score either context -> aoe_delta 0

    def _fresh():
        return _rpr_run(100.0, delivered=1000, idealized=2000,
                        mt_casts=[(10.0, COMMUNIO, 2)])

    you = _fresh()
    M._credit_multi_target_run(you, windows, schedule, data, _FakeSim())
    uncapped = you.aspects["Scoring"].state["mt_ceiling_delta"]
    you2 = _fresh()
    M._credit_multi_target_run(you2, windows, schedule, data, _FakeSim(),
                               ability_caps=((COMMUNIO, 2),))
    capped = you2.aspects["Scoring"].state["mt_ceiling_delta"]
    _check("uncapped: sp*(3-1) per ST-sim cast", uncapped == sp * 2 * 2,
           f"{uncapped} vs {sp * 2 * 2}")
    _check("capped: sp*(2-1) per ST-sim cast", capped == sp * 1 * 2,
           f"{capped} vs {sp * 1 * 2}")
    _check("capped ceiling still >= delivered (guard intact)",
           you2.aspects["Scoring"].state["delivered_multitarget"]
           <= you2.aspects["Scoring"].state["idealized_multitarget"] * 1.0005)


def test_inject_stashes_ability_caps() -> None:
    """`_inject_multi_target` stashes the caps beside the schedule so the
    displayed idealized lane (`_user_sim_context`) scores on the same capped
    basis — and the same LRU slots — as the credited headline."""
    print()
    print("Test: _inject_multi_target — stashes mt_ability_caps")
    import sidecar.main as M
    COMMUNIO = 24398
    casts = [(float(t), COMMUNIO, 2) for t in range(50, 550, 60)]
    you = _rpr_run(600.0, delivered=100_000, idealized=110_000, mt_casts=casts)
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0, COMMUNIO, 2), (200.0, COMMUNIO, 2)]) for _ in range(5)]
    M._inject_multi_target("Reaper", you, refs)
    st = you.aspects["Scoring"].state
    _check("caps stashed", st.get("mt_ability_caps") == ((COMMUNIO, 2),),
           f"{st.get('mt_ability_caps')}")


def test_run_summary_emits_credited_flag_present_only() -> None:
    """`_run_summary` emits `multiTargetCredited` only when the Scoring state
    has the key (multi-target pulls) — single-target responses stay
    byte-identical. The frontend uses the flag to know whether a run's pair is
    the credited (splash-inclusive) or the single-target one."""
    print()
    print("Test: _run_summary — multiTargetCredited present-only")
    import sidecar.main as M
    COMMUNIO = 24398
    casts = [(float(t), COMMUNIO, 2) for t in range(50, 550, 60)]
    you = _rpr_run(600.0, delivered=100_000, idealized=110_000, mt_casts=casts)
    refs = [_rpr_run(600.0, 100_000, 110_000,
                     [(80.0, COMMUNIO, 2), (200.0, COMMUNIO, 2)]) for _ in range(5)]
    plain = _rpr_run(600.0, 100_000, 110_000, [])
    out_plain = M._run_summary(plain)
    _check("absent without the state key (single-target byte-identical)",
           "multiTargetCredited" not in out_plain, f"{out_plain.keys()}")
    M._inject_multi_target("Reaper", you, refs)
    out_you = M._run_summary(you)
    _check("present + True on the credited run",
           out_you.get("multiTargetCredited") is True,
           f"{out_you.get('multiTargetCredited')}")
    out_ref = M._run_summary(refs[0])
    _check("present on each ref too",
           "multiTargetCredited" in out_ref, f"{out_ref.keys()}")


def test_build_target_schedule_floors_at_observed() -> None:
    """The schedule is the window's modal target_count, FLOORED at the max hits
    any run actually landed inside it — so a player who cleaved more than the
    modal count can't push delivered past the ceiling (the <=100% seam)."""
    print()
    print("Test: _build_target_schedule — floors N at max observed hits")
    import sidecar.main as M
    from jobs._core.downtime_sources import MultiTargetWindow

    class _Run:
        def __init__(self, casts):
            self.observed_multi_target_casts = tuple(casts)

    windows = [MultiTargetWindow(start_s=0.0, end_s=100.0, target_count=2)]
    # One ref hit 4 targets inside the window; the modal/consensus count was 2.
    runs = [_Run([(10.0, 999, 2)]), _Run([(20.0, 999, 4)])]
    sched = M._build_target_schedule(windows, runs)
    _check("one interval", len(sched) == 1, f"got {sched}")
    s, e, n = sched[0]
    _check("spans the window", s == 0.0 and e == 100.0, f"({s},{e})")
    _check("floored at 4 (max observed), not 2", n == 4, f"got {n}")


def test_inject_credits_aoe_only_job_sam() -> None:
    """SAM has NO splash_potencies (its AoE is all dedicated buttons), so the old
    `not splash` gate skipped it. With aoe_potencies + the AoE-aware sim it now
    credits: the ceiling rises (AoE rotation at N=3) and delivered <= ceiling."""
    print()
    print("Test: _inject_multi_target — credits an AoE-only job (Samurai)")
    import sidecar.main as M
    from jobs import AspectResult, ModuleResult, Track, get_job
    from jobs.samurai import data as sd
    get_job("Samurai")
    dur = 120.0

    def _sam_run(delivered, idealized, casts):
        mr = ModuleResult(
            label="t", fight_duration_s=dur, downtime_windows=[],
            multi_target_windows=((0.0, dur, 3),),
            observed_multi_target_casts=tuple(casts))
        mr.aspects["Scoring"] = AspectResult(
            name="Scoring", track=Track(name="Scoring", events=[]),
            state={"delivered_potency": float(delivered),
                   "idealized_strict": float(idealized),
                   "downtime_windows": [], "fight_duration_s": dur})
        return mr

    casts = [(float(t), sd.TENKA_GOKEN, 3) for t in range(20, 110, 20)]
    you = _sam_run(20_000, 60_000, casts)
    refs = [_sam_run(20_000, 60_000,
                     [(30.0, sd.TENKA_GOKEN, 3), (70.0, sd.TENKA_GOKEN, 3)])
            for _ in range(5)]
    M._inject_multi_target("Samurai", you, refs)
    st = you.aspects["Scoring"].state
    _check("credited (aoe-only job)", st.get("multi_target_credited") is True,
           f"{st.get('multi_target_credited')}")
    _check("ceiling rose via the AoE sim",
           st["idealized_multitarget"] > 60_000, f"{st['idealized_multitarget']}")
    _check("delivered rose", st["delivered_multitarget"] > 20_000,
           f"{st['delivered_multitarget']}")
    _check("delivered <= ceiling (>100% guard)",
           st["delivered_multitarget"] <= st["idealized_multitarget"] * 1.0005)


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_single_boss_no_windows()
    test_two_bosses_whole_fight()
    test_add_phase_in_the_middle()
    test_pure_handoff_no_window()
    test_brief_overlap_filtered()
    test_three_enemies_peak_count()
    test_non_boss_no_events_ignored()
    test_open_ended_add_no_phantom_overlap()
    test_repeated_spawn_add_window_localized()
    test_disclaim_threshold()
    test_fetch_end_to_end()
    test_fetch_client_failure_empty()
    test_packetid_grouping()
    test_packetid_fallback_by_time()
    test_observed_empty_on_failure()
    test_consensus_min_ref_count_gate()
    test_consensus_confirms_window()
    test_consensus_below_threshold_rejects()
    test_consensus_modal_n_capped_at_peak()
    test_consensus_only_casts_in_window_count()
    test_inject_credits_and_holds_invariant()
    test_inject_guard_blocks_over_100()
    test_inject_noop_without_consensus()
    test_per_window_deltas_sum_to_totals()
    test_per_ref_window_deltas_serialized()
    test_observed_reach_caps_construction()
    test_capped_splash_ceiling_binds()
    test_inject_stashes_ability_caps()
    test_run_summary_emits_credited_flag_present_only()
    test_build_target_schedule_floors_at_observed()
    test_inject_credits_aoe_only_job_sam()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
