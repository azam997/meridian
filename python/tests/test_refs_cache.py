"""Reference warm-cache + dev disk cache + catalog.

Hermetic — no network, no FFLogs client. Exercises:
  - `sidecar.main._get_refs`: caches a built ref list and collapses concurrent
    same-key callers to a single build; serves later calls from cache.
  - `sidecar.dev_cache.DevDiskCacheClient`: write-then-read round trip, cross-
    instance ("relaunch") persistence, distinct keys, `None` caching, and
    attribute passthrough.
  - `sidecar.main.get_catalog`: shape + supported-jobs-only contents.

Runs under pytest (from python/) and standalone (`python tests/test_refs_cache.py`).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Allow `python tests/test_refs_cache.py` standalone (pytest gets this via
# conftest, but a bare script run starts with sys.path[0] = tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sidecar.main as m  # noqa: E402
from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402


# --- _get_refs cache + dedup ------------------------------------------------

def _check_get_refs_dedup() -> None:
    m._refs_cache.clear()
    m._refs_inflight.clear()
    calls: list[tuple] = []
    lock = threading.Lock()

    def fake_build(client, job, enc, bucket, progress):
        with lock:
            calls.append((job, enc, bucket))
        # Stay "in flight" long enough that concurrent callers register as
        # waiters on the in-flight Event instead of all racing to own.
        time.sleep(0.05)
        return [f"ref-{job}-{enc}"]  # non-empty → cached

    orig = m._build_refs
    m._build_refs = fake_build  # type: ignore[assignment]
    try:
        results: list = []
        rlock = threading.Lock()

        def worker():
            r = m._get_refs(None, "Machinist", 103, "Top 10", lambda *a, **k: None)
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1, f"expected one build, got {len(calls)}: {calls}"
        assert all(r == ["ref-Machinist-103"] for r in results), results

        # A later call is served from cache — still just one build total.
        again = m._get_refs(None, "Machinist", 103, "Top 10", lambda *a, **k: None)
        assert again == ["ref-Machinist-103"]
        assert len(calls) == 1
    finally:
        m._build_refs = orig  # type: ignore[assignment]
        m._refs_cache.clear()
        m._refs_inflight.clear()


def _check_get_refs_does_not_cache_empty() -> None:
    m._refs_cache.clear()
    m._refs_inflight.clear()
    calls = {"n": 0}

    def fake_build(client, job, enc, bucket, progress):
        calls["n"] += 1
        return []  # failure / no rankings → must NOT be cached

    orig = m._build_refs
    m._build_refs = fake_build  # type: ignore[assignment]
    try:
        a = m._get_refs(None, "Samurai", 101, "Top 10", lambda *x, **k: None)
        b = m._get_refs(None, "Samurai", 101, "Top 10", lambda *x, **k: None)
        assert a == [] and b == []
        # Empty results aren't cached, so the second call rebuilds (retry).
        assert calls["n"] == 2, calls
    finally:
        m._build_refs = orig  # type: ignore[assignment]
        m._refs_cache.clear()
        m._refs_inflight.clear()


# --- DevDiskCacheClient -----------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.calls = 0

    def get_rankings(self, encounter_id, class_name, spec_name,
                     difficulty=101, metric="rdps", page=1):
        self.calls += 1
        return {"rankings": [{"enc": encounter_id, "seq": self.calls}]}

    def get_report_summary(self, code):
        self.calls += 1
        return {"code": code, "seq": self.calls}

    def whoami(self):  # not in the cached set → must pass through
        return "fake"


def _check_dev_cache_roundtrip(cache_dir: Path) -> None:
    inner = _FakeClient()
    c = DevDiskCacheClient(inner, cache_dir)

    r1 = c.get_rankings(103, "Machinist", "Machinist")
    r2 = c.get_rankings(103, "Machinist", "Machinist")
    assert r1 == r2
    assert inner.calls == 1, "second identical call should hit disk, not inner"

    # Distinct args → distinct key → fresh fetch.
    c.get_rankings(104, "Machinist", "Machinist")
    assert inner.calls == 2

    # Unknown attribute passes through to the inner client.
    assert c.whoami() == "fake"

    # A brand-new wrapper over the same dir reads the persisted file — the
    # cross-"launch" guarantee the dev cache exists for.
    inner2 = _FakeClient()
    c2 = DevDiskCacheClient(inner2, cache_dir)
    assert c2.get_rankings(103, "Machinist", "Machinist") == r1
    assert inner2.calls == 0, "fresh instance should serve purely from disk"


def _check_dev_cache_caches_none(cache_dir: Path) -> None:
    class _NoneClient:
        def __init__(self):
            self.calls = 0

        def get_report_summary(self, code):
            self.calls += 1
            return None

    inner = _NoneClient()
    c = DevDiskCacheClient(inner, cache_dir)
    assert c.get_report_summary("ABC") is None
    assert c.get_report_summary("ABC") is None
    # `None` is a legitimate cached value (sentinel distinguishes it from miss).
    assert inner.calls == 1, "None should be cached, not re-fetched"


def _check_dev_cache_ttl(cache_dir: Path) -> None:
    """Prod policy: only the mutable rankings query expires; immutable report
    data (no TTL key) keeps serving from disk past the rankings horizon."""
    inner = _FakeClient()
    c = DevDiskCacheClient(inner, cache_dir, ttls={"get_rankings": 0.05})
    c.get_rankings(103, "Machinist", "Machinist")
    c.get_report_summary("CODE")
    assert inner.calls == 2
    # Within the rankings TTL → still cached.
    c.get_rankings(103, "Machinist", "Machinist")
    assert inner.calls == 2
    time.sleep(0.07)
    # Rankings expired → re-fetch; the (TTL-less) report summary stays cached.
    c.get_rankings(103, "Machinist", "Machinist")
    assert inner.calls == 3, "expired rankings should re-fetch"
    c.get_report_summary("CODE")
    assert inner.calls == 3, "report summary has no TTL → still cached"


def _check_dev_cache_eviction(cache_dir: Path) -> None:
    """A size-capped cache evicts oldest-first once over the cap (here on init,
    re-opening a dir that a prior uncapped session filled past the new cap).
    Blobs are incompressible (random hex) so the on-disk gzip size — what the
    cap governs — stays ~8 KB per entry."""
    class _BigClient:
        def __init__(self):
            self.calls = 0

        def get_report_summary(self, code):
            self.calls += 1
            return {"code": code, "blob": os.urandom(8000).hex()}

    inner = _BigClient()
    # Fill uncapped with six ~8 KB entries.
    c1 = DevDiskCacheClient(inner, cache_dir)
    for i in range(6):
        c1.get_report_summary(f"C{i}")
    assert len(list(cache_dir.glob("*.json"))) == 6

    # Re-open with a small cap → init eviction trims the dir under it.
    cap = 24_000  # room for ~3 entries
    DevDiskCacheClient(inner, cache_dir, max_bytes=cap)
    files = list(cache_dir.glob("*.json"))
    total = sum(p.stat().st_size for p in files)
    assert total <= cap, f"dir not trimmed under cap: {total} > {cap}"
    assert len(files) < 6, "some entries should have been evicted"


def _check_dev_cache_gzip_and_legacy(cache_dir: Path) -> None:
    """New entries are gzipped compact JSON; plain-JSON files written by older
    builds are still read (magic-byte sniff), so an upgrade keeps the warm
    cache."""
    inner = _FakeClient()
    c = DevDiskCacheClient(inner, cache_dir)

    val = c.get_report_summary("GZ")
    # The summary key carries a fields-version component (bumped when the
    # summary query grows a field consumers rely on — see _SUMMARY_FIELDS_V).
    key = ("get_report_summary", "GZ", DevDiskCacheClient._SUMMARY_FIELDS_V)
    path = c._path_for(key)
    raw = path.read_bytes()
    assert raw[:2] == b"\x1f\x8b", "fresh entries should be gzipped"
    assert json.loads(gzip.decompress(raw)) == val

    # Legacy plain-JSON entry (what pre-gzip builds wrote) still serves a hit.
    legacy = {"code": "LEG", "seq": 999}
    c._path_for(("get_report_summary", "LEG",
                 DevDiskCacheClient._SUMMARY_FIELDS_V)).write_text(
        json.dumps(legacy), encoding="utf-8")
    before = inner.calls
    assert c.get_report_summary("LEG") == legacy
    assert inner.calls == before, "legacy file should be a hit, not a re-fetch"


# --- get_catalog ------------------------------------------------------------

def _check_catalog() -> None:
    cat = m.get_catalog({})
    assert set(cat) == {"supportedJobs", "simBackedJobs", "encounters",
                        "buffProviders"}
    jobs = cat["supportedJobs"]
    assert "Machinist" in jobs and "Samurai" in jobs and "Reaper" in jobs
    assert "Red Mage" in jobs and "Paladin" in jobs and "Warrior" in jobs
    assert "White Mage" in jobs and "Dancer" in jobs and "Black Mage" in jobs
    assert "Viper" in jobs and "Dragoon" in jobs and "Gunbreaker" in jobs
    assert "Ninja" in jobs and "Monk" in jobs and "Bard" in jobs
    assert "Pictomancer" in jobs and "Summoner" in jobs
    assert "Dark Knight" in jobs and "Astrologian" in jobs and "Scholar" in jobs
    assert "Sage" in jobs
    # Supported jobs only — exactly the registered set today.
    assert len(jobs) == 21, jobs
    # Sim-backed jobs — the theorizer's job picker. All twenty-one registered
    # jobs ship a full idealized simulator (Sage is the twenty-first, the
    # fourth healer).
    sim_jobs = cat["simBackedJobs"]
    assert "Machinist" in sim_jobs and "Reaper" in sim_jobs and "Red Mage" in sim_jobs
    assert "Paladin" in sim_jobs and "Warrior" in sim_jobs
    assert "Samurai" in sim_jobs and "White Mage" in sim_jobs and "Dancer" in sim_jobs
    assert "Black Mage" in sim_jobs and "Viper" in sim_jobs and "Dragoon" in sim_jobs
    assert "Gunbreaker" in sim_jobs and "Ninja" in sim_jobs and "Monk" in sim_jobs \
        and "Bard" in sim_jobs and "Pictomancer" in sim_jobs \
        and "Summoner" in sim_jobs and "Dark Knight" in sim_jobs \
        and "Astrologian" in sim_jobs and "Scholar" in sim_jobs \
        and "Sage" in sim_jobs, sim_jobs
    encs = cat["encounters"]
    # id + name, the Savage/Ultimates tab tag (encounter_category), and whether
    # a hand-authored premade ("PF") mit plan ships (the planner's PF toggle).
    assert encs and all(
        set(e) == {"id", "name", "category", "hasPfPlan"} for e in encs)
    assert all(e["category"] in ("savage", "ultimate") for e in encs)
    assert any(e["id"] == 103 for e in encs)  # The Tyrant (M11S)
    # Dancing Mad is the wired ultimate; the Savage tier is 'savage'.
    assert any(e["id"] == 1085 and e["category"] == "ultimate" for e in encs)
    assert next(e for e in encs if e["id"] == 103)["category"] == "savage"
    # Dancing Mad ships a premade plan (premade/1085.json); Savage does not.
    assert next(e for e in encs if e["id"] == 1085)["hasPfPlan"] is True
    assert next(e for e in encs if e["id"] == 103)["hasPfPlan"] is False
    # Raid-buff providers — the Kill Time Theorizer's comp picker set.
    providers = cat["buffProviders"]
    assert providers and "Bard" in providers and "Dragoon" in providers


# --- pytest entry points ----------------------------------------------------

def test_get_refs_caches_and_dedups():
    _check_get_refs_dedup()


def test_get_refs_does_not_cache_empty():
    _check_get_refs_does_not_cache_empty()


def test_dev_disk_cache_roundtrip(tmp_path):
    _check_dev_cache_roundtrip(tmp_path)


def test_dev_disk_cache_caches_none(tmp_path):
    _check_dev_cache_caches_none(tmp_path)


def test_dev_disk_cache_ttl(tmp_path):
    _check_dev_cache_ttl(tmp_path)


def test_dev_disk_cache_eviction(tmp_path):
    _check_dev_cache_eviction(tmp_path)


def test_dev_disk_cache_gzip_and_legacy(tmp_path):
    _check_dev_cache_gzip_and_legacy(tmp_path)


def test_get_catalog_shape():
    _check_catalog()


def main() -> None:
    _check_get_refs_dedup()
    _check_get_refs_does_not_cache_empty()
    with tempfile.TemporaryDirectory() as d:
        _check_dev_cache_roundtrip(Path(d) / "rt")
        _check_dev_cache_caches_none(Path(d) / "none")
        _check_dev_cache_ttl(Path(d) / "ttl")
        _check_dev_cache_eviction(Path(d) / "evict")
        _check_dev_cache_gzip_and_legacy(Path(d) / "gz")
    _check_catalog()
    print("test_refs_cache: OK")


if __name__ == "__main__":
    main()
