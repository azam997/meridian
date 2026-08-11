"""Mitigation-planner classification tests (pure, synthetic multi-log data).

Covers: forced-vs-avoidable voting, kill-time eligibility, kind detection
(raidwide / tankbuster / bleed / multiHit / spread), auto-attack exclusion +
tank drain, cross-log clustering (jitter + same-log re-split guard), school
coercion, and the per-person-total damage semantics.

Run from python/:  python tests/test_mitplan_classify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitplan.classify import (  # noqa: E402
    Hit, LogData, classify, school_for,
)

# 8-player party: ids 1,2 tanks; 3,4 healers; 5-8 dps.
ROLES = {1: "tank", 2: "tank", 3: "healer", 4: "healer",
         5: "dps", 6: "dps", 7: "dps", 8: "dps"}

NAMES = {100: "Big Raidwide", 200: "Sharp Buster", 300: "Creeping Bleed",
         400: "Triple Slam", 500: "attack", 600: "Role Spread"}
TYPES: dict[int, object] = {100: "1024", 200: 128, 300: "64", 400: 1024,
                            500: 128, 600: 1024}


def hit(t, target, aid, unmit, *, tick=False, pid=None, mult=0.9):
    return Hit(t=t, target=target, role=ROLES[target], ability_id=aid,
               unmit=unmit, amount=unmit * mult, absorbed=0.0,
               multiplier=mult, tick=tick, pid=pid)


def raidwide(t, aid=100, unmit=150_000, pid=None):
    return [hit(t, i, aid, unmit if ROLES[i] == "dps" else unmit * 0.7,
                pid=pid) for i in range(1, 9)]


def log(hits, kill_s=300.0, code="L", fight_id=1):
    return LogData(code=code, fight_id=fight_id, kill_s=kill_s,
                   party_size=8, hits=hits)


def run(logs):
    return classify(logs, NAMES, TYPES)


def test_forced_voting_and_noise_floor():
    # In 8/10 logs → forced; in 5/10 → avoidable; sub-1k damage → avoidable.
    logs = []
    for i in range(10):
        hits = []
        if i < 8:
            hits += raidwide(30.0 + (i % 3))          # presence 0.8 ≥ 0.7
        if i < 5:
            hits += raidwide(60.0, aid=400)           # presence 0.5 < 0.7
        hits += raidwide(90.0, aid=600, unmit=500)    # below the noise floor
        logs.append(log(hits, code=f"L{i}"))
    mechs, avoidable, _, _ = run(logs)
    assert [m.boss_ability_ids[0] for m in mechs] == [100]
    assert mechs[0].kind == "raidwide"
    assert avoidable == 2
    assert 0.75 <= mechs[0].presence_ratio <= 0.85


def test_kill_time_eligibility():
    # A late mechanic only reached by 7 of 10 kills: the 3 short kills must
    # not vote against it (presence = 7/7 among eligible).
    logs = []
    for i in range(10):
        short = i >= 7
        hits = raidwide(20.0)
        if not short:
            hits += raidwide(250.0, aid=400)
        logs.append(log(hits, kill_s=100.0 if short else 300.0, code=f"L{i}"))
    mechs, avoidable, _, _ = run(logs)
    ids = {m.boss_ability_ids[0] for m in mechs}
    assert ids == {100, 400}
    assert avoidable == 0
    late = next(m for m in mechs if m.boss_ability_ids[0] == 400)
    assert late.presence_ratio == 1.0


def test_tankbuster_and_double_hit_totals():
    # A 2-burst buster on one tank: per-PERSON total = both hits summed.
    logs = []
    for i in range(6):
        hits = [hit(50.0, 1, 200, 200_000, pid=10),
                hit(51.5, 1, 200, 200_000, pid=11)]
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    assert len(mechs) == 1
    m = mechs[0]
    assert m.kind == "tankbuster"
    assert m.school == "physical"
    assert m.tank_targets == 1
    assert abs(m.unmitigated["tank"] - 400_000) < 1.0
    assert m.unmitigated["dps"] == 0.0
    assert len(m.hits) == 2
    assert abs(sum(h["unmitigated"]["tank"] for h in m.hits) - 400_000) < 1.0


def test_bleed_tick_chain():
    # 5 party-wide ticks 3s apart chain into ONE bleed; totals are per person.
    logs = []
    for i in range(6):
        hits = []
        for k in range(5):
            hits += [hit(80.0 + 3.0 * k, t, 300,
                         20_000 if ROLES[t] == "dps" else 14_000, tick=True)
                     for t in range(1, 9)]
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    assert len(mechs) == 1
    m = mechs[0]
    assert m.kind == "bleed"
    assert abs(m.unmitigated["dps"] - 100_000) < 1.0       # 5 × 20k
    assert m.end_s - m.time_s >= 11.0                       # spans the chain


def test_multi_hit_train():
    logs = []
    for i in range(6):
        hits = (raidwide(100.0, aid=400, unmit=60_000, pid=1)
                + raidwide(101.2, aid=400, unmit=60_000, pid=2)
                + raidwide(102.4, aid=400, unmit=60_000, pid=3))
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    m = next(m for m in mechs if m.boss_ability_ids[0] == 400)
    assert m.kind == "multiHit"
    assert len(m.hits) == 3
    assert abs(m.unmitigated["dps"] - 180_000) < 1.0        # summed per person


def test_spread_per_person_semantics():
    # 4 bursts, each hitting ONE distinct dps for 120k: a hit person takes
    # 120k total, and the hit rows must SUM to that (not 4 × 120k).
    logs = []
    for i in range(6):
        hits = [hit(40.0 + 0.9 * k, 5 + k, 600, 120_000, pid=k + 1)
                for k in range(4)]
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    m = next(m for m in mechs if m.boss_ability_ids[0] == 600)
    assert abs(m.unmitigated["dps"] - 120_000) < 1.0
    assert abs(sum(h["unmitigated"]["dps"] for h in m.hits) - 120_000) < 1.0


def test_auto_attack_exclusion_and_drain():
    logs = []
    for i in range(6):
        hits = []
        for k in range(60):                    # 3s cadence tank autos
            hits.append(hit(3.0 * k, 1, 500, 20_000, mult=1.0))
        hits += raidwide(30.0)
        logs.append(log(hits, kill_s=180.0, code=f"L{i}"))
    mechs, _, drain, _ = run(logs)
    assert all(m.boss_ability_ids[0] != 500 for m in mechs)
    # 60 autos × 20k over 180s, halved per tank → ~3.3k HP/s.
    assert 2_000 < drain < 5_000


def test_cluster_jitter_and_ordinals():
    # Two occurrences (~30s / ~90s) with ±3s jitter → exactly two mechanics.
    logs = []
    for i in range(8):
        j = (i % 7) - 3
        hits = raidwide(30.0 + j) + raidwide(90.0 + j)
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    assert len(mechs) == 2
    assert [m.id for m in mechs] == ["100#0", "100#1"]
    assert mechs[0].time_s < 40 < 80 < mechs[1].time_s


def test_same_log_re_split_guard():
    # Occurrences 8s apart (below the split gap) would pool into one cluster —
    # the same-log guard must split them back into two mechanics.
    logs = []
    for i in range(6):
        hits = raidwide(30.0) + raidwide(38.0)
        logs.append(log(hits, code=f"L{i}"))
    mechs, _, _, _ = run(logs)
    assert len(mechs) == 2
    assert abs(mechs[0].time_s - 30.0) < 1.0
    assert abs(mechs[1].time_s - 38.0) < 1.0


def test_hpset_detection():
    # 8/10 logs show the party healed from ~0 HP at ~180s; an enemy begincast
    # at 175.5 names it. One hpSet mechanic, zero damage, correct name/time.
    names = dict(NAMES)
    names[700] = "Charybdistopia"
    logs = []
    for i in range(10):
        logs.append(LogData(
            code=f"L{i}", fight_id=1, kill_s=300.0, party_size=8,
            hits=raidwide(30.0), enemy_casts=[(175.5, 700)],
            hp1_windows=[180.0 + (i % 3)] if i < 8 else []))
    mechs, _, _, _ = classify(logs, names, TYPES)
    hp = [m for m in mechs if m.kind == "hpSet"]
    assert len(hp) == 1
    assert hp[0].name == "Charybdistopia"
    assert 178.0 <= hp[0].time_s <= 183.0
    assert hp[0].unmitigated == {"tank": 0.0, "healer": 0.0, "dps": 0.0}
    assert hp[0].observed_mit_pct == 0.0
    assert any("1 HP" in n for n in hp[0].notes)


def test_hpset_presence_gating():
    # Windows in only 3/10 logs (someone nearly died) never become a mechanic.
    logs = [LogData(code=f"L{i}", fight_id=1, kill_s=300.0, party_size=8,
                    hits=raidwide(30.0), enemy_casts=[],
                    hp1_windows=[180.0] if i < 3 else [])
            for i in range(10)]
    mechs, _, _, _ = classify(logs, NAMES, TYPES)
    assert not any(m.kind == "hpSet" for m in mechs)


def test_school_coercion():
    assert school_for([100], {100: "1024"}) == "magical"
    assert school_for([100], {100: 128}) == "physical"
    assert school_for([100], {100: "32"}) == "special"
    assert school_for([100], {100: None}) == "unknown"
    assert school_for([100], {}) == "unknown"
    assert school_for([100, 200], {100: "1024", 200: "128"}) == "mixed"
    assert school_for([100], {100: "bogus"}) == "unknown"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("test_mitplan_classify: all passed")


if __name__ == "__main__":
    main()
