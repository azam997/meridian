"""Gunbreaker simulator validation against REAL pulls (no network at test time).

The companion to test_gunbreaker_sim.py (internal invariants). This one validates the
sim against actual human play — real quartile-stratified GNB pulls captured from FFLogs
top rankings (one per quartile per encounter), under tests/fixtures/gnb/. Validating
against the sim's own output would be circular; these are real cast streams, so the
tests confirm the sim never under-rates a real top parse (the exact-100% gate,
de-guarded via `ceiling_witness_gap`; the Fuseir Warblade fixture is the documented
live survivor — see NEXT_STEPS.md — kept here as permanent corpus coverage).

GNB's No Mercy self-buff rides the player's own casts, so the captured cast stream is
sufficient — no per-pull scalar. The fixtures carry no DamageDone / Buffs stream, which
is fine: No Mercy / the DoTs are derived from the cast timeline, not from buff events.
Caveat: with no Buffs stream the delivered side is scored UNPOTTED while the ceiling
pots, so fixture efficiency reads ~1-2% lower than a live run — this gate is therefore
slightly SOFTER than the live 0/60 sweep, which remains the authority on the >100%
guard. (Shared pattern across every job's fixture suite.)

Regenerate / add encounters with:
    python scripts/add_gunbreaker_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Run from python/:  python tests/test_gunbreaker_pulls.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.gunbreaker import scoring as sc
from jobs.gunbreaker.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

GNB_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gnb"
_BUCKETS = ("topq", "q2", "q3", "botq")
# Per-job efficiency tolerance. The ceiling is a true upper bound, so NO real pull may
# exceed 100% (delivered <= idealized by construction). GNB's ceiling runs at the
# gear-true 2.50 GCD; this is a genuine correctness guard, not a fudge factor.
_EFFICIENCY_TOL = 1.0 + 1e-9   # exact 100% gate (owner directive, v1.1): fix or document every over


class MockClient:
    """Serves a real captured GNB pull (casts + targetability + comp). Returns [] for
    DamageDone / Buffs (No Mercy + DoTs are derived from the cast timeline). No network."""

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
                    "subType": "Gunbreaker", "petOwner": None, "gameID": 37,
                }, *other, *npc_actors],
            },
        }


def _fixture_names() -> list[str]:
    return [p.stem for p in sorted(GNB_FIXTURES_DIR.glob("*.json"))
            if p.stem != "synthetic"]


_FIXTURE_NAMES = _fixture_names()


@functools.lru_cache(maxsize=None)
def _analyze(name: str):
    fix = json.loads((GNB_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    client = MockClient(fix)
    mr = analyze_pull("Gunbreaker", client, fix["report_code"], fix["fight_id"],
                      ranking_name=fix.get("label"), label=fix.get("label", "fixture"))
    return mr, fix


def _bucket(name: str) -> str | None:
    for b in _BUCKETS:
        if b in name.split("_"):
            return b
    return None


_GNB_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no GNB pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_pull_invariants(name: str) -> None:
    """Per real pull: pipeline runs, every aspect present, delivered potency in a sane
    p/sec band, and idealized >= delivered within the per-job tolerance."""
    mr, fix = _analyze(name)
    for aname in _GNB_ASPECTS:
        assert aname in mr.aspects, f"{name}: missing {aname}"
    st = mr.aspects["Scoring"].state
    delivered = st.get("delivered_potency", 0.0)
    assert delivered > 0, f"{name}: delivered={delivered}"
    pps = delivered / fix["duration_s"]
    assert 150 <= pps <= 700, f"{name}: p/sec {pps:.1f} out of band"
    # De-guarded: production floors idealized_strict at delivered (the witness
    # guard), which would blind this gate - the RAW search ceiling is the signal.
    ideal = st["idealized_potency"] - float(st.get("ceiling_witness_gap") or 0.0)
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= _EFFICIENCY_TOL, \
        f"{name}: efficiency {ratio:.1%} (delivered {delivered:.0f} ideal {ideal:.0f})"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no GNB pull fixtures")
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_sim_monotonicity(name: str) -> None:
    """perfect >= optimal >= default on every real fixture's duration."""
    dur = json.loads(
        (GNB_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["duration_s"]
    d = sc.score_delivered_potency(simulate_idealized(dur, [])[0])
    o = sc.score_delivered_potency(simulate_idealized_optimal(dur, [])[0])
    p = sc.score_delivered_potency(simulate_idealized_perfect(dur, [])[0])
    assert o >= d - 1e-6, f"{name}: optimal {o} < default {d}"
    assert p >= o - 1e-6, f"{name}: perfect {p} < optimal {o}"


_FUSEIR = "m12sp1_fuseir_1"


@pytest.mark.skipif(_FUSEIR not in _FIXTURE_NAMES, reason="no Fuseir fixture")
def test_fuseir_ceiling_dominates_replay() -> None:
    """The structural pin on the replay-seeded ceiling leg (fails on the pre-leg
    code): the production strict ceiling, pot-stripped, must dominate the
    player's own line replayed pot-free through the model. This was the v1.1
    survivor — a PROVEN pure search gap (player raw 120,324 vs beam raw
    119,416; NEXT_STEPS.md holds the decomposition) that the witness guard
    papered over. The raw comparison is the real gate here: under the fixture's
    unpotted delivered lens the witness guard rarely fires, so asserting
    `ceiling_witness_gap` absent alone would be near-vacuous."""
    from jobs._core.sim.replay import replay_state
    from jobs._core.tincture import TINCTURE_ACTION_ID
    from jobs.gunbreaker import data as gd
    from jobs.gunbreaker import simulator as gnb_sim

    mr, fix = _analyze(_FUSEIR)
    st = mr.aspects["Scoring"].state
    ctx = st["sim_context"]
    assert getattr(ctx, "demonstrated", None), \
        "the stashed arg-max context lost the demonstrated stream"
    dur = float(fix["duration_s"])
    dt = list(st["downtime_windows"])

    def _raw(tl, aux, score):
        return score([(t, a) for t, a in tl if a != TINCTURE_ACTION_ID],
                     aux, None)

    ceil_tl, ceil_aux = gnb_sim.simulate_idealized_perfect(
        dur, dt, None, sim_context=ctx)
    model = gnb_sim._model_for(dur, ctx)
    score = gnb_sim._make_score(model.mt_schedule)
    rst = replay_state(model, list(mr.norm_casts), dur, dur, dt,
                       gcd_ids=gnb_sim._GCD_IDS, params=gnb_sim.SimParams(),
                       skip_ids=gd.DEFENSIVE_IDS)
    ceiling_raw = _raw(ceil_tl, ceil_aux, score)
    replay_raw = _raw(rst.timeline, model.final_aux(rst), score)
    assert ceiling_raw + 1e-6 >= replay_raw, \
        f"search gap reopened: ceiling raw {ceiling_raw:.0f} < " \
        f"player replay raw {replay_raw:.0f} (Δ {replay_raw - ceiling_raw:.0f}p)"
    # Belt-and-braces: with the leg live the guard should have nothing to hide.
    assert not st.get("ceiling_witness_gap"), \
        f"witness gap {st.get('ceiling_witness_gap')} — the ceiling under-fits"


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="no GNB pull fixtures")
def test_quartile_efficiency_clustered() -> None:
    """Efficiency clusters across the rDPS-ranked quartiles and all stay in a sane band.
    The metric is crit/gear-NEUTRAL (it scores potency execution, not rDPS), so the
    FFLogs rDPS ranking does NOT predict it — the elite top-100 execute near-identically.
    A real ceiling bug would leave one quartile far off the others; we assert
    tightly-clustered + all-calibrated rather than a (false) topq > botq. See the
    efficiency-is-crit-neutral-potency memory."""
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
    assert max(means.values()) - min(means.values()) <= 0.06, \
        f"quartile efficiency spread too wide (not gear-neutral?): {means}"


def main() -> int:
    if not _FIXTURE_NAMES:
        print("no GNB pull fixtures — run scripts/add_gunbreaker_fixtures.py")
        return 0
    for name in _FIXTURE_NAMES:
        test_pull_invariants(name)
        test_sim_monotonicity(name)
        mr, fix = _analyze(name)
        st = mr.aspects["Scoring"].state
        eff = st["delivered_potency"] / st["idealized_potency"]
        print(f"  [OK  ] {name:24s} eff={eff:.1%} "
              f"pps={st['delivered_potency']/fix['duration_s']:.0f}")
    if _FUSEIR in _FIXTURE_NAMES:
        test_fuseir_ceiling_dominates_replay()
        print(f"  [OK  ] {_FUSEIR}: ceiling dominates the player replay (raw)")
    test_quartile_efficiency_clustered()
    print(f"\nAll GNB real-pull tests passed ({len(_FIXTURE_NAMES)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
