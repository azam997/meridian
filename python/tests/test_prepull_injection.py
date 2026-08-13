"""Buff-proven pre-pull instant reconstruction into the shared cast stream.

An instant pressed during the countdown (GNB Bloodfest, SAM Meikyo, MCH
Reassemble, MNK Form Shift) produces NO cast event in FFLogs, but its buff
survives — the status's first Buffs event in the fight is a remove/refresh/
removebuffstack, never an apply. `jobs/_core/casts.py` injects the proven cast
at t = -2.0 so every norm_casts consumer sees it:

  * the drift detector's initial-charge reduction (no phantom "capped since
    t=0" on the first in-fight press),
  * the entry-gauge walk (pre-pull generation precedes the opener's spends —
    the GNB Bloodfest double-count fix),
  * the Timeline pre-pull zone.

Run from python/:  python tests/test_prepull_injection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.casts import fetch_norm_casts
from jobs._core.entry_gauge import measure_entry_gauge
from jobs.gunbreaker import data as gd

_START_MS = 1_000_000
_END_MS = 1_360_000
_SRC = 7

_FIGHT = {"startTime": _START_MS, "endTime": _END_MS}
_ACTOR = {"id": _SRC, "subType": "Gunbreaker"}


class _Client:
    """Synthetic client: a GNB opener whose 3 early cartridge spends were
    funded by a pre-pull Bloodfest (dropped by FFLogs)."""

    def __init__(self, *, prepull_bloodfest_proven: bool):
        self._proven = prepull_bloodfest_proven
        t = _START_MS
        self._casts = [
            {"timestamp": t + 1_000, "type": "cast", "abilityGameID": gd.KEEN_EDGE},
            {"timestamp": t + 3_500, "type": "cast", "abilityGameID": gd.GNASHING_FANG},
            {"timestamp": t + 6_000, "type": "cast", "abilityGameID": gd.BURST_STRIKE},
            {"timestamp": t + 8_500, "type": "cast", "abilityGameID": gd.BURST_STRIKE},
        ]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._casts if start <= e["timestamp"] <= end]

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        sid = 1_000_000 + gd.READY_TO_REIGN_STATUS_ID
        if self._proven:
            # First event is a removebuff — the buff was applied pre-pull.
            return [{"timestamp": _START_MS + 9_000, "type": "removebuff",
                     "abilityGameID": sid}]
        # In-fight apply first — no pre-pull press.
        return [{"timestamp": _START_MS + 30_000, "type": "applybuff",
                 "abilityGameID": sid},
                {"timestamp": _START_MS + 39_000, "type": "removebuff",
                 "abilityGameID": sid}]


def test_proven_prepull_is_injected() -> None:
    casts = fetch_norm_casts(_Client(prepull_bloodfest_proven=True),
                             "code", _FIGHT, _ACTOR)
    pre = [(t, aid) for t, aid in casts if t < 0]
    assert pre == [(-2.0, gd.BLOODFEST)], f"got {pre}"
    assert casts == sorted(casts, key=lambda c: c[0])


def test_unproven_prepull_not_injected() -> None:
    casts = fetch_norm_casts(_Client(prepull_bloodfest_proven=False),
                             "code", _FIGHT, _ACTOR)
    assert all(t >= 0 for t, _aid in casts), f"unexpected pre-pull cast: {casts}"


def test_stacked_buff_first_event_counts() -> None:
    """A stacked countdown buff's first event is a removebuffstack (SAM
    Meikyo) — accepted as proof."""
    client = _Client(prepull_bloodfest_proven=True)
    sid = 1_000_000 + gd.READY_TO_REIGN_STATUS_ID

    def stacked(code, start, end, actor_id, data_type="Buffs"):
        return [{"timestamp": _START_MS + 5_000, "type": "removebuffstack",
                 "abilityGameID": sid}]
    client.get_aura_events = stacked
    casts = fetch_norm_casts(client, "code", _FIGHT, _ACTOR)
    assert (-2.0, gd.BLOODFEST) in casts


def test_entry_gauge_sees_prepull_generation() -> None:
    """The GNB double-count fix end-to-end: with the injected Bloodfest the
    opener's 3 cartridge spends are funded (entry 0); without it they read as
    a 3-cartridge carried-gauge deficit."""
    proven = fetch_norm_casts(_Client(prepull_bloodfest_proven=True),
                              "code", _FIGHT, _ACTOR)
    cold = fetch_norm_casts(_Client(prepull_bloodfest_proven=False),
                            "code", _FIGHT, _ACTOR)
    g_proven = measure_entry_gauge(proven, gd.JOB_DATA.gauges)
    g_cold = measure_entry_gauge(cold, gd.JOB_DATA.gauges)
    assert g_proven.get("cartridges", 0) == 0, g_proven
    assert g_cold.get("cartridges", 0) == 3, g_cold


def test_no_double_injection_when_cast_present() -> None:
    """A stream that already carries a real t<0 cast of the ability (local
    capture) is left alone."""
    client = _Client(prepull_bloodfest_proven=True)
    client._casts.insert(0, {"timestamp": _START_MS - 1_500, "type": "cast",
                             "abilityGameID": gd.BLOODFEST})
    casts = fetch_norm_casts(client, "code", _FIGHT, _ACTOR)
    pre = [aid for t, aid in casts if t < 0]
    assert pre.count(gd.BLOODFEST) == 1, f"doubled: {pre}"


def main() -> None:
    test_proven_prepull_is_injected()
    test_unproven_prepull_not_injected()
    test_stacked_buff_first_event_counts()
    test_entry_gauge_sees_prepull_generation()
    test_no_double_injection_when_cast_present()
    print("test_prepull_injection: all checks passed")


if __name__ == "__main__":
    main()
