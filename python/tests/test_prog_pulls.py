"""Tests for the prog-pull discovery request (`sidecar.main.list_prog_pulls`).

Stubbed client end-to-end: the kill/encounter/duration/job-presence filters,
zone pre-filtering of recent reports (with the scan-all fallback), newest-first
sort + cap, the serialized wire shape, and the pasted-report-code mode.

Run from python/:  python tests/test_prog_pulls.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sidecar.main as main_mod
from sidecar.main import _serialize_prog_pull, list_prog_pulls

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _fight(fid: int, *, kill: bool = False, enc: int = 101,
           start: int = 0, dur_ms: int = 240_000, pct: float | None = 61.0,
           phase: int = 2, players: list[int] | None = None) -> dict:
    return {
        "id": fid, "name": "Boss", "encounterID": enc, "kill": kill,
        "startTime": start, "endTime": start + dur_ms,
        "fightPercentage": pct, "bossPercentage": 70.0, "lastPhase": phase,
        "friendlyPlayers": players if players is not None else [1],
    }


def _summary(fights: list[dict], *, start_ms: int = 1_700_000_000_000,
             jobs: dict[int, str] | None = None) -> dict:
    jobs = jobs or {1: "Machinist"}
    return {
        "startTime": start_ms,
        "fights": fights,
        "masterData": {"actors": [
            {"id": aid, "type": "Player", "subType": st}
            for aid, st in jobs.items()
        ]},
    }


class _StubClient:
    def __init__(self, recent: list[dict], summaries: dict[str, dict]):
        self._recent = recent
        self._summaries = summaries
        self.summary_calls: list[str] = []
        self.prefetched: list[list[str]] = []

    def get_character_recent_reports(self, lodestone_id, limit=10):
        return self._recent

    def prefetch_report_summaries(self, codes):
        self.prefetched.append(list(codes))

    def get_report_summary(self, code):
        self.summary_calls.append(code)
        if code not in self._summaries:
            raise RuntimeError("unknown report")
        return self._summaries[code]


def _with_client(stub, fn):
    orig = main_mod._client
    main_mod._client = lambda: stub
    try:
        return fn()
    finally:
        main_mod._client = orig


def test_filters() -> None:
    fights = [
        _fight(1),                                   # keeper
        _fight(2, kill=True),                        # kill -> excluded
        _fight(3, enc=102),                          # wrong encounter
        _fight(4, dur_ms=5_000),                     # sub-20s reset
        _fight(5, players=[2]),                      # job not present
    ]
    stub = _StubClient([{"code": "AAA", "zone_id": 73}],
                       {"AAA": _summary(fights, jobs={1: "Machinist",
                                                      2: "Reaper"})})
    out = _with_client(stub, lambda: list_prog_pulls(
        {"lodestoneId": 1, "encounterId": 101, "spec": "Machinist"}))
    _check("only the wipe survives", [p["fightId"] for p in out["pulls"]] == [1],
           str(out))
    _check("source recent", out["source"] == "recent", str(out))


def test_spaced_job_names_match() -> None:
    """`spec` arrives spaced ('Red Mage'); actor subType may be either form."""
    stub = _StubClient([{"code": "AAA", "zone_id": 73}],
                       {"AAA": _summary([_fight(1)], jobs={1: "RedMage"})})
    out = _with_client(stub, lambda: list_prog_pulls(
        {"lodestoneId": 1, "encounterId": 101, "spec": "Red Mage"}))
    _check("spaceless subType matches", len(out["pulls"]) == 1, str(out))


def test_zone_prefilter_and_fallback() -> None:
    recent = [{"code": "TIER", "zone_id": 73}, {"code": "DUNGEON", "zone_id": 57}]
    stub = _StubClient(recent, {"TIER": _summary([_fight(1)]),
                                "DUNGEON": _summary([_fight(9)])})
    out = _with_client(stub, lambda: list_prog_pulls(
        {"lodestoneId": 1, "encounterId": 101, "spec": "Machinist"}))
    _check("zone pre-filter skips off-zone reports",
           stub.summary_calls == ["TIER"] and len(out["pulls"]) == 1, str(out))

    stub2 = _StubClient([{"code": "MIXED", "zone_id": 57}],
                        {"MIXED": _summary([_fight(1)])})
    out2 = _with_client(stub2, lambda: list_prog_pulls(
        {"lodestoneId": 1, "encounterId": 101, "spec": "Machinist"}))
    _check("no zone match -> scan all", len(out2["pulls"]) == 1, str(out2))


def test_sort_and_cap() -> None:
    fights = [_fight(i, start=i * 1_000_000) for i in range(1, 60)]
    stub = _StubClient([{"code": "AAA", "zone_id": 73}],
                       {"AAA": _summary(fights)})
    out = _with_client(stub, lambda: list_prog_pulls(
        {"lodestoneId": 1, "encounterId": 101, "spec": "Machinist"}))
    pulls = out["pulls"]
    _check("capped", len(pulls) == main_mod._PROG_MAX_PULLS, str(len(pulls)))
    _check("newest first", pulls[0]["fightId"] == 59
           and pulls[0]["startTimeMs"] > pulls[-1]["startTimeMs"], str(pulls[0]))


def test_serialized_shape_and_label() -> None:
    p = _serialize_prog_pull("AAA", _fight(7, start=60_000),
                             1_700_000_000_000)
    _check("wire keys", set(p.keys()) == {
        "reportCode", "fightId", "startTimeMs", "durationS",
        "fightPercentage", "bossPercentage", "lastPhase", "label"}, str(p))
    _check("values", p["reportCode"] == "AAA" and p["fightId"] == 7
           and p["startTimeMs"] == 1_700_000_060_000
           and abs(p["durationS"] - 240.0) < 1e-6
           and p["fightPercentage"] == 61.0 and p["lastPhase"] == 2, str(p))
    _check("label carries duration/pct/phase",
           "4:00" in p["label"] and "61% left (P2)" in p["label"], p["label"])
    # No fightPercentage (old logs): label omits the progress part.
    p2 = _serialize_prog_pull("AAA", _fight(8, pct=None), 1_700_000_000_000)
    _check("no-pct label degrades", "% left" not in p2["label"]
           and p2["fightPercentage"] is None, p2["label"])


def test_pasted_report_mode() -> None:
    stub = _StubClient([], {"PASTED": _summary([_fight(1)])})
    out = _with_client(stub, lambda: list_prog_pulls(
        {"reportCode": "PASTED", "encounterId": 101, "spec": "Machinist"}))
    _check("pasted mode lists wipes", len(out["pulls"]) == 1
           and out["source"] == "report", str(out))

    stub2 = _StubClient([], {})
    try:
        _with_client(stub2, lambda: list_prog_pulls(
            {"reportCode": "NOPE", "encounterId": 101, "spec": "Machinist"}))
        _check("bad pasted code raises", False, "no exception")
    except RuntimeError as e:
        _check("bad pasted code raises", "NOPE" in str(e), str(e))


def main() -> None:
    for fn in [test_filters, test_spaced_job_names_match,
               test_zone_prefilter_and_fallback, test_sort_and_cap,
               test_serialized_shape_and_label, test_pasted_report_mode]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
