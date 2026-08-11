"""FFLogs → wire-format export tooling.

Three pieces, shared by the replay test and the fixture-minting script:

* `RecordingClient` — transparent proxy that records every call to the six
  analyze-path read methods as ``{method, args, response}``.
* `ReplayClient` — serves a recording back verbatim (exact-args lookup); the
  "run 1" client for a committed recording fixture.
* `responses_to_wire` / `serialize_ndjson` / `verify` — build a wire capture
  from a recording, and prove the round trip: every recorded response must be
  reproduced exactly by a `LocalCaptureClient` over the built capture. That
  `verify` step is the mint-time oracle that pins the contract's filter
  predicates against real FFLogs payloads.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from .capture import CONTRACT_VERSION, LocalCapture
from .client import LocalCaptureClient

_STREAM_FIELDS = ("data_type", "start", "end", "source_id", "ability_id",
                  "filter_expression", "hostility", "include_resources")


def _stream_to_dict(stream: Any) -> dict[str, Any]:
    return {f: getattr(stream, f, None) if f != "include_resources"
            else bool(getattr(stream, f, False))
            for f in _STREAM_FIELDS}


class RecordingClient:
    """Wraps any FFLogs-shaped client and records the six read methods.
    Everything else passes through untouched."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.recording: list[dict[str, Any]] = []

    def _record(self, method: str, args: dict[str, Any], response: Any) -> Any:
        self.recording.append({
            "method": method,
            "args": args,
            # Deepcopy at record time: callers mutate returned lists/dicts.
            "response": copy.deepcopy(response),
        })
        return response

    def get_report_summary(self, code: str) -> dict[str, Any]:
        return self._record("get_report_summary", {"code": code},
                            self._inner.get_report_summary(code))

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> list[dict[str, Any]]:
        return self._record(
            "get_events",
            {"code": code, "start": start, "end": end, "source_id": source_id,
             "data_type": data_type, "ability_id": ability_id},
            self._inner.get_events(code, start, end, source_id,
                                   data_type=data_type, ability_id=ability_id))

    def get_event_bundle(self, code: str, streams: list) -> list[list[dict]]:
        return self._record(
            "get_event_bundle",
            {"code": code, "streams": [_stream_to_dict(s) for s in streams]},
            self._inner.get_event_bundle(code, streams))

    def get_targetability_events(self, code: str, start: int,
                                 end: int) -> list[dict[str, Any]]:
        return self._record(
            "get_targetability_events",
            {"code": code, "start": start, "end": end},
            self._inner.get_targetability_events(code, start, end))

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict[str, Any]]:
        return self._record(
            "get_enemy_cast_events",
            {"code": code, "start": start, "end": end},
            self._inner.get_enemy_cast_events(code, start, end))

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict[str, Any]]:
        return self._record(
            "get_aura_events",
            {"code": code, "start": start, "end": end, "actor_id": actor_id,
             "data_type": data_type},
            self._inner.get_aura_events(code, start, end, actor_id,
                                        data_type=data_type))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ReplayClient:
    """Serves a recording back by exact-args lookup. A call the recording has
    never seen raises KeyError — replay must be a faithful re-run."""

    def __init__(self, recording: list[dict[str, Any]]):
        self._responses: dict[tuple[str, str], Any] = {}
        for rec in recording:
            self._responses[_call_key(rec["method"], rec["args"])] = rec["response"]

    def _serve(self, method: str, args: dict[str, Any]) -> Any:
        key = _call_key(method, args)
        if key not in self._responses:
            raise KeyError(f"no recorded response for {method} {args!r}")
        return copy.deepcopy(self._responses[key])

    def get_report_summary(self, code: str) -> dict[str, Any]:
        return self._serve("get_report_summary", {"code": code})

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> list[dict[str, Any]]:
        return self._serve("get_events", {
            "code": code, "start": start, "end": end, "source_id": source_id,
            "data_type": data_type, "ability_id": ability_id})

    def get_event_bundle(self, code: str, streams: list) -> list[list[dict]]:
        return self._serve("get_event_bundle", {
            "code": code, "streams": [_stream_to_dict(s) for s in streams]})

    def get_targetability_events(self, code: str, start: int,
                                 end: int) -> list[dict[str, Any]]:
        return self._serve("get_targetability_events",
                           {"code": code, "start": start, "end": end})

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict[str, Any]]:
        return self._serve("get_enemy_cast_events",
                           {"code": code, "start": start, "end": end})

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict[str, Any]]:
        return self._serve("get_aura_events", {
            "code": code, "start": start, "end": end, "actor_id": actor_id,
            "data_type": data_type})


def _call_key(method: str, args: dict[str, Any]) -> tuple[str, str]:
    return method, json.dumps(args, sort_keys=True, default=str)


# --- recording → wire records ------------------------------------------------

def _classify(method: str, args: dict[str, Any]) -> tuple | None:
    """Map a recorded call to its stream bucket (window excluded). Returns
    None for get_report_summary. Bucket keys share the space used by
    `_decompose_bundle`."""
    if method == "get_events":
        return ("events", args["source_id"], args["data_type"],
                args.get("ability_id"))
    if method == "get_targetability_events":
        return ("targetability",)
    if method == "get_enemy_cast_events":
        return ("enemy_casts",)
    if method == "get_aura_events":
        return ("aura", args["actor_id"], args["data_type"])
    return None


def _decompose_bundle(args: dict[str, Any], response: list[list[dict]]
                      ) -> list[tuple[tuple, int, int, list[dict]]]:
    """Split a bundle call into per-stream (bucket, start, end, events),
    dispatching in prime_bundle's precedence — identical to
    `LocalCaptureClient.get_event_bundle`."""
    out = []
    for stream, evs in zip(args["streams"], response):
        data_type = stream["data_type"]
        source_id = stream["source_id"]
        if data_type in ("Buffs", "Debuffs") and source_id is not None:
            bucket = ("aura", source_id, data_type)
        elif stream.get("hostility") is not None:
            bucket = ("enemy_casts",)
        elif stream.get("filter_expression") is not None and source_id is None:
            bucket = ("targetability",)
        else:
            bucket = ("events", source_id, data_type, stream.get("ability_id"))
        out.append((bucket, stream["start"], stream["end"], evs))
    return out


def _event_key(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, default=str)


def responses_to_wire(recording: list[dict[str, Any]], *,
                      capture_id: str = "replay",
                      generator: str = "local_capture.export",
                      ) -> list[dict[str, Any]]:
    """Build the wire records (meta + summary + events + end) reproducing a
    recorded analyze_pull run.

    Per bucket the widest-window response is taken as the stream's truth;
    every narrower recorded response must equal the widest filtered to its
    window (else the recording cannot be represented as one flat list and we
    fail loudly). Events are deduped ACROSS buckets by content with
    per-bucket max multiplicity (a duplicate within one stream is real and
    kept; the same event served to two streams must appear once, because the
    adapter re-filters the flat list per stream). The flat list is stable-
    sorted by timestamp — each source stream is ascending, so stability
    preserves every stream's internal order.
    """
    summary: dict[str, Any] | None = None
    # bucket -> list of (start, end, events)
    buckets: dict[tuple, list[tuple[int, int, list[dict]]]] = {}

    for rec in recording:
        method, args, response = rec["method"], rec["args"], rec["response"]
        if method == "get_report_summary":
            summary = response  # last wins, mirroring the wire rule
            continue
        if method == "get_event_bundle":
            for bucket, start, end, evs in _decompose_bundle(args, response):
                buckets.setdefault(bucket, []).append((start, end, evs))
            continue
        bucket = _classify(method, args)
        if bucket is None:
            continue
        buckets.setdefault(bucket, []).append(
            (args["start"], args["end"], response))

    if summary is None:
        raise ValueError("recording contains no get_report_summary call")

    emitted: Counter[str] = Counter()
    flat: list[dict[str, Any]] = []
    for bucket in sorted(buckets, key=repr):
        entries = buckets[bucket]
        widest_start = min(s for s, _, _ in entries)
        widest_end = max(e for _, e, _ in entries)
        widest = next(((s, e, evs) for s, e, evs in entries
                       if s == widest_start and e == widest_end), None)
        if widest is None:
            raise ValueError(
                f"stream {bucket!r}: no single recorded window spans all "
                f"requests ([{widest_start}, {widest_end}] needed) — cannot "
                f"represent as one flat capture")
        _, _, widest_evs = widest
        for start, end, evs in entries:
            filtered = [ev for ev in widest_evs
                        if ev.get("timestamp") is not None
                        and start <= ev["timestamp"] <= end]
            if filtered != evs:
                raise ValueError(
                    f"stream {bucket!r}: response for window [{start}, {end}] "
                    f"is not the widest window's time-filtered subset "
                    f"({len(evs)} vs {len(filtered)} events) — recording is "
                    f"not representable as one flat capture")
        seen: Counter[str] = Counter()
        for ev in widest_evs:
            key = _event_key(ev)
            seen[key] += 1
            if seen[key] > emitted[key]:
                flat.append(copy.deepcopy(ev))
                emitted[key] += 1

    flat.sort(key=lambda e: e.get("timestamp", 0))

    summary = {k: v for k, v in summary.items() if k != "kind"}
    fights = summary.get("fights") or []
    last_fight = fights[-1] if fights else {}
    kill = last_fight.get("kill")
    outcome = "kill" if kill is True else ("wipe" if kill is False else "abort")
    end_time = max((f.get("endTime", 0) for f in fights), default=0)

    records: list[dict[str, Any]] = [
        {"kind": "meta", "contractVersion": CONTRACT_VERSION,
         "captureId": capture_id, "generator": generator},
        {"kind": "summary", **summary},
    ]
    records.extend({"kind": "event", **ev} for ev in flat)
    records.append({"kind": "end", "endTime": end_time, "outcome": outcome})
    return records


def serialize_ndjson(records: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r, separators=(",", ":"), default=str)
                     for r in records) + "\n"


def verify(recording: list[dict[str, Any]], capture: LocalCapture) -> int:
    """Replay every recorded call through a `LocalCaptureClient` over
    ``capture`` and require exact equality with the recorded response.
    Returns the number of calls checked; raises ValueError on the first
    divergence. This is the empirical proof that the contract's filter
    predicates reproduce FFLogs' stream semantics for this pull."""
    client = LocalCaptureClient(capture)
    checked = 0
    for rec in recording:
        method, args, expected = rec["method"], rec["args"], rec["response"]
        if method == "get_report_summary":
            actual: Any = client.get_report_summary(args["code"])
            expected_summary = {k: v for k, v in expected.items()
                                if k != "kind"}
            if actual != expected_summary:
                raise ValueError("get_report_summary diverges from recording")
            checked += 1
            continue
        if method == "get_event_bundle":
            streams = [SimpleNamespace(**s) for s in args["streams"]]
            actual = client.get_event_bundle(args["code"], streams)
        else:
            call_args = {k: v for k, v in args.items() if k != "code"}
            actual = getattr(client, method)(args["code"], **call_args)
        if actual != expected:
            detail = ""
            if isinstance(actual, list) and isinstance(expected, list):
                detail = f" ({len(actual)} vs {len(expected)} entries)"
            raise ValueError(
                f"{method} {args!r} diverges from recording{detail}")
        checked += 1
    return checked
