"""Tests for the job-agnostic Pulls list (`sidecar.main.list_character_pulls`)
and the chunked kill probe (`fflogs_api.get_character_kill_probe`).

Stubbed client end-to-end: the probe -> per-spec kills -> wipe-scan passes,
identity attribution (name+server, spaced/spaceless subType), merge/dedupe/
sort, the encounter catalog synthesis, the handler memo + forceRefresh, the
pasted-report mode, and the probe's chunk-halving complexity fallback.

Run from python/:  python tests/test_character_pulls.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fflogs_api
import sidecar.main as main_mod
from encounters import ALL_ENCOUNTERS, ZONE_GROUPS
from sidecar.main import _serialize_pull_row, list_character_pulls

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fight(fid: int, *, kill: bool | None = False, enc: int = 101,
           start: int = 0, dur_ms: int = 240_000, pct: float | None = 61.0,
           phase: int = 2, players: list[int] | None = None) -> dict:
    return {
        "id": fid, "name": "Boss", "encounterID": enc, "kill": kill,
        "startTime": start, "endTime": start + dur_ms,
        "fightPercentage": pct, "bossPercentage": 70.0, "lastPhase": phase,
        "friendlyPlayers": players if players is not None else [1],
    }


def _summary(fights: list[dict], *, start_ms: int = 1_700_000_000_000,
             actors: list[dict] | None = None) -> dict:
    if actors is None:
        actors = [{"id": 1, "type": "Player", "name": "Pull Tester",
                   "server": "Hyperion", "subType": "Machinist"}]
    return {"startTime": start_ms, "fights": fights,
            "masterData": {"actors": actors}}


def _kill(code: str, fid: int, start_ms: int, *, parse: float = 50.0,
          dps: float = 30_000.0) -> dict:
    """A `_pulls_from_encounter_ranks`-shaped ranked kill."""
    return {"report_code": code, "fight_id": fid, "start_time_ms": start_ms,
            "duration_s": 480.0, "parse_pct": parse, "dps": dps,
            "spec": None, "label": "x"}


class _StubClient:
    """Everything list_character_pulls touches, canned."""

    def __init__(self, *, probe: dict[str, list[dict]] | None = None,
                 setups: dict[str, dict] | None = None,
                 recent: list[dict] | None = None,
                 summaries: dict[str, dict] | None = None):
        self._probe = probe or {}
        self._setups = setups or {}
        self._recent = recent or []
        self._summaries = summaries or {}
        self.probe_calls = 0
        self.setup_calls: list[str] = []
        self.summary_calls: list[str] = []

    def get_character_kill_probe(self, lodestone_id, groups, spec_names,
                                 chunk=5, on_chunk=None):
        self.probe_calls += 1
        if on_chunk:
            on_chunk(len(spec_names), len(spec_names))
        return {s: self._probe.get(s, []) for s in spec_names}

    def get_character_setup(self, lodestone_id, groups, spec_name):
        self.setup_calls.append(spec_name)
        return self._setups.get(spec_name,
                                {"encounters": [], "pulls": {}})

    def get_character_recent_reports(self, lodestone_id, limit=10):
        return self._recent[:limit]

    def prefetch_report_summaries(self, codes):
        pass

    def get_report_summary(self, code):
        self.summary_calls.append(code)
        if code not in self._summaries:
            raise RuntimeError("unknown report")
        return self._summaries[code]


def _run(stub, req) -> dict:
    """Run the handler with the stub client, progress emission swallowed and
    the handler memo cleared (module state)."""
    orig_client, orig_emit = main_mod._client, main_mod._emit
    main_mod._client = lambda: stub
    main_mod._emit = lambda obj: None
    main_mod._char_pulls_cache.clear()
    try:
        return list_character_pulls(req, "test")
    finally:
        main_mod._client, main_mod._emit = orig_client, orig_emit


_REQ = {"lodestoneId": 1, "characterName": "Pull Tester",
        "server": "Hyperion"}


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

def test_merge_dedupe_sort() -> None:
    """Kills from two specs + wipes from reports, newest-first; a kill row
    beats a wipe row on the same (report, fight)."""
    stub = _StubClient(
        probe={"Machinist": [{"id": 101, "name": "Vamp Fatale",
                              "total_kills": 2, "best_parse_pct": 66.6}],
               "Reaper": [{"id": 101, "name": "Vamp Fatale",
                           "total_kills": 1, "best_parse_pct": 20.0}]},
        setups={
            "Machinist": {"encounters": [{"id": 101, "name": "Vamp Fatale",
                                          "total_kills": 2}],
                          "pulls": {101: [_kill("AAA", 3, 3_000_000),
                                          _kill("AAA", 1, 1_000_000)]}},
            "Reaper": {"encounters": [{"id": 101, "name": "Vamp Fatale",
                                       "total_kills": 1}],
                       "pulls": {101: [_kill("BBB", 1, 2_000_000)]}},
        },
        recent=[{"code": "CCC", "zone_id": 73}],
        summaries={"CCC": _summary(
            [_fight(9, start=0, dur_ms=240_000),
             # same (report, fight) as a kill row -> the kill wins
             _fight(1, start=500, dur_ms=240_000)],
            start_ms=4_000_000)},
    )
    # Give the colliding wipe the same report code as a kill row.
    stub._summaries["AAA"] = stub._summaries.pop("CCC")
    stub._recent = [{"code": "AAA", "zone_id": 73}]

    out = _run(stub, _REQ)
    keys = [(r["reportCode"], r["fightId"], r["kill"]) for r in out["pulls"]]
    _check("kill beats wipe on same fight",
           ("AAA", 1, True) in keys and ("AAA", 1, False) not in keys,
           str(keys))
    _check("wipe row survives on its own fight", ("AAA", 9, False) in keys,
           str(keys))
    times = [r["startTimeMs"] for r in out["pulls"]]
    _check("newest first", times == sorted(times, reverse=True), str(times))
    _check("both specs fetched", set(stub.setup_calls) == {"Machinist",
                                                           "Reaper"},
           str(stub.setup_calls))
    _check("jobs in role order (MCH before RPR reversed)",
           out["jobs"] == ["Reaper", "Machinist"], str(out["jobs"]))
    mch_kill = next(r for r in out["pulls"] if r["kill"] and r["job"] == "Machinist")
    _check("kill row carries parse/dps",
           mch_kill["parsePct"] == 50.0 and mch_kill["dps"] == 30_000.0,
           str(mch_kill))


def test_wipe_attribution() -> None:
    """Wipes are attributed by name+server; fights without the character are
    skipped; spaceless subType maps back to the spaced internal name."""
    actors = [
        {"id": 1, "type": "Player", "name": "Pull Tester",
         "server": "Hyperion", "subType": "RedMage"},
        {"id": 2, "type": "Player", "name": "Someone Else",
         "server": "Hyperion", "subType": "Machinist"},
    ]
    stub = _StubClient(
        recent=[{"code": "AAA", "zone_id": 73}],
        summaries={"AAA": _summary([
            _fight(1, players=[1, 2]),          # ours, as Red Mage
            _fight(2, players=[2]),             # not in this fight
            _fight(3, players=[1, 2], dur_ms=5_000),  # sub-20s reset
        ], actors=actors)},
    )
    out = _run(stub, _REQ)
    _check("one attributed wipe", [r["fightId"] for r in out["pulls"]] == [1],
           str(out["pulls"]))
    _check("spaceless subType -> spaced job",
           out["pulls"][0]["job"] == "Red Mage", str(out["pulls"][0]))
    _check("wipe row carries phase fields",
           out["pulls"][0]["fightPercentage"] == 61.0
           and out["pulls"][0]["lastPhase"] == 2, str(out["pulls"][0]))

    # Wrong server -> no attribution.
    out2 = _run(stub, dict(_REQ, server="Behemoth"))
    _check("server mismatch skips", out2["pulls"] == [], str(out2["pulls"]))


def test_encounter_catalog() -> None:
    """Full catalog: kill totals summed across specs, zero-kill encounters
    (incl. ultimates) synthesized with catalog names."""
    stub = _StubClient(
        probe={"Machinist": [{"id": 101, "name": "Vamp Fatale",
                              "total_kills": 2, "best_parse_pct": 66.6}],
               "Reaper": [{"id": 101, "name": "Vamp Fatale",
                           "total_kills": 3, "best_parse_pct": 10.0}]},
        setups={"Machinist": {"encounters": [], "pulls": {}},
                "Reaper": {"encounters": [], "pulls": {}}},
    )
    out = _run(stub, _REQ)
    by_id = {e["id"]: e for e in out["encounters"]}
    _check("all catalog encounters present",
           set(by_id) == {eid for eid, _ in ALL_ENCOUNTERS}, str(sorted(by_id)))
    _check("kill totals summed across specs", by_id[101]["totalKills"] == 5,
           str(by_id[101]))
    _check("FFLogs name kept where killed",
           by_id[101]["name"] == "Vamp Fatale", str(by_id[101]))
    _check("ultimate synthesized with category",
           by_id[1085]["totalKills"] == 0
           and by_id[1085]["category"] == "ultimate", str(by_id[1085]))


def test_memo_and_force_refresh() -> None:
    stub = _StubClient(recent=[], summaries={})
    orig_client, orig_emit = main_mod._client, main_mod._emit
    main_mod._client = lambda: stub
    main_mod._emit = lambda obj: None
    main_mod._char_pulls_cache.clear()
    try:
        list_character_pulls(_REQ, "t")
        list_character_pulls(_REQ, "t")
        _check("second call hits the memo", stub.probe_calls == 1,
               str(stub.probe_calls))
        list_character_pulls(dict(_REQ, forceRefresh=True), "t")
        _check("forceRefresh re-fetches", stub.probe_calls == 2,
               str(stub.probe_calls))
        list_character_pulls(dict(_REQ, recentLimit=25), "t")
        _check("recentLimit is part of the key", stub.probe_calls == 3,
               str(stub.probe_calls))
    finally:
        main_mod._client, main_mod._emit = orig_client, orig_emit
        main_mod._char_pulls_cache.clear()


def test_pasted_report_mode() -> None:
    """Pasted mode: no rankings calls at all; kills and wipes both listed,
    kills without parse/dps; zero matches raises."""
    actors = [{"id": 1, "type": "Player", "name": "Pull Tester",
               "server": "Hyperion", "subType": "Samurai"}]
    stub = _StubClient(summaries={"PASTED01PASTED01": _summary([
        _fight(1, kill=True, start=0),
        _fight(2, kill=False, start=1_000),
        _fight(3, kill=None, start=2_000),      # unknown outcome -> skipped
    ], actors=actors)})
    out = _run(stub, dict(_REQ, reportCode="PASTED01PASTED01"))
    _check("no rankings traffic", stub.probe_calls == 0
           and stub.setup_calls == [], str(stub.setup_calls))
    kinds = {(r["fightId"], r["kill"]) for r in out["pulls"]}
    _check("kill + wipe listed, unknown skipped",
           kinds == {(1, True), (2, False)}, str(kinds))
    kill_row = next(r for r in out["pulls"] if r["kill"])
    _check("pasted kill has no parse/dps",
           kill_row["parsePct"] is None and kill_row["dps"] is None,
           str(kill_row))
    _check("recentLimit 0 flags pasted mode", out["recentLimit"] == 0,
           str(out))

    try:
        _run(stub, dict(_REQ, reportCode="NOPE", characterName="Nobody"))
        _check("zero matches raises", False, "no exception")
    except RuntimeError as e:
        _check("zero matches raises", "NOPE" in str(e), str(e))


def test_serialized_row_shape() -> None:
    r = _serialize_pull_row(report_code="AAA", fight_id=7, encounter_id=101,
                            job="Machinist", kill=True, start_time_ms=123,
                            duration_s=480.0, parse_pct=66.6, dps=36_600.0)
    _check("wire keys", set(r.keys()) == {
        "reportCode", "fightId", "encounterId", "job", "kill", "startTimeMs",
        "durationS", "parsePct", "dps", "fightPercentage", "bossPercentage",
        "lastPhase"}, str(r))
    _check("absent fields are None", r["fightPercentage"] is None
           and r["lastPhase"] is None, str(r))


# ---------------------------------------------------------------------------
# Probe chunk-halving (fflogs_api level)
# ---------------------------------------------------------------------------

def test_probe_chunk_halving() -> None:
    """A complexity error on a 5-spec chunk halves down until it fits; a
    single-spec failure propagates."""
    c = fflogs_api.FFLogsClient.__new__(fflogs_api.FFLogsClient)
    calls: list[int] = []

    def fake_query(gql: str, variables=None):
        n = gql.count("zoneRankings") // len(ZONE_GROUPS)
        calls.append(n)
        if n > 2:
            raise RuntimeError("GraphQL error: query complexity exceeded")
        return {"characterData": {"character": {}}}

    c.query = fake_query
    specs = ["Machinist", "Reaper", "Samurai", "Red Mage", "Paladin"]
    out = c.get_character_kill_probe(1, ZONE_GROUPS, specs, chunk=5)
    _check("all specs answered", set(out) == set(specs), str(sorted(out)))
    _check("halved 5 -> 2 after the failure",
           calls[0] == 5 and max(calls[1:]) <= 2, str(calls))

    def always_fail(gql: str, variables=None):
        raise RuntimeError("GraphQL error: nope")

    c.query = always_fail
    try:
        c.get_character_kill_probe(1, ZONE_GROUPS, ["Machinist"], chunk=1)
        _check("single-spec failure propagates", False, "no exception")
    except RuntimeError as e:
        _check("single-spec failure propagates", "nope" in str(e), str(e))


def test_probe_alias_shape() -> None:
    """The per-chunk query aliases one zoneRankings per (spec, group) and the
    parser reads them back per spec."""
    c = fflogs_api.FFLogsClient.__new__(fflogs_api.FFLogsClient)
    seen: dict[str, str] = {}

    def fake_query(gql: str, variables=None):
        seen["gql"] = gql
        return {"characterData": {"character": {
            "s0z73": {"rankings": [{"encounter": {"id": 101, "name": "VF"},
                                    "totalKills": 4, "rankPercent": 50.0}]},
            "s0z76": {},
            "s1z73": {},
            "s1z76": {},
        }}}

    c.query = fake_query
    out = c.get_character_kill_probe(1, ZONE_GROUPS,
                                     ["Red Mage", "Machinist"], chunk=5)
    _check("spec slug inlined spaceless",
           'specName: "RedMage"' in seen["gql"], seen["gql"][:400])
    _check("hits parsed per spec", [e["id"] for e in out["Red Mage"]] == [101]
           and out["Machinist"] == [], str(out))


def main() -> None:
    for fn in [test_merge_dedupe_sort, test_wipe_attribution,
               test_encounter_catalog, test_memo_and_force_refresh,
               test_pasted_report_mode, test_serialized_row_shape,
               test_probe_chunk_halving, test_probe_alias_shape]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
