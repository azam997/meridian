"""Read an ACT/FFXIV-plugin network log through the six-method client interface.

The network log is the same raw data the FFLogs uploader parses, which makes
it a local, upload-free parity oracle for the Meridian Companion collector:
`compare_local_capture.py --act-log` diffs a capture against it directly.
It is a *pre-gate* — FFLogs applies its own server-side interpretation, so
the official Phase 1 acceptance diff still runs against a real FFLogs report.

Line types consumed (pipe-delimited, cactbot-documented):
  03 AddCombatant       actor identity: id, name, owner (pets), world
  20 StartsUsing        -> begincast
  21/22 (AOE)Ability    -> cast (deduped per sequence) + calculateddamage per
                           damage-type effect pair
  24 DoTHoT             -> damage with tick:true (informational bucket)
  25 Death              -> death (victim rides targetID)
  26/30 Gains/LosesEffect -> aura events in the 1e6 status space; a re-gain
                           while active synthesizes refresh/stack variants
  34 NameToggle         -> targetabilityupdate

Effect pairs are two dwords packing the wire's 8-byte entry:
  dword1 = Param2<<24 | Param1<<16 | Param0<<8 | Type
  dword2 = Value<<16  | Param3<<8  | Param4
so the amount decode is the same arithmetic the collector uses in-game
(Value, plus Param3<<16 when Param4 has the 0x40 high-word flag; crit/direct
hit in Param0 bits 0x20/0x40).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

STATUS_ID_OFFSET = 1_000_000
_PLAYER_ID_MIN, _PLAYER_ID_MAX = 0x10000000, 0x1FFFFFFF
_INVALID_ID = 0xE0000000

_DAMAGE_TYPES = frozenset((3, 5, 6))  # Damage, BlockedDamage, ParriedDamage

_TS_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_ts_ms(text: str) -> int:
    """ACT stamps carry 7 fractional digits; fromisoformat wants <= 6."""
    from datetime import datetime

    return int(datetime.fromisoformat(_TS_FRACTION.sub(r"\1", text)).timestamp() * 1000)


def _hex(field: str) -> int:
    try:
        return int(field, 16) if field else 0
    except ValueError:
        return 0


def _decode_damage_pair(dword1: int, dword2: int) -> tuple[int, int, bool] | None:
    """(amount, hitType, directHit) for damage-type pairs, else None."""
    effect_type = dword1 & 0xFF
    if effect_type not in _DAMAGE_TYPES:
        return None
    param0 = (dword1 >> 8) & 0xFF
    param3 = (dword2 >> 8) & 0xFF
    param4 = dword2 & 0xFF
    amount = dword2 >> 16
    if param4 & 0x40:
        amount += param3 << 16
    hit_type = 2 if param0 & 0x20 else 1
    return amount, hit_type, bool(param0 & 0x40)


class ActLogClient:
    """The six read methods over one pull's window of a network log."""

    def __init__(self, path: str | Path, window_start_ms: int, window_end_ms: int,
                 *, pre_margin_ms: int = 15_000, post_margin_ms: int = 5_000):
        self.start = window_start_ms
        self.end = window_end_ms
        self._events: list[dict[str, Any]] = []
        self._actors: dict[int, dict[str, Any]] = {}
        self._owners: dict[int, int] = {}
        self._parse(Path(path), window_start_ms - pre_margin_ms, window_end_ms + post_margin_ms)
        self._events.sort(key=lambda e: e["timestamp"])
        self._npc_ids = {aid for aid, a in self._actors.items() if a["type"] == "NPC"}

    # -- parsing ---------------------------------------------------------

    def _see_actor(self, actor_id: int, name: str, world: str | None = None) -> None:
        if actor_id == 0 or actor_id >= _INVALID_ID:
            return
        actor = self._actors.setdefault(actor_id, {"name": name, "world": None})
        if name and not actor["name"]:
            actor["name"] = name
        if world:
            actor["world"] = world
        owner = self._owners.get(actor_id)
        if _PLAYER_ID_MIN <= actor_id <= _PLAYER_ID_MAX:
            actor["type"] = "Player"
        elif owner:
            actor["type"] = "Pet"
        else:
            actor["type"] = "NPC"

    def _recipient_is_enemy(self, actor_id: int) -> bool:
        return self._actors.get(actor_id, {}).get("type", "NPC") == "NPC"

    def _parse(self, path: Path, lo: int, hi: int) -> None:
        # (statusId, sourceId, targetId) -> stacks, for refresh/stack synthesis.
        active: dict[tuple[int, int, int], int] = {}
        seen_sequences: set[tuple[int, int]] = set()

        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                kind = line[:2]
                if kind not in ("03", "20", "21", "22", "24", "25", "26", "30", "34"):
                    continue
                fields = line.rstrip("\n").split("|")
                if len(fields) < 7:
                    continue
                ts = _parse_ts_ms(fields[1])
                if kind == "03":
                    # Identity lines are accepted slightly early too — the
                    # combatant list arrives on zone-in, before the window.
                    owner = _hex(fields[6])
                    actor_id = _hex(fields[2])
                    if owner and owner < _INVALID_ID:
                        self._owners[actor_id] = owner
                    self._see_actor(actor_id, fields[3], fields[8] or None)
                    continue
                if ts < lo or ts > hi:
                    continue
                if kind == "20" and len(fields) >= 8:
                    self._on_begincast(ts, fields)
                elif kind in ("21", "22") and len(fields) >= 24:
                    self._on_ability(ts, fields, seen_sequences)
                elif kind == "24" and len(fields) >= 7:
                    self._on_tick(ts, fields)
                elif kind == "25" and len(fields) >= 5:
                    self._on_death(ts, fields)
                elif kind in ("26", "30") and len(fields) >= 9:
                    self._on_aura(ts, kind, fields, active)
                elif kind == "34" and len(fields) >= 7:
                    self._on_toggle(ts, fields)

    def _on_begincast(self, ts: int, fields: list[str]) -> None:
        source = _hex(fields[2])
        target = _hex(fields[6])
        self._see_actor(source, fields[3])
        event = {"timestamp": ts, "type": "begincast", "sourceID": source,
                 "abilityGameID": _hex(fields[4])}
        if target and target < _INVALID_ID:
            self._see_actor(target, fields[7])
            event["targetID"] = target
        self._events.append(event)

    def _on_ability(self, ts: int, fields: list[str], seen: set[tuple[int, int]]) -> None:
        source = _hex(fields[2])
        ability = _hex(fields[4])
        target = _hex(fields[6])
        sequence = _hex(fields[-11]) if len(fields) >= 12 else 0
        self._see_actor(source, fields[3])
        if target and target < _INVALID_ID:
            self._see_actor(target, fields[7])

        # One cast per action use: AOE lines share a sequence, one per target.
        key = (source, sequence)
        if key not in seen:
            seen.add(key)
            cast = {"timestamp": ts, "type": "cast", "sourceID": source,
                    "abilityGameID": ability, "packetID": sequence}
            if target and target < _INVALID_ID:
                cast["targetID"] = target
            self._events.append(cast)

        if not target or target >= _INVALID_ID:
            return
        for index in range(8, 24, 2):
            if index + 1 >= len(fields):
                break
            decoded = _decode_damage_pair(_hex(fields[index]), _hex(fields[index + 1]))
            if decoded is None:
                continue
            amount, hit_type, direct = decoded
            event = {"timestamp": ts, "type": "calculateddamage", "sourceID": source,
                     "targetID": target, "abilityGameID": ability, "amount": amount,
                     "hitType": hit_type, "packetID": sequence}
            if direct:
                event["directHit"] = True
            self._events.append(event)

    def _on_tick(self, ts: int, fields: list[str]) -> None:
        target = _hex(fields[2])
        source = _hex(fields[16]) if len(fields) > 16 else 0
        self._see_actor(target, fields[3])
        self._events.append({"timestamp": ts, "type": "damage", "tick": True,
                             "sourceID": source, "targetID": target,
                             "abilityGameID": _hex(fields[5]),
                             "amount": _hex(fields[6])})

    def _on_death(self, ts: int, fields: list[str]) -> None:
        victim = _hex(fields[2])
        self._see_actor(victim, fields[3])
        self._events.append({"timestamp": ts, "type": "death", "targetID": victim,
                             "sourceID": _hex(fields[4]) or -1})

    def _on_aura(self, ts: int, kind: str, fields: list[str],
                 active: dict[tuple[int, int, int], int]) -> None:
        status = _hex(fields[2])
        source = _hex(fields[5])
        target = _hex(fields[7]) if len(fields) > 8 else 0
        stacks = _hex(fields[9]) if len(fields) > 9 else 0
        # Status 0 lines are plugin bookkeeping, not auras.
        if not status or not target or target >= _INVALID_ID:
            return
        self._see_actor(source, fields[6])
        self._see_actor(target, fields[8])
        enemy = self._recipient_is_enemy(target)
        key = (status, source, target)

        if kind == "30":
            active.pop(key, None)
            variant = "removedebuff" if enemy else "removebuff"
        elif key not in active:
            active[key] = stacks
            variant = "applydebuff" if enemy else "applybuff"
        elif stacks > active[key] > 0 or (stacks > 0 and active[key] == 0):
            active[key] = stacks
            variant = "applydebuffstack" if enemy else "applybuffstack"
        elif 0 < stacks < active[key]:
            active[key] = stacks
            variant = "removedebuffstack" if enemy else "removebuffstack"
        else:
            active[key] = stacks
            variant = "refreshdebuff" if enemy else "refreshbuff"

        event = {"timestamp": ts, "type": variant, "sourceID": source or target,
                 "targetID": target, "abilityGameID": STATUS_ID_OFFSET + status}
        if stacks and variant.endswith("stack"):
            event["stacks"] = stacks
        self._events.append(event)

    def _on_toggle(self, ts: int, fields: list[str]) -> None:
        target = _hex(fields[2])
        self._see_actor(target, fields[3])
        # The collector (and the analyzer's downtime evidence) only track
        # enemy targetability; pet/minion NameToggles are summoning noise.
        if self._recipient_is_enemy(target):
            self._events.append({"timestamp": ts, "type": "targetabilityupdate",
                                 "sourceID": target, "targetID": target,
                                 "targetable": 1 if _hex(fields[6]) else 0})

    # -- the six methods -------------------------------------------------

    def get_report_summary(self, code: str) -> dict[str, Any]:
        actors = [{"id": -1, "name": "Environment", "server": None, "type": "NPC",
                   "subType": "NPC", "petOwner": None, "gameID": 0}]
        friendly: list[int] = []
        enemy_npcs: list[dict[str, Any]] = []
        for actor_id, actor in sorted(self._actors.items()):
            entry = {"id": actor_id, "name": actor["name"], "server": actor.get("world"),
                     "type": actor["type"],
                     "subType": "Pet" if actor["type"] == "Pet" else "Unknown",
                     "petOwner": self._owners.get(actor_id), "gameID": 0}
            actors.append(entry)
            if actor["type"] == "Player":
                friendly.append(actor_id)
            elif actor["type"] == "NPC":
                enemy_npcs.append({"id": actor_id, "gameID": None, "petOwner": None})
        return {
            "title": "ACT network log",
            "startTime": self.start,
            "endTime": self.end,
            "fights": [{
                "id": 1, "name": "ACT window", "encounterID": None, "difficulty": None,
                "kill": None, "startTime": self.start, "endTime": self.end,
                "friendlyPlayers": friendly, "fightPercentage": None,
                "bossPercentage": None, "lastPhase": None,
                "lastPhaseIsIntermission": None, "phaseTransitions": None,
                "enemyNPCs": enemy_npcs,
            }],
            "masterData": {"actors": actors, "abilities": []},
            "phases": [],
        }

    def _window(self, start: int, end: int, types: tuple[str, ...]) -> list[dict[str, Any]]:
        return [e for e in self._events
                if start <= e["timestamp"] <= end and e["type"] in types]

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict[str, Any]]:
        if data_type == "Casts":
            events = [e for e in self._window(start, end, ("cast", "begincast"))
                      if e.get("sourceID") == source_id]
        elif data_type == "DamageDone":
            events = [e for e in self._window(start, end, ("calculateddamage", "damage"))
                      if e.get("sourceID") == source_id]
        elif data_type == "Deaths":
            events = [e for e in self._window(start, end, ("death",))
                      if e.get("targetID") == source_id]
        else:
            return []
        if ability_id is not None:
            events = [e for e in events if e.get("abilityGameID") == ability_id]
        return events

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict[str, Any]]:
        if data_type == "Buffs":
            types = ("applybuff", "applybuffstack", "refreshbuff", "removebuff", "removebuffstack")
        else:
            types = ("applydebuff", "applydebuffstack", "refreshdebuff", "removedebuff", "removedebuffstack")
        return [e for e in self._window(start, end, types) if e.get("targetID") == actor_id]

    def get_targetability_events(self, code: str, start: int, end: int) -> list[dict[str, Any]]:
        return self._window(start, end, ("targetabilityupdate",))

    def get_enemy_cast_events(self, code: str, start: int, end: int) -> list[dict[str, Any]]:
        return [e for e in self._window(start, end, ("cast", "begincast"))
                if e.get("sourceID") in self._npc_ids]

    def get_event_bundle(self, code: str, streams: list[Any]) -> list[list[dict[str, Any]]]:
        out: list[list[dict[str, Any]]] = []
        for stream in streams:
            if stream.data_type in ("Buffs", "Debuffs") and stream.source_id is not None:
                out.append(self.get_aura_events(code, stream.start, stream.end,
                                                stream.source_id, stream.data_type))
            elif getattr(stream, "hostility", None) is not None:
                out.append(self.get_enemy_cast_events(code, stream.start, stream.end))
            elif getattr(stream, "filter_expression", None) and stream.source_id is None:
                out.append(self.get_targetability_events(code, stream.start, stream.end))
            else:
                out.append(self.get_events(code, stream.start, stream.end,
                                           stream.source_id, stream.data_type,
                                           stream.ability_id))
        return out
