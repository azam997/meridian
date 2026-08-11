"""`LocalCaptureClient` — the FFLogs-client shim over one local capture.

Plays the `FFLogsClient` role for `analyze_pull`: implements exactly the six
read methods the analyze path uses, backed by the capture's single flat event
list. Signatures are copied verbatim from `fflogs_api.py` (names AND
positional order — some callers pass `data_type` positionally).

All FFLogs *request* quirks live here, per WIRE_CONTRACT.md §2:
  * aura streams: the filter arg named like a source selects the RECIPIENT —
    payload recipient rides `targetID`;
  * death streams: the filter arg selects the actor that DIED (`targetID`);
  * `hostility="Enemies"` casts: sourced by the summary-derived enemy set;
  * bundle streams dispatch in `CachedEventsClient.prime_bundle`'s exact
    precedence so primed cache keys match what the aspects re-request.

Windows are inclusive on both ends; the aura-narrowing optimization in
`CachedEventsClient` additionally assumes `timestamp >= start` filtering,
which the inclusive window provides.
"""
from __future__ import annotations

import copy
from typing import Any

from .capture import LocalCapture

_CAST_TYPES = ("cast", "begincast")
_DAMAGE_TYPES = ("damage", "calculateddamage")
_BUFF_TYPES = ("applybuff", "applybuffstack", "refreshbuff",
               "removebuff", "removebuffstack")
_DEBUFF_TYPES = ("applydebuff", "applydebuffstack", "refreshdebuff",
                 "removedebuff", "removedebuffstack")


class LocalCaptureClient:
    def __init__(self, capture: LocalCapture):
        self._capture = capture

    # -- the six methods ---------------------------------------------------

    def get_report_summary(self, code: str) -> dict[str, Any]:
        # Deepcopy per call: analyze_pull stashes private keys (__deaths__,
        # __downtime__, …) onto the report dict — a shared dict would bleed
        # state between analyses of the same capture.
        return copy.deepcopy(self._capture.summary)

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> list[dict[str, Any]]:
        if data_type == "Casts":
            types, actor_key = _CAST_TYPES, "sourceID"
        elif data_type == "DamageDone":
            types, actor_key = _DAMAGE_TYPES, "sourceID"
        elif data_type == "Deaths":
            types, actor_key = ("death",), "targetID"
        else:
            return []
        out = [e for e in self._capture.events
               if e.get("type") in types and e.get(actor_key) == source_id
               and _in_window(e, start, end)]
        if ability_id is not None:
            out = [e for e in out if e.get("abilityGameID") == ability_id]
        return out

    def get_event_bundle(self, code: str, streams: list
                         ) -> list[list[dict[str, Any]]]:
        # Dispatch precedence mirrors CachedEventsClient.prime_bundle exactly
        # (aura → hostility → filter-expression → default) so every primed
        # cache key lands where its reader will look.
        out: list[list[dict[str, Any]]] = []
        for s in streams:
            data_type = s.data_type
            source_id = s.source_id
            if data_type in ("Buffs", "Debuffs") and source_id is not None:
                out.append(self.get_aura_events(
                    code, s.start, s.end, source_id, data_type))
            elif getattr(s, "hostility", None) is not None:
                out.append(self.get_enemy_cast_events(code, s.start, s.end))
            elif s.filter_expression is not None and source_id is None:
                out.append(self.get_targetability_events(code, s.start, s.end))
            else:
                out.append(self.get_events(
                    code, s.start, s.end, source_id,
                    data_type=data_type, ability_id=s.ability_id))
        return out

    def get_targetability_events(self, code: str, start: int,
                                 end: int) -> list[dict[str, Any]]:
        return [e for e in self._capture.events
                if e.get("type") == "targetabilityupdate"
                and _in_window(e, start, end)]

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict[str, Any]]:
        enemy_ids = self._capture.enemy_ids
        return [e for e in self._capture.events
                if e.get("type") in _CAST_TYPES
                and e.get("sourceID") in enemy_ids
                and _in_window(e, start, end)]

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict[str, Any]]:
        if data_type == "Buffs":
            types = _BUFF_TYPES
        elif data_type == "Debuffs":
            types = _DEBUFF_TYPES
        else:
            return []
        return [e for e in self._capture.events
                if e.get("type") in types and e.get("targetID") == actor_id
                and _in_window(e, start, end)]


def _in_window(event: dict[str, Any], start: int, end: int) -> bool:
    ts = event.get("timestamp")
    return ts is not None and start <= ts <= end
