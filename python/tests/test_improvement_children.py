"""Tests for the located improvement-card children (Tier 3 of the
potential-improvements release audit).

Covers the per-instance children added to:
  * uptime cards — RPR Death's Design / WAR Surging Tempest / DRK Darkside
    (per-uncovered-window buckets; the bucket sum must reproduce the historic
    total exactly),
  * positional cards — RPR / DRG / MNK (per-miss records),
  * proc cards — RDM / DNC (located overwrites; expiry waste stays unlocated).

Shared conventions under test: children appear only at 2+ instances (a single
instance keeps the card a directly-jumpable leaf), old-shape states (missing
the new keys) degrade to childless cards, and children never sum past the
parent.

Run from python/:  python tests/test_improvement_children.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs.dancer.procs as dnc_procs
import jobs.darkknight.scoring as drk_scoring
import jobs.dragoon.positionals as drg_pos
import jobs.monk.positionals as mnk_pos
import jobs.reaper.death_design as rpr_dd
import jobs.reaper.positionals as rpr_pos
import jobs.redmage.procs as rdm_procs
import jobs.warrior.surging_tempest as war_st
from jobs.dancer import data as dnc_d
from jobs.darkknight import data as drk_d
from jobs.dragoon import data as drg_d
from jobs.monk import data as mnk_d
from jobs.reaper import data as rpr_d
from jobs.redmage import data as rdm_d
from jobs.warrior import data as war_d

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _child_sum_leq_parent(name: str, card) -> None:
    kids = card.children or []
    total = sum(c.lost_potency for c in kids)
    _check(f"{name}: child sum <= parent", total <= card.lost_potency + 1e-6,
           f"children {total} vs parent {card.lost_potency}")


# --- Uptime cards: hand-built states ----------------------------------------

def test_dd_children_two_windows() -> None:
    print("test_dd_children_two_windows")
    state = {
        "coverage_pct": 90.0,
        "uncovered_windows": [(10.0, 20.0), (50.0, 55.0)],
        "uncovered_lost": [100.0, 40.0],
        "lost_potency": 140.0,
    }
    cards = rpr_dd.improvements_from_deaths_design(state)
    _check("one parent card", len(cards) == 1)
    card = cards[0]
    _check("parent located at first window", card.time_s == 10.0)
    _check("two children", len(card.children) == 2)
    _check("child kinds", all(c.kind == "deaths_design" for c in card.children))
    _check("child ability id", all(c.ability_id == rpr_d.SHADOW_OF_DEATH
                                   for c in card.children))
    _check("child times = window starts",
           [c.time_s for c in card.children] == [10.0, 50.0])
    _check("child pricing = buckets",
           [c.lost_potency for c in card.children] == [100.0, 40.0])
    _check("child names the recast",
           "Shadow of Death" in card.children[0].summary)
    _check("child names the span", "0:10" in card.children[0].summary
           and "0:20" in card.children[0].summary)
    _child_sum_leq_parent("dd", card)


def test_dd_single_window_stays_leaf() -> None:
    print("test_dd_single_window_stays_leaf")
    state = {
        "coverage_pct": 95.0,
        "uncovered_windows": [(10.0, 20.0)],
        "uncovered_lost": [140.0],
        "lost_potency": 140.0,
    }
    cards = rpr_dd.improvements_from_deaths_design(state)
    _check("single window -> no children", cards[0].children == [])


def test_dd_old_shape_childless() -> None:
    print("test_dd_old_shape_childless")
    state = {
        "coverage_pct": 90.0,
        "uncovered_windows": [(10.0, 20.0), (50.0, 55.0)],
        "lost_potency": 140.0,
    }
    cards = rpr_dd.improvements_from_deaths_design(state)
    _check("old shape -> childless card", cards[0].children == [])
    _check("old shape keeps total", cards[0].lost_potency == 140.0)


def test_st_children_two_windows() -> None:
    print("test_st_children_two_windows")
    state = {
        "coverage_pct": 88.0,
        "uncovered_windows": [(30.0, 45.0), (90.0, 96.0)],
        "uncovered_lost": [200.0, 60.0],
        "lost_potency": 260.0,
    }
    cards = war_st.improvements_from_surging_tempest(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("child ability id", all(c.ability_id == war_d.STORMS_EYE
                                   for c in card.children))
    _check("child names the refresh",
           "Storm's Eye" in card.children[0].summary)
    _child_sum_leq_parent("st", card)


def test_st_old_shape_childless() -> None:
    print("test_st_old_shape_childless")
    state = {
        "coverage_pct": 88.0,
        "uncovered_windows": [(30.0, 45.0), (90.0, 96.0)],
        "lost_potency": 260.0,
    }
    cards = war_st.improvements_from_surging_tempest(state)
    _check("old shape -> childless card", cards[0].children == [])


# --- RPR Death's Design coverage: end-to-end bucket invariant ---------------

class _DDClient:
    """Stub: DamageDone events (with/without the DD buff token) + Casts."""

    def __init__(self, dmg: list[dict], casts: list[dict]):
        self._dmg = dmg
        self._casts = casts

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type == "DamageDone":
            return list(self._dmg)
        if data_type == "Casts":
            return list(self._casts)
        return []


def test_dd_coverage_buckets_per_window() -> None:
    print("test_dd_coverage_buckets_per_window")
    tok = str(1000000 + rpr_d.DEATHS_DESIGN_STATUS_ID)
    dmg = [
        {"type": "calculateddamage", "timestamp": t * 1000, "buffs": b}
        for t, b in [(0, tok), (4, tok), (8, tok), (20, ""), (30, tok)]
    ]
    # Covered: (0,12) + (30,34); uncovered over a 60s fight: (12,30) + (34,60).
    casts = [
        {"type": "cast", "timestamp": t * 1000, "abilityGameID": rpr_d.GIBBET}
        for t in (5, 15, 40, 50)   # 5 covered; 15 in w1; 40+50 in w2
    ]
    fight = {"startTime": 0, "endTime": 60000}
    report = {"__downtime__": {"windows": []}}
    state = rpr_dd._dd_coverage(
        _DDClient(dmg, casts), "CODE", fight, {"id": 1}, report)
    windows = [(round(s, 1), round(e, 1)) for s, e in state["uncovered_windows"]]
    _check("two uncovered windows", windows == [(12.0, 30.0), (34.0, 60.0)])
    per_cast = rpr_d.POTENCIES[rpr_d.GIBBET] * (rpr_d.DEATHS_DESIGN_MULT - 1.0)
    _check("buckets per window",
           state["uncovered_lost"] == [round(per_cast, 1), round(2 * per_cast, 1)])
    _check("buckets sum to the historic total",
           abs(sum(state["uncovered_lost"]) - state["lost_potency"]) < 0.15,
           f"{state['uncovered_lost']} vs {state['lost_potency']}")


# --- DRK Darkside: pure-function bucket invariant (incl. the tail window) ---

def test_darkside_stats_buckets_sum() -> None:
    print("test_darkside_stats_buckets_sum")
    E, H = drk_d.EDGE_OF_SHADOW, drk_d.HARD_SLASH
    timeline = [(0.0, E), (5.0, H), (35.0, H), (40.0, E),
                (75.0, H), (80.0, E), (120.0, H)]
    state = drk_scoring.darkside_stats(timeline, 130.0)
    _check("three windows incl. the tail",
           state["darkside_uncovered"] == [(30.0, 40.0), (70.0, 80.0),
                                           (110.0, 130.0)])
    hs = drk_d.POTENCIES[H] * (drk_d.DARKSIDE_MULT - 1.0)      # 30.0
    edge = drk_d.POTENCIES[E] * (drk_d.DARKSIDE_MULT - 1.0)    # 46.0
    _check("buckets (re-grant cast lands in its own window)",
           state["darkside_uncovered_lost"] == [round(hs + edge, 1),
                                                round(hs + edge, 1),
                                                round(hs, 1)])
    _check("buckets sum == historic total exactly",
           abs(sum(state["darkside_uncovered_lost"])
               - state["darkside_lost_potency"]) < 1e-6)


def test_darkside_children() -> None:
    print("test_darkside_children")
    E, H = drk_d.EDGE_OF_SHADOW, drk_d.HARD_SLASH
    timeline = [(0.0, E), (5.0, H), (35.0, H), (40.0, E),
                (75.0, H), (80.0, E), (120.0, H)]
    state = drk_scoring.darkside_stats(timeline, 130.0)
    cards = drk_scoring.improvements_from_darkside(state)
    card = cards[0]
    _check("three children", len(card.children) == 3)
    _check("child times = window starts",
           [c.time_s for c in card.children] == [30.0, 70.0, 110.0])
    _check("child names the spender",
           "Edge of Shadow" in card.children[0].summary)
    _child_sum_leq_parent("darkside", card)


# --- Positionals: detectors emit per-miss records ---------------------------

def test_rpr_positional_detector_and_children() -> None:
    print("test_rpr_positional_detector_and_children")
    evs = [
        {"type": "calculateddamage", "timestamp": 11000,
         "abilityGameID": rpr_d.GIBBET, "bonusPercent": 0},        # miss
        {"type": "calculateddamage", "timestamp": 21000,
         "abilityGameID": rpr_d.GALLOWS, "bonusPercent": 10},      # hit
        {"type": "calculateddamage", "timestamp": 31000,
         "abilityGameID": rpr_d.EXEC_GALLOWS, "bonusPercent": 0},  # miss
    ]
    state = rpr_pos.detect_positional_misses(evs, 1000)
    _check("detected", state["detected"])
    _check("miss records", [(m["time_s"], m["ability_id"]) for m in state["misses"]]
           == [(10.0, rpr_d.GIBBET), (30.0, rpr_d.EXEC_GALLOWS)])
    per_gibbet = float(rpr_d.POTENCIES[rpr_d.GIBBET]
                       - rpr_d.POSITIONAL_MISS_POTENCY[rpr_d.GIBBET])
    _check("per-miss pricing", state["misses"][0]["lost_potency"] == per_gibbet)

    cards = rpr_pos.improvements_from_positionals(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("child named + directed",
           "Gibbet" in card.children[0].summary
           and "flank" in card.children[0].summary)
    _check("rear direction on Executioner's Gallows",
           "rear" in card.children[1].summary)
    _check("True North hint", "True North" in card.children[0].summary)
    _child_sum_leq_parent("rpr positionals", card)


def test_rpr_positional_single_miss_stays_leaf() -> None:
    print("test_rpr_positional_single_miss_stays_leaf")
    # A NON-representative ability: the leaf must carry the ACTUAL missed
    # ability so the timeline's ability-aware highlight lands on the miss.
    evs = [{"type": "calculateddamage", "timestamp": 11000,
            "abilityGameID": rpr_d.EXEC_GALLOWS, "bonusPercent": 0}]
    state = rpr_pos.detect_positional_misses(evs, 1000)
    cards = rpr_pos.improvements_from_positionals(state)
    _check("single miss -> no children", cards[0].children == [])
    _check("single miss -> located at it", cards[0].time_s == 10.0)
    _check("single miss -> the actual ability, not the representative",
           cards[0].ability_id == rpr_d.EXEC_GALLOWS
           and cards[0].ability_name == "Executioner's Gallows")


def test_rpr_positional_old_shape_childless() -> None:
    print("test_rpr_positional_old_shape_childless")
    state = {"detected": True, "total": 4, "missed": 2,
             "miss_times": [10.0, 30.0], "lost_potency": 120.0}
    cards = rpr_pos.improvements_from_positionals(state)
    _check("old shape -> childless card", cards[0].children == [])


def test_drg_positional_detector_and_children() -> None:
    print("test_drg_positional_detector_and_children")
    evs = [
        {"type": "damage", "timestamp": 11000,
         "abilityGameID": drg_d.CHAOTIC_SPRING, "bonusPercent": 53},  # miss
        {"type": "damage", "timestamp": 21000,
         "abilityGameID": drg_d.FANG_AND_CLAW, "bonusPercent": 58},   # hit
        {"type": "damage", "timestamp": 31000,
         "abilityGameID": drg_d.FANG_AND_CLAW, "bonusPercent": 53},   # miss
    ]
    state = drg_pos.detect_positional_misses(evs, 1000)
    _check("two miss records", len(state["misses"]) == 2)
    cards = drg_pos.improvements_from_positionals(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("rear on Chaotic Spring", "Chaotic Spring" in card.children[0].summary
           and "rear" in card.children[0].summary)
    _check("flank on Fang and Claw", "flank" in card.children[1].summary)
    _child_sum_leq_parent("drg positionals", card)


def test_mnk_positional_detector_and_children() -> None:
    print("test_mnk_positional_detector_and_children")
    hit_bp = mnk_d.POSITIONAL_HIT_MIN_BP
    evs = [
        {"type": "damage", "timestamp": 11000,
         "abilityGameID": mnk_d.DEMOLISH, "bonusPercent": 0},          # miss
        {"type": "damage", "timestamp": 21000,
         "abilityGameID": mnk_d.POUNCING_COEURL, "bonusPercent": hit_bp},  # hit
        {"type": "damage", "timestamp": 31000,
         "abilityGameID": mnk_d.POUNCING_COEURL, "bonusPercent": 0},   # miss
    ]
    state = mnk_pos.detect_positional_misses(evs, 1000)
    _check("two miss records", len(state["misses"]) == 2)
    cards = mnk_pos.improvements_from_positionals(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("rear on Demolish", "Demolish" in card.children[0].summary
           and "rear" in card.children[0].summary)
    _check("flank on Pouncing Coeurl", "flank" in card.children[1].summary)
    _child_sum_leq_parent("mnk positionals", card)


# --- Procs: located overwrites ----------------------------------------------

class _BuffClient:
    def __init__(self, events: list[dict]):
        self._events = events

    def get_aura_events(self, code, start, end, source_id, kind):
        return list(self._events)


def test_rdm_proc_stats_overwrite_events() -> None:
    print("test_rdm_proc_stats_overwrite_events")
    off = 1000000
    # The overwrite is a VERSTONE proc — the single-overwrite card must carry
    # Verstone, not the fixed Verfire representative, so the timeline's
    # ability-aware highlight targets the right proc color.
    buffs = [
        {"abilityGameID": off + rdm_d.VERFIRE_READY_STATUS_ID,
         "type": "applybuff", "timestamp": 5000},
        {"abilityGameID": off + rdm_d.VERSTONE_READY_STATUS_ID,
         "type": "applybuff", "timestamp": 8000},
        {"abilityGameID": off + rdm_d.VERSTONE_READY_STATUS_ID,
         "type": "refreshbuff", "timestamp": 12000},   # the overwrite
    ]
    fight = {"startTime": 0, "endTime": 100000}
    norm_casts = [(30.0, rdm_d.VERFIRE), (35.0, rdm_d.VERSTONE)]
    state = rdm_procs._proc_stats(_BuffClient(buffs), "CODE", fight, {"id": 1},
                                  norm_casts)
    _check("one wasted (the overwrite)", state["total_wasted"] == 1)
    ows = state["overwrite_events"]
    _check("one located overwrite", len(ows) == 1)
    _check("overwrite time + names",
           ows[0]["time_s"] == 12.0
           and ows[0]["status_name"] == "Verstone Ready"
           and ows[0]["consumer_id"] == rdm_d.VERSTONE)

    # Exactly one overwrite: the card itself is located at it, no children,
    # and it carries the overwrite's actual consumer.
    cards = rdm_procs.improvements_from_procs(state)
    _check("single overwrite locates the card", cards[0].time_s == 12.0)
    _check("single overwrite -> no children", cards[0].children == [])
    _check("single overwrite -> the actual consumer",
           cards[0].ability_id == rdm_d.VERSTONE
           and cards[0].ability_name == "Verstone")
    _check("no expired note", "expired" not in cards[0].summary)


def test_rdm_proc_single_overwrite_with_expiry_notes_it() -> None:
    print("test_rdm_proc_single_overwrite_with_expiry_notes_it")
    # One located overwrite + one count-inferred expiry: the leaf jumps to the
    # overwrite but its price covers both — the summary must say so.
    state = {
        "total_wasted": 2, "overwrites": 1, "utilization_pct": 85.0,
        "lost_potency": 2.0 * rdm_procs.PROC_VALUE_P,
        "overwrite_events": [
            {"time_s": 12.0, "status_name": "Verfire Ready",
             "consumer_name": "Verfire", "consumer_id": rdm_d.VERFIRE,
             "lost_potency": float(rdm_procs.PROC_VALUE_P)},
        ],
    }
    cards = rdm_procs.improvements_from_procs(state)
    _check("located at the overwrite", cards[0].time_s == 12.0)
    _check("no children at one overwrite", cards[0].children == [])
    _check("expiry note on the located leaf",
           "plus 1 expired unused" in cards[0].summary)


def test_rdm_proc_children_and_expired_note() -> None:
    print("test_rdm_proc_children_and_expired_note")
    state = {
        "total_wasted": 3, "overwrites": 2, "utilization_pct": 80.0,
        "lost_potency": 3.0 * rdm_procs.PROC_VALUE_P,
        "overwrite_events": [
            {"time_s": 12.0, "status_name": "Verfire Ready",
             "consumer_name": "Verfire", "consumer_id": rdm_d.VERFIRE,
             "lost_potency": float(rdm_procs.PROC_VALUE_P)},
            {"time_s": 44.0, "status_name": "Verstone Ready",
             "consumer_name": "Verstone", "consumer_id": rdm_d.VERSTONE,
             "lost_potency": float(rdm_procs.PROC_VALUE_P)},
        ],
    }
    cards = rdm_procs.improvements_from_procs(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("child summary names status + spender",
           "Verfire Ready" in card.children[0].summary
           and "spend Verfire" in card.children[0].summary)
    _check("parent notes the unlocated expiry",
           "plus 1 expired unused" in card.summary)
    _check("parent located at first overwrite", card.time_s == 12.0)
    _child_sum_leq_parent("rdm procs", card)


def test_rdm_proc_old_shape_childless() -> None:
    print("test_rdm_proc_old_shape_childless")
    state = {"total_wasted": 2, "utilization_pct": 90.0,
             "lost_potency": 2.0 * rdm_procs.PROC_VALUE_P}
    cards = rdm_procs.improvements_from_procs(state)
    _check("old shape -> childless, unlocated", cards[0].children == []
           and cards[0].time_s == 0.0)


def test_dnc_proc_children_per_proc_values() -> None:
    print("test_dnc_proc_children_per_proc_values")
    silken = float(dnc_d.POTENCIES[dnc_d.REVERSE_CASCADE]
                   - dnc_d.POTENCIES[dnc_d.CASCADE])
    fan4 = float(dnc_d.POTENCIES[dnc_d.FAN_DANCE_IV])
    state = {
        "total_wasted": 2, "overwrites": 2, "utilization_pct": 85.0,
        "lost_potency": silken + fan4,
        "overwrite_events": [
            {"time_s": 15.0, "status_name": "Silken Symmetry",
             "consumer_name": "Reverse Cascade",
             "consumer_id": dnc_d.REVERSE_CASCADE, "lost_potency": silken},
            {"time_s": 42.0, "status_name": "Fourfold Fan Dance",
             "consumer_name": "Fan Dance IV",
             "consumer_id": dnc_d.FAN_DANCE_IV, "lost_potency": fan4},
        ],
    }
    cards = dnc_procs.improvements_from_procs(state)
    card = cards[0]
    _check("two children", len(card.children) == 2)
    _check("per-proc values", [c.lost_potency for c in card.children]
           == [silken, fan4])
    _check("child names the spender",
           "Fan Dance IV" in card.children[1].summary)
    _check("no expired note when all waste is located",
           "expired" not in card.summary)
    _child_sum_leq_parent("dnc procs", card)


def test_dnc_proc_stats_overwrite_events() -> None:
    print("test_dnc_proc_stats_overwrite_events")
    off = 1000000
    buffs = [
        {"abilityGameID": off + dnc_d.SILKEN_SYMMETRY_STATUS_ID,
         "type": "applybuff", "timestamp": 5000},
        {"abilityGameID": off + dnc_d.SILKEN_SYMMETRY_STATUS_ID,
         "type": "refreshbuff", "timestamp": 9000},   # the overwrite
    ]
    fight = {"startTime": 0, "endTime": 100000}
    norm_casts = [(20.0, dnc_d.REVERSE_CASCADE)]
    state = dnc_procs._proc_stats(_BuffClient(buffs), "CODE", fight, {"id": 1},
                                  norm_casts)
    ows = state["overwrite_events"]
    _check("one located overwrite", len(ows) == 1)
    _check("overwrite record", ows[0]["time_s"] == 9.0
           and ows[0]["consumer_name"] == "Reverse Cascade")


def main() -> int:
    test_dd_children_two_windows()
    test_dd_single_window_stays_leaf()
    test_dd_old_shape_childless()
    test_st_children_two_windows()
    test_st_old_shape_childless()
    test_dd_coverage_buckets_per_window()
    test_darkside_stats_buckets_sum()
    test_darkside_children()
    test_rpr_positional_detector_and_children()
    test_rpr_positional_single_miss_stays_leaf()
    test_rpr_positional_old_shape_childless()
    test_drg_positional_detector_and_children()
    test_mnk_positional_detector_and_children()
    test_rdm_proc_stats_overwrite_events()
    test_rdm_proc_single_overwrite_with_expiry_notes_it()
    test_rdm_proc_children_and_expired_note()
    test_rdm_proc_old_shape_childless()
    test_dnc_proc_children_per_proc_values()
    test_dnc_proc_stats_overwrite_events()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  FAILED: {item}")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
