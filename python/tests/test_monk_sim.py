"""Monk scoring + simulator invariants (network-free).

Mirrors test_ninja_sim.py / test_gunbreaker_sim.py for the fourteenth job
simulator:

  * Pipeline doesn't crash — `analyze_pull('Monk', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * MNK rotation balance: the form-cycle generator/spender alternation (DK:LO
    1:1, TS:RR ~1:1, Demolish:PC ~1:2), the Perfect Balance charge economy and
    the blitz mix (EB/RP/PR, never Celestial Revolution; every Phantom Rush
    paid for by a prior Lunar + Solar), the reply pairings (Fire's Reply per
    RoF, Wind's Reply per RoW), and the budgeted The Forbidden Chakra spend.
  * Scoring math: the Riddle of Fire x1.15 window, the Fury bonuses
    (+200/+200/+150), the Leaping Opo guaranteed crit x1.62 (opo-eligibility
    gated), and the Perfect Balance form-free eligibility — each pinned exactly.
  * Downtime lowers the ceiling; the edge priming (Forbidden Meditation pump +
    Form Shift re-arm) emits only zero-potency casts inside the window.

Run from python/:  python tests/test_monk_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.monk import data as md
from jobs.monk import scoring as sc
from jobs.monk.simulator import (
    MonkCtx,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic MNK cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-MNK pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime)."""

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
            "title": "MNK fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Monk", "server": "T",
                     "type": "Player", "subType": "Monk", "petOwner": None,
                     "gameID": 20},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Monk", client, "AbCd1234", 1,
                        ranking_name=None, label="mnk-fixture")


def _counts(duration_s: float = _DURATION_S) -> Counter:
    return Counter(a for _t, a in simulate_idealized(duration_s, [])[0])


# --- Pipeline invariants ---------------------------------------------------

_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment",
            "BuffDrift", "Scoring", "Positionals"]


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
    assert 260 <= pps <= 480, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_tfc_budget_measured():
    """The synthetic pull's chakra budget = its own The Forbidden Chakra count,
    threaded to the ceiling via sim_context (the DNC budget pattern)."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    tl, _ = simulate_idealized(_DURATION_S, [])
    n_tfc = sum(1 for t, a in tl if t >= 0 and a == md.THE_FORBIDDEN_CHAKRA)
    assert st.get("tfcBudget") == n_tfc, (st.get("tfcBudget"), n_tfc)


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
    assert time.monotonic() - start <= 25.0


# --- Rotation-balance invariants --------------------------------------------

def test_form_cycle_alternation():
    """The Fury-greedy form cycle: Dragon Kick and Leaping Opo alternate 1:1,
    Twin Snakes and Rising Raptor ~1:1, and Demolish's 2-stack grant feeds ~2
    Pouncing Coeurls per Demolish."""
    c = _counts()
    assert abs(c[md.DRAGON_KICK] - c[md.LEAPING_OPO]) <= 2, \
        f"DK {c[md.DRAGON_KICK]} vs LO {c[md.LEAPING_OPO]}"
    assert abs(c[md.TWIN_SNAKES] - c[md.RISING_RAPTOR]) <= 2, \
        f"TS {c[md.TWIN_SNAKES]} vs RR {c[md.RISING_RAPTOR]}"
    assert c[md.DEMOLISH] >= 2 and c[md.POUNCING_COEURL] >= 2
    assert c[md.POUNCING_COEURL] <= 2 * c[md.DEMOLISH] + 2, \
        f"PC {c[md.POUNCING_COEURL]} outruns Coeurl's Fury from {c[md.DEMOLISH]}"


def test_blitz_economy():
    """Every Perfect Balance yields exactly one blitz; the sim never produces the
    2+1 Celestial Revolution; each Phantom Rush is paid for by a prior Lunar
    (Elixir Burst) + Solar (Rising Phoenix); the PB count fits the 2-charge/40s
    budget."""
    c = _counts()
    pb = c[md.PERFECT_BALANCE]
    blitzes = (c[md.ELIXIR_BURST] + c[md.RISING_PHOENIX]
               + c[md.PHANTOM_RUSH] + c[md.CELESTIAL_REVOLUTION])
    assert pb >= 2
    assert pb - 1 <= blitzes <= pb, f"PB {pb} vs blitzes {blitzes}"
    assert c[md.CELESTIAL_REVOLUTION] == 0, "sim produced the 2+1 mistake blitz"
    assert c[md.PHANTOM_RUSH] <= c[md.ELIXIR_BURST], "PR without a Lunar"
    assert c[md.PHANTOM_RUSH] <= c[md.RISING_PHOENIX], "PR without a Solar"
    assert c[md.PHANTOM_RUSH] >= 1, "no Phantom Rush in 6 minutes"
    budget = 2 + _DURATION_S / md.COOLDOWNS[md.PERFECT_BALANCE][0]
    assert pb <= budget + 1e-6, f"PB {pb} exceeds charge budget {budget:.1f}"


def test_burst_cadence():
    """Riddle of Fire / Brotherhood / Riddle of Wind track their cooldowns, and
    each arms its reply (Fire's per RoF, Wind's per RoW)."""
    c = _counts()
    assert c[md.RIDDLE_OF_FIRE] >= int(_DURATION_S / 60.0), c[md.RIDDLE_OF_FIRE]
    assert c[md.BROTHERHOOD] >= int(_DURATION_S / 120.0)
    assert c[md.RIDDLE_OF_WIND] >= int(_DURATION_S / 90.0)
    assert c[md.RIDDLE_OF_FIRE] - 1 <= c[md.FIRES_REPLY] <= c[md.RIDDLE_OF_FIRE]
    assert c[md.RIDDLE_OF_WIND] - 1 <= c[md.WINDS_REPLY] <= c[md.RIDDLE_OF_WIND]


def test_tfc_budget_spent():
    """The default chakra budget (~1 TFC / 12s) is fully spent — never exceeded,
    never left banked."""
    c = _counts()
    budget = int(_DURATION_S / 12.0)
    assert c[md.THE_FORBIDDEN_CHAKRA] == budget, \
        f"TFC {c[md.THE_FORBIDDEN_CHAKRA]} vs budget {budget}"


def test_tfc_budget_context_respected():
    """A per-pull MonkCtx budget bounds the ceiling's TFC count exactly."""
    tl, _ = simulate_idealized(_DURATION_S, [], sim_context=MonkCtx(tfc_budget=7))
    n = sum(1 for _t, a in tl if a == md.THE_FORBIDDEN_CHAKRA)
    assert n == 7, n


def test_six_sided_star_is_the_last_gcd():
    """The fight-end squeeze: Six-Sided Star (780) closes the fight."""
    tl, _ = simulate_idealized(_DURATION_S, [])
    gcds = [a for _t, a in tl if a in md.POTENCIES and a not in md.OGCD_IDS
            and md.POTENCIES.get(a, 0) >= 0]
    assert gcds[-1] == md.SIX_SIDED_STAR, gcds[-6:]


# --- Scoring math (pinned exactly) ------------------------------------------

def test_riddle_of_fire_window_multiplier():
    """A cast inside the 20.7s RoF window is priced x1.15; outside, raw. RoF's
    own (zero-potency) cast is never amped by the window it opens."""
    rof, dk = md.RIDDLE_OF_FIRE, md.DRAGON_KICK
    # t>=40 so the initial pre-pull Formless assumption has expired (30s) and DK
    # scores bare (no opo-eligibility interplay).
    inside = sc.score_delivered_potency([(40.0, rof), (45.0, dk)])
    outside = sc.score_delivered_potency([(40.0, rof), (62.0, dk)])
    p = md.POTENCIES[dk]
    assert abs(inside - p * md.RIDDLE_OF_FIRE_MULT) < 1e-6, inside
    assert abs(outside - p) < 1e-6, outside


def test_leaping_opo_guaranteed_crit_and_fury():
    """Leaping Opo: x1.62 only when opo-eligible (the initial Formless / opo
    form / PB), +200 only under Opo-opo's Fury — and the whole Fury-boosted hit
    crits."""
    lo, dk, fs = md.LEAPING_OPO, md.DRAGON_KICK, md.FORM_SHIFT
    crit = md.GUARANTEED_CRIT_MULT
    # Opo-eligible via the initial Formless: no fury -> 260 x 1.62.
    got = sc.score_delivered_potency([(0.0, lo)])
    assert abs(got - md.POTENCIES[lo] * crit) < 1e-6, got
    # DK (formless, grants fury) -> Form Shift re-arms -> LO: (260+200) x 1.62.
    got = sc.score_delivered_potency([(0.0, dk), (2.0, fs), (4.0, lo)])
    want = md.POTENCIES[dk] + (md.POTENCIES[lo] + md.OPO_FURY_BONUS_P) * crit
    assert abs(got - want) < 1e-6, (got, want)
    # Fury banked while eligible (t=0, Formless) then spent after every
    # form/Formless window has lapsed: the +200 still pays, but no crit.
    got = sc.score_delivered_potency([(0.0, dk), (40.0, lo)])
    want = md.POTENCIES[dk] + md.POTENCIES[lo] + md.OPO_FURY_BONUS_P
    assert abs(got - want) < 1e-6, (got, want)
    # A form-less Dragon Kick (t past every window) grants NOTHING (the grant is
    # form-gated), so the following Leaping Opo is bare.
    got = sc.score_delivered_potency([(40.0, dk), (42.0, lo)])
    assert abs(got - (md.POTENCIES[dk] + md.POTENCIES[lo])) < 1e-6, got


def test_raptor_coeurl_fury_bonuses():
    """Twin Snakes banks +200 for the next Rising Raptor; Demolish banks two
    +150s for the next two Pouncing Coeurls (the third is unstacked)."""
    got = sc.score_delivered_potency([(40.0, md.TWIN_SNAKES),
                                      (42.0, md.RISING_RAPTOR),
                                      (44.0, md.RISING_RAPTOR)])
    want = (md.POTENCIES[md.TWIN_SNAKES]
            + md.POTENCIES[md.RISING_RAPTOR] + md.RAPTOR_FURY_BONUS_P
            + md.POTENCIES[md.RISING_RAPTOR])
    assert abs(got - want) < 1e-6, (got, want)
    got = sc.score_delivered_potency([(40.0, md.DEMOLISH),
                                      (42.0, md.POUNCING_COEURL),
                                      (44.0, md.POUNCING_COEURL),
                                      (46.0, md.POUNCING_COEURL)])
    pc = md.POTENCIES[md.POUNCING_COEURL]
    want = (md.POTENCIES[md.DEMOLISH]
            + 2 * (pc + md.COEURL_FURY_BONUS_P) + pc)
    assert abs(got - want) < 1e-6, (got, want)


def test_perfect_balance_grants_opo_eligibility():
    """Under PB every weaponskill counts as in-form: Leaping Opo crits even deep
    into the fight with no form state."""
    got = sc.score_delivered_potency([(40.0, md.PERFECT_BALANCE),
                                      (42.0, md.LEAPING_OPO)])
    assert abs(got - md.POTENCIES[md.LEAPING_OPO] * md.GUARANTEED_CRIT_MULT) < 1e-6


def test_blitz_flat_potencies():
    """Blitzes score at their flat table values (900/900/600/1500)."""
    tl = [(40.0, md.ELIXIR_BURST), (42.0, md.RISING_PHOENIX),
          (44.0, md.PHANTOM_RUSH), (46.0, md.CELESTIAL_REVOLUTION)]
    got = sc.score_delivered_potency(tl)
    want = sum(md.POTENCIES[a] for _t, a in tl)
    assert abs(got - want) < 1e-6, (got, want)


# --- Downtime ---------------------------------------------------------------

def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_downtime_priming_is_zero_potency():
    """The downtime moves — the Forbidden Meditation chakra pump + the Form
    Shift Formless re-arm — emit only zero-potency casts inside the window, and
    the first uptime GCD is a full-value opo entry (Formless re-armed)."""
    dt = [(120.0, 150.0)]
    tl, _ = simulate_idealized_perfect(_DURATION_S, dt)
    in_window = [(t, a) for t, a in tl if 120.0 <= t < 150.0]
    assert in_window, "nothing primed in the window"
    scored = [(t, a) for t, a in in_window if md.POTENCIES.get(a, 0) > 0]
    assert not scored, f"scored casts inside downtime: {scored}"
    assert any(a == md.FORBIDDEN_MEDITATION for _t, a in in_window), \
        "no Meditation pump in the window"
    assert any(a == md.FORM_SHIFT for _t, a in in_window), \
        "Formless not re-armed at the window edge"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all monk sim tests passed")


if __name__ == "__main__":
    main()
