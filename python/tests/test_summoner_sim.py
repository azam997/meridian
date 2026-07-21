"""Summoner scoring + simulator invariants (network-free).

Mirrors test_pictomancer_sim.py for the seventeenth job (the first pet-cycle
caster):

  * Pipeline doesn't crash — `analyze_pull('Summoner', ...)` runs every aspect
    via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock
    budget.
  * The pet folds are pure table potency (the summon carries its 4 autos, the
    Enkindle its payoff) — no state-derived scoring families.
  * The rotation structure holds: the demi cycle is Solar -> Bahamut -> Solar
    -> Phoenix on a ~60s cadence, every window carries its impulses + exactly
    one Enkindle + one flare (Phoenix none), rites run 2/4/4 per primal,
    Mountain Buster == Topaz Rite, the aetherflow economy closes, Searing
    Flash == Searing Light, and no rites fire inside demi windows.
  * Per-ability recasts shape `gcd_duration` (Emerald 1.5 / Topaz 2.5 / Ruby
    3.0 / Slipstream 3.5); the 1.5s Emerald slot single-weaves.
  * The demi downtime hold fires (never at the fight tail — the fold symmetry
    rule); downtime lowers the ceiling.

Run from python/:  python tests/test_summoner_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.summoner import data as sdta
from jobs.summoner import scoring as sc
from jobs.summoner.scoring import score_delivered_potency
from jobs.summoner.simulator import (
    SimParams,
    SummonerRotationModel,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic SMN cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-SMN pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no aura events
    -> no observed tincture/raid windows; no pet actors -> nothing to fetch —
    the pet folds ride the player cast ids)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._casts if start <= e["timestamp"] <= end]

    def get_targetability_events(self, code, start, end):
        return []

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return []

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "SMN fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Fight", "encounterID": 103, "difficulty": 101,
                "kill": True, "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "Test Summoner", "server": "T",
                     "type": "Player", "subType": "Summoner", "petOwner": None,
                     "gameID": 42},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Summoner", client, "AbCd1234", 1,
                        ranking_name=None, label="smn-fixture")


# --- Pipeline invariants ---------------------------------------------------

_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment",
            "BuffDrift", "Scoring"]


def test_pipeline_runs_and_has_aspects():
    mr = _run_pipeline()
    for name in _ASPECTS:
        assert name in mr.aspects, f"missing {name}"


def test_delivered_in_band_and_below_ceiling():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    delivered = st["delivered_potency"]
    ideal = st["idealized_strict"]
    assert delivered > 0
    pps = delivered / _DURATION_S
    assert 220 <= pps <= 360, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


# --- Simulator invariants --------------------------------------------------

def test_sim_monotonicity():
    s_d = score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    s_o = score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    s_p = score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert s_o >= s_d - 1e-6, f"optimal {s_o} < default {s_d}"
    assert s_p >= s_o - 1e-6, f"perfect {s_p} < optimal {s_o}"


def test_idealized_beats_degraded_delivered():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]  # drop half the casts
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = score_delivered_potency(degraded)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 30.0


def test_pet_folds_are_pure_table_potency():
    """The summon carries its folded autos, the Enkindle its payoff — plain
    table lookups, symmetric with the sim's incremental score. The pet's own
    damage ids score zero (they never appear in a cast stream)."""
    for aid in (sdta.SUMMON_SOLAR_BAHAMUT, sdta.SUMMON_BAHAMUT,
                sdta.SUMMON_PHOENIX, sdta.ENKINDLE_SOLAR_BAHAMUT,
                sdta.ENKINDLE_BAHAMUT, sdta.SUMMON_IFRIT_II):
        got = score_delivered_potency([(0.0, aid)])
        assert abs(got - sdta.POTENCIES[aid]) < 1e-6, (aid, got)
    for pid in sdta.PET_IDS:
        assert score_delivered_potency([(0.0, pid)]) == 0.0, pid


def _window_segments(timeline):
    """(summon_id, start, casts-inside-window) per demi summon."""
    demis = [(t, a) for t, a in timeline if a in sdta.DEMI_SUMMON_IDS]
    out = []
    for t, a in demis:
        # A weave in the summon's own slot logs at exactly t — include it.
        inside = [(tt, aa) for tt, aa in timeline
                  if t <= tt <= t + sdta.DEMI_WINDOW_S and aa > 0
                  and aa not in sdta.DEMI_SUMMON_IDS]
        out.append((a, t, inside))
    return out


def test_rotation_structure():
    """Demi cadence + gem/attunement/favor/aetherflow economies on a long
    greedy run."""
    dur = 634.0
    timeline, _ = simulate_idealized(dur, [])
    casts = [(t, a) for t, a in timeline if a > 0]
    c = Counter(a for _, a in casts)

    # The demi cycle order is fixed: Solar -> Bahamut -> Solar -> Phoenix.
    demis = [(t, a) for t, a in casts if a in sdta.DEMI_SUMMON_IDS]
    for i, (_t, aid) in enumerate(demis):
        assert aid == sdta.DEMI_CYCLE[i % 4], f"demi cycle broken at {i}"
    # ~1 demi per 60s, cadence-locked.
    gaps = [b - a for (a, _x), (b, _y) in zip(demis, demis[1:])]
    assert all(59.0 <= g <= 66.0 for g in gaps), gaps
    assert len(demis) >= int(dur // 60.5)

    # Every window: >= 5 impulses, exactly one Enkindle, one flare (Phoenix
    # none), no rites / primal summons inside.
    for aid, t, inside in _window_segments(casts):
        names = Counter(a for _t, a in inside)
        impulses = sum(v for k, v in names.items() if k in sdta.IMPULSE_IDS)
        if t <= dur - sdta.DEMI_WINDOW_S:
            assert impulses >= 5, (aid, t, impulses)
            assert names.get(sdta.DEMI_ENKINDLE[aid], 0) == 1, (aid, t)
            if aid in sdta.DEMI_FLARE:
                assert names.get(sdta.DEMI_FLARE[aid], 0) == 1, (aid, t)
        assert not any(k in sdta.RITE_IDS or k in sdta.PRIMAL_SUMMON_IDS
                       for k in names), (aid, t, names)

    # Impulses fire ONLY inside windows.
    window_spans = [(t, t + sdta.DEMI_WINDOW_S) for t, _a in demis]
    for t, a in casts:
        if a in sdta.IMPULSE_IDS:
            assert any(s < t <= e for s, e in window_spans), (t, a)

    # Gem phases: 2/4/4 rites per primal summon (tail phase may truncate).
    prims = [(t, a) for t, a in casts if a in sdta.PRIMAL_SUMMON_IDS]
    bounds = [t for t, _a in prims] + [dur + 1]
    for i, (t, aid) in enumerate(prims):
        rite, count = sdta.PRIMAL_RITES[aid]
        inside = sum(1 for tt, aa in casts if t < tt < bounds[i + 1] and aa == rite)
        if bounds[i + 1] <= dur:
            assert inside == count, (aid, t, inside, count)
    # Roughly 3 primals per demi cycle.
    assert len(prims) >= 3 * (len(demis) - 1)

    # Favors: Mountain Buster == Topaz Rite; Cyclone -> Strike pairs; one
    # Slipstream per Garuda (tail may hold one of each).
    assert abs(c[sdta.MOUNTAIN_BUSTER] - c[sdta.TOPAZ_RITE]) <= 1
    assert abs(c[sdta.CRIMSON_CYCLONE] - c[sdta.SUMMON_IFRIT_II]) <= 1
    assert abs(c[sdta.CRIMSON_STRIKE] - c[sdta.CRIMSON_CYCLONE]) <= 1
    assert abs(c[sdta.SLIPSTREAM] - c[sdta.SUMMON_GARUDA_II]) <= 1

    # Aetherflow closes: 2 Necrotize per Energy Drain; Ruin IV <= Energy Drain.
    assert c[sdta.NECROTIZE] <= 2 * c[sdta.ENERGY_DRAIN]
    assert c[sdta.NECROTIZE] >= 2 * (c[sdta.ENERGY_DRAIN] - 1)
    assert c[sdta.RUIN_IV] <= c[sdta.ENERGY_DRAIN]

    # Searing Flash pairs 1:1 with Searing Light (tail may hold one).
    assert abs(c[sdta.SEARING_FLASH] - c[sdta.SEARING_LIGHT]) <= 1


def test_recast_mults_shape_gcd_duration():
    """Per-ability recasts: Emerald 1.5 / Topaz 2.5 / Ruby max(2.8, 3.0) = 3.0 /
    Slipstream max(3.0, 3.5) = 3.5 / Ruin III max(1.5, 2.5) = 2.5; the 1.5s
    Emerald slot single-weaves while an instant impulse double-weaves."""
    model = SummonerRotationModel()
    st = model.init_state()
    params = SimParams()
    st.t = 10.0
    assert abs(model.gcd_duration(st, sdta.EMERALD_RITE, params) - 1.5) < 1e-9
    assert abs(model.gcd_duration(st, sdta.TOPAZ_RITE, params) - 2.5) < 1e-9
    assert abs(model.gcd_duration(st, sdta.RUBY_RITE, params) - 3.0) < 1e-9
    assert abs(model.gcd_duration(st, sdta.SLIPSTREAM, params) - 3.5) < 1e-9
    assert abs(model.gcd_duration(st, sdta.RUIN_III, params) - 2.5) < 1e-9
    assert abs(model.gcd_duration(st, sdta.UMBRAL_IMPULSE, params) - 2.5) < 1e-9

    model.gcd_duration(st, sdta.EMERALD_RITE, params)
    assert model.weave_budget(st, sdta.EMERALD_RITE, params) == 1
    model.gcd_duration(st, sdta.UMBRAL_IMPULSE, params)
    assert model.weave_budget(st, sdta.UMBRAL_IMPULSE, params) == 2
    model.gcd_duration(st, sdta.RUBY_RITE, params)
    assert model.weave_budget(st, sdta.RUBY_RITE, params) == 1  # hardcast


def test_demi_downtime_hold_and_tail_escape():
    """A ready demi is held when a gap would eat the trance — but never at the
    fight tail (the fold symmetry rule: a player's full-credit tail summon must
    be matchable)."""
    model = SummonerRotationModel()
    st = model.init_state()
    st.fight_duration_s = 300.0
    st.t = 100.0
    st.downtime_windows = [(108.0, 130.0)]
    assert model._demi_burns_into_downtime(st)
    picked = model.pick_gcd(st, SimParams())
    assert picked not in sdta.DEMI_SUMMON_IDS, picked
    # Same geometry at the fight tail -> fire anyway.
    st2 = model.init_state()
    st2.fight_duration_s = 300.0
    st2.t = 285.0
    st2.downtime_windows = [(293.0, 299.0)]
    assert not model._demi_burns_into_downtime(st2)
    assert model.pick_gcd(st2, SimParams()) in sdta.DEMI_SUMMON_IDS


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all summoner sim tests passed")


if __name__ == "__main__":
    main()
