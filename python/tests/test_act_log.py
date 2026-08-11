"""ActLogClient parses a synthetic network-log slice into client-served events."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_capture.act_log import ActLogClient  # noqa: E402

_BASE = "2026-07-27T15:00:{:02d}.0000000-04:00"


def _ts_ms(second: int) -> int:
    return int(datetime.fromisoformat(_BASE.format(second).replace("0000000", "000000")).timestamp() * 1000)


def _ability_line(kind: str, second: int, source: str, source_name: str, ability: str,
                  target: str, target_name: str, pair1: tuple[str, str], sequence: str) -> str:
    fields = [kind, _BASE.format(second), source, source_name, ability, "Test Ability",
              target, target_name, pair1[0], pair1[1]] + ["0"] * 14
    # Trailing block: hp/pos padding, then sequence|targetIndex|targetCount|…|hash.
    fields += ["1000", "1000", "10000", "10000", "", "", "0", "0", "0", "0",
               "1000", "1000", "10000", "10000", "", "", "0", "0", "0", "0",
               sequence, "0", "2", "00", "", "01", ability, ability, "0.600", "FFFF", "hash"]
    return "|".join(fields)


def _build_log() -> str:
    # Damage pair: type 3 (damage), Param0 0x60 (crit + direct hit),
    # amount 70 000 via the high-word rule: Value 0x1170, P3 1, P4 0x40.
    damage_pair = ("6003", "11700140")
    lines = [
        # Identity: one player, their pet (owner set), one NPC.
        "03|" + _BASE.format(0) + "|10001234|Test Player|21|64|00|1D|Hyperion|0|0|50000|50000|10000|10000|||0|0|0|0|hash",
        "03|" + _BASE.format(0) + "|40001111|Test Carbuncle|00|50|10001234|00||1398|1008|29184|29184|10000|10000|||0|0|0|0|hash",
        "03|" + _BASE.format(0) + "|40002222|Test Boss|00|64|00|00||9999|8888|9000000|9000000|10000|10000|||0|0|0|0|hash",
        # begincast, then a two-target AOE sharing one sequence.
        "20|" + _BASE.format(1) + "|10001234|Test Player|1D89|Test Cast|40002222|Test Boss|2.500|0|0|0|0|hash",
        _ability_line("22", 3, "10001234", "Test Player", "1D89", "40002222", "Test Boss", damage_pair, "0000AAAA"),
        _ability_line("22", 3, "10001234", "Test Player", "1D89", "40001111", "Test Carbuncle", damage_pair, "0000AAAA"),
        # Aura on the player: gain → re-gain (refresh) → lose; status 0 noise skipped.
        "26|" + _BASE.format(4) + "|4C4|Test Buff|30.00|10001234|Test Player|10001234|Test Player|00|50000|50000|hash",
        "26|" + _BASE.format(6) + "|4C4|Test Buff|30.00|10001234|Test Player|10001234|Test Player|00|50000|50000|hash",
        "30|" + _BASE.format(8) + "|4C4|Test Buff|0.00|10001234|Test Player|10001234|Test Player|00|50000|50000|hash",
        "26|" + _BASE.format(5) + "|0|Bookkeeping|0.00|10001234|Test Player|10001234|Test Player|00|50000|50000|hash",
        # Targetability: boss toggle counts, pet toggle is summoning noise.
        "34|" + _BASE.format(7) + "|40002222|Test Boss|40002222|Test Boss|00|hash",
        "34|" + _BASE.format(7) + "|40001111|Test Carbuncle|40001111|Test Carbuncle|01|hash",
        # Death, and an out-of-window event that must be excluded.
        "25|" + _BASE.format(9) + "|10001234|Test Player|40002222|Test Boss|hash",
        "21|" + _BASE.format(59) + "|10001234|Test Player|1D89|Test Ability|40002222|Test Boss|"
        + "|".join(["0"] * 16) + "|0000BBBB|0|1|00||01|1D89|1D89|0.600|FFFF|hash",
    ]
    return "\n".join(lines) + "\n"


def test_act_log_client(tmp_path: Path) -> None:
    log = tmp_path / "Network_test.log"
    log.write_text(_build_log(), encoding="utf-8")
    start, end = _ts_ms(0), _ts_ms(20)
    client = ActLogClient(log, start, end, pre_margin_ms=0, post_margin_ms=0)

    summary = client.get_report_summary("act")
    actors = {a["name"]: a for a in summary["masterData"]["actors"]}
    assert actors["Test Player"]["type"] == "Player"
    assert actors["Test Carbuncle"]["type"] == "Pet"
    assert actors["Test Carbuncle"]["petOwner"] == 0x10001234
    assert actors["Test Boss"]["type"] == "NPC"
    assert summary["fights"][0]["friendlyPlayers"] == [0x10001234]

    casts = client.get_events("act", start, end, 0x10001234, "Casts")
    assert [e["type"] for e in casts] == ["begincast", "cast"]
    assert all(e["abilityGameID"] == 0x1D89 for e in casts)

    damage = client.get_events("act", start, end, 0x10001234, "DamageDone")
    assert len(damage) == 2  # one AOE sequence, two targets, ONE cast
    assert all(e["amount"] == 70_000 for e in damage)
    assert all(e["hitType"] == 2 and e["directHit"] for e in damage)
    assert {e["targetID"] for e in damage} == {0x40002222, 0x40001111}
    assert len({e["packetID"] for e in damage}) == 1

    buffs = sorted(client.get_aura_events("act", start, end, 0x10001234, "Buffs"),
                   key=lambda e: e["timestamp"])
    assert [e["type"] for e in buffs] == ["applybuff", "refreshbuff", "removebuff"]
    assert all(e["abilityGameID"] == 1_000_000 + 0x4C4 for e in buffs)  # status-0 line skipped

    toggles = client.get_targetability_events("act", start, end)
    assert len(toggles) == 1  # pet toggle skipped
    assert toggles[0]["targetID"] == 0x40002222
    assert toggles[0]["targetable"] == 0

    deaths = client.get_events("act", start, end, 0x10001234, "Deaths")
    assert len(deaths) == 1 and deaths[0]["targetID"] == 0x10001234

    # The second-59 cast is outside the window on every stream.
    assert len(client.get_events("act", start, end, 0x10001234, "Casts")) == 2

    enemy = client.get_enemy_cast_events("act", start, end)
    assert enemy == []


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_act_log_client(Path(tmp))
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
