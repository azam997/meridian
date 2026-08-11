"""Local-capture wire format: parser + in-memory model.

The normative contract is WIRE_CONTRACT.md at the repo root (mirrored into the
Meridian Companion repo). A capture is an NDJSON stream of records — one
optional ``meta`` line, one-or-more ``summary`` records (last wins), many
``event`` records, and a terminal ``end`` record. This module parses that
stream into a `LocalCapture`; `local_capture.client.LocalCaptureClient` then
serves the six FFLogs-client read methods from it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

CONTRACT_VERSION = 1

# Wire fields that are integers on the contract. C#/JS writers can emit
# integral floats (5.0); coerce those, reject non-integral values. Percentage
# fields (fightPercentage/bossPercentage) are legitimately fractional and are
# deliberately absent.
_INT_KEYS = frozenset({
    "timestamp", "sourceID", "targetID", "abilityGameID", "amount",
    "packetID", "stacks", "sourceInstance", "targetInstance", "targetable",
    "id", "gameID", "petOwner", "startTime", "endTime", "encounterID",
    "difficulty", "lastPhase", "contractVersion",
})


class CaptureError(ValueError):
    """Malformed or invalid capture."""


class CaptureIncompleteError(CaptureError):
    """Capture has no terminal ``end`` record (truncated / crashed writer)."""


@dataclass
class LocalCapture:
    """One parsed capture: the effective summary, the flat event list (wire
    order preserved), the terminal record, and parse bookkeeping."""
    summary: dict[str, Any]
    events: list[dict[str, Any]]
    end: dict[str, Any] | None
    meta: dict[str, Any] | None
    ignored_kinds: int = 0
    records_after_end: int = 0
    enemy_ids: frozenset[int] = field(init=False)

    def __post_init__(self) -> None:
        self.enemy_ids = _enemy_ids(self.summary)


def _enemy_ids(summary: dict[str, Any]) -> frozenset[int]:
    """Hostile actor ids: NPC-typed actors ∪ every fight's enemyNPCs.
    (WIRE_CONTRACT.md §2 — the `get_enemy_cast_events` source set.)"""
    ids: set[int] = set()
    actors = ((summary.get("masterData") or {}).get("actors")) or []
    for a in actors:
        if a.get("type") == "NPC" and a.get("id") is not None:
            ids.add(a["id"])
    for f in summary.get("fights") or []:
        for npc in f.get("enemyNPCs") or []:
            if npc.get("id") is not None:
                ids.add(npc["id"])
    return frozenset(ids)


def _strip_kind(rec: dict[str, Any]) -> dict[str, Any]:
    rec.pop("kind", None)
    return rec


def _coerce_ints(obj: Any) -> Any:
    """Recursively coerce integral floats to int on known int-valued keys.
    Non-integral values on those keys are contract violations."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _INT_KEYS and isinstance(v, float):
                if v.is_integer():
                    v = int(v)
                else:
                    raise CaptureError(
                        f"field {k!r} must be an integer, got {v!r}")
            else:
                v = _coerce_ints(v)
            out[k] = v
        return out
    if isinstance(obj, list):
        return [_coerce_ints(x) for x in obj]
    return obj


def _validate_summary(summary: dict[str, Any]) -> None:
    fights = summary.get("fights")
    if not isinstance(fights, list) or not fights:
        raise CaptureError("summary record has no fights")
    for f in fights:
        kill = f.get("kill")
        if kill is not None and not isinstance(kill, bool):
            # The analyzer's wipe gate is `fight.get("kill") is False` — an
            # identity check. A 0/1 here would silently score a wipe as a
            # kill, so reject it at the boundary.
            raise CaptureError(
                f"fights[].kill must be a JSON boolean or null, got {kill!r}")
    if not ((summary.get("masterData") or {}).get("actors")):
        raise CaptureError("summary record has no masterData.actors")


def parse_capture_lines(lines: Iterable[str], *,
                        allow_partial: bool = False) -> LocalCapture:
    """Parse NDJSON lines into a `LocalCapture`.

    Edge-case behavior (contract §1): unknown kinds ignored+counted; BOM and
    blank lines tolerated; malformed JSON is an error with the line number;
    re-emitted summary → last wins; records after `end` ignored+counted;
    missing `end` raises `CaptureIncompleteError` unless ``allow_partial``;
    missing `summary` is always an error; absent `meta` means version 1.
    """
    meta: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    end: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    ignored = 0
    after_end = 0

    for lineno, raw in enumerate(lines, 1):
        line = raw.lstrip("﻿") if lineno == 1 else raw
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"line {lineno}: malformed JSON ({exc})") from exc
        if not isinstance(rec, dict):
            raise CaptureError(f"line {lineno}: record is not a JSON object")
        kind = rec.get("kind")
        if end is not None:
            # `end` is terminal — everything after it is ignored but counted
            # so a misbehaving writer is visible.
            after_end += 1
            continue
        if kind == "meta":
            version = rec.get("contractVersion", CONTRACT_VERSION)
            if isinstance(version, float) and version.is_integer():
                version = int(version)
            if version != CONTRACT_VERSION:
                raise CaptureError(
                    f"line {lineno}: contractVersion {version!r} is not "
                    f"supported (this reader speaks {CONTRACT_VERSION})")
            if meta is None:
                meta = _strip_kind(_coerce_ints(rec))
            else:
                ignored += 1
        elif kind == "summary":
            summary = _coerce_ints(rec)
        elif kind == "event":
            # The wire's `kind` is framing, not event data — the analyzer
            # must see the FFLogs event shape verbatim.
            events.append(_strip_kind(_coerce_ints(rec)))
        elif kind == "end":
            end = _strip_kind(_coerce_ints(rec))
        else:
            ignored += 1

    if summary is None:
        raise CaptureError("capture has no summary record")
    _validate_summary(summary)
    if end is None and not allow_partial:
        raise CaptureIncompleteError(
            "capture has no terminal end record (truncated capture); "
            "pass allow_partial=True to read it anyway")

    summary = dict(summary)
    summary.pop("kind", None)
    return LocalCapture(summary=summary, events=events, end=end, meta=meta,
                        ignored_kinds=ignored, records_after_end=after_end)


def parse_capture_text(text: str, *, allow_partial: bool = False) -> LocalCapture:
    return parse_capture_lines(text.splitlines(), allow_partial=allow_partial)
