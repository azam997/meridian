"""Unit tests for the job-agnostic raid-buff windowing engine
(jobs/_core/buff_windows.py + raid_buffs.resolve_status_ids).

Run from python/:  python tests/test_buff_windows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.buff_windows import (
    BuffWindow,
    expected_windows,
    fetch_observed_buff_windows,
    multiplier_at,
    multiplier_intervals,
    observed_windows_from_events,
    pair_aura_intervals,
)
from jobs._core.raid_buffs import (
    PROVIDER_BUFFS,
    BuffProvider,
    present_providers,
    resolve_status_ids,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _ev(ts: int, typ: str, aid: int) -> dict:
    return {"timestamp": ts, "type": typ, "abilityGameID": aid}


# --- pair_aura_intervals ---------------------------------------------------

def test_pair_clean() -> None:
    print("\nTest: pair_aura_intervals — clean apply/remove")
    evs = [_ev(1000, "applybuff", 7), _ev(21000, "removebuff", 7)]
    out = pair_aura_intervals(evs, 1000, 601000)
    _check("one 0->20s window", out == [(0.0, 20.0)], f"got {out}")


def test_pair_trailing_open() -> None:
    print("\nTest: pair_aura_intervals — trailing open closes at fight end")
    out = pair_aura_intervals([_ev(1000, "applydebuff", 7)], 1000, 31000)
    _check("0->30s (auto-close)", out == [(0.0, 30.0)], f"got {out}")


def test_pair_refresh_no_reopen() -> None:
    print("\nTest: pair_aura_intervals — refresh doesn't reopen")
    evs = [_ev(1000, "applybuff", 7), _ev(5000, "refreshbuff", 7),
           _ev(21000, "removebuff", 7)]
    out = pair_aura_intervals(evs, 1000, 601000)
    _check("single 0->20s window", out == [(0.0, 20.0)], f"got {out}")


# --- observed_windows_from_events ------------------------------------------

def test_observed_filters_and_tags() -> None:
    print("\nTest: observed_windows_from_events — maps statuses, ignores others")
    p_lit = BuffProvider("Dragoon", "Battle Litany", 1.03, 20.0)
    p_emb = BuffProvider("RedMage", "Embolden", 1.05, 20.0)
    status_map = {100: p_lit, 200: p_emb}
    evs = [
        _ev(1000, "applybuff", 100), _ev(21000, "removebuff", 100),
        _ev(1000, "applybuff", 200), _ev(21000, "removebuff", 200),
        _ev(1000, "applybuff", 999), _ev(5000, "removebuff", 999),  # not mapped
    ]
    out = observed_windows_from_events(evs, status_map, 1000, 601000)
    mults = sorted(w.multiplier for w in out)
    _check("two windows (unmapped ignored)", len(out) == 2, f"got {len(out)}")
    _check("multipliers 1.03 & 1.05", mults == [1.03, 1.05], f"got {mults}")


# --- expected_windows ------------------------------------------------------

def test_expected_cadence() -> None:
    print("\nTest: expected_windows — opener + 2-min cadence for present providers")
    out = expected_windows(310.0, ["Machinist", "Dragoon", "Scholar"])
    starts = sorted({w.start_s for w in out})
    # Each provider is phased at its own opener offset, then every 120s. Read
    # the offsets from the registry so a calibration refresh doesn't break this.
    drg = PROVIDER_BUFFS["Dragoon"].opener_offset_s
    sch = PROVIDER_BUFFS["Scholar"].opener_offset_s
    want = sorted({drg + 120.0 * k for k in range(3)}
                  | {sch + 120.0 * k for k in range(3)})
    _check("per-provider opener phase + 2-min cadence", starts == want,
           f"got {starts}, want {want}")
    # 2 providers (Dragoon+Scholar) x 3 bursts = 6 windows
    _check("2 providers x 3 bursts", len(out) == 6, f"got {len(out)}")


def test_expected_no_providers_empty() -> None:
    print("\nTest: expected_windows — no providers => empty")
    out = expected_windows(600.0, ["Machinist", "Warrior", "WhiteMage"])
    _check("empty", out == [], f"got {len(out)}")


# --- multiplier_intervals / multiplier_at ----------------------------------

def test_multiplier_product_on_overlap() -> None:
    print("\nTest: multiplier_intervals — overlapping buffs multiply")
    wins = [BuffWindow(0.0, 20.0, 1.05, "A"), BuffWindow(10.0, 30.0, 1.04, "B")]
    iv = multiplier_intervals(wins)
    # 0-10: 1.05, 10-20: 1.05*1.04, 20-30: 1.04
    _check("three segments", len(iv) == 3, f"got {iv}")
    _check("mid-overlap is product",
           abs(multiplier_at(15.0, iv) - 1.05 * 1.04) < 1e-9,
           f"got {multiplier_at(15.0, iv)}")
    _check("first segment single buff",
           abs(multiplier_at(5.0, iv) - 1.05) < 1e-9)
    _check("outside all => 1.0", multiplier_at(50.0, iv) == 1.0)


def test_multiplier_empty() -> None:
    print("\nTest: multiplier_intervals — no windows => no segments")
    _check("empty", multiplier_intervals([]) == [], "")
    _check("multiplier_at 1.0", multiplier_at(5.0, []) == 1.0)


# --- resolve_status_ids ----------------------------------------------------

def test_resolve_status_ids_by_name() -> None:
    print("\nTest: resolve_status_ids — name match, aura form only")
    abilities = [
        {"gameID": 3557, "name": "Battle Litany"},        # cast action (ignored)
        {"gameID": 1000786, "name": "Battle Litany"},      # aura form (kept)
        {"gameID": 1001221, "name": "Chain Stratagem"},
        {"gameID": 1001239, "name": "Embolden"},
        {"gameID": 1009999, "name": "Sprint"},             # not a raid buff
    ]
    jobs = ["Machinist", "Dragoon", "Scholar"]   # RedMage absent
    out = resolve_status_ids(abilities, jobs)
    _check("battle litany aura mapped (cast action excluded)",
           1000786 in out and 3557 not in out, f"got {sorted(out)}")
    _check("chain stratagem mapped (on_enemy)",
           1001221 in out and out[1001221].on_enemy, f"got {sorted(out)}")
    _check("embolden NOT mapped (RedMage absent)",
           1001239 not in out, f"got {sorted(out)}")


def test_buff_drift_detection() -> None:
    """compute_buff_drift: flags missing uses + drifted gaps, silent when
    the cadence is clean."""
    print("\nTest: compute_buff_drift — missing / gap / clean")
    from jobs._aspects.buff_drift import compute_buff_drift
    jobs = ["Machinist", "Dragoon"]   # provider = Battle Litany
    dur = 360.0   # expect bursts at 0,120,240 -> 3
    # Clean: 3 Battle Litany windows on cadence -> no findings.
    clean = [BuffWindow(t, t + 20, 1.03, "Battle Litany")
             for t in (5.0, 125.0, 245.0)]
    _check("clean cadence => no drift",
           compute_buff_drift(clean, jobs, dur) == [], "")
    # Missing: only 1 use observed of ~3 -> a missing finding.
    missing = compute_buff_drift([BuffWindow(5.0, 25.0, 1.03, "Battle Litany")],
                                 jobs, dur)
    _check("too-few uses => missing finding",
           any(f.kind == "missing" for f in missing), f"got {missing}")
    # Gap: two uses 200s apart -> a gap finding.
    gap = compute_buff_drift(
        [BuffWindow(5.0, 25.0, 1.03, "Battle Litany"),
         BuffWindow(205.0, 225.0, 1.03, "Battle Litany"),
         BuffWindow(245.0, 265.0, 1.03, "Battle Litany")], jobs, dur)
    _check("drifted gap => gap finding",
           any(f.kind == "gap" for f in gap), f"got {gap}")
    # No providers => never anything.
    _check("no providers => empty",
           compute_buff_drift(clean, ["Machinist", "Warrior"], dur) == [], "")


def test_fetch_observed_both_paths() -> None:
    """fetch_observed_buff_windows: on-player buff via received-buff stream,
    on-enemy debuff via the provider's cast. Uses a stub client."""
    print("\nTest: fetch_observed_buff_windows — player-buff + on-enemy-cast paths")
    FS, FE = 1_000_000, 1_300_000  # 300s fight
    # Battle Litany aura 1000786 (Dragoon, on player); Chain Stratagem cast
    # 7436 (Scholar, on enemy). Embolden absent (no RedMage).
    report = {"masterData": {
        "actors": [
            {"id": 1, "type": "Player", "subType": "Machinist"},
            {"id": 2, "type": "Player", "subType": "Dragoon"},
            {"id": 3, "type": "Player", "subType": "Scholar"},
        ],
        "abilities": [
            {"gameID": 1000786, "name": "Battle Litany"},
            {"gameID": 7436, "name": "Chain Stratagem"},
            {"gameID": 1001221, "name": "Chain Stratagem"},
        ],
    }}
    fight = {"startTime": FS, "endTime": FE, "friendlyPlayers": [1, 2, 3]}

    class _Stub:
        def get_aura_events(self, code, s, e, actor_id, data_type="Buffs"):
            # player (1) received Battle Litany at 10s for 20s
            if actor_id == 1 and data_type == "Buffs":
                return [{"timestamp": FS + 10_000, "type": "applybuff",
                         "abilityGameID": 1000786},
                        {"timestamp": FS + 30_000, "type": "removebuff",
                         "abilityGameID": 1000786}]
            return []

        def get_events(self, code, s, e, sid, data_type="Casts", ability_id=None):
            # Scholar (3) cast Chain Stratagem (7436) at 12s
            if sid == 3 and ability_id == 7436:
                return [{"timestamp": FS + 12_000}]
            return []

    wins = fetch_observed_buff_windows(_Stub(), "c", report, fight, 1)
    labels = sorted(w.label for w in wins)
    _check("two windows: Battle Litany + Chain Stratagem",
           labels == ["Battle Litany", "Chain Stratagem"], f"got {labels}")
    lit = next((w for w in wins if w.label == "Battle Litany"), None)
    cs = next((w for w in wins if w.label == "Chain Stratagem"), None)
    _check("battle litany 10->30s from buff stream",
           lit and abs(lit.start_s - 10) < 0.1 and abs(lit.end_s - 30) < 0.1,
           f"got {lit}")
    _check("chain stratagem 12->32s from cast (+20s duration)",
           cs and abs(cs.start_s - 12) < 0.1 and abs(cs.end_s - 32) < 0.1,
           f"got {cs}")


def main() -> int:
    test_pair_clean()
    test_pair_trailing_open()
    test_pair_refresh_no_reopen()
    test_observed_filters_and_tags()
    test_expected_cadence()
    test_expected_no_providers_empty()
    test_multiplier_product_on_overlap()
    test_multiplier_empty()
    test_resolve_status_ids_by_name()
    test_fetch_observed_both_paths()
    test_buff_drift_detection()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
