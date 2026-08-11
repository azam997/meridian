"""Black Mage scoring + simulator invariants (network-free).

Mirrors test_samurai_sim.py / test_redmage_sim.py for the second caster:

  * Pipeline doesn't crash — `analyze_pull('Black Mage', ...)` runs every aspect
    via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * MP phase economy: every Flare Star consumes exactly six Fire IV (the soul gate),
    the Astral Fire / Umbral Ice phases alternate (one Blizzard III + Blizzard IV +
    Fire III entry per cycle), and Despair appears (the MP dump).
  * Polyglot is spent (Xenoglossy ≈ accrued stacks); High Thunder DoT over-refresh
    is overcap-safe; Ley Lines hastes the GCD; the in-sim tincture is placed;
    downtime lowers the ceiling.

Run from python/:  python tests/test_blackmage_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.blackmage import data as bd
from jobs.blackmage import scoring as sc
from jobs.blackmage.simulator import (
    BlackMageRotationModel,
    SimParams,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic BLM cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class MockClient:
    """Serves a synthetic single-BLM pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no buff events)."""

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
            "title": "BLM fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Black Mage", "server": "T",
                     "type": "Player", "subType": "BlackMage", "petOwner": None,
                     "gameID": 25},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Black Mage", client, "AbCd1234", 1,
                        ranking_name=None, label="blm-fixture")


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
    assert 150 <= pps <= 280, f"p/sec out of band: {pps:.1f}"
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
    s_d = sc.score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    s_o = sc.score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    s_p = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert s_o >= s_d - 1e-6, f"optimal {s_o} < default {s_d}"
    assert s_p >= s_o - 1e-6, f"perfect {s_p} < optimal {s_o}"


def test_idealized_beats_degraded_delivered():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]  # drop half the casts
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = sc.score_delivered_potency(degraded)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_astral_soul_gate():
    """Every Flare Star spends exactly six Fire IV worth of Astral Soul, so the
    in-fight Fire IV count is 6×Flare Star + a small leftover (0-5 souls built but
    not yet dumped at the fight-end boundary). This is the MP/soul gate working —
    a fire phase never starts a Fire IV it can't turn into a Flare Star."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for t, a in timeline if t >= 0)
    assert c[bd.FLARE_STAR] >= 1
    leftover = c[bd.FIRE_IV] - bd.ASTRAL_SOUL_CAP * c[bd.FLARE_STAR]
    assert 0 <= leftover <= 5, \
        f"FireIV={c[bd.FIRE_IV]} FlareStar={c[bd.FLARE_STAR]} leftover={leftover}"


def test_phase_alternation():
    """Astral Fire <-> Umbral Ice alternation: one ice entry (Blizzard III) +
    Blizzard IV + fire entry (in-fight Fire III) per cycle. Manafont extends a fire
    phase WITHOUT adding an ice phase, so the entry counts match each other."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for t, a in timeline if t >= 0)
    assert c[bd.BLIZZARD_III] == c[bd.BLIZZARD_IV], c
    assert c[bd.BLIZZARD_III] == c[bd.FIRE_III], c   # in-fight Fire III = ice exits
    assert c[bd.DESPAIR] >= 1                         # MP dump present


def test_polyglot_spent():
    """Polyglot accrues 1/30s of Enochian (+1 per Amplifier) and is spent on
    Xenoglossy; the ceiling spends ~all of it (no large overcap waste)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for t, a in timeline if t >= 0)
    base_accrued = int(_DURATION_S / bd.POLYGLOT_INTERVAL_S)
    assert c[bd.XENOGLOSSY] >= base_accrued - 2, \
        f"Xenoglossy={c[bd.XENOGLOSSY]} accrued~{base_accrued}"


def test_high_thunder_dot_overrefresh_safe():
    """Over-refreshing High Thunder credits by time-to-next-cast (capped at 30s),
    so two casts 5s apart credit far less than two 30s apart — never doubled."""
    tight = sc._high_thunder_dot_potency(
        [(0.0, bd.HIGH_THUNDER), (5.0, bd.HIGH_THUNDER)], None)
    spaced = sc._high_thunder_dot_potency(
        [(0.0, bd.HIGH_THUNDER), (30.0, bd.HIGH_THUNDER)], None)
    assert tight < spaced
    one = sc._high_thunder_dot_potency([(0.0, bd.HIGH_THUNDER)], None)
    max_one = (bd.HIGH_THUNDER_DOT_DURATION_S / bd.DOT_TICK_S
               * bd.HIGH_THUNDER_DOT_TICK_P)
    assert one <= max_one + 1e-6


def test_ley_lines_hastes_gcd():
    """A GCD slot inside the Ley Lines window runs ~15% faster than outside."""
    model = BlackMageRotationModel()
    state = model.init_state()
    state.astral_fire = 3
    state.t = 0.0
    state.ley_end = 0.0
    base = model.gcd_duration(state, bd.FIRE_IV, SimParams())
    state.ley_end = 100.0
    hasted = model.gcd_duration(state, bd.FIRE_IV, SimParams())
    assert hasted < base
    assert abs(hasted - base * bd.LEY_LINES_HASTE) < 1e-6


def test_tincture_placed_in_ceiling():
    """The optimizer places the in-sim tincture pot marker on the perfect timeline."""
    timeline, _ = simulate_idealized_perfect(_DURATION_S, [])
    assert any(aid == TINCTURE_ACTION_ID for _t, aid in timeline)


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_mp_never_negative():
    """Step the model through a full run; MP stays within [0, cap] after every
    cast (the fire-phase gate + ice regen keep the economy valid)."""
    from jobs._core.sim import engine
    model = BlackMageRotationModel()
    state = model.init_state()
    state.fight_duration_s = _DURATION_S
    state.downtime_windows = []
    model.prepull(state, SimParams())
    iters = 0
    while state.t < _DURATION_S and iters < 6000:
        iters += 1
        gcd_id, dur = model.gcd_slot(state, SimParams())
        engine._commit_gcd(model, state, SimParams(), gcd_id, dur)
        assert 0 <= state.mp <= bd.MP_CAP, f"MP out of range: {state.mp}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all blackmage sim tests passed")


if __name__ == "__main__":
    main()
