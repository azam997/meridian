"""Advisory cleave-geometry verdicts (jobs._core.cleave_geometry +
sidecar.main._annotate_cleave_geometry).

Pins the verdict semantics (reachable / unreachable / unknown), the
evidence-gating (any failure => no verdict key, byte-identical behavior), and
the guarantee that geometry never touches the credit math. Runs under pytest
and standalone (its main() runs the same checks).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.cleave_geometry import (  # noqa: E402
    DEFAULT_SPLASH_RADIUS_YALM, HITBOX_ALLOWANCE_YALM, MARGIN_YALM,
    job_reach_yalm, sample_enemy_positions, window_verdict)


def _check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    assert ok, f"{name}: {detail}"


def _track(tid_pts: dict[int, list[tuple[float, float, float]]]):
    return {tid: sorted(pts) for tid, pts in tid_pts.items()}


REACH = 9.0   # 5y default + 3y hitbox + 1y margin


def test_stacked_enemies_reachable() -> None:
    print()
    print("Test: window_verdict — stacked enemies => reachable")
    pos = _track({
        1: [(float(t), 100.0, 100.0) for t in range(0, 30, 2)],
        2: [(float(t), 103.0, 102.0) for t in range(0, 30, 2)],   # ~3.6y away
    })
    verdict, detail = window_verdict(0.0, 30.0, pos, REACH)
    _check("reachable", verdict == "reachable", f"{verdict}: {detail}")


def test_far_enemies_unreachable() -> None:
    print()
    print("Test: window_verdict — far enemies => unreachable")
    pos = _track({
        1: [(float(t), 100.0, 100.0) for t in range(0, 30, 2)],
        2: [(float(t), 100.0, 130.0) for t in range(0, 30, 2)],   # 30y away
    })
    verdict, detail = window_verdict(0.0, 30.0, pos, REACH)
    _check("unreachable", verdict == "unreachable", f"{verdict}: {detail}")
    _check("detail names the closest distance", "30.0y" in detail, detail)


def test_sparse_samples_unknown() -> None:
    print()
    print("Test: window_verdict — too few concurrent samples => unknown")
    pos = _track({
        1: [(1.0, 100.0, 100.0)],
        2: [(20.0, 100.0, 130.0)],   # never within PAIR_DT_S of enemy 1
    })
    verdict, _ = window_verdict(0.0, 30.0, pos, REACH)
    _check("unknown", verdict == "unknown", verdict)


def test_single_enemy_unknown() -> None:
    print()
    print("Test: window_verdict — positions for only one enemy => unknown")
    pos = _track({1: [(float(t), 100.0, 100.0) for t in range(0, 30, 2)]})
    verdict, _ = window_verdict(0.0, 30.0, pos, REACH)
    _check("unknown", verdict == "unknown", verdict)
    verdict, _ = window_verdict(0.0, 30.0, {}, REACH)
    _check("no enemies at all => unknown", verdict == "unknown", verdict)


def test_briefly_in_reach_unknown() -> None:
    print()
    print("Test: window_verdict — briefly within reach => unknown (not denied)")
    # 1 of 15 paired samples in reach: below REACHABLE_FRACTION but nonzero, so
    # neither confidently reachable nor safely deniable.
    a = [(float(t), 100.0, 100.0) for t in range(0, 30, 2)]
    b = [(float(t), 100.0, 130.0) for t in range(0, 28, 2)] + [(28.0, 100.0, 105.0)]
    verdict, _ = window_verdict(0.0, 30.0, _track({1: a, 2: b}), REACH)
    _check("unknown", verdict == "unknown", verdict)


def test_job_reach_defaults_and_overrides() -> None:
    print()
    print("Test: job_reach_yalm — 5y default kit vs MCH's line/cone overrides")
    from jobs import get_job
    base = DEFAULT_SPLASH_RADIUS_YALM + HITBOX_ALLOWANCE_YALM + MARGIN_YALM
    rpr = job_reach_yalm(get_job("Reaper").data)     # no overrides -> default
    _check("RPR reach = default", rpr == base, f"{rpr} vs {base}")
    mch = job_reach_yalm(get_job("Machinist").data)  # Chain Saw 25y line
    _check("MCH reach = 25y line + allowances",
           mch == 25.0 + HITBOX_ALLOWANCE_YALM + MARGIN_YALM, f"{mch}")


def test_sample_enemy_positions_stub_client() -> None:
    print()
    print("Test: sample_enemy_positions — parses bundle, converts units")
    fight = {"startTime": 1_000_000, "endTime": 1_100_000, "id": 1,
             "enemyNPCs": [{"id": 7}, {"id": 8}]}

    class _Stub:
        def get_event_bundle(self, code, streams):
            return [[
                {"type": "damage", "targetID": 7, "timestamp": 1_010_000,
                 "targetResources": {"x": 10000, "y": 9000}},
                {"type": "damage", "targetID": 8, "timestamp": 1_011_000,
                 "targetResources": {"x": 10500, "y": 9000}},
                {"type": "damage", "targetID": 9, "timestamp": 1_012_000,   # not an enemy
                 "targetResources": {"x": 0, "y": 0}},
                {"type": "damage", "targetID": 7, "timestamp": 1_013_000},  # no resources
            ]]

    wins = [{"startSec": 0.0, "endSec": 60.0}]
    track = sample_enemy_positions(_Stub(), "abc", fight, wins)
    _check("two enemies tracked", sorted(track) == [7, 8], f"{sorted(track)}")
    _check("centi-yalms converted", track[7] == [(10.0, 100.0, 90.0)],
           f"{track[7]}")
    _check("resource-less rows skipped", len(track[7]) == 1, f"{track[7]}")


def test_annotate_evidence_gated_and_credit_untouched() -> None:
    print()
    print("Test: _annotate_cleave_geometry — failure => no key; credit math untouched")
    import sidecar.main as M
    from jobs import AspectResult, ModuleResult, Track, get_job
    get_job("Reaper")

    def _you(state_extra: dict):
        mr = ModuleResult(label="t", fight_duration_s=100.0, downtime_windows=[])
        mr.aspects["Scoring"] = AspectResult(
            name="Scoring", track=Track(name="Scoring", events=[]),
            state={"delivered_potency": 1000.0, "idealized_strict": 2000.0,
                   **state_extra})
        return mr

    class _Raising:
        def get_report_summary(self, code):
            raise RuntimeError("network down")

    wins = [{"startSec": 0.0, "endSec": 50.0, "targetCount": 2,
             "deliveredSplash": 10.0, "ceilingSplash": 20.0}]
    you = _you({"multi_target_credited": True,
                "multi_target_windows": [dict(w) for w in wins],
                "delivered_multitarget": 1010.0,
                "idealized_multitarget": 2020.0})
    M._annotate_cleave_geometry(_Raising(), "Reaper", "abc", 1, you)
    st = you.aspects["Scoring"].state
    _check("no cleaveGeometry key on fetch failure",
           all("cleaveGeometry" not in w for w in st["multi_target_windows"]))
    _check("credit numbers untouched",
           st["delivered_multitarget"] == 1010.0
           and st["idealized_multitarget"] == 2020.0)

    # Uncredited / windowless runs never fetch (the raising client would throw
    # before the try/except swallowed it — but more to the point, no key).
    you2 = _you({})
    M._annotate_cleave_geometry(_Raising(), "Reaper", "abc", 1, you2)
    _check("no-op without multi-target state",
           "multi_target_windows" not in you2.aspects["Scoring"].state)


def test_annotate_happy_path_stub() -> None:
    print()
    print("Test: _annotate_cleave_geometry — verdicts attached via stub client")
    import sidecar.main as M
    from jobs import AspectResult, ModuleResult, Track, get_job
    get_job("Reaper")
    fight = {"id": 1, "startTime": 0, "endTime": 100_000,
             "enemyNPCs": [{"id": 7}, {"id": 8}]}

    class _Stub:
        def get_report_summary(self, code):
            return {"fights": [fight]}

        def get_event_bundle(self, code, streams):
            # Two enemies ~3y apart, sampled every 2s -> reachable.
            evs = []
            for t in range(0, 50, 2):
                evs.append({"type": "damage", "targetID": 7,
                            "timestamp": t * 1000,
                            "targetResources": {"x": 10000, "y": 10000}})
                evs.append({"type": "damage", "targetID": 8,
                            "timestamp": t * 1000 + 100,
                            "targetResources": {"x": 10300, "y": 10000}})
            return [evs]

    mr = ModuleResult(label="t", fight_duration_s=100.0, downtime_windows=[])
    mr.aspects["Scoring"] = AspectResult(
        name="Scoring", track=Track(name="Scoring", events=[]),
        state={"multi_target_credited": True,
               "multi_target_windows": [
                   {"startSec": 0.0, "endSec": 50.0, "targetCount": 2,
                    "deliveredSplash": 10.0, "ceilingSplash": 20.0}]})
    M._annotate_cleave_geometry(_Stub(), "Reaper", "abc", 1, mr)
    w = mr.aspects["Scoring"].state["multi_target_windows"][0]
    _check("verdict attached", w.get("cleaveGeometry", {}).get("verdict") == "reachable",
           f"{w.get('cleaveGeometry')}")
    _check("detail present", bool(w["cleaveGeometry"].get("detail")))


def test_annotate_own_cleave_short_circuit() -> None:
    print()
    print("Test: _annotate_cleave_geometry — own cleaves => reachable, no fetch")
    import sidecar.main as M
    from jobs import AspectResult, ModuleResult, Track, get_job
    get_job("Reaper")
    fight = {"id": 1, "startTime": 0, "endTime": 100_000,
             "enemyNPCs": [{"id": 7}, {"id": 8}]}

    class _Stub:
        def get_report_summary(self, code):
            return {"fights": [fight]}

        def get_event_bundle(self, code, streams):
            raise AssertionError("must not fetch positions — player cleaved here")

    mr = ModuleResult(label="t", fight_duration_s=100.0, downtime_windows=[],
                      observed_multi_target_casts=((10.0, 24398, 2),))
    mr.aspects["Scoring"] = AspectResult(
        name="Scoring", track=Track(name="Scoring", events=[]),
        state={"multi_target_credited": True,
               "multi_target_windows": [
                   {"startSec": 0.0, "endSec": 50.0, "targetCount": 2,
                    "deliveredSplash": 10.0, "ceilingSplash": 20.0}]})
    M._annotate_cleave_geometry(_Stub(), "Reaper", "abc", 1, mr)
    w = mr.aspects["Scoring"].state["multi_target_windows"][0]
    _check("reachable via own cleave",
           w.get("cleaveGeometry", {}).get("verdict") == "reachable",
           f"{w.get('cleaveGeometry')}")
    _check("detail says so", "you cleaved" in w["cleaveGeometry"]["detail"],
           w["cleaveGeometry"]["detail"])


def main() -> int:
    test_stacked_enemies_reachable()
    test_far_enemies_unreachable()
    test_sparse_samples_unknown()
    test_single_enemy_unknown()
    test_briefly_in_reach_unknown()
    test_job_reach_defaults_and_overrides()
    test_sample_enemy_positions_stub_client()
    test_annotate_evidence_gated_and_credit_untouched()
    test_annotate_happy_path_stub()
    test_annotate_own_cleave_short_circuit()
    print()
    print("=" * 60)
    print("cleave_geometry: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
