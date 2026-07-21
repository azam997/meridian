"""Ninja scoring + simulator invariants (network-free).

Mirrors test_viper_sim.py / test_samurai_sim.py for the thirteenth job simulator:

  * Pipeline doesn't crash — `analyze_pull('Ninja', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * NIN rotation balance: the mudra charge economy (2 / 20s; Kassatsu free), the
    Kunai's Bane <- Shadow Walker feed (every KB paid for by a Suiton), the
    Kassatsu -> Hyosho Ranryu pairing, the TCJ ladder + Tenri Jindo, the 120s
    Dokumori/Meisui/Zesho cycle, the Raiju and Ninki economies, Bunshin -> Phantom
    Kamaitachi, and the Kazematoi finisher cadence.
  * Scoring math: the Kunai's Bane x1.10 window, Kassatsu x1.30, Meisui +150,
    Kazematoi +100, and the Bunshin +160 mirror — each pinned exactly.
  * Downtime lowers the ceiling; the edge-of-window mudra pre-cast primes the
    ninjutsu as the first uptime GCD, and nothing scores inside downtime.

Run from python/:  python tests/test_ninja_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.ninja import data as nd
from jobs.ninja import scoring as sc
from jobs.ninja.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic NIN cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-NIN pull. Casts come from the sim; every other
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
            "title": "NIN fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Ninja", "server": "T",
                     "type": "Player", "subType": "Ninja", "petOwner": None,
                     "gameID": 30},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Ninja", client, "AbCd1234", 1,
                        ranking_name=None, label="nin-fixture")


def _counts(duration_s: float = _DURATION_S) -> Counter:
    return Counter(a for _t, a in simulate_idealized(duration_s, [])[0])


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
    assert 260 <= pps <= 480, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_cold_start_has_no_entry_gauge():
    """A fresh synthetic pull carries nothing in -> no EntryState -> the ceiling
    stays pure (duration, downtime, buffs) data."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    assert st.get("entryGauges") == {}, st.get("entryGauges")


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

def test_mudra_charge_economy():
    """Paid ninjutsu (everything but Kassatsu's) spend the shared 2-charge/20s
    pool: their count can never exceed opener charges + full-fight regen."""
    c = _counts()
    kassatsu_free = c[nd.HYOSHO_RANRYU]
    paid = (c[nd.RAITON] + c[nd.SUITON] + c[nd.FUMA_SHURIKEN]) - 0
    budget = 2 + _DURATION_S / nd.COOLDOWNS[nd.TEN][0]
    assert paid <= budget + 1e-6, f"paid ninjutsu {paid} exceed charge budget {budget:.1f}"
    assert kassatsu_free >= 1


def test_kunais_bane_shadow_walker_feed():
    """Every Kunai's Bane needs a Shadow Walker: KB count <= Suiton + TCJ-Suiton,
    and KB tracks its 60s cooldown."""
    c = _counts()
    kb = c[nd.KUNAIS_BANE]
    sw_sources = c[nd.SUITON] + c[nd.TCJ_SUITON]
    meisui = c[nd.MEISUI]
    assert kb >= int(_DURATION_S / 60.0) - 1, f"too few KBs: {kb}"
    assert kb <= _DURATION_S / 60.0 + 1
    # SW economy closes: KB + Meisui both consume one.
    assert kb + meisui <= sw_sources + 1, \
        f"SW overdrawn: KB {kb} + Meisui {meisui} > sources {sw_sources}"


def test_kassatsu_hyosho_pairing():
    """Each Kassatsu arms exactly one (free, x1.30) Hyosho Ranryu."""
    c = _counts()
    k = c[nd.KASSATSU]
    assert k >= 2
    assert k - 1 <= c[nd.HYOSHO_RANRYU] <= k


def test_tcj_ladder_and_tenri():
    """Each Ten Chi Jin runs the full Fuma -> Raiton -> Suiton ladder and arms one
    Tenri Jindo."""
    c = _counts()
    tcj = c[nd.TEN_CHI_JIN]
    assert tcj >= 2, f"too few TCJs: {tcj}"
    for step in (nd.TCJ_FUMA, nd.TCJ_RAITON, nd.TCJ_SUITON):
        assert tcj - 1 <= c[step] <= tcj, f"TCJ step {step}: {c[step]} vs {tcj}"
    assert tcj - 1 <= c[nd.TENRI_JINDO] <= tcj


def test_dokumori_meisui_zesho_cycle():
    """The 120s cycle: Dokumori (Higi) and Meisui each once per 2 minutes; every
    Zesho Meppo consumes a Higi (so Zesho <= Dokumori)."""
    c = _counts()
    doku = c[nd.DOKUMORI]
    assert doku >= int(_DURATION_S / 120.0), f"too few Dokumori: {doku}"
    assert c[nd.ZESHO_MEPPO] <= doku
    assert c[nd.MEISUI] <= doku + 1


def test_raiju_economy():
    """Raiju Ready comes 1 per Raiton (incl. TCJ's); the Raiju GCDs consume 1 each
    and can never exceed the grants."""
    c = _counts()
    grants = c[nd.RAITON] + c[nd.TCJ_RAITON]
    spends = c[nd.FLEETING_RAIJU] + c[nd.FORKED_RAIJU]
    assert spends <= grants
    assert spends >= grants - 3, f"Raiju left banked: {spends} of {grants}"


def test_bunshin_phantom_kamaitachi():
    """Each Bunshin (90s, 50 Ninki) arms one Phantom Kamaitachi."""
    c = _counts()
    b = c[nd.BUNSHIN]
    assert b >= int(_DURATION_S / 90.0), f"too few Bunshin: {b}"
    assert b - 1 <= c[nd.PHANTOM_KAMAITACHI] <= b


def test_ninki_economy_closes():
    """Ninki spent (50 x spenders) never exceeds Ninki generated (weaponskills +
    Dokumori + Meisui + the 5x5 Bunshin mirrors) plus one banked spender."""
    c = _counts()
    generated = sum(n * c[aid] for aid, n in nd.NINKI_GENERATORS.items())
    generated += 25 * c[nd.BUNSHIN]          # the shadow's +5 per mirrored hit
    spent = 50 * (c[nd.BHAVACAKRA] + c[nd.ZESHO_MEPPO] + c[nd.BUNSHIN])
    assert spent <= generated + 50, f"Ninki overdrawn: spent {spent} > gen {generated}"


def test_kazematoi_cadence():
    """Aeolian Edge spends the Kazematoi Armor Crush grants (2 per) — spends can
    never exceed grants, and the greedy keeps roughly the 1:2 AC:AE cadence."""
    c = _counts()
    ac, ae = c[nd.ARMOR_CRUSH], c[nd.AEOLIAN_EDGE]
    assert ac >= 2 and ae >= 2
    assert ae <= 2 * ac + 2, f"Aeolian {ae} outruns Kazematoi from {ac} Armor Crush"


# --- Scoring math (pinned exactly) ------------------------------------------

def test_kunais_bane_window_multiplier():
    """A cast inside the 15s KB window is priced x1.10; outside, raw. KB's own
    hit is never amped by the window it opens."""
    kb, rai = nd.KUNAIS_BANE, nd.RAITON
    inside = sc.score_delivered_potency([(0.0, kb), (5.0, rai)])
    outside = sc.score_delivered_potency([(0.0, kb), (20.0, rai)])
    p_kb, p_rai = nd.POTENCIES[kb], nd.POTENCIES[rai]
    assert abs(inside - (p_kb + p_rai * nd.KUNAIS_BANE_MULT)) < 1e-6
    assert abs(outside - (p_kb + p_rai)) < 1e-6


def test_kassatsu_multiplier_on_consuming_ninjutsu():
    """Kassatsu boosts exactly the ninjutsu that consumes it (x1.30), one time."""
    tl = [(0.0, nd.KASSATSU), (2.0, nd.HYOSHO_RANRYU), (10.0, nd.RAITON)]
    got = sc.score_delivered_potency(tl)
    want = nd.POTENCIES[nd.HYOSHO_RANRYU] * nd.KASSATSU_MULT + nd.POTENCIES[nd.RAITON]
    assert abs(got - want) < 1e-6, f"{got} != {want}"


def test_meisui_bonus_on_next_spender():
    """Meisui adds +150 to exactly the next Bhavacakra / Zesho Meppo."""
    tl = [(0.0, nd.MEISUI), (2.0, nd.BHAVACAKRA), (4.0, nd.BHAVACAKRA)]
    got = sc.score_delivered_potency(tl)
    want = (nd.POTENCIES[nd.BHAVACAKRA] + nd.MEISUI_BONUS_P) + nd.POTENCIES[nd.BHAVACAKRA]
    assert abs(got - want) < 1e-6, f"{got} != {want}"


def test_kazematoi_bonus_on_aeolian():
    """Armor Crush banks 2 Kazematoi; each subsequent Aeolian Edge spends 1 for
    +100 — the third Aeolian is unstacked."""
    tl = [(0.0, nd.ARMOR_CRUSH), (2.0, nd.AEOLIAN_EDGE), (4.0, nd.AEOLIAN_EDGE),
          (6.0, nd.AEOLIAN_EDGE)]
    got = sc.score_delivered_potency(tl)
    ae, ac = nd.POTENCIES[nd.AEOLIAN_EDGE], nd.POTENCIES[nd.ARMOR_CRUSH]
    want = ac + 2 * (ae + nd.AEOLIAN_KAZEMATOI_BONUS_P) + ae
    assert abs(got - want) < 1e-6, f"{got} != {want}"


def test_bunshin_mirror_credit():
    """Each of the 5 weaponskills after Bunshin is mirrored for +160; the 6th is
    not. Mudra/ninjutsu casts don't consume mirrors."""
    tl = [(0.0, nd.BUNSHIN)]
    tl += [(2.0 + i, nd.SPINNING_EDGE) for i in range(6)]
    tl.insert(2, (2.5, nd.RAITON))   # a ninjutsu mid-stream: no mirror, no consume
    got = sc.score_delivered_potency(tl)
    want = (6 * nd.POTENCIES[nd.SPINNING_EDGE] + 5 * nd.BUNSHIN_MIRROR_P
            + nd.POTENCIES[nd.RAITON])
    assert abs(got - want) < 1e-6, f"{got} != {want}"


# --- Downtime ---------------------------------------------------------------

def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_downtime_primes_mudras_at_edge():
    """The signature NIN downtime move: mudras pre-cast inside the window (they
    need no target, and deal nothing), the ninjutsu landing as the FIRST uptime
    GCD — and no scored cast inside the window."""
    dt = [(120.0, 150.0)]
    tl, _ = simulate_idealized_perfect(_DURATION_S, dt)
    in_window = [(t, a) for t, a in tl if 120.0 <= t < 150.0]
    assert in_window, "nothing pre-cast in the window"
    assert all(a in nd.MUDRA_IDS or a < 0 for _t, a in in_window), \
        f"non-mudra casts inside downtime: {in_window}"
    scored = [(t, a) for t, a in in_window if nd.POTENCIES.get(a, 0) > 0]
    assert not scored, f"scored casts inside downtime: {scored}"
    after = [(t, a) for t, a in tl if t >= 150.0]
    assert after and after[0][1] in nd.NINJUTSU_IDS, \
        f"first uptime GCD not the primed ninjutsu: {after[:3]}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all ninja sim tests passed")


if __name__ == "__main__":
    main()
