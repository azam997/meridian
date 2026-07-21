"""Samurai simulator validation against REAL pulls (no network at test time).

The companion to test_samurai_sim.py (internal invariants). This one validates
the sim against actual human play — real quartile-stratified SAM pulls captured
from FFLogs top rankings (one per quartile per encounter), under
tests/fixtures/sam/. Validating against the sim's own output would be circular;
these are real cast streams, so the tests confirm the sim correlates with skill
and never under-rates a real top parse.

Two measured per-pull inputs ride in the fixture / MockClient:
  * **Tengentsu Kenki** — the fixture stores `tengentsu_kenki`; the MockClient
    replays it as Foresight applybuff events so the ceiling spends the same bonus
    Kenki the player got (the symmetric sim_context). Without this the 0-bonus
    ceiling would sit below the player's real Tengentsu-funded Shinten.
  * **Fugetsu coverage** — the captured fixtures carry no DamageDone stream, so
    `measured_fugetsu_intervals` falls back to FULL coverage; the x1.13 then
    cancels in delivered/idealized (accurate for top pulls, which hold it ~100%).

Regenerate / add encounters with:
    python scripts/add_samurai_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Run from python/:  python tests/test_samurai_pulls.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.samurai import data as sd
from jobs.samurai import scoring as sc
from jobs.samurai.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

SAM_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sam"
_BUCKETS = ("topq", "q2", "q3", "botq")
# Per-job efficiency tolerance. SAM has more moving parts than RPR (two maintained
# self-buffs, a guaranteed-crit Iaijutsu mix, Tengentsu Kenki) so a real top parse
# can edge slightly over the greedy ceiling; a small headroom avoids overfitting.
_EFFICIENCY_TOL = 1.02


class MockClient:
    """Serves a real captured SAM pull (casts + targetability + comp). Replays the
    fixture's measured Tengentsu Kenki as Foresight applybuff events; returns []
    for DamageDone so Fugetsu falls back to full coverage. No network."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]
        n = int(fixture.get("tengentsu_kenki", 0)) // sd.TENGENTSU_KENKI_PER_PROC
        self._foresight = [
            {"timestamp": fixture["fight_start_ms"], "type": "applybuff",
             "abilityGameID": sd.TENGENTSU_FORESIGHT_STATUS_ID}
            for _ in range(n)
        ]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end):
        evs = self._fixture.get("targetability_events") or []
        return [e for e in evs if start <= e.get("timestamp", 0) <= end]

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return list(self._foresight) if data_type == "Buffs" else []

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
                    "subType": "Samurai", "petOwner": None, "gameID": 34,
                }, *other, *npc_actors],
            },
        }


def _fixture_names() -> list[str]:
    return [p.stem for p in sorted(SAM_FIXTURES_DIR.glob("*.json"))
            if p.stem != "synthetic"]


_FIXTURE_NAMES = _fixture_names()


@functools.lru_cache(maxsize=None)
def _analyze(name: str):
    fix = json.loads((SAM_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    client = MockClient(fix)
    mr = analyze_pull("Samurai", client, fix["report_code"], fix["fight_id"],
                      ranking_name=fix.get("label"), label=fix.get("label", "fixture"))
    return mr, fix


def _bucket(name: str) -> str | None:
    for b in _BUCKETS:
        if b in name.split("_"):
            return b
    return None


_SAM_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SAM pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_pull_invariants(name: str) -> None:
    """Per real pull: pipeline runs, every aspect present, delivered potency in a
    sane p/sec band, and idealized >= delivered within the per-job tolerance."""
    mr, fix = _analyze(name)
    for aname in _SAM_ASPECTS:
        assert aname in mr.aspects, f"{name}: missing {aname}"
    st = mr.aspects["Scoring"].state
    delivered = st.get("delivered_potency", 0.0)
    assert delivered > 0, f"{name}: delivered={delivered}"
    pps = delivered / fix["duration_s"]
    assert 150 <= pps <= 700, f"{name}: p/sec {pps:.1f} out of band"
    ideal = st["idealized_potency"]
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= _EFFICIENCY_TOL, \
        f"{name}: efficiency {ratio:.1%} (delivered {delivered:.0f} ideal {ideal:.0f})"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SAM pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_sim_monotonicity(name: str) -> None:
    """perfect >= optimal >= default on every real fixture's duration."""
    dur = json.loads(
        (SAM_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["duration_s"]
    d = sc.score_delivered_potency(simulate_idealized(dur, [])[0])
    o = sc.score_delivered_potency(simulate_idealized_optimal(dur, [])[0])
    p = sc.score_delivered_potency(simulate_idealized_perfect(dur, [])[0])
    assert o >= d - 1e-6, f"{name}: optimal {o} < default {d}"
    assert p >= o - 1e-6, f"{name}: perfect {p} < optimal {o}"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no SAM pull fixtures")
def test_quartile_spread() -> None:
    """Top-quartile real pulls average higher efficiency than bottom-quartile —
    validates the ceiling correlates with real skill, not just that it runs."""
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
    if "topq" in means and "botq" in means:
        assert means["topq"] > means["botq"], (
            f"topq {means['topq']:.1%} !> botq {means['botq']:.1%}")


def main() -> int:
    if not _FIXTURE_NAMES:
        print("no SAM pull fixtures — run scripts/add_samurai_fixtures.py")
        return 0
    for name in _FIXTURE_NAMES:
        test_pull_invariants(name)
        test_sim_monotonicity(name)
        mr, fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        eff = st["delivered_potency"] / st["idealized_potency"]
        print(f"  [OK  ] {name:24s} eff={eff:.1%} "
              f"pps={st['delivered_potency']/fix['duration_s']:.0f}")
    test_quartile_spread()
    print(f"\nAll SAM real-pull tests passed ({len(_FIXTURE_NAMES)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
