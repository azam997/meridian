"""Per-pull event cache + session-level rankings cache.

Two wrappers compose:
- `SessionCachedClient` wraps a long-lived FFLogsClient and caches
  `get_rankings`, the report summaries, and the character list/pull queries
  (`get_character_zone_encounters` / `get_character_encounter_pulls`) across
  the whole sidecar session.
- `CachedEventsClient` wraps the session client for the duration of one
  `analyze_pull` and memoizes per-aspect `get_events` calls.

Both pass unknown attrs through to the inner client.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any


_DEFAULT_RANKINGS_CACHE_CAP = 32
_DEFAULT_REPORT_CACHE_CAP = 48
# Character list/pull queries. A character browsing every job × tier encounter
# tops out around (jobs × encounters) tiny entries, so this is sized generously
# — the whole point is to keep them all resident for instant job re-selection.
_DEFAULT_CHAR_CACHE_CAP = 128


class SessionCachedClient:
    """Process-lifetime wrapper that caches the read-only FFLogs queries that
    don't change within a sidecar session: `get_rankings`, report summaries,
    and the character list/pull queries (`get_character_zone_encounters` /
    `get_character_encounter_pulls`). The same request can then be reused
    across ref fetches and across repeated UI navigation (e.g. swapping a job
    away and back in SetupView).

    LRU-style eviction (FIFO once cap reached). Thread-safe."""

    def __init__(self, inner: Any, cap: int = _DEFAULT_RANKINGS_CACHE_CAP,
                 report_cap: int = _DEFAULT_REPORT_CACHE_CAP,
                 char_cap: int = _DEFAULT_CHAR_CACHE_CAP):
        self._inner = inner
        self._rankings_cache: OrderedDict[tuple, Any] = OrderedDict()
        self._rankings_lock = threading.Lock()
        self._cap = cap
        # Report-summary cache (mirrors the rankings cache). Lets the batched
        # `prefetch_report_summaries` warm every ref's summary in one request,
        # which each per-ref `analyze_pull` then hits.
        self._report_cache: OrderedDict[str, Any] = OrderedDict()
        self._report_lock = threading.Lock()
        self._report_cap = report_cap
        # Character list/pull caches. These back the SetupView encounter
        # dropdown + the per-encounter pulls fan-out; without them every job
        # switch re-issues a live GraphQL round-trip for data that doesn't
        # change within a session.
        self._encounters_cache: OrderedDict[tuple, Any] = OrderedDict()
        self._encounters_lock = threading.Lock()
        self._pulls_cache: OrderedDict[tuple, Any] = OrderedDict()
        self._pulls_lock = threading.Lock()
        # The one-shot SetupView query (encounters + all pulls in one round trip).
        self._setup_cache: OrderedDict[tuple, Any] = OrderedDict()
        self._setup_lock = threading.Lock()
        self._char_cap = char_cap

    def get_report_summary(self, code: str) -> Any:
        with self._report_lock:
            cached = self._report_cache.get(code)
            if cached is not None:
                self._report_cache.move_to_end(code)
                return cached
        fresh = self._inner.get_report_summary(code)
        self._store_report(code, fresh)
        return fresh

    def _store_report(self, code: str, value: Any) -> None:
        if value is None:
            return
        with self._report_lock:
            self._report_cache[code] = value
            self._report_cache.move_to_end(code)
            while len(self._report_cache) > self._report_cap:
                self._report_cache.popitem(last=False)

    def prefetch_report_summaries(self, codes: list[str]) -> None:
        """Batch-fetch the summaries for `codes` not already cached, in one
        `get_report_summaries` request, and cache each. Best-effort: a failure
        is swallowed so callers fall back to per-report `get_report_summary`."""
        with self._report_lock:
            missing = [c for c in dict.fromkeys(codes)
                       if c not in self._report_cache]
        if not missing:
            return
        try:
            fetched = self._inner.get_report_summaries(missing)
        except Exception:
            return
        for code, summary in (fetched or {}).items():
            self._store_report(code, summary)

    def get_rankings(self, encounter_id: int, class_name: str, spec_name: str,
                     difficulty: int = 101, metric: str = "rdps",
                     page: int = 1) -> Any:
        key = (encounter_id, class_name, spec_name, difficulty, metric, page)
        with self._rankings_lock:
            cached = self._rankings_cache.get(key)
            if cached is not None:
                self._rankings_cache.move_to_end(key)
                return cached
        # Outside the lock — let other rankings calls proceed in parallel.
        fresh = self._inner.get_rankings(
            encounter_id=encounter_id, class_name=class_name,
            spec_name=spec_name, difficulty=difficulty,
            metric=metric, page=page,
        )
        with self._rankings_lock:
            self._rankings_cache[key] = fresh
            self._rankings_cache.move_to_end(key)
            while len(self._rankings_cache) > self._cap:
                self._rankings_cache.popitem(last=False)
        return fresh

    @staticmethod
    def _lru_get_or_fetch(cache: "OrderedDict[tuple, Any]", lock: threading.Lock,
                          cap: int, key: tuple, fetch: Any) -> Any:
        """LRU get-or-fetch. Returns the cached value for `key`, or computes it
        via `fetch()` (called OUTSIDE the lock so concurrent distinct keys
        don't serialize) and stores it with FIFO eviction at `cap`. An empty
        list/result is a legitimate answer ("no logs for this job") and is
        cached too — it won't change within the session."""
        with lock:
            hit = cache.get(key)
            if hit is not None:
                cache.move_to_end(key)
                return hit
        fresh = fetch()
        with lock:
            cache[key] = fresh
            cache.move_to_end(key)
            while len(cache) > cap:
                cache.popitem(last=False)
        return fresh

    def get_character_zone_encounters(self, lodestone_id: int, zone_id: int,
                                      spec_name: str = "Machinist",
                                      difficulty: int = 101) -> Any:
        key = (lodestone_id, zone_id, spec_name, difficulty)
        return self._lru_get_or_fetch(
            self._encounters_cache, self._encounters_lock, self._char_cap, key,
            lambda: self._inner.get_character_zone_encounters(
                lodestone_id=lodestone_id, zone_id=zone_id,
                spec_name=spec_name, difficulty=difficulty),
        )

    def get_character_encounter_pulls(self, lodestone_id: int, encounter_id: int,
                                      spec_name: str = "Machinist",
                                      difficulty: int = 101) -> Any:
        key = (lodestone_id, encounter_id, spec_name, difficulty)
        return self._lru_get_or_fetch(
            self._pulls_cache, self._pulls_lock, self._char_cap, key,
            lambda: self._inner.get_character_encounter_pulls(
                lodestone_id=lodestone_id, encounter_id=encounter_id,
                spec_name=spec_name, difficulty=difficulty),
        )

    def get_character_setup(self, lodestone_id: int,
                            groups: list[tuple[int, int, list[int]]],
                            spec_name: str = "Machinist") -> Any:
        key = (lodestone_id, spec_name,
               tuple((zid, diff, tuple(eids)) for zid, diff, eids in groups))
        return self._lru_get_or_fetch(
            self._setup_cache, self._setup_lock, self._char_cap, key,
            lambda: self._inner.get_character_setup(
                lodestone_id=lodestone_id, groups=groups,
                spec_name=spec_name),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class CachedEventsClient:
    """Thin wrapper that memoizes `get_events` calls for the lifetime of one
    `analyze_pull`. Multiple aspects (Queen, Wildfire, Tools, Abilities, …)
    each fetch the same Casts stream — without this they'd each pay a
    separate paginated round-trip. Other methods (report summary, rankings,
    etc.) pass through unchanged.

    Cache is per-instance, so concurrent analyze_pull() workers don't share
    state; the underlying FFLogsClient *is* shared and remains thread-safe.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self._events_cache: dict[tuple, list[dict[str, Any]]] = {}
        self._targetability_cache: dict[tuple, list[dict[str, Any]]] = {}
        self._aura_cache: dict[tuple, list[dict[str, Any]]] = {}
        self._enemy_casts_cache: dict[tuple, list[dict[str, Any]]] = {}

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts",
                   ability_id: int | None = None) -> list[dict[str, Any]]:
        key = (code, start, end, source_id, data_type, ability_id)
        cached = self._events_cache.get(key)
        if cached is not None:
            # Aspects mutate `cast_events` via `.sort()`. Hand each caller a
            # fresh shallow copy so cross-aspect side effects can't poison
            # the cache for later aspects.
            return list(cached)
        fresh = self._inner.get_events(code, start, end, source_id,
                                        data_type=data_type, ability_id=ability_id)
        self._events_cache[key] = fresh
        return list(fresh)

    def get_targetability_events(self, code: str, start: int,
                                  end: int) -> list[dict[str, Any]]:
        key = (code, start, end)
        cached = self._targetability_cache.get(key)
        if cached is not None:
            return list(cached)
        fresh = self._inner.get_targetability_events(code, start, end)
        self._targetability_cache[key] = fresh
        return list(fresh)

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict[str, Any]]:
        """Memoized enemy-activity fetch (the silent-despawn tail evidence,
        see downtime_sources). One fetch per pull regardless of how many
        passes read it."""
        key = (code, start, end)
        cached = self._enemy_casts_cache.get(key)
        if cached is not None:
            return list(cached)
        fresh = self._inner.get_enemy_cast_events(code, start, end)
        self._enemy_casts_cache[key] = fresh
        return list(fresh)

    def get_aura_events(self, code: str, start: int, end: int, actor_id: int,
                        data_type: str = "Buffs") -> list[dict[str, Any]]:
        """Memoized aura (buff/debuff) fetch. Without this, the two callers of
        `fetch_observed_buff_windows` (scoring + buff_drift) each pay a separate
        round-trip per pull.

        A miss is first served by NARROWING a wider cached stream (same
        code/actor/type/end, earlier start) to `timestamp >= start`: FFLogs'
        `startTime` bound is exactly that filter (verified live and across
        cached pulls — no boundary synthesis on v2 aura streams), so the
        pre-pull-widened Buffs stream the bundle primes also answers the
        exact-fight fetches without another round trip."""
        key = (code, start, end, actor_id, data_type)
        cached = self._aura_cache.get(key)
        if cached is not None:
            return list(cached)
        wider = None
        for (c2, s2, e2, a2, d2), evs in self._aura_cache.items():
            if (c2, e2, a2, d2) == (code, end, actor_id, data_type) \
                    and s2 <= start:
                wider = evs
                break
        if wider is not None:
            narrowed = [e for e in wider if e.get("timestamp", 0) >= start]
            self._aura_cache[key] = narrowed
            return list(narrowed)
        fresh = self._inner.get_aura_events(code, start, end, actor_id,
                                            data_type=data_type)
        self._aura_cache[key] = fresh
        return list(fresh)

    def prime_bundle(self, code: str, streams: list) -> None:
        """Fetch many event streams for this pull in ONE round trip and seed the
        per-pull caches under the exact keys the aspects will request, so their
        `get_events` / `get_targetability_events` / `get_aura_events` calls become
        hits. Each `BundleStream` is routed to the cache matching the method that
        will read it:

          * Buffs/Debuffs with a source actor  → aura cache
          * a `hostility` (Enemies) stream      → enemy-casts cache
          * a `filterExpression` stream with no source (targetabilityupdate)
                                                → targetability cache
          * everything else (Casts, DamageDone, …) → events cache

        Raises on a fetch error — the caller (`analyze_pull`) catches and falls
        back to per-stream fetches.
        """
        if not streams:
            return
        results = self._inner.get_event_bundle(code, streams)
        for stream, evs in zip(streams, results):
            if stream.data_type in ("Buffs", "Debuffs") and stream.source_id is not None:
                self._aura_cache[
                    (code, stream.start, stream.end, stream.source_id, stream.data_type)
                ] = evs
            elif getattr(stream, "hostility", None) is not None:
                self._enemy_casts_cache[(code, stream.start, stream.end)] = evs
            elif stream.filter_expression is not None and stream.source_id is None:
                self._targetability_cache[(code, stream.start, stream.end)] = evs
            else:
                self._events_cache[
                    (code, stream.start, stream.end, stream.source_id,
                     stream.data_type, stream.ability_id)
                ] = evs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# Backwards-compatible alias for existing imports.
_CachedEventsClient = CachedEventsClient
