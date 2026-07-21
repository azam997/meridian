"""Bard simulator validation against REAL pulls (no network at test time).

The companion to test_bard_sim.py (internal invariants). This one validates the
sim against actual human play — real quartile-stratified BRD pulls captured from
FFLogs top rankings (one per quartile per encounter), under tests/fixtures/brd/.
Validating against the sim's own output would be circular; these are real cast
streams, so the tests confirm the sim correlates with skill and never under-rates
a real top parse.

BRD's per-pull sim_context (the RNG-resource budgets: Refulgent / Pitch Perfect /
Apex / Blast / Heartbreak counts) is measured from the fixture's own cast stream
by the Scoring aspect — the ceiling spends the same counts the player got.

Regenerate / add encounters with:
    python scripts/add_bard_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Run from python/:  python tests/test_bard_pulls.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.bard.scoring import score_delivered_potency
from jobs.bard.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

BRD_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "brd"
_BUCKETS = ("topq", "q2", "q3", "botq")
# Per-job efficiency tolerance. The ceiling is a true upper bound, so NO real pull
# may exceed 100% (delivered <= idealized by construction) — a genuine correctness
# guard, not a fudge factor.
_EFFICIENCY_TOL = 1.0


class MockClient:
    """Serves a real captured BRD pull (casts + targetability + comp). Returns []
    for Buffs / DamageDone (no observed tincture/raid windows — the strict lens
    under test is buff-agnostic anyway). No network."""

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
                "encounterID": 103, "difficulty": 101, "kill": True,
                "startTime": f["fight_start_ms"], "endTime": f["fight_end_ms"],
                "friendlyPlayers": friendly_ids, "enemyNPCs": enemy_npcs,
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"], "name": f.get("label", "Player"),
                    "server": "TestServer", "type": "Player",
                    "subType": "Bard", "petOwner": None, "gameID": 23,
                }, *other, *npc_actors],
            },
        }


def _fixture_names() -> list[str]:
    if not BRD_FIXTURES_DIR.exists():
        return []
    return [p.stem for p in sorted(BRD_FIXTURES_DIR.glob("*.json"))
            if p.stem != "synthetic"]


_FIXTURE_NAMES = _fixture_names()


@functools.lru_cache(maxsize=None)
def _analyze(name: str):
    fix = json.loads((BRD_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    client = MockClient(fix)
    mr = analyze_pull("Bard", client, fix["report_code"], fix["fight_id"],
                      ranking_name=fix.get("label"), label=fix.get("label", "fixture"))
    return mr, fix


def _bucket(name: str) -> str | None:
    for b in _BUCKETS:
        if b in name.split("_"):
            return b
    return None


_BRD_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no BRD pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_pull_invariants(name: str) -> None:
    """Per real pull: pipeline runs, every aspect present, delivered potency in a
    sane p/sec band, and idealized >= delivered within the per-job tolerance."""
    mr, fix = _analyze(name)
    for aname in _BRD_ASPECTS:
        assert aname in mr.aspects, f"{name}: missing {aname}"
    st = mr.aspects["Scoring"].state
    delivered = st.get("delivered_potency", 0.0)
    assert delivered > 0, f"{name}: delivered={delivered}"
    pps = delivered / fix["duration_s"]
    assert 120 <= pps <= 400, f"{name}: p/sec {pps:.1f} out of band"
    ideal = st["idealized_potency"]
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= _EFFICIENCY_TOL, \
        f"{name}: efficiency {ratio:.1%} (delivered {delivered:.0f} ideal {ideal:.0f})"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no BRD pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_sim_monotonicity(name: str) -> None:
    """perfect >= optimal >= default on every real fixture's duration."""
    dur = json.loads(
        (BRD_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["duration_s"]
    d = score_delivered_potency(simulate_idealized(dur, [])[0])
    o = score_delivered_potency(simulate_idealized_optimal(dur, [])[0])
    p = score_delivered_potency(simulate_idealized_perfect(dur, [])[0])
    assert o >= d - 1e-6, f"{name}: optimal {o} < default {d}"
    assert p >= o - 1e-6, f"{name}: perfect {p} < optimal {o}"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no BRD pull fixtures")
def test_quartile_efficiency_clustered() -> None:
    """Efficiency clusters tightly across the rDPS-ranked quartiles — and all stay
    in a sane band. The metric is crit/gear-NEUTRAL (it scores potency execution,
    not rDPS), so the FFLogs rDPS ranking does NOT predict it: the top and bottom
    quartiles of the elite top-100 execute near-identically. We assert the
    quartile means are TIGHTLY CLUSTERED + all calibrated — a real ceiling bug
    would leave one quartile far off the others — rather than a (false)
    topq > botq (the VPR finding)."""
    by_q: dict[str, list[float]] = {b: [] for b in _BUCKETS}
    for name in _FIXTURE_NAMES:
        b = _bucket(name)
        if b is None:
            continue
        mr, _fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        if st["idealized_potency"] > 0:
            by_q[b].append(st["delivered_potency"] / st["idealized_potency"])
    means = {q: sum(v) / len(v) for q, v in by_q.items() if v}
    for q in _BUCKETS:
        if q in means:
            print(f"  {q}: {len(by_q[q])} samples, mean efficiency {means[q]:.1%}")
    assert means, "no fixtures analyzed"
    assert all(0.85 <= m <= _EFFICIENCY_TOL for m in means.values()), \
        f"a quartile mean out of the calibrated band: {means}"
    assert max(means.values()) - min(means.values()) <= 0.05, \
        f"quartile efficiency spread too wide (not gear-neutral?): {means}"


def main() -> int:
    if not _FIXTURE_NAMES:
        print("no BRD pull fixtures — run scripts/add_bard_fixtures.py")
        return 0
    for name in _FIXTURE_NAMES:
        test_pull_invariants(name)
        test_sim_monotonicity(name)
        mr, fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        eff = st["delivered_potency"] / st["idealized_potency"]
        print(f"  [OK  ] {name:24s} eff={eff:.1%} "
              f"pps={st['delivered_potency']/fix['duration_s']:.0f}")
    test_quartile_efficiency_clustered()
    print(f"\nAll BRD real-pull tests passed ({len(_FIXTURE_NAMES)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
