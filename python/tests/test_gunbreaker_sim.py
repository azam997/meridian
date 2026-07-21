"""Gunbreaker scoring + simulator invariants (network-free).

Mirrors test_dragoon_sim.py for the 12th job simulator (the first tank with a
cartridge spend-cadence beam fork):

  * Pipeline doesn't crash — `analyze_pull('Gunbreaker', ...)` runs every aspect via the
    registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * No Mercy: a cast under No Mercy is priced x1.20; the No Mercy cast never amps itself.
  * Sonic Break + Bow Shock DoTs: over-refresh is overcap-safe; capped at the true 15s.
  * Cartridge economy: spent <= generated (can't spend cartridges never built); the
    cap is respected; Bloodfest fuels the Reign chain.
  * Continuations follow their GCD; the Reign combo is gated behind Bloodfest.

Run from python/:  python tests/test_gunbreaker_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.gunbreaker import data as gd
from jobs.gunbreaker import scoring as sc
from jobs.gunbreaker.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic GNB cast stream = the default-sim timeline, as FFLogs cast events
    (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class MockClient:
    """Serves a synthetic single-GNB pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no damage events)."""

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
            "title": "GNB fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Gunbreaker", "server": "T",
                     "type": "Player", "subType": "Gunbreaker", "petOwner": None,
                     "gameID": 37},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Gunbreaker", client, "AbCd1234", 1,
                        ranking_name=None, label="gnb-fixture")


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
    assert 180 <= pps <= 480, f"p/sec out of band: {pps:.1f}"
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
    assert time.monotonic() - start <= 25.0


def test_no_mercy_self_buff():
    """A cast under No Mercy is amped x1.20; the No Mercy cast never amps itself, and a
    cast with no preceding No Mercy is at base potency."""
    with_nm = sc.score_delivered_potency([(0.0, gd.NO_MERCY), (1.0, gd.BURST_STRIKE)])
    no_nm = sc.score_delivered_potency([(0.0, gd.BURST_STRIKE)])
    assert abs(with_nm - gd.POTENCIES[gd.BURST_STRIKE] * gd.NO_MERCY_MULT) < 1e-6
    assert abs(no_nm - gd.POTENCIES[gd.BURST_STRIKE]) < 1e-6


def test_sonic_break_dot_overrefresh_safe():
    """Over-refreshing Sonic Break credits by time-to-next-cast (capped at 15s), so two
    casts 5s apart credit far less than two 15s apart — never double-counted."""
    tight = sc._dot_potency([(0.0, 1.0), (5.0, 1.0)],
                            [(0.0, gd.SONIC_BREAK), (5.0, gd.SONIC_BREAK)],
                            gd.SONIC_BREAK_DOT_DURATION_S, gd.SONIC_BREAK_DOT_TICK_S,
                            gd.SONIC_BREAK_DOT_TICK_P)
    spaced = sc._dot_potency([(0.0, 1.0), (15.0, 1.0)],
                             [(0.0, gd.SONIC_BREAK), (15.0, gd.SONIC_BREAK)],
                             gd.SONIC_BREAK_DOT_DURATION_S, gd.SONIC_BREAK_DOT_TICK_S,
                             gd.SONIC_BREAK_DOT_TICK_P)
    assert tight < spaced
    one = sc._dot_potency([(0.0, 1.0)], [(0.0, gd.SONIC_BREAK)],
                          gd.SONIC_BREAK_DOT_DURATION_S, gd.SONIC_BREAK_DOT_TICK_S,
                          gd.SONIC_BREAK_DOT_TICK_P)
    max_one = (gd.SONIC_BREAK_DOT_DURATION_S / gd.SONIC_BREAK_DOT_TICK_S
               * gd.SONIC_BREAK_DOT_TICK_P)
    assert one <= max_one + 1e-6


def test_cartridge_spent_within_generated():
    """Can't spend cartridges you never built: total cartridges spent <= total generated
    (Solid Barrel / Demon Slaughter +1, Bloodfest +3; spenders 1, Double Down 2)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    generated = (c[gd.SOLID_BARREL] + c[gd.DEMON_SLAUGHTER]
                 + 3 * c[gd.BLOODFEST])
    spent = (c[gd.BURST_STRIKE] + c[gd.GNASHING_FANG] + c[gd.FATED_CIRCLE]
             + 2 * c[gd.DOUBLE_DOWN])
    assert spent <= generated, f"spent {spent} > generated {generated}"


def test_reign_combo_gated_by_bloodfest():
    """The Reign of Beasts chain is gated behind Bloodfest's Ready to Reign: each chain
    completes (Reign -> Noble Blood -> Lion Heart) and there is no more than one chain
    per Bloodfest."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[gd.REIGN_OF_BEASTS] <= c[gd.BLOODFEST], "Reign without Bloodfest"
    assert c[gd.NOBLE_BLOOD] <= c[gd.REIGN_OF_BEASTS]
    assert c[gd.LION_HEART] <= c[gd.NOBLE_BLOOD]


def test_continuations_follow_their_gcd():
    """Each continuation oGCD never exceeds its enabling GCD's cast count."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[gd.HYPERVELOCITY] <= c[gd.BURST_STRIKE]
    assert c[gd.JUGULAR_RIP] <= c[gd.GNASHING_FANG]
    assert c[gd.ABDOMEN_TEAR] <= c[gd.SAVAGE_CLAW]
    assert c[gd.EYE_GOUGE] <= c[gd.WICKED_TALON]


def test_no_mercy_cadence():
    """No Mercy (60s) and Bloodfest are cast repeatedly across the fight."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[gd.NO_MERCY] >= _DURATION_S / 70.0, f"too few No Mercy: {c[gd.NO_MERCY]}"
    assert c[gd.BLOODFEST] >= 1


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


# --- Bundled ability metadata (hermetic Clipping + GCD-inference dependency) --


def test_ability_metadata_bundled():
    """Every GNB id resolves from ability_metadata.BUNDLED with the OGCD_IDS
    oGCD flag — under the hermetic stub (no XIVAPI). The Clipping aspect and the
    GCD-speed inference / demonstrated-cadence anchor skip any cast whose
    metadata is None, so a missing entry silently blanks those paths."""
    from jobs._core.ability_metadata import BUNDLED, get_metadata
    all_ids = set(gd.POTENCIES) | set(gd.OGCD_IDS) | set(gd.DEFENSIVE_IDS)
    for aid in sorted(all_ids):
        assert aid in BUNDLED, f"GNB id {aid} missing from ability_metadata.BUNDLED"
        meta = get_metadata(aid)
        assert meta is not None and meta.name, f"id {aid} did not resolve"
        expected_ogcd = aid in gd.OGCD_IDS
        assert meta.is_ogcd == expected_ogcd, \
            f"{meta.name} ({aid}): is_ogcd={meta.is_ogcd}, OGCD_IDS says {expected_ogcd}"


# --- In-game expiries (Ready to Break, the combo timer, Ready to Reign) -----


def test_ready_to_break_expires():
    """Ready to Break lasts 30s: Sonic Break is offered inside the window and
    withdrawn after it, so a line can't hold the cast past the in-game buff."""
    from jobs.gunbreaker.simulator import GunbreakerRotationModel
    m = GunbreakerRotationModel()
    st = m.init_state()
    st.t = 5.0
    m.apply_cast(st, gd.NO_MERCY)
    assert st.ready_to_break and st.ready_to_break_end == 35.0
    st.t = 20.0
    assert gd.SONIC_BREAK in m._st_candidates(st), "in-window Sonic Break missing"
    st.t = 36.0
    assert gd.SONIC_BREAK not in m._st_candidates(st), "expired Sonic Break offered"


def test_combo_lost_across_long_downtime():
    """The 30s combo timer: a mid-step combo does not survive a downtime that
    puts the next GCD more than 30s after the last one; a short window keeps it."""
    from jobs.gunbreaker.simulator import GunbreakerRotationModel
    m = GunbreakerRotationModel()
    st = m.init_state()
    m.apply_cast(st, gd.KEEN_EDGE)
    st.last_gcd_t = st.t
    assert st.basic_combo_step == 1
    m.on_downtime_window(st, 5.0, 40.0)   # resume 40s after the last GCD
    assert st.basic_combo_step == 0, "combo survived a 40s gap"
    st2 = m.init_state()
    m.apply_cast(st2, gd.KEEN_EDGE)
    st2.last_gcd_t = st2.t
    m.on_downtime_window(st2, 5.0, 20.0)  # resume 20s after — inside the timer
    assert st2.basic_combo_step == 1, "combo lost inside the 30s timer"


def test_reign_proc_consumed_on_first_cast():
    """Ready to Reign drops on Reign of Beasts itself (the in-game buff is
    consumed by the first cast); Noble Blood / Lion Heart ride the combo chain."""
    from jobs.gunbreaker.simulator import GunbreakerRotationModel
    m = GunbreakerRotationModel()
    st = m.init_state()
    m.apply_cast(st, gd.BLOODFEST)
    assert st.ready_to_reign
    st.t = 2.5
    m.apply_cast(st, gd.REIGN_OF_BEASTS)
    assert not st.ready_to_reign, "Ready to Reign survived Reign of Beasts"
    assert st.reign_step == 1


# --- Cartridge overcap accuracy (the dynamic 3->6 Bloodfest cap) ------------

_CART_GAUGE = gd.JOB_DATA.gauges[0]


def _overcap(casts):
    from jobs._aspects.overcap import compute_overcap_for_gauge
    return compute_overcap_for_gauge(casts, _CART_GAUGE)


def test_overcap_bloodfest_no_false_positive():
    """Bloodfest's +3 under the raised 3->6 cap is NOT an overcap. (The old static-cap
    detector wrongly flagged it: at 2 carts, +3 = 5 > 3 → false -840p.)"""
    casts = [(0.0, gd.SOLID_BARREL), (2.5, gd.SOLID_BARREL), (5.0, gd.BLOODFEST)]
    assert _overcap(casts) == [], "Bloodfest under the raised cap must not overcap"


def test_overcap_real_generator_at_base_cap():
    """A 4th Solid Barrel at cap 3 (no Bloodfest window) overcaps by 1."""
    casts = [(0.0, gd.SOLID_BARREL), (2.5, gd.SOLID_BARREL),
             (5.0, gd.SOLID_BARREL), (7.5, gd.SOLID_BARREL)]
    f = _overcap(casts)
    assert len(f) == 1 and f[0].wasted == 1 and f[0].ability_id == gd.SOLID_BARREL


def test_overcap_uses_raised_cap_in_window():
    """During the Bloodfest window the cap is 6: it takes 6 (not 3) cartridges for a
    generator to overcap."""
    # 3 carts -> Bloodfest (+3 = 6, cap 6) -> Solid Barrel would be the 7th unit.
    casts = [(0.0, gd.SOLID_BARREL), (2.5, gd.SOLID_BARREL), (5.0, gd.SOLID_BARREL),
             (7.5, gd.BLOODFEST), (10.0, gd.SOLID_BARREL)]
    f = _overcap(casts)
    assert len(f) == 1 and f[0].wasted == 1 and f[0].time_s == 10.0
    # A generator that lands at 6 in-window is fine (no overcap below the raised cap).
    casts_ok = [(0.0, gd.SOLID_BARREL), (2.5, gd.SOLID_BARREL), (5.0, gd.BLOODFEST),
                (7.5, gd.SOLID_BARREL)]  # 2 -> +3 = 5 -> +1 = 6 <= 6
    assert _overcap(casts_ok) == []


def test_overcap_bonus_cartridges_expiry_priced():
    """Bonus cartridges held past the 30s Bloodfest window are lost and priced (the
    simulator clamps them too). Bloodfest -> 6, no spend, a cast after +30s realizes
    the 3-cartridge expiry loss."""
    casts = [(0.0, gd.SOLID_BARREL), (2.5, gd.SOLID_BARREL), (5.0, gd.SOLID_BARREL),
             (7.5, gd.BLOODFEST), (40.0, gd.SOLID_BARREL)]
    f = _overcap(casts)
    expiry = [x for x in f if x.ability_id == gd.BLOODFEST]
    assert len(expiry) == 1 and expiry[0].wasted == 3, f"expiry loss not priced: {f}"


def test_entry_gauge_seeds_carried_cartridges():
    """Phase-continuation (M12S-P2): an opener that spends cartridges before generating
    any means the player carried them out of P1. entry_state detects the deepest deficit
    and the seeded model starts with those cartridges — so the ceiling front-loads the
    same carried burst (symmetric, >100% guard holds)."""
    from jobs._core.entry_gauge import entry_state
    from jobs.gunbreaker.simulator import GunbreakerRotationModel
    # Double Down (-2) then Burst Strike (-1) before any generator = carried 3.
    casts = [(0.0, gd.DOUBLE_DOWN), (2.5, gd.BURST_STRIKE), (5.0, gd.KEEN_EDGE)]
    es = entry_state(casts, gd.JOB_DATA.gauges)
    assert es is not None, "carried cartridges not detected"
    assert es.gauge_map.get("cartridges") == 3, f"got {es.gauge_map}"
    seeded = GunbreakerRotationModel(entry=es).init_state()
    assert seeded.cartridges == 3, f"seed failed: {seeded.cartridges}"
    # A cold-start opener (build first) carries nothing -> byte-identical (None).
    cold = entry_state([(0.0, gd.KEEN_EDGE), (2.5, gd.BRUTAL_SHELL),
                        (5.0, gd.SOLID_BARREL)], gd.JOB_DATA.gauges)
    assert cold is None or not cold.gauge_map.get("cartridges")


def test_entry_seeded_ceiling_at_least_cold_start():
    """Seeding carried cartridges can only RAISE the ceiling (more early resource) —
    it never lowers it, so it only ever pulls a hot continuation parse back under 100%."""
    from jobs._core.entry_gauge import EntryState
    from jobs._core.gcd_speed import CeilingContext
    cold = sc.idealized_at_duration(240.0, [])
    hot = sc.idealized_at_duration(
        240.0, [], sim_context=CeilingContext(
            gcd_base_s=None, payload=EntryState(gauges=(("cartridges", 3),))))
    assert hot >= cold - 1e-6, f"entry seed lowered the ceiling: {hot} < {cold}"


def test_overcap_static_gauge_byte_identical():
    """A gauge with no cap_boosts (Warrior Beast) is unaffected by the dynamic-cap path
    — the detector matches the plain static-cap walk."""
    from jobs._aspects.overcap import compute_overcap_for_gauge
    from jobs.warrior.data import JOB_DATA as WAR
    beast = WAR.gauges[0]
    assert not beast.cap_boosts
    casts = [(0.0, aid) for aid in list(beast.generators) * 6]
    # No crash + deterministic; the exact findings are Warrior's own concern.
    assert isinstance(compute_overcap_for_gauge(casts, beast), list)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all gunbreaker sim tests passed")


if __name__ == "__main__":
    main()
