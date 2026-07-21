"""On-disk cache of raw FFLogs responses (dev AND prod).

`DevDiskCacheClient` is a transparent wrapper around `FFLogsClient` (or any
inner client) that memoizes the cost-driving read methods to JSON files
on disk:

    get_report_summary(_summaries), get_events, get_event_bundle,
    get_targetability_events, get_aura_events, get_rankings

Together those dominate the cost of warming the top-10 reference matrix
(rankings + ~10 `analyze_pull` per (job, encounter)). Caching them on disk
means a restart re-warms the whole matrix without re-hitting FFLogs.

Why cache the *response* layer rather than pickling `ModuleResult`:
the analyzer (aspects, state shapes) is under active change, so a pickled
`ModuleResult` would rot. Raw GraphQL responses are stable JSON — recomputing
the (cheap) analysis from cached responses on each launch survives analyzer
edits. The cache is disposable: delete the cache dir to bust it.

`sidecar.main._client()` composes this layer beneath `SessionCachedClient`:
  - dev (`is_dev`): permanent entries under `DEV_CACHE_DIR`.
  - prod: `ttls` give a short expiry to `get_rankings` only (the top-10
    *membership* drifts), while a logged report's events/summary are
    IMMUTABLE so they get no TTL. See `_PROD_CACHE_TTLS` in main.py.
  Both paths get `max_bytes` — the user-set Settings cap (oldest-first
  eviction; `_CACHE_CAP_*` in main.py).

Storage format: gzipped compact JSON, still under the human-greppable
`<method>_<sha1>.json` names (event streams are so repetitive that gzip cuts
the directory ~13x — measured 16x on event bundles — so the same size cap
holds an order of magnitude more pulls). Reads sniff the gzip magic bytes, so
plain-JSON entries written by older builds keep serving hits after an upgrade.

Failure policy: any read/parse/write error is swallowed and treated as a
miss, so a corrupt or unwritable cache can never break a real fetch.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

# The read methods worth caching. Each has an explicit wrapper below so the
# cache key is built from named args (unambiguous regardless of how callers
# pass positional vs keyword).
_CACHED_METHODS = (
    "get_report_summary",
    "get_report_summaries",
    "get_events",
    "get_event_bundle",
    "get_targetability_events",
    "get_enemy_cast_events",
    "get_aura_events",
    "get_rankings",
)

# Run a size-eviction sweep once every this many writes (cheap amortization —
# a full dir scan per write would be wasteful). Only relevant when max_bytes set.
_EVICT_EVERY = 256


class DevDiskCacheClient:
    """Transparent FFLogs response cache for development. Unknown attributes
    pass through to the inner client (so lookups, auth, `query`, etc. are
    untouched)."""

    def __init__(self, inner: Any, cache_dir: Path,
                 ttls: dict[str, float] | None = None,
                 max_bytes: int | None = None):
        self._inner = inner
        self._dir = Path(cache_dir)
        # Per-method max age (seconds). A method absent here (or mapped to None)
        # never expires — used for immutable report/event data; only mutable
        # queries (get_rankings) get a TTL on the prod path. Empty in dev.
        self._ttls = dict(ttls or {})
        # Total on-disk cap. None ⇒ unbounded (dev). Eviction is oldest-first
        # by mtime, run on init and periodically after writes.
        self._max_bytes = max_bytes
        # Per-key locks so two threads warming the same pull don't both write
        # the same file. Cheap: keyed by the on-disk filename.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Writes since the last eviction sweep + the guard that serializes them.
        self._writes = 0
        self._evict_lock = threading.Lock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Trim a dir that's already over cap from a prior session (one-time,
        # best-effort — a huge dir scan at startup is acceptable and rare).
        if self._max_bytes is not None:
            self._evict()

    # --- cache primitives ---------------------------------------------------

    def _path_for(self, parts: tuple) -> Path:
        raw = "|".join(str(p) for p in parts)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        # Prefix the file with the method name for human-greppable cache dirs.
        return self._dir / f"{parts[0]}_{digest}.json"

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._locks_guard:
            lk = self._locks.get(path.name)
            if lk is None:
                lk = threading.Lock()
                self._locks[path.name] = lk
            return lk

    def _read(self, path: Path, ttl: float | None = None) -> Any:
        try:
            if ttl is not None and (time.time() - path.stat().st_mtime) > ttl:
                return _MISS   # expired — treat as a miss so produce() refreshes
            raw = path.read_bytes()
            if raw[:2] == b"\x1f\x8b":   # gzip magic — what _write produces
                raw = gzip.decompress(raw)
            return json.loads(raw)       # legacy entries are plain UTF-8 JSON
        except Exception:
            return _MISS

    def _write(self, path: Path, value: Any) -> None:
        try:
            tmp = path.with_suffix(".json.tmp")
            payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
            # mtime=0 keeps the bytes deterministic for a given value; the
            # TTL/eviction clock is the file's own st_mtime, not this header.
            tmp.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
            os.replace(tmp, path)
        except Exception:
            # Best-effort: a failed write just means a miss next time.
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            except Exception:
                pass

    def _cached(self, parts: tuple, produce) -> Any:
        ttl = self._ttls.get(parts[0])   # parts[0] is the method name
        path = self._path_for(parts)
        hit = self._read(path, ttl)
        if hit is not _MISS:
            return hit
        with self._lock_for(path):
            # Re-check inside the lock — another thread may have produced it.
            hit = self._read(path, ttl)
            if hit is not _MISS:
                return hit
            fresh = produce()
            self._write(path, fresh)
            self._note_write()
            return fresh

    def _note_write(self) -> None:
        """Amortized size-eviction trigger. The counter race is benign — a
        missed/duplicate tick only shifts when the (best-effort) sweep runs."""
        if self._max_bytes is None:
            return
        self._writes += 1
        if self._writes % _EVICT_EVERY == 0:
            self._evict()

    def _evict(self) -> None:
        """Best-effort oldest-first trim to keep the dir under `max_bytes`.
        Serialized by a non-blocking lock so concurrent writers don't all scan."""
        cap = self._max_bytes
        if cap is None or not self._evict_lock.acquire(blocking=False):
            return
        try:
            entries: list[tuple[float, int, Path]] = []
            total = 0
            for p in self._dir.glob("*.json"):
                try:
                    stt = p.stat()
                except Exception:
                    continue
                entries.append((stt.st_mtime, stt.st_size, p))
                total += stt.st_size
            if total <= cap:
                return
            # Trim to ~90% of the cap so we don't evict on every sweep.
            target = int(cap * 0.9)
            entries.sort(key=lambda e: e[0])   # oldest first
            for _mtime, size, p in entries:
                if total <= target:
                    break
                try:
                    p.unlink()
                    total -= size
                except Exception:
                    pass
        finally:
            self._evict_lock.release()

    # --- cached method wrappers --------------------------------------------

    # Bump when _REPORT_SUMMARY_FIELDS grows a field consumers rely on —
    # entries are permanent, so a stale summary would otherwise miss the new
    # field forever. Orphaned old-key files age out via the size cap.
    # v2: abilities gained `type` (damage school, mitigation planner).
    # v3: fights gained `fightPercentage`/`bossPercentage` (prog pulls).
    # v4: report gained top-level `phases` (named phase metadata, phasic ults).
    _SUMMARY_FIELDS_V = 4

    def get_report_summary(self, code: str) -> Any:
        return self._cached(
            ("get_report_summary", code, self._SUMMARY_FIELDS_V),
            lambda: self._inner.get_report_summary(code),
        )

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> Any:
        return self._cached(
            ("get_events", code, start, end, source_id, data_type, ability_id),
            lambda: self._inner.get_events(code, start, end, source_id,
                                           data_type=data_type,
                                           ability_id=ability_id),
        )

    def get_targetability_events(self, code: str, start: int,
                                  end: int) -> Any:
        return self._cached(
            ("get_targetability_events", code, start, end),
            lambda: self._inner.get_targetability_events(code, start, end),
        )

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> Any:
        return self._cached(
            ("get_enemy_cast_events", code, start, end),
            lambda: self._inner.get_enemy_cast_events(code, start, end),
        )

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> Any:
        return self._cached(
            ("get_aura_events", code, start, end, actor_id, data_type),
            lambda: self._inner.get_aura_events(code, start, end, actor_id,
                                                data_type=data_type),
        )

    def get_event_bundle(self, code: str, streams: list) -> Any:
        # Key on the stream specs so two pulls with the same bundle share a file.
        # `include_resources` participates only when set, so every pre-existing
        # key (and its cached entry) is preserved byte-identically.
        spec = json.dumps([
            [s.data_type, s.start, s.end, s.source_id, s.ability_id,
             s.filter_expression, getattr(s, "hostility", None)]
            + ([True] if getattr(s, "include_resources", False) else [])
            for s in streams
        ])
        return self._cached(
            ("get_event_bundle", code, spec),
            lambda: self._inner.get_event_bundle(code, streams),
        )

    def get_report_summaries(self, codes: list[str]) -> dict:
        """Per-code coherent with `get_report_summary`: serve cached codes from
        their individual files, batch-fetch only the misses, and write each
        result under the same single-summary key — so the batch and single
        paths share disk entries."""
        # Coherent with the single-summary path: same key, same (report-summary)
        # TTL — which is permanent today (a logged report is immutable).
        ttl = self._ttls.get("get_report_summary")
        out: dict = {}
        missing: list[str] = []
        for code in dict.fromkeys(codes):
            path = self._path_for(
                ("get_report_summary", code, self._SUMMARY_FIELDS_V))
            hit = self._read(path, ttl)
            if hit is not _MISS:
                out[code] = hit
            else:
                missing.append(code)
        if missing:
            fetched = self._inner.get_report_summaries(missing) or {}
            for code, summary in fetched.items():
                out[code] = summary
                if summary is not None:
                    self._write(self._path_for(
                        ("get_report_summary", code, self._SUMMARY_FIELDS_V)),
                        summary)
                    self._note_write()
        return out

    def get_rankings(self, encounter_id: int, class_name: str, spec_name: str,
                     difficulty: int = 101, metric: str = "rdps",
                     page: int = 1) -> Any:
        return self._cached(
            ("get_rankings", encounter_id, class_name, spec_name, difficulty,
             metric, page),
            lambda: self._inner.get_rankings(
                encounter_id=encounter_id, class_name=class_name,
                spec_name=spec_name, difficulty=difficulty,
                metric=metric, page=page),
        )

    def set_cache_cap(self, max_bytes: int | None) -> None:
        """Live-update the size cap (the Settings slider) and trim to it
        immediately. None ⇒ unbounded."""
        self._max_bytes = max_bytes
        if max_bytes is not None:
            self._evict()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Miss:
    """Sentinel distinct from a legitimately-cached `None`/`null`."""
    __slots__ = ()


_MISS = _Miss()
