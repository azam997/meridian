"""The Research tab's backend surface.

Hermetic — no network, no FFLogs client. Exercises:
  - `sidecar.main.list_rankings`: raw ranking-entry extraction (rank order,
    identity fields, defensive server/percentile handling, skip-on-missing-
    report, limit, fetch-failure -> []).
  - `sidecar.main.run_analysis`: the optional `playerName` param threads into
    the build and splits the result-cache key (a named and an unnamed run of
    the same pull are distinct results).

Runs under pytest (from python/) and standalone (`python tests/test_list_rankings.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python tests/test_list_rankings.py` standalone (pytest gets this via
# conftest, but a bare script run starts with sys.path[0] = tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sidecar.main as m  # noqa: E402


# --- list_rankings ------------------------------------------------------------

_BLOB = {
    "rankings": [
        {   # full entry, server as dict (the characterRankings shape)
            "name": "Alpha One",
            "server": {"name": "Gilgamesh", "region": "NA"},
            "report": {"code": "AAAA1111", "fightID": 3},
            "duration": 505_000,
            "amount": 39_210.5,
            "rankPercent": 100,
        },
        {   # no report → skipped entirely (not loadable)
            "name": "No Report",
            "amount": 39_000,
        },
        {   # server as plain string; optional fields absent → keys omitted
            "name": "Beta Two",
            "server": "Chaos",
            "report": {"code": "BBBB2222", "fightID": 7},
        },
    ]
}


class _FakeRankingsClient:
    def __init__(self, blob=_BLOB, raise_on_call=False):
        self.blob = blob
        self.raise_on_call = raise_on_call
        self.calls: list[dict] = []

    def get_rankings(self, encounter_id, class_name, spec_name, difficulty):
        if self.raise_on_call:
            raise RuntimeError("network down")
        self.calls.append({"encounter_id": encounter_id, "class_name": class_name,
                           "spec_name": spec_name, "difficulty": difficulty})
        return self.blob


def _with_client(client, fn):
    orig = m._client
    m._client = lambda: client  # type: ignore[assignment]
    try:
        return fn()
    finally:
        m._client = orig  # type: ignore[assignment]


def _check_extraction() -> None:
    client = _FakeRankingsClient()
    rows = _with_client(client, lambda: m.list_rankings(
        {"spec": "Samurai", "encounterId": 103}))

    # The skipped no-report entry keeps its slot in the enumerate (rank 2 is
    # dropped, not renumbered) — ranks mirror the FFLogs list positions.
    assert [r["rank"] for r in rows] == [1, 3], rows
    assert rows[0] == {
        "rank": 1, "name": "Alpha One", "reportCode": "AAAA1111", "fightId": 3,
        "server": "Gilgamesh", "durationMs": 505_000, "amount": 39_210.5,
        "percentile": 100,
    }, rows[0]
    # Optional fields absent → keys omitted (the UI hides them), not None/0.
    assert rows[1] == {
        "rank": 3, "name": "Beta Two", "reportCode": "BBBB2222", "fightId": 7,
        "server": "Chaos",
    }, rows[1]

    # The rankings call mirrors _build_refs (job for both class+spec, the
    # encounter's difficulty).
    assert client.calls == [{"encounter_id": 103, "class_name": "Samurai",
                             "spec_name": "Samurai",
                             "difficulty": m.encounter_difficulty(103)}]


def _check_limit() -> None:
    blob = {"rankings": [
        {"name": f"P{i}", "report": {"code": f"C{i}", "fightID": i}}
        for i in range(15)
    ]}
    rows = _with_client(_FakeRankingsClient(blob), lambda: m.list_rankings(
        {"spec": "Machinist", "encounterId": 101}))
    assert len(rows) == 10 and rows[-1]["rank"] == 10, rows
    rows5 = _with_client(_FakeRankingsClient(blob), lambda: m.list_rankings(
        {"spec": "Machinist", "encounterId": 101, "limit": 5}))
    assert len(rows5) == 5, rows5


def _check_fetch_failure_returns_empty() -> None:
    rows = _with_client(_FakeRankingsClient(raise_on_call=True),
                        lambda: m.list_rankings({"spec": "Bard", "encounterId": 102}))
    assert rows == [], rows


# --- run_analysis playerName threading ---------------------------------------

def _check_player_name_splits_cache() -> None:
    m._result_cache.clear()
    m._result_inflight.clear()
    builds: list[str | None] = []

    def fake_build(client, job, code, fight_id, encounter_id, refs_bucket,
                   progress, player_name=None, **kwargs):
        builds.append(player_name)
        return {"who": player_name or "You"}

    orig_build, orig_client, orig_emit = m._analyze_and_build, m._client, m._emit
    m._analyze_and_build = fake_build  # type: ignore[assignment]
    m._client = lambda: None  # type: ignore[assignment]
    m._emit = lambda msg: None  # type: ignore[assignment]
    try:
        base = {"spec": "Samurai", "reportCode": "AAAA1111", "fightId": 3,
                "encounterId": 103, "refsBucket": "Top 10"}
        unnamed = m.run_analysis(dict(base), "r1")
        named = m.run_analysis(dict(base, playerName="Alpha One"), "r2")
        assert unnamed == {"who": "You"} and named == {"who": "Alpha One"}
        # Distinct cache keys → two builds, each threading its own name.
        assert builds == [None, "Alpha One"], builds
        # Both cached: identical re-requests build nothing further.
        assert m.run_analysis(dict(base), "r3") == {"who": "You"}
        assert m.run_analysis(dict(base, playerName="Alpha One"), "r4") == \
            {"who": "Alpha One"}
        assert builds == [None, "Alpha One"], builds
        # Empty string normalizes to the unnamed run (shares its cache entry).
        assert m.run_analysis(dict(base, playerName=""), "r5") == {"who": "You"}
        assert builds == [None, "Alpha One"], builds
    finally:
        m._analyze_and_build = orig_build  # type: ignore[assignment]
        m._client = orig_client  # type: ignore[assignment]
        m._emit = orig_emit  # type: ignore[assignment]
        m._result_cache.clear()
        m._result_inflight.clear()


# --- pytest entry points ----------------------------------------------------

def test_list_rankings_extraction():
    _check_extraction()


def test_list_rankings_limit():
    _check_limit()


def test_list_rankings_fetch_failure_returns_empty():
    _check_fetch_failure_returns_empty()


def test_run_analysis_player_name_splits_cache():
    _check_player_name_splits_cache()


def main() -> None:
    _check_extraction()
    _check_limit()
    _check_fetch_failure_returns_empty()
    _check_player_name_splits_cache()
    print("test_list_rankings: OK")


if __name__ == "__main__":
    main()
