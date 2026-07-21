"""Pictomancer scoring + simulator invariants (network-free).

Mirrors test_bard_sim.py for the sixteenth job (the first downtime-painting
caster):

  * Pipeline doesn't crash — `analyze_pull('Pictomancer', ...)` runs every
    aspect via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock
    budget.
  * The hammer trio scores the tier-measured guaranteed crit+DH multiplier;
    the Star Prism follow-up (34682) scores zero.
  * The rotation structure holds: the aetherhue chain is never violated
    (R->G->B / C->M->Y cyclic, Subtractive restarting at cyan), hammers == 3x
    Striking, the creature muse cycle is Pom->Winged->Clawed->Fanged, portraits
    pair with their completing muses, every Starry Muse yields a Star Prism
    inside Starstruck, and the palette/paint economy closes.
  * Inspiration (-25%) shapes `gcd_duration` on the damaging spells only
    (hammers/motifs unhasted); the Rainbow-Bright Drip runs a 2.5s instant slot.
  * `on_downtime_window` re-paints empty canvases inside the window; downtime
    lowers the ceiling; the Starry windows are excluded from the gear-GCD
    inference.

Run from python/:  python tests/test_pictomancer_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.pictomancer import data as pdta
from jobs.pictomancer import scoring as sc
from jobs.pictomancer.scoring import PCTScoringAspect, score_delivered_potency
from jobs.pictomancer.simulator import (
    PictomancerRotationModel,
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
    """A realistic PCT cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-PCT pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no aura events
    -> no observed tincture/raid windows)."""

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
            "title": "PCT fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Pictomancer", "server": "T",
                     "type": "Player", "subType": "Pictomancer", "petOwner": None,
                     "gameID": 42},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Pictomancer", client, "AbCd1234", 1,
                        ranking_name=None, label="pct-fixture")


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
    assert 200 <= pps <= 340, f"p/sec out of band: {pps:.1f}"
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


def test_hammer_crit_dh_mult():
    """The hammer trio scores table potency x the tier-measured crit+DH factor;
    an RGB filler does not."""
    for aid in pdta.HAMMER_IDS:
        got = score_delivered_potency([(0.0, aid)])
        want = pdta.POTENCIES[aid] * pdta.GUARANTEED_CRIT_DH_MULT
        assert abs(got - want) < 1e-6, (aid, got, want)
    plain = score_delivered_potency([(0.0, pdta.FIRE_IN_RED)])
    assert abs(plain - pdta.POTENCIES[pdta.FIRE_IN_RED]) < 1e-6


def test_star_prism_followup_scores_zero():
    assert score_delivered_potency([(0.0, pdta.STAR_PRISM_FOLLOWUP)]) == 0.0
    both = score_delivered_potency([
        (0.0, pdta.STAR_PRISM), (1.25, pdta.STAR_PRISM_FOLLOWUP)])
    assert abs(both - pdta.POTENCIES[pdta.STAR_PRISM]) < 1e-6


def _chain_ok(timeline: list[tuple[float, int]]) -> bool:
    """Walk the timeline and verify the aetherhue chain is never violated:
    RGB/CMY advance step 0->1->2->0, Subtractive restarts the chain at cyan,
    and a CMY spell only ever fires with a Subtractive stack held."""
    step = 0
    stacks = 0
    rgb = {pdta.FIRE_IN_RED: 0, pdta.AERO_IN_GREEN: 1, pdta.WATER_IN_BLUE: 2,
           pdta.FIRE_II_IN_RED: 0, pdta.AERO_II_IN_GREEN: 1,
           pdta.WATER_II_IN_BLUE: 2}
    cmy = {pdta.BLIZZARD_IN_CYAN: 0, pdta.STONE_IN_YELLOW: 1,
           pdta.THUNDER_IN_MAGENTA: 2, pdta.BLIZZARD_II_IN_CYAN: 0,
           pdta.STONE_II_IN_YELLOW: 1, pdta.THUNDER_II_IN_MAGENTA: 2}
    for _t, aid in timeline:
        if aid == pdta.SUBTRACTIVE_PALETTE:
            step, stacks = 0, pdta.SUBTRACTIVE_STACKS
        elif aid in rgb:
            if rgb[aid] != step or stacks > 0:
                return False
            step = (step + 1) % 3
        elif aid in cmy:
            if cmy[aid] != step or stacks <= 0:
                return False
            step = (step + 1) % 3
            stacks -= 1
    return True


def test_rotation_structure():
    """Chain legality + the muse/portrait/hammer economy on a long greedy run."""
    timeline, _ = simulate_idealized(634.0, [])
    casts = [(t, a) for t, a in timeline if a > 0]
    assert _chain_ok(casts), "aetherhue chain violated"
    c = Counter(a for _, a in casts)
    hammers = sum(c[a] for a in pdta.HAMMER_IDS)
    assert 3 * c[pdta.STRIKING_MUSE] - 2 <= hammers <= 3 * c[pdta.STRIKING_MUSE]
    # Creature muse cycle order: Pom -> Winged -> Clawed -> Fanged, repeating.
    muses = [a for _, a in casts if a in pdta.CREATURE_MUSES]
    for i, aid in enumerate(muses):
        assert aid == pdta.CREATURE_MUSES[i % 4], f"muse cycle broken at {i}"
    # Portraits pair with their completing muses (tail may hold one).
    assert c[pdta.MOG_OF_THE_AGES] <= c[pdta.WINGED_MUSE]
    assert c[pdta.MOG_OF_THE_AGES] >= c[pdta.WINGED_MUSE] - 1
    assert c[pdta.RETRIBUTION_OF_THE_MADEEN] <= c[pdta.FANGED_MUSE]
    # Every muse consumed a canvas: repaints == muses - 1 pre-paint (the tail
    # may leave one canvas unpainted or one motif unconsumed).
    creature_motifs = sum(c[m] for m in pdta.CREATURE_MOTIFS)
    assert abs(creature_motifs - (len(muses) - 1)) <= 1
    assert abs(c[pdta.HAMMER_MOTIF] - (c[pdta.STRIKING_MUSE] - 1)) <= 1
    assert abs(c[pdta.STARRY_SKY_MOTIF] - (c[pdta.STARRY_MUSE] - 1)) <= 1
    # Comet requires a conversion: at most one per Subtractive.
    assert c[pdta.COMET_IN_BLACK] <= c[pdta.SUBTRACTIVE_PALETTE]
    # Palette economy: paid Subtractives (beyond the Starry frees) cost 50 each,
    # fed by Waters at +25.
    paid = c[pdta.SUBTRACTIVE_PALETTE] - c[pdta.STARRY_MUSE]
    waters = c[pdta.WATER_IN_BLUE] + c[pdta.WATER_II_IN_BLUE]
    assert paid * 2 <= waters + 1, f"palette economy broken: {paid} paid vs {waters} waters"


def test_starry_yields_star_prism_in_window():
    timeline, _ = simulate_idealized(634.0, [])
    starry_ts = [t for t, a in timeline if a == pdta.STARRY_MUSE]
    sp_ts = [t for t, a in timeline if a == pdta.STAR_PRISM]
    for st_t in starry_ts:
        if st_t > 634.0 - 6.0:
            continue   # tail Starry may not fit its Star Prism
        assert any(st_t <= t <= st_t + pdta.STARSTRUCK_DURATION_S for t in sp_ts), \
            f"no Star Prism inside the Starstruck window opened at {st_t:.1f}"


def test_inspiration_haste_shapes_gcd_duration():
    """-25% on the damaging spells while Hyperphantasia stacks remain; hammers,
    motifs and the Bright Drip are unhasted; slots return to base at 0 stacks."""
    model = PictomancerRotationModel()
    st = model.init_state()
    params = SimParams()
    st.t = 10.0
    st.hyperphantasia = 3
    assert abs(model.gcd_duration(st, pdta.BLIZZARD_IN_CYAN, params)
               - 3.3 * pdta.INSPIRATION_HASTE) < 1e-9
    assert abs(model.gcd_duration(st, pdta.FIRE_IN_RED, params)
               - 2.5 * pdta.INSPIRATION_HASTE) < 1e-9
    assert abs(model.gcd_duration(st, pdta.STAR_PRISM, params)
               - 2.5 * pdta.INSPIRATION_HASTE) < 1e-9
    assert abs(model.gcd_duration(st, pdta.HAMMER_STAMP, params) - 2.5) < 1e-9
    assert abs(model.gcd_duration(st, pdta.HAMMER_MOTIF, params) - 4.0) < 1e-9
    st.rainbow_bright = True
    st.bright_end = 30.0
    assert abs(model.gcd_duration(st, pdta.RAINBOW_DRIP, params) - 2.5) < 1e-9
    st.hyperphantasia = 0
    st.rainbow_bright = False
    assert abs(model.gcd_duration(st, pdta.BLIZZARD_IN_CYAN, params) - 3.3) < 1e-9
    assert abs(model.gcd_duration(st, pdta.RAINBOW_DRIP, params) - 6.0) < 1e-9
    assert abs(model.gcd_duration(st, pdta.FIRE_IN_RED, params) - 2.5) < 1e-9


def test_downtime_window_repaints_canvases():
    """`on_downtime_window` paints the empty canvases inside the window, the
    last slot ending exactly at the window edge."""
    model = PictomancerRotationModel()
    st = model.init_state()
    st.fight_duration_s = 300.0
    st.t = 60.0
    st.creature_canvas = False
    st.weapon_canvas = False
    st.landscape_canvas = False
    st.creature_stage = 2
    model.on_downtime_window(st, 60.0, 80.0)
    painted = [(t, a) for t, a in st.timeline if a in pdta.MOTIF_IDS]
    assert len(painted) == 3, painted
    assert painted[0][1] == pdta.CLAW_MOTIF          # stage-2 creature first
    assert all(60.0 <= t <= 80.0 for t, _a in painted)
    assert abs(max(t for t, _a in painted) - (80.0 - 4.0)) < 1e-9
    assert st.creature_canvas and st.weapon_canvas and st.landscape_canvas
    # A short window fits fewer motifs.
    st2 = model.init_state()
    st2.fight_duration_s = 300.0
    st2.t = 60.0
    st2.creature_canvas = False
    st2.weapon_canvas = False
    st2.landscape_canvas = False
    model.on_downtime_window(st2, 60.0, 65.0)
    assert len([1 for _t, a in st2.timeline if a in pdta.MOTIF_IDS]) == 1


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_gcd_inference_excludes_starry_windows():
    """Each Starry Muse cast opens a 30s exclusion window (the Inspiration
    hard cap) for the gear-GCD inference — the BLM Ley Lines rule."""
    aspect = PCTScoringAspect()
    casts = [(5.0, pdta.STARRY_MUSE), (10.0, pdta.BLIZZARD_IN_CYAN),
             (125.0, pdta.STARRY_MUSE)]
    excl = aspect.gcd_inference_exclusions(casts)
    assert excl == [(5.0, 35.0), (125.0, 155.0)]


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all pictomancer sim tests passed")


if __name__ == "__main__":
    main()
