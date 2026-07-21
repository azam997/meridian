"""API round-trip condensation: event bundling + report-summary batching.

Hermetic — no network. Exercises:
  - `FFLogsClient.get_event_bundle`: one aliased query for many streams, with
    per-alias pagination (only the still-open stream re-queries).
  - `FFLogsClient.get_report_summaries`: aliased multi-report query chunked at
    `_SUMMARY_BATCH`.
  - `CachedEventsClient.prime_bundle`: seeds the per-pull caches so the matching
    `get_events` / `get_targetability_events` / `get_aura_events` calls are hits
    (inner touched once for the bundle, zero for the individuals).
  - `CachedEventsClient.get_aura_events`: dedups the 2×/pull aura fetch.
  - `SessionCachedClient.prefetch_report_summaries`: batch-fills the report cache.
  - `SessionCachedClient` character caching: `get_character_zone_encounters` /
    `get_character_encounter_pulls` hit the inner once per distinct key (so a
    job swap-away-and-back is served from cache; empty results cached too).
  - `DevDiskCacheClient`: caches aura / bundle / summaries, with the batch and
    single summary paths sharing per-code disk files.

Runs under pytest (from python/) and standalone.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fflogs_api  # noqa: E402
from fflogs_api import BundleStream, FFLogsClient  # noqa: E402
from jobs._core.cached_client import (  # noqa: E402
    CachedEventsClient,
    SessionCachedClient,
)
from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402


# --- get_event_bundle -------------------------------------------------------

def _check_event_bundle_pagination() -> None:
    c = FFLogsClient("id", "secret")
    calls: list[str] = []

    def fake_query(gql, variables=None):
        calls.append(gql)
        if len(calls) == 1:
            # First page: both streams aliased in one request.
            assert "s0:" in gql and "s1:" in gql
            assert gql.count("events(") == 2
            return {"reportData": {"report": {
                "s0": {"data": [{"t": 1}], "nextPageTimestamp": 100},
                "s1": {"data": [{"t": 2}], "nextPageTimestamp": None},
            }}}
        # Follow-up: only the still-open stream (s0) re-queries.
        assert "s0:" in gql and "s1:" not in gql
        return {"reportData": {"report": {
            "s0": {"data": [{"t": 3}], "nextPageTimestamp": None},
        }}}

    c.query = fake_query  # type: ignore[assignment]
    streams = [
        BundleStream(data_type="Casts", start=0, end=1000, source_id=7),
        BundleStream(data_type="DamageDone", start=0, end=1000, source_id=8),
    ]
    out = c.get_event_bundle("RPT", streams)
    assert out == [[{"t": 1}, {"t": 3}], [{"t": 2}]], out
    assert len(calls) == 2, f"expected 1 page + 1 follow-up, got {len(calls)}"


# --- get_report_summaries ---------------------------------------------------

def _check_report_summaries_chunking() -> None:
    c = FFLogsClient("id", "secret")
    chunks: list[int] = []

    def fake_query(gql, variables=None):
        vals = list(variables.values())
        chunks.append(len(vals))
        return {"reportData": {f"r{j}": {"code": code}
                               for j, code in enumerate(vals)}}

    c.query = fake_query  # type: ignore[assignment]
    codes = [f"C{i:02d}" for i in range(12)]
    out = c.get_report_summaries(codes)
    assert set(out) == set(codes)
    assert all(out[code] == {"code": code} for code in codes)
    # 12 codes at _SUMMARY_BATCH=5 → chunks of 5, 5, 2.
    assert chunks == [5, 5, 2], chunks
    assert fflogs_api._SUMMARY_BATCH == 5


def _check_report_summaries_dedupes() -> None:
    c = FFLogsClient("id", "secret")
    seen: list[list[str]] = []

    def fake_query(gql, variables=None):
        vals = list(variables.values())
        seen.append(vals)
        return {"reportData": {f"r{j}": {"code": code}
                               for j, code in enumerate(vals)}}

    c.query = fake_query  # type: ignore[assignment]
    out = c.get_report_summaries(["A", "B", "A", "B", "C"])
    assert set(out) == {"A", "B", "C"}
    assert seen == [["A", "B", "C"]], seen


# --- CachedEventsClient.prime_bundle ----------------------------------------

class _CountingInner:
    def __init__(self):
        self.bundle = 0
        self.events = 0
        self.targ = 0
        self.aura = 0

    def get_event_bundle(self, code, streams):
        self.bundle += 1
        return [[{"stream": s.data_type, "sid": s.source_id}] for s in streams]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        self.events += 1
        return [{"miss": "events"}]

    def get_targetability_events(self, code, start, end):
        self.targ += 1
        return [{"miss": "targ"}]

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        self.aura += 1
        return [{"miss": "aura"}]


def _check_prime_bundle_seeds_caches() -> None:
    inner = _CountingInner()
    c = CachedEventsClient(inner)
    streams = [
        BundleStream(data_type="Casts", start=-5000, end=600000, source_id=7),
        BundleStream(data_type="All", start=0, end=600000,
                     filter_expression='type="targetabilityupdate"'),
        BundleStream(data_type="Buffs", start=0, end=600000, source_id=7),
        BundleStream(data_type="DamageDone", start=0, end=600000, source_id=42),
    ]
    c.prime_bundle("RPT", streams)
    assert inner.bundle == 1

    # The matching individual calls are now hits — inner is never touched.
    casts = c.get_events("RPT", -5000, 600000, 7, data_type="Casts")
    targ = c.get_targetability_events("RPT", 0, 600000)
    buffs = c.get_aura_events("RPT", 0, 600000, 7, data_type="Buffs")
    pet = c.get_events("RPT", 0, 600000, 42, data_type="DamageDone")
    assert inner.events == 0 and inner.targ == 0 and inner.aura == 0
    assert casts == [{"stream": "Casts", "sid": 7}]
    assert targ == [{"stream": "All", "sid": None}]
    assert buffs == [{"stream": "Buffs", "sid": 7}]
    assert pet == [{"stream": "DamageDone", "sid": 42}]

    # A stream NOT in the bundle still falls through to the inner client.
    c.get_events("RPT", 0, 600000, 99, data_type="Casts")
    assert inner.events == 1


def _check_aura_events_dedup() -> None:
    inner = _CountingInner()
    c = CachedEventsClient(inner)
    a = c.get_aura_events("RPT", 0, 100, 5)
    b = c.get_aura_events("RPT", 0, 100, 5)
    assert inner.aura == 1, "second aura fetch should hit cache"
    assert a == b == [{"miss": "aura"}]


def _check_aura_narrowing() -> None:
    """The pre-pull-widened primed Buffs stream serves the exact-fight aura
    fetches by narrowing to `timestamp >= start` (FFLogs' own startTime
    filter). Only a same-code/actor/type/END superset with an earlier start
    may be narrowed — anything else falls through to the inner client."""
    wide = [
        {"timestamp": 995_000, "type": "removebuff", "abilityGameID": 1},
        {"timestamp": 1_000_000, "type": "applybuff", "abilityGameID": 2},
        {"timestamp": 1_500_000, "type": "removebuff", "abilityGameID": 2},
    ]

    class _WideBundleInner(_CountingInner):
        def get_event_bundle(self, code, streams):
            self.bundle += 1
            return [list(wide)]

    inner = _WideBundleInner()
    c = CachedEventsClient(inner)
    c.prime_bundle("RPT", [BundleStream(data_type="Buffs", start=990_000,
                                        end=1_600_000, source_id=7)])
    # Exact-fight fetch (start at pull) narrows the primed wide stream.
    got = c.get_aura_events("RPT", 1_000_000, 1_600_000, 7, "Buffs")
    assert got == wide[1:], got
    assert inner.aura == 0, "narrowed hit must not touch the inner client"
    # The pre-pull detector's own widened fetch is a direct hit.
    assert c.get_aura_events("RPT", 990_000, 1_600_000, 7, "Buffs") == wide
    # The narrowed result was seeded under its exact key → plain hit now.
    assert c.get_aura_events("RPT", 1_000_000, 1_600_000, 7, "Buffs") == wide[1:]
    assert inner.aura == 0
    # A different end is NOT derivable → falls through.
    c.get_aura_events("RPT", 1_000_000, 1_700_000, 7, "Buffs")
    assert inner.aura == 1
    # A start EARLIER than every cached stream is NOT derivable either.
    c.get_aura_events("RPT", 980_000, 1_600_000, 7, "Buffs")
    assert inner.aura == 2
    # Different actor / data_type are distinct as ever.
    c.get_aura_events("RPT", 1_000_000, 1_600_000, 8, "Buffs")
    c.get_aura_events("RPT", 1_000_000, 1_600_000, 7, "Debuffs")
    assert inner.aura == 4


# --- SessionCachedClient.prefetch_report_summaries --------------------------

class _ReportInner:
    def __init__(self):
        self.single = 0
        self.batches: list[list[str]] = []

    def get_report_summary(self, code):
        self.single += 1
        return {"code": code, "via": "single"}

    def get_report_summaries(self, codes):
        self.batches.append(list(codes))
        return {c: {"code": c, "via": "batch"} for c in codes}


def _check_session_prefetch() -> None:
    inner = _ReportInner()
    s = SessionCachedClient(inner)
    s.prefetch_report_summaries(["A", "B", "C", "A"])
    assert inner.batches == [["A", "B", "C"]], inner.batches  # deduped, one batch
    # Subsequent single lookups hit the cache the batch filled.
    assert s.get_report_summary("A") == {"code": "A", "via": "batch"}
    assert s.get_report_summary("B") == {"code": "B", "via": "batch"}
    assert inner.single == 0


# --- SessionCachedClient character list/pull caching ------------------------

class _CharInner:
    def __init__(self):
        self.enc_calls: list[tuple] = []
        self.pull_calls: list[tuple] = []

    def get_character_zone_encounters(self, lodestone_id, zone_id,
                                      spec_name="Machinist", difficulty=101):
        self.enc_calls.append((lodestone_id, zone_id, spec_name, difficulty))
        return [] if spec_name == "Dancer" else [{"id": 1, "name": spec_name}]

    def get_character_encounter_pulls(self, lodestone_id, encounter_id,
                                      spec_name="Machinist", difficulty=101):
        self.pull_calls.append((lodestone_id, encounter_id, spec_name, difficulty))
        return [{"report_code": "R", "fight_id": encounter_id}]


def _check_session_char_caching() -> None:
    inner = _CharInner()
    s = SessionCachedClient(inner)

    # First fetch hits the inner; repeats (e.g. swap job away + back) are cached.
    a1 = s.get_character_zone_encounters(99, 73, spec_name="Machinist", difficulty=101)
    a2 = s.get_character_zone_encounters(99, 73, spec_name="Machinist", difficulty=101)
    assert a1 == a2 == [{"id": 1, "name": "Machinist"}]
    assert len(inner.enc_calls) == 1, inner.enc_calls

    # A different spec is a distinct key — fetched once, then cached.
    s.get_character_zone_encounters(99, 73, spec_name="Samurai", difficulty=101)
    s.get_character_zone_encounters(99, 73, spec_name="Samurai", difficulty=101)
    assert len(inner.enc_calls) == 2, inner.enc_calls

    # An empty result ("no logs for this job") is a real answer and is cached.
    s.get_character_zone_encounters(99, 73, spec_name="Dancer", difficulty=101)
    s.get_character_zone_encounters(99, 73, spec_name="Dancer", difficulty=101)
    assert len(inner.enc_calls) == 3, inner.enc_calls

    # Pulls cache keys on (lodestone, encounter, spec, difficulty).
    p1 = s.get_character_encounter_pulls(99, 1, spec_name="Machinist", difficulty=101)
    p2 = s.get_character_encounter_pulls(99, 1, spec_name="Machinist", difficulty=101)
    assert p1 == p2
    s.get_character_encounter_pulls(99, 2, spec_name="Machinist", difficulty=101)
    assert len(inner.pull_calls) == 2, inner.pull_calls


# --- DevDiskCacheClient -----------------------------------------------------

class _DevInner:
    def __init__(self):
        self.aura = 0
        self.bundle = 0
        self.batches: list[list[str]] = []
        self.single = 0

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        self.aura += 1
        return [{"a": actor_id}]

    def get_event_bundle(self, code, streams):
        self.bundle += 1
        return [[{"s": s.data_type}] for s in streams]

    def get_report_summaries(self, codes):
        self.batches.append(list(codes))
        return {c: {"code": c} for c in codes}

    def get_report_summary(self, code):
        self.single += 1
        return {"code": code, "via": "single"}


def _check_dev_cache_new_methods(cache_dir: Path) -> None:
    inner = _DevInner()
    c = DevDiskCacheClient(inner, cache_dir)

    assert c.get_aura_events("R", 0, 100, 5) == [{"a": 5}]
    assert c.get_aura_events("R", 0, 100, 5) == [{"a": 5}]
    assert inner.aura == 1

    streams = [BundleStream(data_type="Casts", start=0, end=100, source_id=5)]
    assert c.get_event_bundle("R", streams) == [[{"s": "Casts"}]]
    assert c.get_event_bundle("R", streams) == [[{"s": "Casts"}]]
    assert inner.bundle == 1

    # Batch summaries write per-code files keyed identically to get_report_summary.
    out = c.get_report_summaries(["X", "Y"])
    assert out == {"X": {"code": "X"}, "Y": {"code": "Y"}}
    assert inner.batches == [["X", "Y"]]
    # get_report_summary("X") now reads the file the batch wrote — inner single
    # path is never hit.
    assert c.get_report_summary("X") == {"code": "X"}
    assert inner.single == 0
    # A second batch is fully served from disk (no new inner batch).
    c.get_report_summaries(["X", "Y"])
    assert inner.batches == [["X", "Y"]]


# --- get_character_setup (one-shot SetupView query) -------------------------

def _check_get_character_setup() -> None:
    c = FFLogsClient("id", "secret")
    captured: dict = {}

    def fake_query(gql, variables=None):
        captured["gql"] = gql
        captured["vars"] = variables
        # One character query aliasing per-zone zoneRankings + per-encounter ranks.
        return {"characterData": {"character": {
            "z73": {"rankings": [
                {"encounter": {"id": 101, "name": "M9S"}, "totalKills": 3, "rankPercent": 88.0},
                # 0 kills → dropped from the encounter list.
                {"encounter": {"id": 102, "name": "M10S"}, "totalKills": 0, "rankPercent": None},
            ]},
            "z76": {"rankings": [
                {"encounter": {"id": 1085, "name": "Dancing Mad"}, "totalKills": 1, "rankPercent": 50.0},
            ]},
            "e101": {"ranks": [
                {"report": {"code": "AAA", "fightID": 7}, "startTime": 1000,
                 "duration": 500000, "rankPercent": 88.0, "amount": 36000.0, "spec": "Machinist"},
            ]},
            "e102": {"ranks": []},
            "e1085": {"ranks": []},
        }}}

    c.query = fake_query  # type: ignore[assignment]
    out = c.get_character_setup(
        99, [(73, 101, [101, 102]), (76, 100, [1085])], spec_name="Machinist")
    # One aliased request: a z{zone} alias per group (each with its own
    # difficulty literal) + an e{id} alias per encounter.
    assert "z73: zoneRankings(zoneID: 73" in captured["gql"]
    assert "z76: zoneRankings(zoneID: 76" in captured["gql"]
    assert "difficulty: 101" in captured["gql"]
    assert "e101: encounterRankings(encounterID: 101" in captured["gql"]
    assert "e102: encounterRankings(encounterID: 102" in captured["gql"]
    assert "e1085: encounterRankings(encounterID: 1085, specName: $spec, difficulty: 100" in captured["gql"]
    assert captured["vars"]["spec"] == "Machinist"
    # Encounters merged in group order; only those with kills survive.
    assert out["encounters"] == [
        {"id": 101, "name": "M9S", "total_kills": 3, "best_parse_pct": 88.0},
        {"id": 1085, "name": "Dancing Mad", "total_kills": 1, "best_parse_pct": 50.0}]
    # Pulls split by encounter id; the empty encounters map to [].
    assert out["pulls"][101][0]["report_code"] == "AAA"
    assert out["pulls"][101][0]["fight_id"] == 7
    assert out["pulls"][102] == []
    assert out["pulls"][1085] == []


class _SetupInner:
    def __init__(self):
        self.calls = 0

    def get_character_setup(self, lodestone_id, groups, spec_name="Machinist"):
        self.calls += 1
        return {"encounters": [{"id": 1, "name": spec_name}], "pulls": {1: []}}


def _check_session_setup_caching() -> None:
    inner = _SetupInner()
    s = SessionCachedClient(inner)
    groups = [(73, 101, [1, 2])]
    a = s.get_character_setup(99, groups, spec_name="Machinist")
    b = s.get_character_setup(99, groups, spec_name="Machinist")
    assert a == b
    assert inner.calls == 1, "second identical setup call should hit cache"
    # A different spec is a distinct key — one more fetch.
    s.get_character_setup(99, groups, spec_name="Samurai")
    assert inner.calls == 2, inner.calls
    # A different group list (new tier / added ultimate) is a distinct key too.
    s.get_character_setup(99, [(73, 101, [1, 2]), (76, 100, [3])],
                          spec_name="Samurai")
    assert inner.calls == 3, inner.calls


# --- provider_cast_streams (on-enemy raid-buff casts folded into the bundle) -

def _check_provider_cast_streams() -> None:
    from jobs._core.buff_windows import provider_cast_streams
    # Scholar present → Chain Stratagem (on_enemy) cast stream expected.
    report = {"masterData": {
        "actors": [
            {"id": 5, "type": "Player", "subType": "Scholar"},
            {"id": 6, "type": "Player", "subType": "Machinist"},
        ],
        "abilities": [
            {"name": "Chain Stratagem", "gameID": 7436},      # cast action (< 1M)
            {"name": "Chain Stratagem", "gameID": 1001221},   # aura form (ignored)
        ],
    }}
    fight = {"startTime": 1000, "endTime": 601000, "friendlyPlayers": [5, 6]}
    streams = provider_cast_streams(report, fight)
    assert len(streams) == 1, streams
    st = streams[0]
    assert st.data_type == "Casts" and st.source_id == 5 and st.ability_id == 7436
    assert st.start == 1000 and st.end == 601000
    # No on-enemy provider in the party → no streams.
    fight_no_prov = {"startTime": 0, "endTime": 100, "friendlyPlayers": [6]}
    assert provider_cast_streams(report, fight_no_prov) == []


# --- pytest entry points ----------------------------------------------------

def test_get_character_setup():
    _check_get_character_setup()


def test_session_setup_caching():
    _check_session_setup_caching()


def test_provider_cast_streams():
    _check_provider_cast_streams()


def test_event_bundle_pagination():
    _check_event_bundle_pagination()


def test_report_summaries_chunking():
    _check_report_summaries_chunking()


def test_report_summaries_dedupes():
    _check_report_summaries_dedupes()


def test_prime_bundle_seeds_caches():
    _check_prime_bundle_seeds_caches()


def test_aura_events_dedup():
    _check_aura_events_dedup()


def test_aura_narrowing():
    _check_aura_narrowing()


def test_session_prefetch_report_summaries():
    _check_session_prefetch()


def test_session_char_caching():
    _check_session_char_caching()


def test_dev_cache_new_methods(tmp_path):
    _check_dev_cache_new_methods(tmp_path)


def main() -> None:
    _check_get_character_setup()
    _check_session_setup_caching()
    _check_provider_cast_streams()
    _check_event_bundle_pagination()
    _check_report_summaries_chunking()
    _check_report_summaries_dedupes()
    _check_prime_bundle_seeds_caches()
    _check_aura_events_dedup()
    _check_aura_narrowing()
    _check_session_prefetch()
    _check_session_char_caching()
    with tempfile.TemporaryDirectory() as d:
        _check_dev_cache_new_methods(Path(d))
    print("test_api_batching: OK")


if __name__ == "__main__":
    main()
