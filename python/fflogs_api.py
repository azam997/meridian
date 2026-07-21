"""FFLogs v2 GraphQL API client.

Two auth modes, one client class:
  - client-credentials (legacy/dev): client_id + client_secret from
    config.json, queries /api/v2/client. What tests and scripts use.
  - user token (shipped app): a user's own OAuth token from the PKCE
    sign-in (see fflogs_auth.py), persisted in auth.json, queries
    /api/v2/user. No secret ships with the app; refresh happens here,
    inside the client, so a mid-analysis expiry is transparent.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

API_URL = "https://www.fflogs.com/api/v2/client"
USER_API_URL = "https://www.fflogs.com/api/v2/user"
TOKEN_URL = "https://www.fflogs.com/oauth/token"


class AuthExpiredError(RuntimeError):
    """The persisted FFLogs user sign-in is gone or can no longer be
    refreshed — the user must sign in again. The sidecar maps this to
    errorCode 'auth_expired' so the UI shows a re-sign-in prompt instead
    of a generic failure."""

REPORT_CODE_RE = re.compile(r"reports/(?:a:)?([A-Za-z0-9]{16})")
FIGHT_ID_RE = re.compile(r"fight=(\d+)")

# How many reports to alias into one `get_report_summaries` GraphQL request.
# Bounded so a batch query stays well under FFLogs' per-request complexity /
# point ceiling (each report pulls full masterData).
_SUMMARY_BATCH = 5

# Shared selection set for a report summary — used by both the single
# `get_report_summary` and the aliased multi-report `get_report_summaries`,
# so the two can never drift.
_REPORT_SUMMARY_FIELDS = """
  title
  startTime
  endTime
  fights(killType: Encounters) {
    id
    name
    encounterID
    difficulty
    kill
    startTime
    endTime
    friendlyPlayers
    fightPercentage
    bossPercentage
    lastPhase
    lastPhaseIsIntermission
    phaseTransitions { id startTime }
    enemyNPCs { id gameID petOwner }
  }
  masterData {
    actors {
      id
      name
      server
      type
      subType
      petOwner
      gameID
    }
    abilities {
      gameID
      name
      type
    }
  }
  phases {
    encounterID
    separatesWipes
    phases { id name isIntermission }
  }
"""


@dataclass(frozen=True)
class BundleStream:
    """One event stream to fetch as part of a `get_event_bundle` request. Mirrors
    the args of `get_events` / `get_targetability_events` / `get_aura_events` so a
    caller can map a stream back to the exact per-pull cache key those methods use.

    `data_type` is a FFLogs `EventDataType` token ("Casts", "DamageDone", "Buffs",
    "All", …). `source_id` is omitted for the targetability stream (which selects
    by `filter_expression` instead). `hostility` is a `HostilityType` token
    ("Enemies") for source-less enemy-side streams. `start` / `end` are
    report-relative ms. `include_resources` asks FFLogs to attach
    `targetResources` / `sourceResources` (HP/MP/position) to each event —
    used by the mitigation planner's DamageTaken streams; note the sourced
    DamageTaken quirk: `source_id` selects the friendly *taking* the damage
    (probe: scripts/probe_damage_taken.py)."""
    data_type: str
    start: int
    end: int
    source_id: int | None = None
    ability_id: int | None = None
    filter_expression: str | None = None
    hostility: str | None = None
    include_resources: bool = False


def fflogs_spec_slug(job_name: str) -> str:
    """FFLogs `className` / `specName` for a job. The rankings + character
    GraphQL queries want the spaceless form ("RedMage", "DarkKnight",
    "BlackMage"); our internal job names (and the actor `subType`) keep the
    space ("Red Mage"). For every FFXIV job stripping spaces is the exact
    transform, so single-word jobs (Machinist/Reaper/Samurai) are unchanged."""
    return job_name.replace(" ", "")


def parse_report_url(url: str) -> tuple[str, int | None]:
    """Extract (report_code, fight_id) from a FFLogs URL. fight_id is None if not present or 'last'."""
    url = url.strip()
    m = REPORT_CODE_RE.search(url)
    if not m:
        # Allow passing a bare report code
        if re.fullmatch(r"[A-Za-z0-9]{16}", url):
            return url, None
        raise ValueError(f"Could not parse report code from: {url}")
    code = m.group(1)
    fm = FIGHT_ID_RE.search(url)
    fight = int(fm.group(1)) if fm else None
    return code, fight


class FFLogsClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._session = requests.Session()
        # Guards token refresh so concurrent workers don't issue duplicate
        # OAuth requests when the existing token has expired.
        self._token_lock = threading.Lock()
        # User-token mode (see for_user): duck-typed store with
        # load()/save()/delete() over auth.json. None = client-credentials.
        self._auth_store: Any = None
        self._api_url = API_URL
        self._last_user_refresh = 0.0

    @classmethod
    def for_user(cls, auth_store: Any, client_id: str) -> "FFLogsClient":
        """User-token mode: bearer tokens come from the PKCE sign-in persisted
        in `auth_store` (fflogs_auth.AuthStore), refreshed here on expiry.
        Queries go to /api/v2/user (superset of the public client schema).
        No client secret exists in this mode — PKCE public client."""
        c = cls(client_id, "")
        c._auth_store = auth_store
        c._api_url = USER_API_URL
        return c

    def _token_value(self) -> str:
        if self._auth_store is not None:
            return self._user_token_value()
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        with self._token_lock:
            # Re-check after acquiring — another thread may have refreshed.
            if self._token and time.time() < self._token_expires - 60:
                return self._token
            r = self._session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=30,
            )
            if r.status_code != 200:
                raise RuntimeError(f"OAuth token request failed ({r.status_code}): {r.text}")
            data = r.json()
            self._token = data["access_token"]
            self._token_expires = time.time() + int(data.get("expires_in", 3600))
            return self._token

    def _user_token_value(self, force_refresh: bool = False) -> str:
        tokens = self._auth_store.load()
        if not tokens or not tokens.get("access_token"):
            raise AuthExpiredError("Not signed in to FFLogs.")
        if not force_refresh and time.time() < tokens.get("expires_at", 0) - 60:
            return tokens["access_token"]
        with self._token_lock:
            # Re-check under the lock — another thread may have refreshed (the
            # store is re-read so a rotated refresh token is never reused).
            tokens = self._auth_store.load()
            if not tokens or not tokens.get("access_token"):
                raise AuthExpiredError("Not signed in to FFLogs.")
            fresh_enough = time.time() < tokens.get("expires_at", 0) - 60
            just_refreshed = time.time() - self._last_user_refresh < 10
            if (not force_refresh and fresh_enough) or just_refreshed:
                return tokens["access_token"]
            return self._refresh_user_token(tokens)

    def _refresh_user_token(self, tokens: dict[str, Any]) -> str:
        """Refresh-token grant (no secret — PKCE public client). Caller holds
        `_token_lock`. Raises AuthExpiredError on any failure: a dead refresh
        token means the user must sign in again."""
        rt = tokens.get("refresh_token")
        if not rt:
            raise AuthExpiredError("FFLogs sign-in expired (no refresh token). Please sign in again.")
        try:
            r = self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                    "client_id": self.client_id,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            # Network trouble is not an auth problem — don't nuke the session.
            raise RuntimeError(f"FFLogs token refresh failed (network): {e}") from e
        if r.status_code != 200:
            raise AuthExpiredError(
                f"FFLogs sign-in expired (refresh rejected, HTTP {r.status_code}). Please sign in again."
            )
        data = r.json()
        new_tokens = {
            "access_token": data["access_token"],
            # Rotation optional server-side: keep the old one if none returned.
            "refresh_token": data.get("refresh_token", rt),
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
            "user_name": tokens.get("user_name", ""),
        }
        self._auth_store.save(new_tokens)
        self._last_user_refresh = time.time()
        return new_tokens["access_token"]

    def query(self, gql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._token_value()
        r = self._session.post(
            self._api_url,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": gql, "variables": variables or {}},
            timeout=60,
        )
        if r.status_code == 401 and self._auth_store is not None:
            # Server-side revocation can outpace our expires_at bookkeeping —
            # force one refresh and retry once before surfacing re-sign-in.
            token = self._user_token_value(force_refresh=True)
            r = self._session.post(
                self._api_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"query": gql, "variables": variables or {}},
                timeout=60,
            )
        if r.status_code != 200:
            raise RuntimeError(f"GraphQL HTTP {r.status_code}: {r.text}")
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # --- Convenience wrappers --------------------------------------------------

    def get_report_summary(self, code: str) -> dict[str, Any]:
        """Return fights + actors (players and pets) for a report.

        Fights additionally carry `enemyNPCs`, `phaseTransitions`,
        `lastPhase`, `lastPhaseIsIntermission` so the Tier-A downtime
        path can resolve boss actor IDs per fight without a second query.
        The report-level `phases` block carries the encounter's named phase
        metadata (id → name, intermission flag) for phasic ultimate analysis;
        `phaseTransitions` gives per-fight boundary timestamps.
        """
        q = ("query($code: String!) { reportData { report(code: $code) {"
             + _REPORT_SUMMARY_FIELDS + "} } }")
        return self.query(q, {"code": code})["reportData"]["report"]

    def get_report_summaries(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """Batch variant of `get_report_summary`: fetch many reports' summaries
        by aliasing `rN: report(code: $cN) { … }` under one `reportData`,
        chunked at `_SUMMARY_BATCH` to stay under the request complexity ceiling.

        Returns `{code: report_summary}` (a code maps to None if that report
        wasn't found). Input is de-duplicated — a report hosting several fights
        is fetched once.
        """
        out: dict[str, dict[str, Any]] = {}
        unique = list(dict.fromkeys(codes))  # preserve order, drop dupes
        for i in range(0, len(unique), _SUMMARY_BATCH):
            chunk = unique[i:i + _SUMMARY_BATCH]
            var_decls = ", ".join(f"$c{j}: String!" for j in range(len(chunk)))
            parts = [
                f"r{j}: report(code: $c{j}) {{{_REPORT_SUMMARY_FIELDS}}}"
                for j in range(len(chunk))
            ]
            q = f"query({var_decls}) {{ reportData {{ {' '.join(parts)} }} }}"
            variables = {f"c{j}": code for j, code in enumerate(chunk)}
            rd = (self.query(q, variables) or {}).get("reportData") or {}
            for j, code in enumerate(chunk):
                out[code] = rd.get(f"r{j}")
        return out

    def get_event_bundle(self, code: str,
                         streams: list[BundleStream]) -> list[list[dict[str, Any]]]:
        """Fetch many event streams from a single report in one aliased query.

        Returns a list of event-lists parallel to `streams`. Each stream is
        emitted as `sN: events(…) { data nextPageTimestamp }` under one
        `report(code)`. Per-stream pagination is handled by re-issuing a
        follow-up query containing only the streams that still have a
        `nextPageTimestamp` — so the common case (single-actor streams that
        fit one page) is exactly one round trip for all streams combined.
        """
        n = len(streams)
        if n == 0:
            return []
        out: list[list[dict[str, Any]]] = [[] for _ in range(n)]
        # Per-stream cursor; None once that stream is fully drained.
        cursors: list[float | None] = [float(s.start) for s in streams]

        # Bounded to avoid runaway pagination across the whole bundle.
        for _ in range(50):
            active = [i for i in range(n) if cursors[i] is not None]
            if not active:
                break
            fields = [self._events_field(f"s{i}", streams[i], cursors[i])
                      for i in active]
            q = ("query($code: String!) { reportData { report(code: $code) { "
                 + " ".join(fields) + " } } }")
            report = ((self.query(q, {"code": code}) or {})
                      .get("reportData") or {}).get("report") or {}
            for i in active:
                block = report.get(f"s{i}") or {}
                out[i].extend(block.get("data") or [])
                nxt = block.get("nextPageTimestamp")
                cursors[i] = nxt if (nxt is not None and nxt > cursors[i]) else None
        return out

    @staticmethod
    def _events_field(alias: str, stream: BundleStream,
                      start_override: float) -> str:
        """Render one aliased `events(...)` selection for `get_event_bundle`.
        Args are inlined as literals — all values are our own ints / known enum
        tokens / controlled filter strings (the latter JSON-quoted)."""
        args = [
            f"startTime: {float(start_override)}",
            f"endTime: {float(stream.end)}",
            f"dataType: {stream.data_type}",
            "limit: 10000",
        ]
        if stream.source_id is not None:
            args.append(f"sourceID: {int(stream.source_id)}")
        if stream.ability_id is not None:
            args.append(f"abilityID: {float(stream.ability_id)}")
        if stream.filter_expression is not None:
            args.append(f"filterExpression: {json.dumps(stream.filter_expression)}")
        if stream.hostility is not None:
            args.append(f"hostilityType: {stream.hostility}")
        if stream.include_resources:
            args.append("includeResources: true")
        return f"{alias}: events({', '.join(args)}) {{ data nextPageTimestamp }}"

    def get_targetability_events(self, code: str, start: int,
                                  end: int) -> list[dict[str, Any]]:
        """Fetch every `targetabilityupdate` event in [start, end].

        Filter expression confirmed via live probe against FFLogs v2:
        `dataType: All` is required, `filterExpression='type="targetabilityupdate"'`
        is the exact syntax that returns events. Each event carries
        `targetID`, `sourceID`, `timestamp`, and `targetable` (0|1).
        """
        q = """
        query($code: String!, $start: Float!, $end: Float!, $expr: String) {
          reportData {
            report(code: $code) {
              events(
                startTime: $start,
                endTime:   $end,
                dataType:  All,
                filterExpression: $expr,
                limit: 10000
              ) {
                data
                nextPageTimestamp
              }
            }
          }
        }
        """
        out: list[dict[str, Any]] = []
        cur = float(start)
        for _ in range(20):
            data = self.query(q, {
                "code": code, "start": cur, "end": float(end),
                "expr": 'type="targetabilityupdate"',
            })
            block = ((data.get("reportData") or {})
                     .get("report", {}) or {}).get("events") or {}
            out.extend(block.get("data") or [])
            nxt = block.get("nextPageTimestamp")
            if nxt is None or nxt <= cur:
                break
            cur = nxt
        return out

    def get_enemy_cast_events(self, code: str, start: int,
                              end: int) -> list[dict[str, Any]]:
        """Fetch every enemy-side cast event in [start, end] (all enemy
        actors — bosses, adds, autos). Used as the per-actor activity
        heartbeat that closes silently-despawned bosses' open targetability
        tails (see `downtime_sources.enemy_last_activity`). Each event
        carries `sourceID` and `timestamp`.
        """
        q = """
        query($code: String!, $start: Float!, $end: Float!) {
          reportData {
            report(code: $code) {
              events(
                startTime: $start,
                endTime:   $end,
                dataType:  Casts,
                hostilityType: Enemies,
                limit: 10000
              ) {
                data
                nextPageTimestamp
              }
            }
          }
        }
        """
        out: list[dict[str, Any]] = []
        cur = float(start)
        for _ in range(20):
            data = self.query(q, {"code": code, "start": cur, "end": float(end)})
            block = ((data.get("reportData") or {})
                     .get("report", {}) or {}).get("events") or {}
            out.extend(block.get("data") or [])
            nxt = block.get("nextPageTimestamp")
            if nxt is None or nxt <= cur:
                break
            cur = nxt
        return out

    def get_aura_events(self, code: str, start: int, end: int,
                        actor_id: int, data_type: str = "Buffs"
                        ) -> list[dict[str, Any]]:
        """Fetch buff/debuff application events for the actor the aura is
        *on*, in [start, end]. `data_type` is "Buffs" (party buffs a player
        receives) or "Debuffs" (debuffs a boss receives).

        NOTE the FFLogs quirk: for aura event streams the `sourceID` filter
        selects the actor the aura is *on* (the recipient), not the caster.
        So to get the raid buffs a player received we filter `sourceID =
        <player>` — confirmed empirically (e.g. a Dragoon's Battle Litany
        shows up under the buffed player's `sourceID`). Each event carries
        `type` (applybuff/removebuff/…) and `abilityGameID` (the status)."""
        q = """
        query($code: String!, $start: Float!, $end: Float!, $sid: Int!, $type: EventDataType!) {
          reportData {
            report(code: $code) {
              events(
                startTime: $start,
                endTime:   $end,
                sourceID:  $sid,
                dataType:  $type,
                limit: 10000
              ) {
                data
                nextPageTimestamp
              }
            }
          }
        }
        """
        out: list[dict[str, Any]] = []
        cur = float(start)
        for _ in range(20):
            data = self.query(q, {
                "code": code, "start": cur, "end": float(end),
                "sid": actor_id, "type": data_type,
            })
            block = ((data.get("reportData") or {})
                     .get("report", {}) or {}).get("events") or {}
            out.extend(block.get("data") or [])
            nxt = block.get("nextPageTimestamp")
            if nxt is None or nxt <= cur:
                break
            cur = nxt
        return out

    def get_events(
        self,
        code: str,
        start: int,
        end: int,
        source_id: int,
        data_type: str = "Casts",
        ability_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Paginated event fetch. data_type: Casts, DamageDone, Resources, etc."""
        q = """
        query($code: String!, $start: Float!, $end: Float!, $sid: Int!, $type: EventDataType!, $aid: Float) {
          reportData {
            report(code: $code) {
              events(
                startTime: $start,
                endTime: $end,
                sourceID: $sid,
                dataType: $type,
                abilityID: $aid,
                limit: 10000
              ) {
                data
                nextPageTimestamp
              }
            }
          }
        }
        """
        out: list[dict[str, Any]] = []
        cur = start
        # Safety cap to avoid runaway pagination
        for _ in range(50):
            data = self.query(q, {
                "code": code, "start": cur, "end": end,
                "sid": source_id, "type": data_type, "aid": ability_id,
            })["reportData"]["report"]["events"]
            out.extend(data["data"] or [])
            nxt = data.get("nextPageTimestamp")
            if nxt is None or nxt <= cur:
                break
            cur = nxt
        return out

    def get_rankings(self, encounter_id: int, class_name: str, spec_name: str,
                     difficulty: int = 101, metric: str = "rdps",
                     page: int = 1) -> dict[str, Any]:
        """Top character rankings for an encounter. Returns the rankings JSON blob."""
        q = """
        query($eid: Int!, $cls: String!, $spec: String!, $diff: Int!, $metric: CharacterRankingMetricType!, $page: Int) {
          worldData {
            encounter(id: $eid) {
              characterRankings(
                className: $cls,
                specName: $spec,
                difficulty: $diff,
                metric: $metric,
                page: $page,
                includeCombatantInfo: false
              )
            }
          }
        }
        """
        return self.query(q, {
            "eid": encounter_id, "cls": fflogs_spec_slug(class_name),
            "spec": fflogs_spec_slug(spec_name),
            "diff": difficulty, "metric": metric, "page": page,
        })["worldData"]["encounter"]["characterRankings"]

    # --- Character lookup ----------------------------------------------------

    def find_character(self, name: str, server_slug: str,
                       server_region: str) -> dict[str, Any] | None:
        """Look up a character by name + server. Returns None if not found.

        server_slug is the FFLogs slug (lowercase server name, e.g. 'behemoth').
        server_region is one of NA, EU, JP, OC.
        """
        q = """
        query($name: String!, $slug: String!, $region: String!) {
          characterData {
            character(name: $name, serverSlug: $slug, serverRegion: $region) {
              id
              name
              lodestoneID
              server { name region { slug } }
            }
          }
        }
        """
        data = self.query(q, {"name": name, "slug": server_slug, "region": server_region})
        return data["characterData"]["character"]

    def get_character_zone_encounters(self, lodestone_id: int, zone_id: int,
                                       spec_name: str = "Machinist",
                                       difficulty: int = 101) -> list[dict[str, Any]]:
        """Encounters in a zone where the character has at least one logged pull.

        Returns list of {id, name, total_kills, best_parse_pct}, in zone order.
        """
        q = """
        query($lid: Int!, $zid: Int!, $spec: String!, $diff: Int!) {
          characterData {
            character(lodestoneID: $lid) {
              zoneRankings(zoneID: $zid, specName: $spec, difficulty: $diff)
            }
          }
        }
        """
        data = self.query(q, {"lid": lodestone_id, "zid": zone_id,
                              "spec": fflogs_spec_slug(spec_name),
                              "diff": difficulty})
        char = data["characterData"]["character"]
        if not char:
            return []
        return _encounters_from_zone_rankings(char.get("zoneRankings") or {})

    def get_character_encounter_pulls(self, lodestone_id: int, encounter_id: int,
                                       spec_name: str = "Machinist",
                                       difficulty: int = 101) -> list[dict[str, Any]]:
        """Return this character's ranked kills on an encounter, newest first.

        Each entry has: report_code, fight_id, start_time_ms, duration_s,
        parse_pct, dps, spec, label (user-facing dropdown string).
        """
        q = """
        query($lid: Int!, $eid: Int!, $spec: String!, $diff: Int!) {
          characterData {
            character(lodestoneID: $lid) {
              encounterRankings(encounterID: $eid, specName: $spec, difficulty: $diff)
            }
          }
        }
        """
        data = self.query(q, {
            "lid": lodestone_id, "eid": encounter_id,
            "spec": fflogs_spec_slug(spec_name), "diff": difficulty,
        })
        char = data["characterData"]["character"]
        if not char:
            return []
        return _pulls_from_encounter_ranks(char.get("encounterRankings") or {})

    def get_character_recent_reports(self, lodestone_id: int,
                                     limit: int = 10) -> list[dict[str, Any]]:
        """The character's most recent uploaded reports, newest first — the
        prog-log discovery source (wipes never appear in rankings, so a
        progging character's pulls can only be found through their reports).
        Each entry: {code, start_time_ms, end_time_ms, zone_id} — zone_id lets
        the caller pre-filter to the relevant tier before fetching summaries.
        Probe: scripts/probe_recent_reports.py."""
        q = """
        query($lid: Int!, $limit: Int!) {
          characterData {
            character(lodestoneID: $lid) {
              recentReports(limit: $limit, page: 1) {
                data { code startTime endTime zone { id } }
              }
            }
          }
        }
        """
        data = self.query(q, {"lid": lodestone_id, "limit": limit})
        char = (data.get("characterData") or {}).get("character")
        if not char:
            return []
        out: list[dict[str, Any]] = []
        for rep in ((char.get("recentReports") or {}).get("data") or []):
            code = rep.get("code")
            if not code:
                continue
            out.append({
                "code": code,
                "start_time_ms": rep.get("startTime") or 0,
                "end_time_ms": rep.get("endTime") or 0,
                "zone_id": (rep.get("zone") or {}).get("id"),
            })
        return out

    def get_character_setup(self, lodestone_id: int,
                            groups: list[tuple[int, int, list[int]]],
                            spec_name: str = "Machinist") -> dict[str, Any]:
        """One round trip for everything SetupView needs for a (character, spec):
        per-encounter kill counts + best parse (`zoneRankings`) AND each
        encounter's individual ranked pulls (`encounterRankings`), aliased
        `z{zone}:` / `e{id}:` under one `character` query. `groups` is the
        catalog's `(zone_id, difficulty, encounter_ids)` list (encounters.py
        ZONE_GROUPS — the Savage tier plus each ultimate zone, which FFLogs
        ranks at a different difficulty); all ids are our own ints, inlined
        as literals.

        Returns `{"encounters": [...], "pulls": {encounter_id: [pull, ...]}}`
        — encounters merged in group order — the same per-encounter shapes
        `get_character_zone_encounters` / `get_character_encounter_pulls`
        return, so the sidecar reshapes them identically. An encounter with
        no logs maps to an empty pull list."""
        all_eids = [eid for _, _, eids in groups for eid in eids]
        fields = "\n          ".join(
            [f"z{int(zid)}: zoneRankings(zoneID: {int(zid)}, "
             f"specName: $spec, difficulty: {int(diff)})"
             for zid, diff, _ in groups]
            + [f"e{int(eid)}: encounterRankings(encounterID: {int(eid)}, "
               f"specName: $spec, difficulty: {int(diff)})"
               for _, diff, eids in groups for eid in eids]
        )
        q = f"""
        query($lid: Int!, $spec: String!) {{
          characterData {{
            character(lodestoneID: $lid) {{
              {fields}
            }}
          }}
        }}
        """
        data = self.query(q, {"lid": lodestone_id,
                              "spec": fflogs_spec_slug(spec_name)})
        char = (data.get("characterData") or {}).get("character")
        if not char:
            return {"encounters": [], "pulls": {eid: [] for eid in all_eids}}
        encounters: list[dict[str, Any]] = []
        for zid, _, _ in groups:
            encounters.extend(_encounters_from_zone_rankings(
                char.get(f"z{int(zid)}") or {}))
        return {
            "encounters": encounters,
            "pulls": {
                eid: _pulls_from_encounter_ranks(char.get(f"e{int(eid)}") or {})
                for eid in all_eids
            },
        }


def _encounters_from_zone_rankings(zr: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform a `zoneRankings` blob into our encounter dicts (id, name,
    total_kills, best_parse_pct), keeping only encounters with >= 1 kill, in
    zone order. Shared by `get_character_zone_encounters` + `get_character_setup`."""
    out: list[dict[str, Any]] = []
    for r in (zr or {}).get("rankings") or []:
        enc = r.get("encounter") or {}
        tk = r.get("totalKills") or 0
        if not enc.get("id") or tk <= 0:
            continue
        out.append({
            "id": enc["id"],
            "name": enc.get("name", f"Encounter {enc['id']}"),
            "total_kills": tk,
            "best_parse_pct": r.get("rankPercent"),
        })
    return out


def _pulls_from_encounter_ranks(er: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform one `encounterRankings` blob's `ranks` into our pull dicts
    (report_code, fight_id, timing, parse, dps, label), newest first. Shared by
    `get_character_encounter_pulls` + `get_character_setup`."""
    pulls: list[dict[str, Any]] = []
    for r in (er or {}).get("ranks") or []:
        rep = r.get("report") or {}
        code = rep.get("code")
        fid = rep.get("fightID")
        if not code or fid is None:
            continue
        start_ms = r.get("startTime") or rep.get("startTime") or 0
        pulls.append({
            "report_code": code,
            "fight_id": fid,
            "start_time_ms": start_ms,
            "duration_s": (r.get("duration") or 0) / 1000.0,
            "parse_pct": r.get("rankPercent") or 0.0,
            "dps": r.get("amount") or 0.0,
            "spec": r.get("spec"),
            "label": _format_pull_label(start_ms, r.get("rankPercent"), r.get("amount")),
        })
    pulls.sort(key=lambda p: p["start_time_ms"], reverse=True)  # newest first
    return pulls


def _format_pull_label(start_ms: int, parse_pct: float | None, dps: float | None) -> str:
    from datetime import datetime
    when = datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d %H:%M") if start_ms else "?"
    pp = f"{parse_pct:.1f}%" if parse_pct is not None else "—"
    dd = f"{dps/1000:.1f}k dps" if dps else "—"
    return f"{when}  —  {pp}  —  {dd}"
