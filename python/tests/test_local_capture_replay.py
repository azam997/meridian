"""Local-capture replay test — the wire contract's byte-identity oracle.

Proves the Milestone-0 reuse path of LIVE_LOCAL_CAPTURE_SPEC.md: an
`analyze_pull` run served by a `LocalCaptureClient` over a wire-format
capture produces the exact `_build_response` payload of the same run served
by an FFLogs-shaped client — because the same events flow through the same
pipeline (WIRE_CONTRACT.md §5).

Dual-run design (no committed snapshot): run 1 drives the pipeline over a
fixture-backed client wrapped in a `RecordingClient`; the recording is
converted to wire NDJSON, re-parsed, and run 2 drives the identical pipeline
over `LocalCaptureClient`. The two responses must match byte-for-byte after
the same normalization the contract snapshot uses.

Run-symmetry note: run 1's client implements ALL SIX read methods
(type-aware, empty where the fixture has no stream) — never the 4-method
snapshot stub. With the stub, run 1 would take the AttributeError-degradation
paths (prime_bundle swallowed, enemy-cast fetch raising into try/except)
while run 2 returns clean empties, and empty-vs-raised is not guaranteed to
converge (Tier-A downtime source selection branches differently).

Tiers:
  * Tier 1 — existing MCH fixtures (casts + targetability); proves the
    plumbing with zero new committed bytes.
  * Tier 2 — a full-stream recorded fixture (casts/damage/auras/deaths/enemy
    casts/bundles) minted by scripts/gen_local_capture_fixture.py from the
    dev disk cache; proves the aura/damage/bundle predicates against real
    FFLogs payloads. Skipped with a notice until the fixture is committed.

Run from python/:
    python tests/test_local_capture_replay.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_capture import (  # noqa: E402
    CaptureError,
    CaptureIncompleteError,
    LocalCaptureClient,
    RecordingClient,
    ReplayClient,
    parse_capture_text,
    responses_to_wire,
    serialize_ndjson,
    verify,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LOCAL_CAPTURE_FIXTURES_DIR = FIXTURES_DIR / "local_capture"

# Existing recorded MCH fixtures (casts + targetability) for Tier 1.
TIER1_FIXTURES = ["topq_1", "vamp_fatale_topq_1"]


# --- Tier 1 fixture client ---------------------------------------------------

class SixMethodFixtureClient:
    """Type-aware FFLogs-shaped client over one recorded cast fixture.

    Implements the full six-method surface so both oracle runs are
    structurally symmetric: streams the fixture lacks return [] (exactly what
    `LocalCaptureClient` serves for them), never AttributeError."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._casts = fixture["cast_events"]
        self._targetability = fixture.get("targetability_events") or []

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        npc_actors = f.get("master_npc_actors") or [
            {"id": 9001, "name": "TestBoss", "type": "NPC",
             "subType": "Boss", "petOwner": None, "gameID": 9001},
        ]
        enemy_npcs = f.get("enemy_npcs") or [{"id": 9001, "gameID": 9001,
                                              "petOwner": None}]
        fa = f.get("friendly_actors") or []
        other_players = [{
            "id": a["id"], "name": a.get("name"), "server": "TestServer",
            "type": "Player", "subType": a.get("subType"),
            "petOwner": None, "gameID": 0,
        } for a in fa if a["id"] != f["source_id"]]
        friendly_ids = [f["source_id"]] + [a["id"] for a in other_players]
        return {
            "title": f.get("label", "Replay fixture"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Replay Fight",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": friendly_ids,
                "enemyNPCs": enemy_npcs,
            }],
            "masterData": {
                "actors": [
                    {
                        "id": f["source_id"],
                        "name": f.get("label", "Replay Player"),
                        "server": "TestServer",
                        "type": "Player",
                        "subType": "Machinist",
                        "petOwner": None,
                        "gameID": 31,
                    },
                    *other_players,
                    *npc_actors,
                ],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> list[dict]:
        if data_type != "Casts":
            return []
        out = [e for e in self._casts
               if e.get("sourceID") == source_id
               and start <= e.get("timestamp", 0) <= end]
        if ability_id is not None:
            out = [e for e in out if e.get("abilityGameID") == ability_id]
        return out

    def get_event_bundle(self, code: str, streams: list) -> list[list[dict]]:
        out: list[list[dict]] = []
        for s in streams:
            if s.data_type in ("Buffs", "Debuffs") and s.source_id is not None:
                out.append(self.get_aura_events(code, s.start, s.end,
                                                s.source_id, s.data_type))
            elif getattr(s, "hostility", None) is not None:
                out.append(self.get_enemy_cast_events(code, s.start, s.end))
            elif s.filter_expression is not None and s.source_id is None:
                out.append(self.get_targetability_events(code, s.start, s.end))
            else:
                out.append(self.get_events(code, s.start, s.end, s.source_id,
                                           data_type=s.data_type,
                                           ability_id=s.ability_id))
        return out

    def get_targetability_events(self, code: str, start: int,
                                 end: int) -> list[dict]:
        return [e for e in self._targetability
                if start <= e.get("timestamp", 0) <= end]

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict]:
        return []

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict]:
        return []


# --- pipeline drive + normalization (contract-snapshot idiom) ---------------

def _run_pipeline(job: str, client: Any, code: str, fight_id: int,
                  ranking_name: str | None = None) -> dict:
    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(job, client, code, fight_id,
                       ranking_name=ranking_name, label="You")
    comparisons = _compare_all_aspects(job, you, [])
    return _build_response(job, you, [], comparisons)


def _normalize(obj: Any) -> Any:
    """Same normalization as test_contract_snapshot: floats to 2 dp (absorbs
    simulator last-bit nondeterminism), int dict keys stringified."""
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, dict):
        return {str(k) if isinstance(k, int) else k: _normalize(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


def _canonical(response: dict) -> str:
    return json.dumps(_normalize(response), sort_keys=True, default=str)


def _assert_identical(resp_fflogs: dict, resp_local: dict, label: str) -> None:
    a, b = _canonical(resp_fflogs), _canonical(resp_local)
    if a == b:
        return
    na, nb = _normalize(resp_fflogs), _normalize(resp_local)
    drift = []
    for k in sorted(set(na) | set(nb)):
        ka = json.dumps(na.get(k), sort_keys=True, default=str)
        kb = json.dumps(nb.get(k), sort_keys=True, default=str)
        if ka != kb:
            drift.append(f"{k} ({len(ka)} vs {len(kb)} chars)")
    raise AssertionError(
        f"{label}: local-capture response diverges from the FFLogs-sourced "
        f"run in top-level keys: {', '.join(drift[:5]) or '<ordering only>'}")


def _replay_roundtrip(job: str, run1_client: Any, code: str, fight_id: int,
                      label: str, ranking_name: str | None = None) -> None:
    """The oracle: run 1 (recorded) vs run 2 (wire round-trip)."""
    recorder = RecordingClient(run1_client)
    resp1 = _run_pipeline(job, recorder, code, fight_id, ranking_name)

    records = responses_to_wire(recorder.recording, capture_id=label)
    text = serialize_ndjson(records)

    capture = parse_capture_text(text)
    verify(recorder.recording, capture)

    resp2 = _run_pipeline(job, LocalCaptureClient(parse_capture_text(text)),
                          code, fight_id, ranking_name)
    _assert_identical(resp1, resp2, label)


# --- Tier 1 ------------------------------------------------------------------

@pytest.mark.parametrize("name", TIER1_FIXTURES)
def test_tier1_replay_matches_fflogs_run(name: str) -> None:
    fixture = json.loads(
        (FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    _replay_roundtrip("Machinist", SixMethodFixtureClient(fixture),
                      fixture["report_code"], fixture["fight_id"],
                      label=f"tier1:{name}")


# --- Tier 2 ------------------------------------------------------------------

def _tier2_recordings() -> list[Path]:
    if not LOCAL_CAPTURE_FIXTURES_DIR.is_dir():
        return []
    return sorted(LOCAL_CAPTURE_FIXTURES_DIR.glob("*.recording.json"))


@pytest.mark.slow
@pytest.mark.parametrize("path", _tier2_recordings() or
                         [pytest.param(None, id="no-fixture")])
def test_tier2_full_stream_replay(path: Path | None) -> None:
    if path is None:
        pytest.skip("no local_capture recording fixture committed yet "
                    "(mint with scripts/gen_local_capture_fixture.py)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _replay_roundtrip(payload["job"], ReplayClient(payload["recording"]),
                      payload["code"], payload["fight_id"],
                      label=f"tier2:{path.stem}",
                      ranking_name=payload.get("player_name"))


# --- LocalCaptureClient predicate unit tests ---------------------------------

def _mini_summary() -> dict:
    return {
        "kind": "summary",
        "title": "mini", "startTime": 0, "endTime": 10000,
        "fights": [{
            "id": 1, "name": "Mini", "encounterID": 1, "difficulty": 101,
            "kill": True, "startTime": 0, "endTime": 10000,
            "friendlyPlayers": [1, 2],
            "enemyNPCs": [{"id": 9, "gameID": 900, "petOwner": None}],
        }],
        "masterData": {
            "actors": [
                {"id": 1, "name": "P1", "server": "S", "type": "Player",
                 "subType": "Samurai", "petOwner": None, "gameID": 0},
                {"id": 2, "name": "P2", "server": "S", "type": "Player",
                 "subType": "Scholar", "petOwner": None, "gameID": 0},
                {"id": 9, "name": "Boss", "server": None, "type": "NPC",
                 "subType": "Boss", "petOwner": None, "gameID": 900},
            ],
            "abilities": [],
        },
    }


_MINI_EVENTS = [
    {"kind": "event", "timestamp": 100, "type": "cast",
     "sourceID": 1, "targetID": 9, "abilityGameID": 11},
    {"kind": "event", "timestamp": 150, "type": "begincast",
     "sourceID": 1, "targetID": 9, "abilityGameID": 12},
    {"kind": "event", "timestamp": 200, "type": "calculateddamage",
     "sourceID": 1, "targetID": 9, "abilityGameID": 11, "amount": 5000,
     "buffs": "1000851.", "packetID": 7},
    {"kind": "event", "timestamp": 300, "type": "applybuff",
     "sourceID": 2, "targetID": 1, "abilityGameID": 1002912},
    {"kind": "event", "timestamp": 350, "type": "removebuff",
     "sourceID": 2, "targetID": 2, "abilityGameID": 1002912},
    {"kind": "event", "timestamp": 400, "type": "applydebuff",
     "sourceID": 1, "targetID": 9, "abilityGameID": 1001228},
    {"kind": "event", "timestamp": 500, "type": "death",
     "sourceID": 9, "targetID": 1},
    {"kind": "event", "timestamp": 600, "type": "targetabilityupdate",
     "sourceID": 9, "targetID": 9, "targetable": 0},
    {"kind": "event", "timestamp": 700, "type": "cast",
     "sourceID": 9, "targetID": 1, "abilityGameID": 99},
]


def _mini_capture_text() -> str:
    records = [_mini_summary(), *_MINI_EVENTS,
               {"kind": "end", "endTime": 10000, "outcome": "kill"}]
    return "\n".join(json.dumps(r) for r in records) + "\n"


def test_client_predicates() -> None:
    client = LocalCaptureClient(parse_capture_text(_mini_capture_text()))

    casts = client.get_events("c", 0, 10000, 1, data_type="Casts")
    assert [e["timestamp"] for e in casts] == [100, 150]
    assert client.get_events("c", 0, 10000, 1, data_type="Casts",
                             ability_id=12)[0]["timestamp"] == 150

    dmg = client.get_events("c", 0, 10000, 1, data_type="DamageDone")
    assert [e["timestamp"] for e in dmg] == [200]
    assert dmg[0]["buffs"] == "1000851."

    # Deaths select the DYING actor via targetID (WIRE_CONTRACT.md §2).
    deaths = client.get_events("c", 0, 10000, 1, data_type="Deaths")
    assert [e["timestamp"] for e in deaths] == [500]
    assert client.get_events("c", 0, 10000, 9, data_type="Deaths") == []

    assert client.get_events("c", 0, 10000, 1, data_type="Healing") == []

    # Auras select the RECIPIENT via targetID.
    buffs = client.get_aura_events("c", 0, 10000, 1)
    assert [e["timestamp"] for e in buffs] == [300]
    debuffs = client.get_aura_events("c", 0, 10000, 9, "Debuffs")
    assert [e["timestamp"] for e in debuffs] == [400]

    targ = client.get_targetability_events("c", 0, 10000)
    assert [e["targetable"] for e in targ] == [0]

    enemy = client.get_enemy_cast_events("c", 0, 10000)
    assert [e["timestamp"] for e in enemy] == [700]

    # Window bounds are inclusive on both ends.
    assert client.get_events("c", 100, 150, 1, data_type="Casts") == casts

    # Bundle dispatch mirrors prime_bundle's precedence.
    streams = [
        SimpleNamespace(data_type="Casts", start=0, end=10000, source_id=1,
                        ability_id=None, filter_expression=None,
                        hostility=None, include_resources=False),
        SimpleNamespace(data_type="Buffs", start=0, end=10000, source_id=1,
                        ability_id=None, filter_expression=None,
                        hostility=None, include_resources=False),
        SimpleNamespace(data_type="Casts", start=0, end=10000, source_id=None,
                        ability_id=None, filter_expression=None,
                        hostility="Enemies", include_resources=False),
        SimpleNamespace(data_type="All", start=0, end=10000, source_id=None,
                        ability_id=None,
                        filter_expression='type="targetabilityupdate"',
                        hostility=None, include_resources=False),
    ]
    bundle = client.get_event_bundle("c", streams)
    assert bundle[0] == casts
    assert bundle[1] == buffs
    assert bundle[2] == enemy
    assert bundle[3] == targ

    # The summary is a fresh deepcopy per call (no state bleed).
    s1 = client.get_report_summary("c")
    s1["fights"][0]["__stash__"] = True
    assert "__stash__" not in client.get_report_summary("c")["fights"][0]


# --- parser edge cases --------------------------------------------------------

def _summary_line(**fight_overrides) -> str:
    s = _mini_summary()
    s["fights"][0].update(fight_overrides)
    return json.dumps(s)


_END_LINE = json.dumps({"kind": "end", "endTime": 10000, "outcome": "kill"})


def test_parser_minimal_and_bom_blank_lines() -> None:
    text = "﻿" + _summary_line() + "\n\n" + _END_LINE + "\n"
    cap = parse_capture_text(text)
    assert cap.summary["fights"][0]["id"] == 1
    assert cap.end["outcome"] == "kill"
    assert cap.meta is None
    assert cap.enemy_ids == frozenset({9})


def test_parser_summary_last_wins() -> None:
    text = (_summary_line() + "\n"
            + _summary_line(endTime=20000) + "\n" + _END_LINE)
    cap = parse_capture_text(text)
    assert cap.summary["fights"][0]["endTime"] == 20000


def test_parser_unknown_kind_ignored() -> None:
    text = (json.dumps({"kind": "positionSnapshot", "x": 1}) + "\n"
            + _summary_line() + "\n" + _END_LINE)
    cap = parse_capture_text(text)
    assert cap.ignored_kinds == 1


def test_parser_records_after_end_ignored() -> None:
    text = (_summary_line() + "\n" + _END_LINE + "\n"
            + json.dumps({"kind": "event", "timestamp": 1, "type": "cast",
                          "sourceID": 1}) + "\n" + _END_LINE)
    cap = parse_capture_text(text)
    assert cap.events == []
    assert cap.records_after_end == 2


def test_parser_malformed_line_reports_lineno() -> None:
    text = _summary_line() + "\n{not json\n" + _END_LINE
    with pytest.raises(CaptureError, match="line 2"):
        parse_capture_text(text)


def test_parser_missing_end_and_allow_partial() -> None:
    with pytest.raises(CaptureIncompleteError):
        parse_capture_text(_summary_line() + "\n")
    cap = parse_capture_text(_summary_line() + "\n", allow_partial=True)
    assert cap.end is None


def test_parser_missing_summary() -> None:
    with pytest.raises(CaptureError, match="no summary"):
        parse_capture_text(_END_LINE + "\n")


def test_parser_kill_must_be_boolean() -> None:
    with pytest.raises(CaptureError, match="kill"):
        parse_capture_text(_summary_line(kill=0) + "\n" + _END_LINE)
    # null is allowed (unknown outcome) — the wipe gate just won't trigger.
    cap = parse_capture_text(_summary_line(kill=None) + "\n" + _END_LINE)
    assert cap.summary["fights"][0]["kill"] is None


def test_parser_integral_float_coercion() -> None:
    ev = {"kind": "event", "timestamp": 5.0, "type": "cast", "sourceID": 1.0,
          "abilityGameID": 11}
    text = _summary_line() + "\n" + json.dumps(ev) + "\n" + _END_LINE
    cap = parse_capture_text(text)
    assert cap.events[0]["timestamp"] == 5
    assert isinstance(cap.events[0]["timestamp"], int)
    assert isinstance(cap.events[0]["sourceID"], int)

    bad = dict(ev, timestamp=5.5)
    with pytest.raises(CaptureError, match="timestamp"):
        parse_capture_text(_summary_line() + "\n" + json.dumps(bad)
                           + "\n" + _END_LINE)


def test_parser_contract_version_mismatch() -> None:
    meta = json.dumps({"kind": "meta", "contractVersion": 2})
    with pytest.raises(CaptureError, match="contractVersion"):
        parse_capture_text(meta + "\n" + _summary_line() + "\n" + _END_LINE)


# --- standalone ---------------------------------------------------------------

def main() -> int:
    print()
    print("Test: local-capture replay (wire-contract oracle)")
    failures = 0

    for name in TIER1_FIXTURES:
        try:
            test_tier1_replay_matches_fflogs_run(name)
            print(f"  [OK  ] tier 1 byte-identity: {name}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            failures += 1
            print(f"  [FAIL] tier 1 {name}: {exc}")

    recordings = _tier2_recordings()
    if not recordings:
        print("  [SKIP] tier 2: no recording fixture committed yet")
    for path in recordings:
        try:
            test_tier2_full_stream_replay(path)
            print(f"  [OK  ] tier 2 byte-identity: {path.stem}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            failures += 1
            print(f"  [FAIL] tier 2 {path.stem}: {exc}")

    unit_tests = [
        test_client_predicates,
        test_parser_minimal_and_bom_blank_lines,
        test_parser_summary_last_wins,
        test_parser_unknown_kind_ignored,
        test_parser_records_after_end_ignored,
        test_parser_malformed_line_reports_lineno,
        test_parser_missing_end_and_allow_partial,
        test_parser_missing_summary,
        test_parser_kill_must_be_boolean,
        test_parser_integral_float_coercion,
        test_parser_contract_version_mismatch,
    ]
    for fn in unit_tests:
        try:
            fn()
            print(f"  [OK  ] {fn.__name__}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            failures += 1
            print(f"  [FAIL] {fn.__name__}: {exc}")

    print()
    print("============================================================")
    passed = len(TIER1_FIXTURES) + len(recordings) + len(unit_tests) - failures
    print(f"Passed: {passed}    Failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
