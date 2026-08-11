"""Scholar simulator validation against REAL pulls (no network at test time).

The companion to test_scholar_sim.py (internal invariants). This one validates the
sim against actual human play — real quartile-stratified SCH pulls captured from
FFLogs top rankings, under tests/fixtures/sch/. Validating against the sim's own
output would be circular; these are real cast streams, so the tests confirm the sim
never under-rates a real top parse (efficiency <= 100%).

Healer note: a real SCH spends GCDs healing (Adloquium / Succor / Physick …) and
diverts some Aetherflow to oGCD heals (Indomitability / Excogitation), which is
genuine damage-efficiency loss vs the *unlocked* ceiling — so even top healers sit
well below 100% here (the mit-plan lock pays that tax in the product flow).
Efficiency is gear-neutral, so the metric is NOT rDPS-ordered across quartiles; the
real signal is TIGHT clustering, not topq > botq (the VPR finding).

Regenerate / add encounters with:
    python scripts/add_scholar_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Run from python/:  python tests/test_scholar_pulls.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.scholar import scoring as sc
from jobs.scholar.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

SCH_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sch"
_BUCKETS = ("topq", "q2", "q3", "botq")
# Per-job efficiency tolerance vs the strict ceiling (sub-GCD noise margin).
_EFFICIENCY_TOL = 1.005


class MockClient:
    """Serves a real captured SCH pull (casts + targetability + comp). No buff
    stream in the fixtures -> no observed raid buffs / tincture windows (the strict
    scenario is the assertion target anyway)."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end):
        evs = self._fixture.get("targetability_events") or []
        return [e for e in evs if start <= e.get("timestamp", 0) <= end]

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return []

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        npc_actors = f.get("master_npc_actors") or []
        enemy_npcs = f.get("enemy_npcs") or []
        fa = f.get("friendly_actors") or []
        other = [{
            "id": a["id"], "name": a.get("name"), "server": "TestServer",
            "type": "Player", "subType": a.get("subType"),
            "petOwner": None, "gameID": 0,
        } for a in fa if a["id"] != f["source_id"]]
        friendly_ids = [f["source_id"]] + [a["id"] for a in other]
        return {
            "title": f.get("label", "Fixture"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"], "name": "Fight",
                "encounterID": f.get("encounter_id", 103),
                "difficulty": f.get("difficulty", 101), "kill": True,
                "startTime": f["fight_start_ms"], "endTime": f["fight_end_ms"],
                "friendlyPlayers": friendly_ids, "enemyNPCs": enemy_npcs,
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"], "name": f.get("label", "Player"),
                    "server": "TestServer", "type": "Player",
                    "subType": "Scholar", "petOwner": None, "gameID": 28,
                }, *other, *npc_actors],
            },
        }


def _fixture_names() -> list[str]:
    return [p.stem for p in sorted(SCH_FIXTURES_DIR.glob("*.json"))
            if p.stem != "synthetic"]


_FIXTURE_NAMES = _fixture_names()


@functools.lru_cache(maxsize=None)
def _analyze(name: str):
    fix = json.loads((SCH_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    client = MockClient(fix)
    mr = analyze_pull("Scholar", client, fix["report_code"], fix["fight_id"],
                      ranking_name=fix.get("label"), label=fix.get("label", "fixture"))
    return mr, fix


def _bucket(name: str) -> str | None:
    for b in _BUCKETS:
        if b in name.split("_"):
            return b
    return None


_SCH_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SCH pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_pull_invariants(name: str) -> None:
    """Per real pull: pipeline runs, every aspect present, delivered potency in a
    sane p/sec band, and idealized >= delivered within the tolerance."""
    mr, fix = _analyze(name)
    for aname in _SCH_ASPECTS:
        assert aname in mr.aspects, f"{name}: missing {aname}"
    st = mr.aspects["Scoring"].state
    delivered = st.get("delivered_potency", 0.0)
    assert delivered > 0, f"{name}: delivered={delivered}"
    pps = delivered / fix["duration_s"]
    assert 70 <= pps <= 320, f"{name}: p/sec {pps:.1f} out of band"
    ideal = st["idealized_potency"]
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= _EFFICIENCY_TOL, \
        f"{name}: efficiency {ratio:.1%} (delivered {delivered:.0f} ideal {ideal:.0f})"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SCH pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_sim_monotonicity(name: str) -> None:
    """perfect >= optimal >= default on every real fixture's duration."""
    dur = json.loads(
        (SCH_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["duration_s"]
    d = sc.score_delivered_potency(simulate_idealized(dur, [])[0])
    o = sc.score_delivered_potency(simulate_idealized_optimal(dur, [])[0])
    p = sc.score_delivered_potency(simulate_idealized_perfect(dur, [])[0])
    assert o >= d - 1e-6, f"{name}: optimal {o} < default {d}"
    assert p >= o - 1e-6, f"{name}: perfect {p} < optimal {o}"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SCH pull fixtures")
def test_quartile_clustering() -> None:
    """Efficiency is crit/gear-neutral, so the metric is NOT rDPS-ordered — the real
    signal is TIGHT clustering across quartiles (a wide spread is the bug signal).
    Assert every real pull is a valid parse (<= tol) and the quartile means sit
    within a modest band."""
    effs: list[float] = []
    for name in _FIXTURE_NAMES:
        mr, _fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        if st["idealized_potency"] > 0:
            effs.append(st["delivered_potency"] / st["idealized_potency"])
    assert effs, "no efficiencies computed"
    assert all(e <= _EFFICIENCY_TOL for e in effs), \
        f"a real pull exceeded the ceiling: max {max(effs):.1%}"
    # Clustering: the whole sample spans less than ~25pp (healer heal-tax varies by
    # fight, but a single encounter's real pulls should cluster tightly).
    print(f"  {len(effs)} pulls: min {min(effs):.1%} max {max(effs):.1%}")
    assert max(effs) - min(effs) < 0.25, \
        f"efficiency spread too wide: {min(effs):.1%}..{max(effs):.1%}"


def main() -> int:
    if not _FIXTURE_NAMES:
        print("no SCH pull fixtures — run scripts/add_scholar_fixtures.py")
        return 0
    for name in _FIXTURE_NAMES:
        test_pull_invariants(name)
        test_sim_monotonicity(name)
        mr, fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        eff = st["delivered_potency"] / st["idealized_potency"]
        print(f"  [OK  ] {name:24s} eff={eff:.1%} "
              f"pps={st['delivered_potency']/fix['duration_s']:.0f}")
    test_quartile_clustering()
    print(f"\nAll SCH real-pull tests passed ({len(_FIXTURE_NAMES)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
