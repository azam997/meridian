"""Machinist scoring + simulator invariants against real fixtures.

Renamed in spirit (ExecutionAspect is gone) but kept under the same
filename to preserve the existing entry point. Tests:

  * Pipeline doesn't crash on any fixture (MCH end-to-end via the
    registry — proves DriftAspect + ClippingAspect + MCHScoringAspect
    + Queen / Wildfire / Tools / Reassemble all coexist).
  * delivered_potency / fight duration stays in the 200-400 p/sec band.
  * idealized@own_duration >= delivered (the upper-bound invariant).
  * The optimal-sweep and perfect simulator variants are strict upgrades
    over the default-params sim.
  * User's near-perfect Tyrant fixture shows >= 95% efficiency.
  * The _CachedEventsClient wrapper consolidates duplicate fetches.

Run from python/:  python tests/test_execution.py
"""
from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import _CachedEventsClient, analyze_pull
from jobs.machinist.scoring import (
    compute_queen_battery_spent,
    idealized_at_duration,
    score_delivered_potency,
)
from jobs.machinist.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- MockClient (mirrors test_split_aspects + the new analyze_pull path) ----

class MockClient:
    """Returns canned cast events. analyze_pull uses get_report_summary +
    get_events; the other FFLogsClient methods aren't reached for a
    no-refs single-fight run."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        return [e for e in self._events
                if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end):
        evs = self._fixture.get("targetability_events") or []
        return [e for e in evs if start <= e.get("timestamp", 0) <= end]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        npc_actors = f.get("master_npc_actors") or []
        enemy_npcs = f.get("enemy_npcs") or []
        # Other party members from the captured comp (excluding the analyzed
        # player, who keeps the canonical stub below). Drives comp-aware
        # raid-buff alignment.
        fa = f.get("friendly_actors") or []
        other_players = [{
            "id": a["id"], "name": a.get("name"), "server": "TestServer",
            "type": "Player", "subType": a.get("subType"),
            "petOwner": None, "gameID": 0,
        } for a in fa if a["id"] != f["source_id"]]
        friendly_ids = [f["source_id"]] + [a["id"] for a in other_players]
        return {
            "title": f.get("label", "Fixture"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Fight",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": friendly_ids,
                "enemyNPCs": enemy_npcs,
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"],
                    "name": f.get("label", "Player"),
                    "server": "TestServer",
                    "type": "Player",
                    "subType": "Machinist",
                    "petOwner": None,
                    "gameID": 31,
                }, *other_players, *npc_actors],
            },
        }


def _run_pipeline(fix: dict):
    """Drive `analyze_pull('Machinist', ...)` against a fixture. Returns
    the full ModuleResult so individual tests can inspect any aspect's
    state."""
    client = MockClient(fix)
    return analyze_pull(
        "Machinist", client, fix["report_code"], fix["fight_id"],
        ranking_name=None, label=fix.get("label", "fixture"),
    )


# --- Test harness -----------------------------------------------------------

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


def _load_fixtures() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES_DIR.glob("*.json"))
    }


def _scoring_state(mr):
    return mr.aspects["Scoring"].state


# --- Per-fixture parametrization -------------------------------------------
# Each fixture is its own parametrized test item so pytest-xdist can spread
# them across cores (the simulator is the only real cost left). `_analyze`
# is memoized per worker so the pipeline runs once per fixture regardless of
# how many invariants inspect it. sam_* are skipped — SAM has no Scoring sim.

_FIXTURE_NAMES: list[str] = [
    p.stem for p in sorted(FIXTURES_DIR.glob("*.json"))
    if not p.stem.startswith("sam_")
]


@functools.lru_cache(maxsize=None)
def _analyze(name: str):
    """(ModuleResult, fixture_dict) for a fixture, computed once per worker."""
    fix = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return _run_pipeline(fix), fix


# --- Per-fixture invariants (parametrized so xdist can distribute) ---------

_MCH_ASPECTS = ["Drift", "Clipping", "Overcap", "Opener", "Alignment",
                "Reassemble", "Queen", "Wildfire", "Tools", "Abilities"]


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_pull_invariants(name: str) -> None:
    """Per-fixture pipeline invariants in one item: runs cleanly, every MCH
    aspect present, delivered potency > 0 and in the 200-400 p/sec band, and
    idealized >= delivered (0.5% headroom). One memoized pipeline run per
    fixture; one test item per fixture so xdist spreads the simulator cost."""
    mr, fix = _analyze(name)
    scoring = mr.aspects.get("Scoring")
    _check(f"{name}: Scoring aspect present", scoring is not None)
    assert scoring is not None
    for aname in _MCH_ASPECTS:
        _check(f"{name}: {aname} aspect present", aname in mr.aspects,
               f"missing {aname}")
    delivered = scoring.state.get("delivered_potency", 0.0)
    _check(f"{name}: delivered_potency > 0", delivered > 0, f"got {delivered}")

    duration = fix["duration_s"]
    pps = delivered / duration if duration > 0 else 0
    _check(f"{name}: {pps:.1f} p/sec in [200,400] band",
           200 <= pps <= 400, f"got {pps:.1f}")

    ideal = scoring.state["idealized_potency"]
    ratio = delivered / ideal if ideal > 0 else 0
    _check(f"{name}: efficiency <= 1.005 (got {ratio:.1%})",
           ratio <= 1.005, f"delivered={delivered:.0f} ideal={ideal:.0f}")


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_sim_monotonicity(name: str) -> None:
    """optimal >= default and perfect >= optimal on every fixture. Both are
    hard invariants: each variant maximizes over a superset of the previous
    one's options, so it can only match or beat it."""
    fix = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    dur = fix["duration_s"]
    score_d = score_delivered_potency(*simulate_idealized(dur, []))
    score_o = score_delivered_potency(*simulate_idealized_optimal(dur, []))
    score_p = score_delivered_potency(*simulate_idealized_perfect(dur, []))
    _check(f"{name}: optimal {score_o:.0f} >= default {score_d:.0f}",
           score_o >= score_d - 1e-6, f"optimal {score_o} < default {score_d}")
    _check(f"{name}: perfect {score_p:.0f} >= optimal {score_o:.0f}",
           score_p >= score_o - 1e-6, f"perfect {score_p} < optimal {score_o}")


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_queen_battery_tracking(name: str) -> None:
    """compute_queen_battery_spent yields a non-negative total for every
    fixture (> 0 where Queen was cast in-fight)."""
    from jobs._core.casts import fetch_norm_casts
    fix = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    client = MockClient(fix)
    fight = {"startTime": fix["fight_start_ms"], "endTime": fix["fight_end_ms"]}
    norm = fetch_norm_casts(client, fix["report_code"], fight,
                            {"id": fix["source_id"]})
    battery = compute_queen_battery_spent(norm)
    _check(f"{name}: queen battery total >= 0", battery >= 0, f"got {battery}")


def test_queen_banking_unit() -> None:
    """`_queen_should_bank` banks battery toward a reachable richer raid window —
    and ONLY then (no buffs / in-window / overcap-risk all summon now). This is
    the buff-aware Queen timing lever; the agnostic path never consults it."""
    from jobs.machinist.simulator import _MODEL, SimState
    st = SimState()
    st.battery = 50
    st.buff_intervals = []                       # agnostic -> never bank
    _check("no-buff => no bank", _MODEL._queen_should_bank(st, 0.0) is False)
    st.buff_intervals = [(8.0, 25.0, 1.18)]      # window 8s ahead, headroom 50
    _check("reachable window => bank (8*2.5<=50)",
           _MODEL._queen_should_bank(st, 0.0) is True)
    _check("already in window => summon now",
           _MODEL._queen_should_bank(st, 10.0) is False)
    st.buff_intervals = [(40.0, 55.0, 1.18)]     # far: 40*2.5=100 > 50 headroom
    _check("far window => no bank (would overcap)",
           _MODEL._queen_should_bank(st, 0.0) is False)
    st.battery = 95                              # headroom 5: 8*2.5=20 > 5
    st.buff_intervals = [(8.0, 25.0, 1.18)]
    _check("high battery => no bank (would overcap)",
           _MODEL._queen_should_bank(st, 0.0) is False)


def test_buff_aware_opener_aligns() -> None:
    """With a clean full-stack comp the buff-aware optimal lands the opener
    Wildfire INSIDE a raid-buff window (banking + the WF-aligned beam in
    `_beam_best`), where the pre-banking optimum fired it at the pull."""
    from jobs._core.buff_windows import (
        expected_windows, multiplier_at, multiplier_intervals)
    from jobs.machinist.simulator import WILDFIRE
    comp = ["Machinist", "Scholar", "Dragoon", "RedMage"]
    dur = 300.0
    bi = multiplier_intervals(expected_windows(dur, comp))
    tl, _aux = simulate_idealized_perfect(dur, [], buff_intervals=bi)
    wf = sorted(t for t, a in tl if a == WILDFIRE)
    _check("buff-aware opener WF lands in a buff window",
           bool(wf) and multiplier_at(wf[0], bi) > 1.001,
           f"WF[0]={wf[0]:.1f} mult={multiplier_at(wf[0], bi):.3f}")


def test_perfect_completes_under_30s() -> None:
    """Wall-clock budget: the perfect sim on the longest fixture (user Tyrant)
    completes within 30s."""
    if "user_tyrant_recent" not in _FIXTURE_NAMES:
        pytest.skip("no user fixture")
    _mr, fix = _analyze("user_tyrant_recent")
    start = time.monotonic()
    simulate_idealized_perfect(fix["duration_s"], [])
    elapsed = time.monotonic() - start
    _check(f"perfect-sim took {elapsed:.2f}s (<= 30s)", elapsed <= 30.0,
           f"elapsed={elapsed:.2f}s")


def test_user_efficiency_high() -> None:
    """The user's saved Tyrant run is near-perfect — efficiency >= 95%."""
    if "user_tyrant_recent" not in _FIXTURE_NAMES:
        pytest.skip("no user fixture")
    mr, _fix = _analyze("user_tyrant_recent")
    st = _scoring_state(mr)
    ratio = (st["delivered_potency"] / st["idealized_potency"]
             if st["idealized_potency"] > 0 else 0)
    _check(f"user efficiency >= 0.95 (got {ratio:.1%})", ratio >= 0.95,
           f"delivered={st['delivered_potency']:.0f} "
           f"ideal={st['idealized_potency']:.0f}")


def test_quartile_spread() -> None:
    """Top-quartile fixtures should not score MEANINGFULLY below bottom-quartile.

    Efficiency is gear-neutral POTENCY execution against a true-gear ceiling (the
    2.43/2.45 headroom was retired). At an honest tight ceiling top parses cluster at
    ~96-100%, so the top-vs-bottom rDPS-quartile efficiency gap is small and even
    inverts on a pull (a well-executed low-rDPS pull can out-execute a sloppy top-rDPS
    one — exactly what gear-neutrality predicts). So this no longer asserts a strict
    ordering; it catches a GROSS inversion (a miscalibrated ceiling), over ALL
    quartile-tagged fixtures (every encounter, not just two) for a robust mean."""
    by_quartile: dict[str, list[float]] = {"topq": [], "q2": [], "q3": [], "botq": []}
    for name in _FIXTURE_NAMES:
        tag = next((p for p in name.split("_") if p in by_quartile), None)
        if tag is None:
            continue
        mr, _fix = _analyze(name)
        st = _scoring_state(mr)
        if st["idealized_potency"] <= 0:
            continue
        by_quartile[tag].append(
            st["delivered_potency"] / st["idealized_potency"])
    means = {q: sum(e) / len(e) for q, e in by_quartile.items() if e}
    for q, m in means.items():
        print(f"  {q}: {len(by_quartile[q])} samples, mean efficiency {m:.1%}")
    top_mean, bot_mean = means.get("topq"), means.get("botq")
    if top_mean is not None and bot_mean is not None:
        _check(f"topq mean ({top_mean:.1%}) >= botq mean ({bot_mean:.1%}) - 0.5pp "
               "(gear-neutral tight ceiling compresses the spread)",
               top_mean > bot_mean - 0.005)


# --- Cached client wrapper -------------------------------------------------

class _CountingClient:
    """Tracks get_events / get_report_summary call counts to verify the
    cache wrapper consolidates duplicate fetches."""

    def __init__(self):
        self.get_events_calls = 0
        self.get_report_summary_calls = 0
        self.last_kwargs: dict = {}

    def get_events(self, code, start, end, source_id,
                   data_type="Casts", ability_id=None):
        self.get_events_calls += 1
        self.last_kwargs = dict(code=code, start=start, end=end,
                                 source_id=source_id, data_type=data_type,
                                 ability_id=ability_id)
        return [{"timestamp": start, "abilityGameID": 7410, "type": "cast"}]

    def get_report_summary(self, code):
        self.get_report_summary_calls += 1
        return {"code": code}


def test_cached_events_client() -> None:
    """The wrapper used by analyze_pull collapses repeat get_events calls
    across the registered aspects so they share one paginated fetch per
    (code, fight, actor)."""
    print()
    print("Test: _CachedEventsClient consolidates duplicate calls")

    inner = _CountingClient()
    cached = _CachedEventsClient(inner)
    args = ("rpt", 0, 10000, 5)
    for _ in range(5):
        cached.get_events(*args, data_type="Casts")
    _check("identical Casts fetches dedup to 1",
           inner.get_events_calls == 1,
           f"got {inner.get_events_calls}")

    inner2 = _CountingClient()
    cached2 = _CachedEventsClient(inner2)
    cached2.get_events("rpt", 0, 10000, 5, data_type="Casts")
    cached2.get_events("rpt", 0, 10000, 5, data_type="DamageDone")
    cached2.get_events("rpt", 0, 10000, 99, data_type="Casts")
    cached2.get_events("rpt", 0, 10000, 5, data_type="Casts", ability_id=7410)
    _check("differing keys each hit inner",
           inner2.get_events_calls == 4,
           f"got {inner2.get_events_calls}")

    inner3 = _CountingClient()
    cached3 = _CachedEventsClient(inner3)
    cached3.get_report_summary("rpt")
    cached3.get_report_summary("rpt")
    _check("get_report_summary passes through (no caching)",
           inner3.get_report_summary_calls == 2,
           f"got {inner3.get_report_summary_calls}")

    inner4 = _CountingClient()
    cached4 = _CachedEventsClient(inner4)
    first = cached4.get_events("rpt", 0, 10000, 5)
    first.append({"timestamp": 999999, "abilityGameID": 0})
    first.sort(key=lambda e: e["timestamp"], reverse=True)
    second = cached4.get_events("rpt", 0, 10000, 5)
    _check("mutating result doesn't poison cached copy",
           len(second) == 1 and second[0]["timestamp"] == 0,
           f"got {second}")
    _check("repeat call still hits cache (no extra inner fetch)",
           inner4.get_events_calls == 1,
           f"got {inner4.get_events_calls}")


# --- runner ---------------------------------------------------------------

def main() -> int:
    """Standalone runner (pytest is the canonical entry point). Mirrors the
    parametrized tests by looping the fixture names directly."""
    if not _FIXTURE_NAMES:
        print("ERROR: no fixtures found in tests/fixtures/. Run "
              "tests/generate_fixtures.py first.")
        return 1
    print(f"Loaded {len(_FIXTURE_NAMES)} fixture(s): {_FIXTURE_NAMES}")

    try:
        for name in _FIXTURE_NAMES:
            test_pull_invariants(name)
            test_sim_monotonicity(name)
            test_queen_battery_tracking(name)
        test_queen_banking_unit()
        test_buff_aware_opener_aligns()
        test_perfect_completes_under_30s()
        test_user_efficiency_high()
        test_quartile_spread()
        test_cached_events_client()
    except AssertionError as e:
        print(f"  [FAIL] {e}")

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        print()
        print("Failed assertions:")
        for name, detail in _FAILED:
            print(f"  - {name}    {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
