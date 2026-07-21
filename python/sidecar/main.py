"""NDJSON sidecar for the React/Tauri UI.

Each line on stdin is one JSON request; each response is one JSON line on
stdout tagged with the same `id`. Progress events share the id but carry a
`progress` field instead of `ok`.

Wraps the Python core in this repo's python/ tree:
  - fflogs_api.FFLogsClient        (GraphQL client)
  - jobs.analyze_pull              (per-pull analysis)
  - jobs.compare_aspect            (per-aspect comparison)
  - encounters.ZONE_GROUPS, ALL_ENCOUNTERS, encounter_difficulty

The JSON contract is mirrored in src/sidecar/contract.ts on the UI side.

Run locally:
    python -m sidecar.main < requests.ndjson

Bundled under Tauri it's invoked as a sidecar binary via tauri-plugin-shell.
"""
from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import platform
import random
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Make the parent package importable when run as `python -m sidecar.main` or
# from inside a PyInstaller bundle.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fflogs_auth  # noqa: E402
from config import (  # noqa: E402
    CACHE_DIR,
    DEV_CACHE_DIR,
    FEEDBACK_DIR,
    load_config,
    save_config,
)
from encounters import (  # noqa: E402
    ALL_ENCOUNTERS,
    ULTIMATE_ENCOUNTERS,
    AAC_HEAVYWEIGHT_ZONE_ID,
    ZONE_GROUPS,
    encounter_category,
    encounter_difficulty,
    zone_difficulty,
)
from fflogs_api import AuthExpiredError, FFLogsClient  # noqa: E402
from jobs import (  # noqa: E402
    ALL_JOBS,
    AspectComparison,
    ModuleResult,
    analyze_pull,
    compare_aspect,
    compute_ranged_windows_for_user,
    compute_tier_b_for_user,
    compute_tier_b_tiered_for_user,
    is_supported,
)
from jobs._core.ability_metadata import get_metadata  # noqa: E402
from jobs._core.cached_client import SessionCachedClient  # noqa: E402
from jobs._core.job import get_job  # noqa: E402
from sidecar import event_log  # noqa: E402
from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402
from sidecar.version import PROTOCOL_VERSION  # noqa: E402

_io_lock = threading.Lock()

# Phase profiling for run_analysis, gated by SIDECAR_PROFILE=1 (a no-op in prod).
# Prints one per-phase wall-time line to stderr — visible under `npm run tauri dev` —
# so the parallelization win (the `refs` / `tierB` / `multiTarget` phases shrinking) is
# measurable without a debugger. See sidecar/sim_pool.py.
_PROFILE = bool(os.environ.get("SIDECAR_PROFILE"))


class _PhaseTimer:
    def __init__(self) -> None:
        self._t = time.perf_counter()
        self._marks: list[tuple[str, float]] = []

    def mark(self, label: str) -> None:
        now = time.perf_counter()
        self._marks.append((label, now - self._t))
        self._t = now

    def dump(self, prefix: str) -> None:
        if not self._marks:
            return
        parts = "  ".join(f"{l}={d:.2f}s" for l, d in self._marks)
        total = sum(d for _, d in self._marks)
        sys.stderr.write(f"[profile] {prefix}  total={total:.2f}s  {parts}\n")
        sys.stderr.flush()

# Process-lifetime client. Wrapped in SessionCachedClient so get_rankings
# calls are deduplicated across this sidecar session. Lazily built on first
# request — keeps cold start cheap.
_session_client: SessionCachedClient | None = None
_session_client_lock = threading.Lock()

# The disk-cache layer inside the current session client, kept so the
# Settings slider can retune its size cap live (set_cache_cap).
_disk_cache: DevDiskCacheClient | None = None

# Prod disk-cache policy (see DevDiskCacheClient). A logged FFLogs report is
# immutable, so its summary/events are cached permanently (no TTL key); only
# the rankings query — whose top-10 *membership* drifts as parses are posted /
# the week resets — expires, after 6h.
_PROD_CACHE_TTLS = {"get_rankings": 6 * 3600.0}

# User-configurable disk-cache size cap (the Settings slider), persisted as
# `cache_cap_mb` in config.json. Applies to BOTH cache dirs (dev and prod) so
# the footer stat, the slider, and the eviction policy always agree; oldest
# entries are evicted first once the cap is exceeded. Entries are gzipped
# (~63 KB per analyzed pull), so the 15 MB default holds ~240 pulls (a few
# fully-warmed jobs) and 100 MB the entire all-jobs matrix. Bounds + notches
# are mirrored in src/sidecar/contract.ts (CACHE_CAP_*).
_CACHE_CAP_MIN_MB = 10
_CACHE_CAP_MAX_MB = 100
_CACHE_CAP_DEFAULT_MB = 15

# Session-level result cache. Re-running analysis for an unchanged
# (pull + bucket + job) selection returns the cached response instantly.
# Sized small because each entry can be MB-scale (track events + per-aspect
# state for `you` + every ref). FIFO eviction once the cap is reached.
from collections import OrderedDict  # noqa: E402

_RESULT_CACHE_CAP = 8
_result_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_result_cache_lock = threading.Lock()

# In-flight run_analysis builds, keyed identically to _result_cache. A
# speculative background run (kicked off when the user selects a pull) and the
# explicit Run that follows collapse to a single computation: the late caller
# waits on the owner's event, then returns the owner's cached result.
_result_inflight: dict[tuple, threading.Event] = {}
_result_inflight_lock = threading.Lock()

# Process-lifetime cache of analyzed reference logs, keyed by
# (job, encounter_id, bucket). Refs are pure per-(job, encounter) data —
# independent of the player's pull — so once warmed they're reused by both
# the background warm matrix (prefetch_refs) and every run_analysis for that
# key. Concurrent requests for the same key collapse to a single build via
# `_refs_inflight`. No size cap: the matrix is small (supported jobs × tier
# encounters) and the whole point is to keep it resident.
_refs_cache: dict[tuple, list[ModuleResult]] = {}
_refs_cache_lock = threading.Lock()
_refs_inflight: dict[tuple, threading.Event] = {}


def _emit(obj: dict) -> None:
    with _io_lock:
        sys.stdout.write(json.dumps(obj, separators=(",", ":"), default=_json_default))
        sys.stdout.write("\n")
        sys.stdout.flush()


def _json_default(o: Any) -> Any:
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _client() -> SessionCachedClient:
    """Process-lifetime, lazily constructed. Returns the wrapped client so
    get_rankings benefits from session-level caching.

    Auth precedence: a persisted user sign-in (auth.json, PKCE user token)
    wins; otherwise legacy client-credentials from config.json (dev/power-
    user path); otherwise the UI is expected to have gated on get_auth_status
    and shown the sign-in card — the raise is a backstop."""
    global _session_client
    if _session_client is not None:
        return _session_client
    with _session_client_lock:
        if _session_client is not None:
            return _session_client
        cfg = load_config()
        store = fflogs_auth.AuthStore()
        cid = cfg.get("client_id", "")
        cs = cfg.get("client_secret", "")
        if store.load() is not None:
            base: Any = FFLogsClient.for_user(store, fflogs_auth.public_client_id(cfg))
        elif cid and cs:
            base = FFLogsClient(cid, cs)
        else:
            raise AuthExpiredError(
                "Not signed in to FFLogs. Sign in from the app (or set "
                "client_id/client_secret in "
                "~/.fflogs_efficiency_analyzer/config.json for dev use)."
            )
        # Cache raw FFLogs responses on disk so restarts re-warm the reference
        # matrix (and re-open prior pulls) without re-hitting the API.
        #   - dev: permanent entries (delete the dir or Clear cache to bust).
        #   - prod: only the mutable rankings query expires (top-10 membership
        #     drifts); a logged report's events/summary are IMMUTABLE so they
        #     never expire.
        # Both dirs share the user-set size cap (oldest-first eviction).
        global _disk_cache
        cap_bytes = _cache_cap_mb(cfg) * 1024 * 1024
        if cfg.get("is_dev"):
            base = DevDiskCacheClient(base, DEV_CACHE_DIR, max_bytes=cap_bytes)
        else:
            base = DevDiskCacheClient(base, CACHE_DIR, ttls=_PROD_CACHE_TTLS,
                                      max_bytes=cap_bytes)
        _disk_cache = base
        _session_client = SessionCachedClient(base)
        return _session_client


def _reset_session_client() -> None:
    """Drop the process-lifetime client so the next request rebuilds it in the
    current auth mode (called after sign-in / logout). The disk caches are
    mode-agnostic (same response shapes), so nothing else needs invalidating."""
    global _session_client
    with _session_client_lock:
        _session_client = None


# ---------------------------------------------------------------------------
# JSON conversion helpers
# ---------------------------------------------------------------------------

def _to_camel(s: str) -> str:
    # Python idiom: trailing `_s` means "seconds" — expand to "_sec" so the
    # JSON key is camelCase-readable (`timeSec`, not `timeS`). Same for
    # `_ms` -> `_msec`.
    if s.endswith("_s") and not s.endswith("__s"):
        s = s[:-2] + "_sec"
    elif s.endswith("_ms"):
        s = s[:-3] + "_msec"
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _camelize_key(k: Any) -> Any:
    """Convert string dict keys to camelCase; coerce non-string keys
    (typically ints used as ability_id) to JSON-safe strings, since JSON
    object keys must be strings anyway."""
    if isinstance(k, str):
        return _to_camel(k)
    return str(k)


def _camelize(o: Any) -> Any:
    """Recursively convert dict keys from snake_case to camelCase. Tuples
    become lists. Dataclasses become dicts. Other values pass through.
    Non-string dict keys are coerced via `str()` (e.g. int ability IDs)."""
    if dataclasses.is_dataclass(o):
        return _camelize(dataclasses.asdict(o))
    if isinstance(o, dict):
        return {_camelize_key(k): _camelize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_camelize(x) for x in o]
    return o


# ---------------------------------------------------------------------------
# Lookup / list passthroughs
# ---------------------------------------------------------------------------

def lookup_character(req: dict) -> dict:
    client = _client()
    name = req["name"]
    server = req["server"]
    region = req["region"]
    char = client.find_character(name=name, server_slug=server.lower(),
                                  server_region=region)
    if not char:
        return {"found": False}

    # If the caller passed a spec, count logs for it on the current tier —
    # this drives the per-job log count on the character card. With no
    # spec (e.g. a first-time user before any job is selected), skip the
    # extra zone query and return logsCount=0; the UI shows '—'.
    spec = req.get("spec") or ""
    logs = 0
    if spec:
        for zone_id, difficulty, _ in ZONE_GROUPS:
            encs = client.get_character_zone_encounters(
                lodestone_id=char["lodestoneID"],
                zone_id=zone_id,
                spec_name=spec,
                difficulty=difficulty,
            )
            logs += sum(e.get("total_kills", 0) for e in encs)
    return {
        "found": True,
        "lodestoneId": char["lodestoneID"],
        "name": char["name"],
        "serverName": (char.get("server") or {}).get("name", server),
        "region": region,
        "logsCount": logs,
    }


def list_encounters(req: dict) -> list[dict]:
    client = _client()
    zone_id = req.get("zoneId", AAC_HEAVYWEIGHT_ZONE_ID)
    encs = client.get_character_zone_encounters(
        lodestone_id=req["lodestoneId"],
        zone_id=zone_id,
        spec_name=req["spec"],
        difficulty=zone_difficulty(zone_id),
    )
    return [
        {
            "id": e["id"],
            "name": e["name"],
            "totalKills": e["total_kills"],
            "bestParsePct": e.get("best_parse_pct"),
        }
        for e in encs
    ]


def get_catalog(req: dict) -> dict:
    """Static catalog the UI needs: which jobs have analyzer support, which of
    those have a rotation simulator (the Kill Time Theorizer's job picker), the
    current tier's encounters (also the theorizer's encounter picker — no
    character needed), and the raid-buff provider jobs. No network. Loads the
    per-job packages (lazily) to test for a simulator; the app warms those for
    references on launch anyway, so it's not extra cost in practice."""
    from jobs import get_job as _get_job
    from jobs._core.raid_buffs import PROVIDER_BUFFS
    from mitplan.premade import has_premade
    supported = [j for j in ALL_JOBS if is_supported(j)]
    return {
        "supportedJobs": supported,
        # Jobs with a rotation simulator — the theorizer needs one to produce an
        # ideal rotation (excludes data-only jobs like Samurai).
        "simBackedJobs": [j for j in supported if _get_job(j).simulator is not None],
        # `hasPfPlan` gates the healer planner's "Use PF mit plan" button
        # (ultimates that ship a hand-authored premade/<id>.json).
        "encounters": [
            {"id": eid, "name": name, "category": encounter_category(eid),
             "hasPfPlan": has_premade(eid)}
            for eid, name in ALL_ENCOUNTERS
        ],
        # Raid-buff provider jobs — the selectable set for the Kill Time
        # Theorizer's party-composition picker (only providers affect the sim).
        "buffProviders": list(PROVIDER_BUFFS.keys()),
    }


def handshake(req: dict) -> dict:
    """Startup compatibility check. The UI sends this once right after spawning
    the sidecar and compares `protocolVersion` against its own
    `PROTOCOL_VERSION` (src/sidecar/contract.ts); a mismatch means the app shell
    and the bundled analyzer are from different builds, so the UI aborts with a
    clear message instead of mis-parsing later payloads. `python` is the runtime
    version for diagnostics. The optional `appVersion` (the Tauri app version)
    is remembered for the event log / feedback bundle — Python has no app
    version constant of its own. Pure — no network, no credentials."""
    app_version = str(req.get("appVersion") or "")
    if app_version:
        event_log.set_context(app_version=app_version)
    event_log.log("info", "lifecycle", "ui connected",
                  {"appVersion": app_version} if app_version else None)
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "python": "%d.%d.%d" % sys.version_info[:3],
    }


def _cache_cap_mb(cfg: dict | None = None) -> int:
    """The user's disk-cache cap (Settings slider), clamped to the slider's
    range. Falls back to the default on a missing/garbled config value."""
    if cfg is None:
        cfg = load_config()
    try:
        mb = int(cfg.get("cache_cap_mb", _CACHE_CAP_DEFAULT_MB))
    except (TypeError, ValueError):
        mb = _CACHE_CAP_DEFAULT_MB
    return max(_CACHE_CAP_MIN_MB, min(_CACHE_CAP_MAX_MB, mb))


def _active_cache_dir(cfg: dict) -> Any:
    return DEV_CACHE_DIR if cfg.get("is_dev") else CACHE_DIR


def cache_stats(req: dict) -> dict:
    """Total size + configured cap of the on-disk FFLogs response cache —
    the active mode's dir (dev or prod), matching what `_client()` composes.
    Pure directory scan: no network, no credentials, safe while signed out."""
    cfg = load_config()
    total = 0
    try:
        for p in _active_cache_dir(cfg).glob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    # Simple handlers emit camelCase directly (like handshake/get_catalog) —
    # the dispatch loop does not camelize.
    return {"totalBytes": total, "capMb": _cache_cap_mb(cfg)}


def set_cache_cap(req: dict) -> dict:
    """Persist the disk-cache cap (Settings slider) to config.json and apply
    it to the live client immediately, evicting down if already over."""
    cap = _cache_cap_mb({"cache_cap_mb": req.get("capMb")})
    cfg = load_config()
    cfg["cache_cap_mb"] = cap
    save_config(cfg)
    if _disk_cache is not None:
        _disk_cache.set_cache_cap(cap * 1024 * 1024)
    return cache_stats(req)


def clear_cache(req: dict) -> dict:
    """Delete every cached FFLogs response in the active cache dir (the
    footer's Clear button). Best-effort per file — the cache is disposable
    by design and refills on demand."""
    cfg = load_config()
    for pattern in ("*.json", "*.json.tmp"):
        try:
            for p in _active_cache_dir(cfg).glob(pattern):
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass
    return cache_stats(req)


# ---------------------------------------------------------------------------
# Diagnostics: forwarded UI events + the user-submitted feedback bundle
# ---------------------------------------------------------------------------

_FEEDBACK_KEEP = 5          # newest bundles kept in FEEDBACK_DIR
_ISSUE_BODY_CAP = 6000      # chars — stays well under GitHub's ~8K URL limit
_DESCRIPTION_CAP = 4000     # chars of user free-text carried into context.json


def log_event(req: dict) -> dict:
    """Forwarded frontend event → the shared event log. The `ui.` prefix marks
    origin; the level is coerced rather than trusted."""
    level = req.get("level")
    if level not in ("info", "warn", "error"):
        level = "info"
    data = req.get("data")
    event_log.log(level, "ui." + str(req.get("cat") or "app"),
                  str(req.get("msg") or ""),
                  data if isinstance(data, dict) else None)
    return {}


def get_recent_events(req: dict) -> dict:
    """The event-log tail for the Feedback view's recent-events list
    (oldest first — log order)."""
    try:
        limit = int(req.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return {"events": event_log.recent_events(max(1, min(limit, 500)))}


def export_feedback_bundle(req: dict) -> dict:
    """Build the user-submitted diagnostics zip + the prefilled GitHub issue
    text. The app never posts anywhere: the UI reveals the zip in Explorer and
    opens the new-issue URL in the default browser; the user attaches the zip
    themselves. Zip contents: the event log, environment.json, and the
    analysis context the UI chose to include — never auth.json/credentials."""
    category = str(req.get("category") or "feedback")
    description = str(req.get("description") or "")[:_DESCRIPTION_CAP]
    analysis_context = req.get("analysisContext")
    if not isinstance(analysis_context, dict):
        analysis_context = None

    environment = {
        "app": "Meridian",
        "appVersion": event_log.get_context().get("appVersion", "unknown"),
        "protocolVersion": PROTOCOL_VERSION,
        "python": sys.version,
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    context = {
        "category": category,
        "description": description,
        "analysisContext": analysis_context,
        "recentCeilingAnomalies": [
            e for e in event_log.recent_events(500)
            if e.get("cat") == "ceiling_anomaly"],
    }

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = FEEDBACK_DIR / (
        time.strftime("meridian-feedback-%Y%m%d-%H%M%S") + ".zip")
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in event_log.log_paths():
                zf.write(p, arcname=f"logs/{p.name}")
            zf.writestr("environment.json", json.dumps(environment, indent=2))
            zf.writestr("context.json",
                        json.dumps(context, indent=2, default=str))
        os.replace(tmp, zip_path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # Prune to the newest few bundles (timestamped names sort chronologically).
    try:
        bundles = sorted(FEEDBACK_DIR.glob("meridian-feedback-*.zip"))
        for old in bundles[:-_FEEDBACK_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

    title, body = _issue_text(category, description, environment,
                              analysis_context, zip_path)
    event_log.log("info", "feedback", "bundle exported",
                  {"category": category, "path": str(zip_path)})
    return {"path": str(zip_path), "issueTitle": title, "issueBody": body}


def _issue_text(category: str, description: str, environment: dict,
                analysis_context: dict | None,
                zip_path: Path) -> tuple[str, str]:
    """The prefilled GitHub new-issue title/body. Built here (not in the UI)
    so it's testable next to the data; the frontend only URL-encodes it."""
    ctx = analysis_context or {}
    anomaly = ctx.get("ceilingAnomaly") if isinstance(
        ctx.get("ceilingAnomaly"), dict) else None
    desc60 = " ".join(description.split())[:60] if description.strip() else ""

    if category == "anomaly":
        if anomaly:
            title = (f"[Anomaly] {anomaly.get('job', '?')} · "
                     f"{anomaly.get('encounterName', '?')} — efficiency "
                     f"{anomaly.get('maxEffPct', '?')}%")
        else:
            title = "[Anomaly] efficiency over 100%"
    elif category == "bug":
        title = f"[Bug] {desc60 or '(no description)'}"
    else:
        title = f"[Feedback] {desc60 or '(no description)'}"

    lines = ["## What happened", "",
             description.strip()[:1500] or "(no description provided)", "",
             "## Context", "",
             f"- App: Meridian {environment['appVersion']} "
             f"(protocol {environment['protocolVersion']})",
             f"- Python: {environment['python'].split()[0]}"
             f"{' (frozen)' if environment['frozen'] else ''}",
             f"- Platform: {environment['platform']}"]
    if ctx.get("job") or ctx.get("encounterName"):
        lines.append(f"- Job / encounter: {ctx.get('job', '?')} · "
                     f"{ctx.get('encounterName', '?')}")
    if ctx.get("reportCode"):
        lines.append(f"- Report: {ctx.get('reportCode')}"
                     f"#{ctx.get('fightId', '?')}")
    if ctx.get("efficiencyPct") is not None:
        eff = f"- Efficiency: {ctx.get('efficiencyPct')}%"
        if ctx.get("efficiencyPctLenient") is not None:
            eff += f" (lenient {ctx.get('efficiencyPctLenient')}%)"
        lines.append(eff)
    if anomaly:
        lines.append(f"- Over-ceiling max: {anomaly.get('maxEffPct', '?')}%")
    lines += ["", "## Diagnostics", "",
              "A diagnostics bundle was exported to:", "",
              f"`{zip_path}`", "",
              "**Please attach that zip to this issue** (drag & drop it into "
              "this text box). It contains the app's event log and analysis "
              "context — no FFLogs credentials."]
    return title, "\n".join(lines)[:_ISSUE_BODY_CAP]


def list_pulls(req: dict) -> list[dict]:
    client = _client()
    pulls = client.get_character_encounter_pulls(
        lodestone_id=req["lodestoneId"],
        encounter_id=req["encounterId"],
        spec_name=req["spec"],
        difficulty=encounter_difficulty(req["encounterId"]),
    )
    return [_serialize_pull(p) for p in pulls]


def _serialize_pull(p: dict) -> dict:
    return {
        "reportCode": p["report_code"],
        "fightId": p["fight_id"],
        "startTimeMs": p["start_time_ms"],
        "durationS": p["duration_s"],
        "parsePct": p["parse_pct"],
        "dps": p["dps"],
        "label": p["label"],
    }


# Prog-pull discovery: how many recent reports to scan, the shortest wipe
# worth listing (sub-20s pulls are resets/false starts), and the list cap.
_PROG_RECENT_REPORTS = 10
_PROG_MIN_DURATION_S = 20.0
_PROG_MAX_PULLS = 40


def _zone_for_encounter(encounter_id: int) -> int | None:
    for zid, _diff, eids in ZONE_GROUPS:
        if encounter_id in eids:
            return zid
    return None


def list_prog_pulls(req: dict) -> dict:
    """In-progress (wipe) pulls on an encounter — the prog-log discovery path.
    Wipes never appear in rankings (a progging character has no ranked kills),
    so they come from report summaries: the character's recent reports
    (pre-filtered to the encounter's zone when the reports carry one) or one
    explicitly pasted report code. Summaries ride the existing batched
    `prefetch_report_summaries` + session/disk caches, so listing is cheap —
    only the ONE wipe the user picks is ever analyzed."""
    from jobs._core.buff_windows import party_jobs_in_fight

    client = _client()
    encounter_id = req["encounterId"]
    want_job = str(req["spec"]).replace(" ", "")
    pasted = (req.get("reportCode") or "").strip()
    if pasted:
        codes = [pasted]
        source = "report"
    else:
        reports = client.get_character_recent_reports(
            req["lodestoneId"], limit=_PROG_RECENT_REPORTS)
        zone = _zone_for_encounter(encounter_id)
        in_zone = [r for r in reports if r.get("zone_id") == zone]
        # The zone pre-filter saves summary fetches; a report's zone is its
        # primary zone, so if nothing matches, scan them all rather than
        # miss wipes filed under a mixed/unknown zone.
        codes = list(dict.fromkeys(r["code"] for r in (in_zone or reports)))
        source = "recent"

    try:
        client.prefetch_report_summaries(codes)
    except Exception:
        pass  # per-code fetches below still work, just less batched

    pulls: list[dict] = []
    for code in codes:
        try:
            summary = client.get_report_summary(code)
        except Exception:
            summary = None
        if not summary:
            if source == "report":
                raise RuntimeError(f"Report {code} not found on FFLogs")
            continue
        report_start_ms = summary.get("startTime") or 0
        for f in summary.get("fights") or []:
            if f.get("encounterID") != encounter_id:
                continue
            if f.get("kill") is not False:
                continue
            dur_s = (f["endTime"] - f["startTime"]) / 1000.0
            if dur_s < _PROG_MIN_DURATION_S:
                continue
            jobs_in = {str(j).replace(" ", "")
                       for j in party_jobs_in_fight(summary, f)}
            if want_job not in jobs_in:
                continue
            pulls.append(_serialize_prog_pull(code, f, report_start_ms))
    pulls.sort(key=lambda p: p["startTimeMs"], reverse=True)
    return {"pulls": pulls[:_PROG_MAX_PULLS], "source": source}


def _serialize_prog_pull(code: str, f: dict, report_start_ms: int) -> dict:
    from datetime import datetime

    start_ms = report_start_ms + (f.get("startTime") or 0)
    dur_s = (f["endTime"] - f["startTime"]) / 1000.0
    fight_pct = f.get("fightPercentage")
    last_phase = f.get("lastPhase") or 0
    when = (datetime.fromtimestamp(start_ms / 1000.0).strftime("%Y-%m-%d %H:%M")
            if start_ms else "?")
    clock = f"{int(dur_s) // 60}:{int(dur_s) % 60:02d}"
    parts = [when, clock]
    if fight_pct is not None:
        left = f"{float(fight_pct):.0f}% left"
        if last_phase >= 1:
            left += f" (P{last_phase})"
        parts.append(left)
    return {
        "reportCode": code,
        "fightId": f["id"],
        "startTimeMs": start_ms,
        "durationS": dur_s,
        "fightPercentage": fight_pct,
        "bossPercentage": f.get("bossPercentage"),
        "lastPhase": last_phase,
        "label": "  —  ".join(parts),
    }


def list_setup(req: dict) -> dict:
    """Everything SetupView needs for a (character, job) in ONE FFLogs round
    trip: the tier's encounters (kill counts + best parse) AND every encounter's
    pulls, keyed by encounter id. Supersedes the separate list_encounters +
    per-encounter list_pulls fan-out, and unblocks the pull dropdown the moment
    this single request returns. Tier zone/encounters are backend-owned
    (encounters.py); the caller passes only lodestoneId + spec."""
    client = _client()
    data = client.get_character_setup(
        lodestone_id=req["lodestoneId"],
        spec_name=req["spec"],
        groups=ZONE_GROUPS,
    )
    encounters = [
        {
            "id": e["id"],
            "name": e["name"],
            "totalKills": e["total_kills"],
            "bestParsePct": e.get("best_parse_pct"),
            "category": encounter_category(e["id"]),
        }
        for e in data["encounters"]
    ]
    # Ultimates are prog-heavy: a character with zero clears has no ranking row,
    # so `get_character_setup` (which keeps only encounters with ≥1 kill) omits
    # them entirely — leaving the Ultimates tab empty and its wipe-analysis flow
    # dead. Synthesize a zero-kill row for every catalog ultimate not already
    # present so the tab always lists the encounter; the prog ("In progress")
    # path discovers wipes independently of the kill count.
    present = {e["id"] for e in encounters}
    for eid, name in ULTIMATE_ENCOUNTERS:
        if eid not in present:
            encounters.append({
                "id": eid,
                "name": name,
                "totalKills": 0,
                "bestParsePct": None,
                "category": "ultimate",
            })
    return {
        "encounters": encounters,
        # JSON object keys must be strings — the frontend indexes by String(id).
        "pullsByEncounterId": {
            str(eid): [_serialize_pull(p) for p in plist]
            for eid, plist in data["pulls"].items()
        },
    }


def list_rankings(req: dict) -> list[dict]:
    """The top-ranked players for a (job, encounter) — the Research tab's list.
    Same rankings blob _build_refs consumes (session+disk cached, so this and a
    'Top 10' refs warm share one network trip), but surfacing the per-entry
    identity (name + reportCode + fightId) the refs pipeline discards, so the
    UI can run a normal analysis with a ranked player as the subject."""
    client = _client()
    job: str = req["spec"]
    encounter_id: int = req["encounterId"]
    limit = int(req.get("limit", 10))
    try:
        rankings = client.get_rankings(
            encounter_id=encounter_id, class_name=job, spec_name=job,
            difficulty=encounter_difficulty(encounter_id),
        )
    except Exception:
        return []
    out: list[dict] = []
    for i, e in enumerate(((rankings or {}).get("rankings") or [])[:limit], 1):
        rep = e.get("report") or {}
        rc = rep.get("code")
        rf = rep.get("fightID")
        if not rc or rf is None:
            continue
        server = e.get("server")
        if isinstance(server, dict):
            server = server.get("name")
        row: dict[str, Any] = {
            "rank": i,
            "name": e.get("name", "?"),
            "reportCode": rc,
            "fightId": rf,
        }
        if isinstance(server, str) and server:
            row["server"] = server
        if isinstance(e.get("duration"), (int, float)):
            row["durationMs"] = e["duration"]
        if isinstance(e.get("amount"), (int, float)):
            row["amount"] = e["amount"]
        if isinstance(e.get("rankPercent"), (int, float)):
            row["percentile"] = e["rankPercent"]
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Analysis flow
# ---------------------------------------------------------------------------

# The named pipeline steps for the dashboard's per-step progress checklist. Each
# `progress` emission is tagged with the active index; the frontend (LoadingView)
# renders one row per step (done / running / queued) alongside the overall bar.
_ANALYSIS_STEPS = (
    "Your pull",
    "Reference logs",
    "Downtime ceiling",
    "Multi-target check",
    "Compare to top parses",
    "Ideal rotations",
)

# Healer runs prepend the mit-plan phase (the damage model + plan whose GCD
# heals get LOCKED into the ceiling). Steps are referenced by label lookup so
# both tuples drive the same pipeline.
_ANALYSIS_STEPS_HEALER = ("Mitigation plan",) + _ANALYSIS_STEPS


def _is_locked_healer_analysis(job: str) -> bool:
    """True when `job` gets the mit-plan locked-GCD analysis: a healer-duo job
    (per the mitplan library — the healer authority for this feature) with a
    registered simulator. Data-driven; lights up automatically when another
    healer's sim ships."""
    from mitplan.comp import slot_for_job
    if slot_for_job(job) is None:
        return False
    try:
        return is_supported(job) and get_job(job).simulator is not None
    except Exception:
        return False


def _comp_override_from_req(req: dict) -> tuple | None:
    """The planner's explicit comp override on a run_analysis request, as a
    normalized hashable tuple (joins the result-cache key), or None when the
    request carries no comp."""
    shield = req.get("shieldHealer")
    regen = req.get("regenHealer")
    tanks = tuple(str(j) for j in (req.get("tanks") or []) if j)
    dps = tuple(str(j) for j in (req.get("dps") or []) if j)
    if not (shield or regen or tanks or dps):
        return None
    return (str(shield or ""), str(regen or ""), tanks, dps)


def run_analysis(req: dict, req_id: str) -> dict:
    t0 = time.monotonic()
    client = _client()
    job: str = req["spec"]
    code: str = req["reportCode"]
    fight_id: int = req["fightId"]
    encounter_id: int = req.get("encounterId", 0)
    refs_bucket: str = req["refsBucket"]
    # Research loads name the subject explicitly (a ranked fight can hold two
    # players of the same job); the normal flow omits it → first same-job actor.
    player_name: str | None = req.get("playerName") or None
    # Healer flow: the planner's (possibly user-adjusted) comp rides along so
    # the locked ceiling matches the plan the user reviewed. `usePfMitPlan`
    # (ultimates only) swaps the auto plan for the hand-authored premade one.
    comp_override = _comp_override_from_req(req)
    use_pf_plan = bool(req.get("usePfMitPlan"))
    steps = (_ANALYSIS_STEPS_HEALER if _is_locked_healer_analysis(job)
             else _ANALYSIS_STEPS)

    # Carries the active pipeline-step index across emissions: a phase sets it via
    # `step=`, and the nested per-ref task emissions (which pass no `step`) inherit
    # the last one, so the checklist stays on "Reference logs" while those stream.
    prog_state: dict[str, int | None] = {"step": None}

    def progress(pct: int, stage: str, tasks: list[dict] | None = None,
                 step: int | None = None) -> None:
        if step is not None:
            prog_state["step"] = step
        msg: dict[str, Any] = {"stage": stage, "pct": pct}
        if prog_state["step"] is not None:
            msg["step"] = prog_state["step"]
            msg["steps"] = list(steps)
        if tasks is not None:
            msg["tasks"] = tasks
        _emit({"id": req_id, "progress": msg})

    # Session-level result cache. Skips analysis entirely if the same
    # (pull, encounter, bucket, job) was already computed this session. The
    # comp override joins the key so a re-run after adjusting the planner's
    # comp never returns the stale plan's numbers.
    cache_key = (code, fight_id, job, encounter_id, refs_bucket, player_name,
                 comp_override, use_pf_plan)
    with _result_cache_lock:
        cached = _result_cache.get(cache_key)
        if cached is not None:
            _result_cache.move_to_end(cache_key)
            progress(100, "Loaded from cache")
            event_log.log("info", "analysis", "run_analysis done",
                          {"job": job, "code": code, "fightId": fight_id,
                           "encounterId": encounter_id, "bucket": refs_bucket,
                           "cacheHit": True})
            return cached

    # Collapse a concurrent identical build (the speculative pre-analysis kicked
    # off on pull-select + the explicit Run): become the owner, or wait on the
    # owner and return its result instead of recomputing.
    with _result_inflight_lock:
        ev = _result_inflight.get(cache_key)
        owner = ev is None
        if owner:
            ev = _result_inflight[cache_key] = threading.Event()
    if not owner:
        progress(15, "Finishing an analysis already in progress…")
        ev.wait()
        with _result_cache_lock:
            done = _result_cache.get(cache_key)
        if done is not None:
            progress(100, "Done")
            return done
        # Owner errored without caching — fall through and compute ourselves
        # (no re-dedup; a rare concurrent retry is acceptable).

    try:
        out = _analyze_and_build(client, job, code, fight_id, encounter_id,
                                 refs_bucket, progress, player_name=player_name,
                                 steps=steps, comp_override=comp_override,
                                 use_pf_plan=use_pf_plan)
    finally:
        if owner:
            with _result_inflight_lock:
                _result_inflight.pop(cache_key, None)
            ev.set()

    with _result_cache_lock:
        _result_cache[cache_key] = out
        _result_cache.move_to_end(cache_key)
        while len(_result_cache) > _RESULT_CACHE_CAP:
            _result_cache.popitem(last=False)

    progress(100, "Done", step=len(steps))
    event_log.log("info", "analysis", "run_analysis done",
                  {"job": job, "code": code, "fightId": fight_id,
                   "encounterId": encounter_id, "bucket": refs_bucket,
                   "durationS": round(time.monotonic() - t0, 2)})
    return out


def _pf_pinned_plan(encounter_id: int, use_pf: bool):
    """The premade ("PF") pinned plan for a run, or None. Gated to ultimates
    that ship a hand-authored premade/<id>.json; otherwise a no-op so the auto
    plan is byte-identical. Returns (pinned, load_warnings)."""
    if not use_pf:
        return None, []
    from mitplan.premade import has_premade, load_premade
    if (encounter_category(encounter_id) != "ultimate"
            or not has_premade(encounter_id)):
        return None, []
    pinned = load_premade(encounter_id)
    if pinned is None:
        return None, []
    return pinned, list(pinned.warnings)


def _heal_lock_payload(client, job: str, code: str, fight_id: int,
                       encounter_id: int, comp_override: tuple | None,
                       progress, use_pf_plan: bool = False) -> dict | None:
    """The healer run's `__heal_locks__` payload: mit plan → the analyzed
    player's locked heal-GCD windows + the headline meta. Comp precedence:
    the planner's explicit override (the comp the user reviewed) > the pull's
    actors > the planner defaults. Failure policy: log + None — the analysis
    proceeds unlocked, never blocks."""
    import mitplan
    from jobs._core import heal_locks
    from jobs._core.actors import find_fight
    from mitplan.comp import resolve_comp_from_fight, slot_for_job

    try:
        report = client.get_report_summary(code)
        fight = find_fight(report, fight_id)
        if fight is None:
            raise ValueError(f"fight {fight_id} not found in {code}")
        # Wipes build a plan too. `locks_from_plan` clips every window to the
        # played span (mechanics past the wipe never happened for this pull), and
        # the Scoring aspect's `reconcile_from_report` re-clips to the terminal
        # death and — the point of prog support — lifts the ceiling's healing tax
        # to the healing the player ACTUALLY delivered, so a wipe's necessary
        # emergency healing is credited instead of scored as missed damage.
        fight_duration = (fight["endTime"] - fight["startTime"]) / 1000.0

        warnings: list[str] = []
        if comp_override is not None:
            shield, regen, tanks, dps = comp_override
            shield, regen = shield or "Sage", regen or "White Mage"
            tanks, dps = list(tanks), list(dps)
            source = "override"
        else:
            try:
                res = resolve_comp_from_fight(report, fight, anchor_job=job)
                shield, regen = res.shield_healer, res.regen_healer
                tanks, dps = list(res.tanks), list(res.dps)
                source, warnings = res.source, list(res.warnings)
            except Exception:
                shield, regen = "Sage", "White Mage"
                tanks = ["Paladin", "Dark Knight"]
                dps = ["Samurai", "Dragoon", "Bard", "Pictomancer"]
                source = "defaults"
                warnings = ["Could not read this pull's party — planned "
                            "with the default comp."]
        if not tanks:
            tanks = ["Paladin", "Dark Knight"]
        if not dps:
            dps = ["Samurai", "Dragoon", "Bard", "Pictomancer"]

        model = _get_mitplan_model(client, encounter_id, progress)
        pinned, pf_warnings = _pf_pinned_plan(encounter_id, use_pf_plan)
        result = mitplan.plan(model, shield, regen, tanks, dps, pinned=pinned)
        warnings = warnings + pf_warnings + [
            w for w in result.warnings if w.startswith("PF plan")]
        slot = slot_for_job(job)
        if slot is None:                       # not a healer-duo job (guarded upstream)
            return None
        locks = heal_locks.locks_from_plan(result, slot, fight_duration)
        count, potency, costed = heal_locks.plan_gcd_cost(result, slot)
        return {
            "locks": locks,
            "count": count,
            "potency": potency,
            "plan_costed_count": costed,
            "comp": [shield, regen, *tanks, *dps],
            "source": source,
            "warnings": warnings,
        }
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        event_log.log("warn", "heal_locks",
                      f"mit-plan lock derivation failed — analyzing unlocked: {exc}",
                      {"job": job, "reportCode": code, "fightId": fight_id,
                       "encounterId": encounter_id})
        return None


def _analyze_and_build(client, job: str, code: str, fight_id: int,
                       encounter_id: int, refs_bucket: str, progress,
                       player_name: str | None = None,
                       steps: tuple = _ANALYSIS_STEPS,
                       comp_override: tuple | None = None,
                       use_pf_plan: bool = False) -> dict:
    """The actual run_analysis computation (no caching / dedup). Progress is
    staged so the *necessary* simulator work is named rather than hidden behind
    a generic 'Building dashboard…' — the bar advances through the user-pull
    scoring, the reference fetch (or cache hit), then the ceiling/comparison/
    ideal-lane sims. Step indices are label lookups so the healer tuple (with
    its prepended mit-plan phase) drives the same pipeline."""
    prof = _PhaseTimer() if _PROFILE else None
    step = {label: i for i, label in enumerate(steps)}.get

    # Healer runs: derive the mit-plan locked heal-GCD windows FIRST — the
    # Scoring aspect inside analyze_pull bakes them into every ceiling.
    extra_report: dict | None = None
    if "Mitigation plan" in steps:
        progress(2, "Building the mitigation plan…", step=step("Mitigation plan"))
        payload = _heal_lock_payload(client, job, code, fight_id, encounter_id,
                                     comp_override, progress,
                                     use_pf_plan=use_pf_plan)
        if payload is not None:
            extra_report = {"__heal_locks__": payload}
        if prof:
            prof.mark("healLocks")

    progress(5, "Downloading your pull from FFLogs…", step=step("Your pull"))
    you = analyze_pull(job, client, code, fight_id, ranking_name=player_name,
                       label=player_name or "You", extra_report=extra_report)
    if prof:
        prof.mark("you")

    # References are normally already warmed from the setup screen (the refs
    # popup), so the analysis just loads them from cache. Reflect that honestly:
    # a warm set is an instant cache hit. If a fast run outraces the warm we
    # still fetch here, but present it as a single "Loading reference logs…"
    # step — NOT the setup screen's per-log "Downloading from FFLogs" bars,
    # which read as a redundant second download and reset the progress bar.
    with _refs_cache_lock:
        refs_warm = (job, encounter_id, refs_bucket) in _refs_cache
    if refs_warm:
        progress(80, "Using cached references ✓", step=step("Reference logs"))
        refs = _get_refs(client, job, encounter_id, refs_bucket, progress)
    else:
        progress(20, "Loading reference logs…", step=step("Reference logs"))
        # Neutral, task-bar-free progress for the cold path: keep the bar
        # advancing 20→80 (via _build_refs' emits) without re-labelling it a
        # fresh FFLogs download or streaming the per-log "downloading" bars.
        def _ref_progress(pct: int, _stage: str, tasks=None) -> None:
            progress(pct, "Loading reference logs…")
        refs = _get_refs(client, job, encounter_id, refs_bucket, _ref_progress)
    if prof:
        prof.mark("refs")

    progress(82, "Modeling forced-downtime ceiling…", step=step("Downtime ceiling"))
    _inject_tier_b(job, you, refs)
    # Promote forced melee-disconnect ("melee downtime") windows onto the STRICT
    # ceiling (the rank basis), symmetric on you + refs, BEFORE multi-target composes
    # its AoE credit on top. No-op for jobs without a ranged_filler_id (all but RPR).
    _inject_melee_downtime(job, you, refs)
    if prof:
        prof.mark("tierB")
    # Confirm + credit multi-target splash (you and refs) over the same windows,
    # so multi-target pulls read a fair efficiency instead of the disclaimed
    # single-target number.
    progress(85, "Checking multi-target windows…", step=step("Multi-target check"))
    _inject_multi_target(job, you, refs)
    # Advisory cleave-geometry verdicts on the confirmed windows (user pull
    # only): were the extra targets ever within splash reach on THIS pull?
    # Purely a UI chip + the frontend's auto-deny default — the credit math
    # above is final before this runs, and any failure leaves the windows
    # verdict-free ("unknown" behavior, byte-identical).
    _annotate_cleave_geometry(client, job, code, fight_id, you)
    if prof:
        prof.mark("multiTarget")

    progress(88, "Comparing to top parses…", step=step("Compare to top parses"))
    comparisons = _compare_all_aspects(job, you, refs)
    if prof:
        prof.mark("compare")

    progress(93, "Modeling ideal-rotation lanes…", step=step("Ideal rotations"))
    out = _build_response(job, you, refs, comparisons)
    # Ceiling-invariant watchdog (>100% efficiency = modeling bug): logs the
    # anomaly + stamps the headline nudge. Here, not in _build_response, because
    # the report/fight identifiers live here — and the caller caches `out`, so
    # a cache hit keeps the nudge while the event logs once per computed run.
    _stamp_ceiling_anomaly(out, you, refs, job, code, fight_id, encounter_id,
                           refs_bucket, player_name)
    if prof:
        prof.mark("buildResponse")
        prof.dump(f"{job} {code}:{fight_id} refsWarm={refs_warm}")
    return out


def prefetch_refs(req: dict, req_id: str) -> dict:
    """Warm the reference cache for one (job, encounter, bucket) without
    running a full analysis. Drives both the blocking priority/bump load and
    the background warm matrix on the UI side. Streams the same per-task
    progress as run_analysis' ref-download phase so the loading popup can
    render one bar per in-flight reference."""
    client = _client()
    job: str = req["spec"]
    encounter_id: int = req["encounterId"]
    bucket: str = req.get("refsBucket", "Top 10")

    key = (job, encounter_id, bucket)
    with _refs_cache_lock:
        cached = _refs_cache.get(key)
    if cached is not None:
        _emit({"id": req_id, "progress": {"stage": "Loaded from cache", "pct": 100}})
        return {"spec": job, "encounterId": encounter_id,
                "count": len(cached), "fromCache": True,
                "avgKillSec": _avg_kill_s(cached)}

    def progress(pct: int, stage: str, tasks: list[dict] | None = None) -> None:
        msg: dict[str, Any] = {"stage": stage, "pct": pct}
        if tasks is not None:
            msg["tasks"] = tasks
        _emit({"id": req_id, "progress": msg})

    progress(5, f"Warming {job} top-10 references…")
    t0 = time.monotonic()
    refs = _get_refs(client, job, encounter_id, bucket, progress)
    progress(100, "Ready")
    event_log.log("info", "analysis", "prefetch_refs done",
                  {"job": job, "encounterId": encounter_id, "bucket": bucket,
                   "count": len(refs),
                   "durationS": round(time.monotonic() - t0, 2)})
    return {"spec": job, "encounterId": encounter_id,
            "count": len(refs), "fromCache": False,
            "avgKillSec": _avg_kill_s(refs)}


def _get_refs(client, job: str, encounter_id: int, bucket: str,
              progress) -> list[ModuleResult]:
    """Analyzed refs for (job, encounter_id, bucket), built once and cached
    for the process lifetime. Concurrent callers for the same key share a
    single build — late arrivals block until the owner finishes, then read
    the cache. Empty results (failed/absent rankings) are NOT cached, so a
    transient failure can be retried on the next request."""
    key = (job, encounter_id, bucket)
    with _refs_cache_lock:
        cached = _refs_cache.get(key)
        if cached is not None:
            return cached
        ev = _refs_inflight.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _refs_inflight[key] = ev
    if not owner:
        ev.wait()
        with _refs_cache_lock:
            return _refs_cache.get(key, [])
    try:
        refs = _build_refs(client, job, encounter_id, bucket, progress)
        if refs:
            with _refs_cache_lock:
                _refs_cache[key] = refs
        return refs
    finally:
        with _refs_cache_lock:
            _refs_inflight.pop(key, None)
        ev.set()


def _build_refs(client, job: str, encounter_id: int, bucket: str,
                progress) -> list[ModuleResult]:
    if encounter_id == 0:
        return []
    n_map = {"Top 10": 10, "Top 50": 10, "Top 100": 10, "Median": 5, "Job median": 5}
    n = n_map.get(bucket, 10)
    try:
        rankings = client.get_rankings(
            encounter_id=encounter_id, class_name=job, spec_name=job,
            difficulty=encounter_difficulty(encounter_id),
        )
    except Exception:
        return []
    entries = ((rankings or {}).get("rankings") or [])[:100]
    chosen = entries[:n] if bucket != "Top 100" else random.sample(entries, k=min(n, len(entries)))

    tasks = []
    for i, e in enumerate(chosen, 1):
        rep = e.get("report") or {}
        rc = rep.get("code")
        rf = rep.get("fightID")
        if rc and rf is not None:
            tasks.append((i, rc, rf, e.get("name", "?"), f"#{i} {e.get('name', '?')}"))

    if not tasks:
        return []

    # Batch-fetch all ref report summaries in one aliased request (chunked
    # server-side), so the per-ref `analyze_pull` get_report_summary calls hit
    # the session cache instead of each paying a round trip. Best-effort.
    prefetch = getattr(client, "prefetch_report_summaries", None)
    if prefetch is not None:
        try:
            prefetch([rc for _, rc, _, _, _ in tasks])
        except Exception:
            pass

    results: list[tuple[int, ModuleResult]] = []
    total = len(tasks)
    lock = threading.Lock()

    # Per-task UI state. Each entry transitions pending → in_flight → done/failed
    # and is broadcast on every change so the LoadingView can show one bar per
    # in-flight reference (matching the 6-way ThreadPoolExecutor below).
    task_states: list[dict] = [
        {"label": rname, "state": "pending"}
        for _, _, _, rname, _ in tasks
    ]

    def emit() -> None:
        done_n = sum(1 for s in task_states if s["state"] in ("done", "failed"))
        pct = 20 + int(60 * done_n / total)
        progress(
            pct,
            f"Downloaded {done_n} of {total} reference logs from FFLogs…",
            tasks=list(task_states),
        )

    def one(t, slot: int):
        idx, rc, rf, rname, lbl = t
        with lock:
            task_states[slot]["state"] = "in_flight"
            emit()
        try:
            r = analyze_pull(job, client, rc, rf, ranking_name=rname, label=lbl)
        except Exception:
            with lock:
                task_states[slot]["state"] = "failed"
                emit()
            return None
        with lock:
            task_states[slot]["state"] = "done"
            emit()
        return idx, r

    # Broadcast the initial all-pending state so the UI can render slots
    # immediately instead of waiting for the first worker to start.
    emit()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(one, t, i) for i, t in enumerate(tasks)]
        for fut in as_completed(futs):
            r = fut.result()
            if r is not None:
                results.append(r)

    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


def _user_sim_context(you: ModuleResult):
    """The run's per-pull sim context for the displayed idealized lanes + the
    improvements diff. Base is the per-pull scalar (RDM proc budget / the gear
    CeilingContext) stashed by the scorer. On a CREDITED multi-target pull it's
    wrapped with the floored `N(t)` `MultiTargetContext` (stashed by
    `_inject_multi_target`), so the idealized timeline the user sees — and the
    missed-cast diff — reflect the SAME AoE-aware rotation as the headline ceiling
    (the improvements budget already reconciles to the multi-target gap). `None`
    for jobs whose ceiling is pure (duration, downtime, buffs) data on a
    single-target pull — byte-identical."""
    sc = you.aspects.get("Scoring")
    if sc is None:
        return None
    ctx = sc.state.get("sim_context")
    schedule = sc.state.get("mt_schedule")
    if sc.state.get("multi_target_credited") and schedule:
        return _with_multitarget(ctx, tuple(schedule),
                                 tuple(sc.state.get("mt_ability_caps") or ()))
    return ctx


def _inject_tier_b(job: str, you: ModuleResult,
                    refs: list[ModuleResult]) -> None:
    """Compute Tier-B consensus windows from refs (plus their sibling, the
    consensus ranged-filler windows — forced melee disconnects the refs
    bridged with e.g. RPR Harpe), then call the job-specific scorer once
    more with Tier A ∪ Tier B downtime and the ranged windows threaded into
    the sim context, to populate `idealized_lenient` on the user's Scoring
    aspect state. Strict is never touched by either signal.

    Mutates `you.aspects['Scoring'].state` in place. No-op for jobs
    without a Scoring aspect (lenient stays equal to strict).
    """
    scoring = you.aspects.get("Scoring")
    if scoring is None:
        return
    windows_b, windows_high = compute_tier_b_tiered_for_user(job, you, refs)
    windows_r = compute_ranged_windows_for_user(
        job, you, refs,
        tier_b_windows=[(w.start_s, w.end_s) for w in windows_b])
    state = scoring.state
    state["downtime_tier_b"] = [
        {"start_s": w.start_s, "end_s": w.end_s,
         "n_idle": w.n_idle, "n_total": w.n_total}
        for w in windows_b
    ]
    # High-confidence sub-tier (near-unanimous ref consensus) — the genuinely
    # forced cores the idealized rotation skips + that are never scored against
    # the player. Trimmed to where the pool truly agrees, so a lone lucky-tick
    # caster at an edge doesn't spoil the core. Subset of downtime_tier_b.
    state["downtime_tier_b_high"] = [
        {"start_s": w.start_s, "end_s": w.end_s,
         "n_idle": w.n_idle, "n_total": w.n_total}
        for w in windows_high
    ]
    state["ranged_windows"] = [
        {"start_s": w.start_s, "end_s": w.end_s,
         "n_casting": w.n_idle, "n_total": w.n_total}
        for w in windows_r
    ]
    if not windows_b and not windows_r:
        return
    # Recompute lenient idealized with Tier A union Tier B windows, via the
    # job's OWN simulator (the IdealizedSimulator.simulate contract returns the
    # scored ceiling). Job-generic — must not import a specific job's scorer, or
    # a second job would be scored against the wrong rotation. The perfect-sim
    # cache is keyed on (duration, windows tuple), so this hits a different slot
    # from the strict run.
    sim = get_job(job).simulator
    if sim is None:
        return
    a_windows = list(state.get("downtime_windows") or [])
    b_windows = [(w.start_s, w.end_s) for w in windows_b]
    merged = sorted(a_windows + b_windows, key=lambda x: x[0])
    # Coalesce overlapping intervals so the simulator gets a clean union.
    coalesced: list[tuple[float, float]] = []
    for s, e in merged:
        if coalesced and s <= coalesced[-1][1]:
            ls, le = coalesced[-1]
            coalesced[-1] = (ls, max(le, e))
        else:
            coalesced.append((s, e))
    duration = float(state.get("fight_duration_s") or you.fight_duration_s)
    sim_ctx = state.get("sim_context")
    # The lenient ceiling places tincture INSIDE the sim (>= delivered by construction, no
    # guard) AND max-sweeps the sub-GCD cadence band like strict (ScoringAspectBase): the
    # lenient efficiency is >= strict (more downtime, lower ceiling), so it needs the same
    # sub-GCD lift or a top parse could beat it where strict held. The job's GCD constant
    # is passed through so a threaded (faster-than-constant) gear GCD gets the same
    # constant-anchored monotone union grid strict used.
    from jobs._core.gcd_speed import (
        CeilingContext, subgcd_gcd_sweep, unwrap_ceiling_context)
    gear, payload = unwrap_ceiling_context(sim_ctx)
    gcd_const = next((a.gcd_constant for a in get_job(job).aspects
                      if getattr(a, "name", "") == "Scoring"
                      and getattr(a, "gcd_constant", None) is not None), None)
    # Consensus ranged-filler windows ride the sim context (nested INSIDE the
    # CeilingContext, see RangedFillerContext): the lenient sim swaps melee GCDs
    # for the job's ranged filler there. Strict never sees this wrapper.
    if windows_r:
        from jobs._core.downtime_sources import RangedFillerContext
        payload = RangedFillerContext(
            inner=payload,
            windows=tuple((float(w.start_s), float(w.end_s))
                          for w in windows_r))
    ctxs = ([CeilingContext(gcd_base_s=g, payload=payload)
             for g in subgcd_gcd_sweep(gear, gcd_const)]
            if gear is not None else [payload])
    # Warm the sub-GCD lenient sweep's perfect-sims in one parallel batch (process pool)
    # so the max() below is all cache hits. No-op without a pool (then max computes inline).
    sim.prime([(duration, tuple(coalesced), None, c) for c in ctxs])
    lenient = max(sim.simulate(duration, tuple(coalesced), sim_context=c).delivered_potency
                  for c in ctxs)
    state["idealized_lenient"] = lenient


def _inject_melee_downtime(job: str, you: ModuleResult,
                           refs: list[ModuleResult]) -> None:
    """Promote the consensus forced-disconnect ("melee downtime") windows onto the
    STRICT ceiling — the rank/headline basis — so M10S-style fights, where every top
    parse is forced out of melee into its ranged filler, stop showing an unreachable
    ceiling. Unlike the lenient ranged pardon (`_inject_tier_b`, user-only, uniform),
    this is:

      * **symmetric** — applied to you AND every ref (strict drives rank), mirroring
        `_inject_multi_target`; it runs BEFORE multi-target so the AoE credit (which
        only reads `idealized_strict`) composes on top of the lowered ceiling;
      * **self-limited per pull** — credited only over `consensus ∩ this pull's own
        disengage marks`, so a player who stayed in melee gets nothing and one who
        handled the mechanic better than consensus isn't over-credited;
      * **floored at delivered** — the recomputed strict never drops below what the
        pull delivered, so efficiency stays ≤100% by construction regardless of the
        mark width.

    Mutates each run's Scoring state: lowers `idealized_strict` and records
    `melee_downtime_credit` (potency) + the per-pull windows for the headline
    attribution. No-op for jobs without a `ranged_filler_id` (every job but RPR).
    """
    scoring = you.aspects.get("Scoring")
    if scoring is None:
        return
    data = get_job(job).data
    sim = get_job(job).simulator
    if data.ranged_filler_id is None or sim is None:
        return
    # Consensus forced-disconnect windows (Tier-A carved inside). NO Tier-B
    # subtraction — Tier-B is a lenient-only pardon, not part of the strict ceiling.
    consensus = compute_ranged_windows_for_user(job, you, refs, tier_b_windows=None)
    consensus_iv = [(w.start_s, w.end_s) for w in consensus]
    if not consensus_iv:
        return
    from jobs._core.downtime_sources import (
        RangedFillerContext, _intersect_intervals, disengage_marks_for)
    from jobs._core.gcd_speed import (
        CeilingContext, subgcd_gcd_sweep, unwrap_ceiling_context)
    gcd_const = next((a.gcd_constant for a in get_job(job).aspects
                      if getattr(a, "name", "") == "Scoring"
                      and getattr(a, "gcd_constant", None) is not None), None)
    for run in [you, *refs]:
        sc = run.aspects.get("Scoring")
        if sc is None:
            continue
        st = sc.state
        old_strict = float(st.get("idealized_strict") or 0)
        delivered = float(st.get("delivered_potency") or 0)
        if old_strict <= 0:
            continue
        dur = float(st.get("fight_duration_s") or run.fight_duration_s)
        tier_a = tuple(st.get("downtime_windows") or ())
        marks = disengage_marks_for(
            list(run.norm_casts), dur, data.role_policy, data.ranged_filler_id,
            list(tier_a), data)
        own = _intersect_intervals(consensus_iv, marks)
        if not own:
            continue   # this pull never disengaged inside the forced windows
        # Recompute strict with the self-limited windows threaded into the sim
        # context (RangedFillerContext nested inside CeilingContext, exactly like the
        # lenient path), max'd over the same sub-GCD cadence band strict uses.
        gear, payload = unwrap_ceiling_context(st.get("sim_context"))
        rpayload = RangedFillerContext(
            inner=payload,
            windows=tuple((float(s), float(e)) for s, e in own))
        ctxs = ([CeilingContext(gcd_base_s=g, payload=rpayload)
                 for g in subgcd_gcd_sweep(gear, gcd_const)]
                if gear is not None else [rpayload])
        sim.prime([(dur, tier_a, None, c) for c in ctxs])
        credited = max(sim.simulate(dur, tier_a, sim_context=c).delivered_potency
                       for c in ctxs)
        new_strict = max(credited, delivered)   # never below delivered → ≤100%
        if new_strict < old_strict:
            st["idealized_strict"] = new_strict
            st["idealized_potency"] = new_strict   # back-compat alias
            st["melee_downtime_credit"] = round(old_strict - new_strict, 1)
            st["melee_downtime_windows"] = [
                {"start_s": round(s, 1), "end_s": round(e, 1)} for s, e in own]


def _build_target_schedule(windows, runs) -> tuple[tuple[float, float, int], ...]:
    """Piecewise `N(t)` for the AoE-aware ceiling: each confirmed window's
    `target_count`, FLOORED at the max targets any run (you + refs) actually hit
    inside it. The floor closes the seam between the two N sources (the ceiling's
    schedule vs. the delivered side's measured per-cast hits, decision 7): a cast
    that hit more than the modal count can't push delivered past the ceiling,
    because the ceiling assumes at least as many targets there. Sorted,
    non-overlapping `(start_s, end_s, n)`; `()` outside every window -> N=1."""
    out: list[tuple[float, float, int]] = []
    for w in windows:
        n = int(w.target_count)
        for run in runs:
            for t, _aid, hit in getattr(run, "observed_multi_target_casts", ()):
                if w.start_s <= t < w.end_s:
                    n = max(n, int(hit))
        out.append((float(w.start_s), float(w.end_s), n))
    out.sort()
    return tuple(out)


def _observed_reach_caps(windows, runs) -> tuple[tuple[int, int], ...]:
    """The observed-reach cap map: for each ability anyone (you or a ref) was
    observed cleaving with inside the confirmed windows, the MAX target count
    any of those casts hit. The ceiling scorer caps that ability's N(t) there,
    so a front-cone / short-radius button (MCH Scattergun, Auto Crossbow) can't
    be credited as reaching spread targets nobody ever reached.

    Ceiling-only and under-credit-safe: delivered keeps true measured N, and
    `cap[aid] >= every delivered n of aid` by construction, so a capped per-cast
    ceiling valuation never drops below the delivered one. Abilities nobody was
    observed cleaving with stay uncapped — the schedule N (itself floored at the
    max observed hits overall) already bounds them, so an all-abilities fallback
    cap would never bind. Sorted tuple, hashable for the perfect-sim LRU."""
    caps: dict[int, int] = {}
    for run in runs:
        for t, aid, n in getattr(run, "observed_multi_target_casts", ()):
            if any(w.start_s <= t < w.end_s for w in windows):
                caps[aid] = max(caps.get(aid, 0), int(n))
    return tuple(sorted(caps.items()))


def _with_multitarget(sim_context, schedule, ability_caps=()):
    """Nest a `MultiTargetContext(schedule, ability_caps)` into `sim_context`,
    preserving the canonical order (CeilingContext GCD axis outermost, then
    MultiTargetContext, then the run's own payload). Empty schedule -> unchanged."""
    if not schedule:
        return sim_context
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import CeilingContext, unwrap_ceiling_context
    gear, payload = unwrap_ceiling_context(sim_context)
    mt = MultiTargetContext(schedule=schedule, inner=payload,
                            ability_caps=tuple(ability_caps))
    return CeilingContext(gcd_base_s=gear, payload=mt) if gear is not None else mt


def _credit_multi_target_run(mr: ModuleResult, windows, schedule, data,
                             sim, ability_caps=()) -> None:
    """Credit multi-target on one run over the confirmed `windows`, on BOTH sides:

      * **delivered** — the player's TRUE measured per-cast target count (decision
        5, uncapped), valued via `aoe_potency.potency_for` (which subsumes the old
        free-splash sum AND the dedicated AoE buttons). In-window only, so a stray
        splash outside a confirmed window can't inflate delivered past the ceiling.
      * **ceiling** — the larger of (a) the legacy free-splash credit over the ST
        sim and (b) the AoE-aware sim's gain at the FLOORED `N(t)` schedule. The
        `max` means a job whose AoE sim is minimal-safe (RDM/PLD) never regresses
        below the shipped splash ceiling, while the richer sims (RPR/SAM/BLM/MCH/
        WAR/DNC) get full AoE credit. The schedule floor (>= max observed hits)
        keeps the ceiling >= delivered by construction.

    Stashes delivered_multitarget / idealized_multitarget + the credited flag."""
    scoring = mr.aspects.get("Scoring")
    if scoring is None:
        return
    from jobs._core.sim.aoe_potency import potency_for
    state = scoring.state
    splash = data.splash_potencies

    def _target_count(t: float):
        for w in windows:
            if w.start_s <= t < w.end_s:
                return w.target_count
        return None

    # --- Delivered: measured per-cast AoE bonus (uncapped), in-window only.
    delivered_delta = 0.0
    for t, aid, n in getattr(mr, "observed_multi_target_casts", ()):
        if _target_count(t) is None:
            continue
        delivered_delta += potency_for(aid, n, data) - potency_for(aid, 1, data)

    # --- Ceiling: max(legacy free-splash over ST sim, AoE sim gain at schedule).
    ceiling_delta = 0.0
    if sim is not None:
        duration = float(state.get("fight_duration_s") or mr.fight_duration_s)
        downtime = tuple(state.get("downtime_windows") or mr.downtime_windows)
        sim_ctx = state.get("sim_context")
        try:
            st_result = sim.simulate(duration, downtime, sim_context=sim_ctx)
        except Exception:
            st_result = None
        # (a) legacy free-splash credit: splash potency over the ST sim's casts,
        #     with each ability's N capped at its observed reach.
        caps = dict(ability_caps)
        splash_delta = 0.0
        for t, aid in (st_result.timeline if st_result else ()):
            sp = splash.get(aid)
            nt = _target_count(t)
            if sp is not None and nt is not None:
                splash_delta += sp * (min(nt, caps.get(aid, nt)) - 1)
        # (b) AoE-aware sim gain at the floored N(t) schedule (same GCD basis as
        #     st; the caps ride the context into the scorer).
        aoe_delta = 0.0
        if schedule and st_result is not None:
            ctx = _with_multitarget(sim_ctx, schedule, ability_caps)
            try:
                aoe = sim.simulate(duration, downtime, sim_context=ctx).delivered_potency
                aoe_delta = max(0.0, aoe - st_result.delivered_potency)
            except Exception:
                aoe_delta = 0.0
        ceiling_delta = max(splash_delta, aoe_delta)

    delivered = float(state.get("delivered_potency", 0) or 0)
    idealized = float(state.get("idealized_strict")
                      or state.get("idealized_potency", 0) or 0)
    delivered_mt = delivered + delivered_delta
    idealized_mt = idealized + ceiling_delta
    state["delivered_multitarget"] = delivered_mt
    state["idealized_multitarget"] = idealized_mt
    # The run's TOTAL AoE deltas, so the per-window deny breakdown
    # (`_per_window_deltas`) can distribute exactly these — denying every window
    # then reverts precisely to the single-target (delivered / idealized_strict).
    state["mt_delivered_delta"] = delivered_delta
    state["mt_ceiling_delta"] = ceiling_delta
    # Credit is trustworthy only when the ratio stays sound (delivered <=
    # ceiling). If a window pushed delivered past the ceiling, leave this run
    # uncredited rather than let `_efficiency_for` show >100% (a tiny tolerance
    # absorbs float noise). Set per-run so refs are credited too — the headline
    # "vs refs" comparison then reads credited-vs-credited.
    state["multi_target_credited"] = (
        idealized_mt > 0 and delivered_mt <= idealized_mt * 1.0005)


def _per_window_deltas(mr: ModuleResult, windows, data) -> dict:
    """Per-window (delivered, ceiling) AoE deltas for the deny UI, summing EXACTLY
    to the run's headline totals (`mt_delivered_delta` / `mt_ceiling_delta`) so
    denying every window reverts precisely to the single-target efficiency
    (delivered / idealized_strict) — the headline ceiling is the AoE *sim* number,
    so a splash-only per-window breakdown would leave residual credit on deny.

    Delivered per window is the player's MEASURED-N bonus there (`potency_for`,
    exact, sums to `mt_delivered_delta`). The ceiling total isn't cheaply
    decomposable from the whole-fight sim, so it's distributed across windows by an
    AoE-opportunity weight (duration × (N−1)); the sum is exact either way, which is
    what the two-sided deny recompute needs."""
    from jobs._core.sim.aoe_potency import potency_for
    state = (mr.aspects.get("Scoring") or _Empty()).state
    out = {i: [0.0, 0.0] for i in range(len(windows))}

    def _idx(t: float):
        for i, w in enumerate(windows):
            if w.start_s <= t < w.end_s:
                return i
        return None

    for t, aid, n in getattr(mr, "observed_multi_target_casts", ()):
        i = _idx(t)
        if i is not None:
            out[i][0] += potency_for(aid, n, data) - potency_for(aid, 1, data)

    ceil_total = float(state.get("mt_ceiling_delta", 0.0) or 0.0)
    weights = [(w.end_s - w.start_s) * max(0, w.target_count - 1) for w in windows]
    wsum = sum(weights)
    if ceil_total > 0 and wsum > 0:
        for i in range(len(windows)):
            out[i][1] = ceil_total * weights[i] / wsum
    return out


def _compute_run_hits(run: ModuleResult, windows, splash: dict,
                      aoe: dict | None = None) -> list[dict]:
    """One run's per-cast multi-target hits inside the confirmed windows, as
    {timeSec, abilityId, hit, max, lostPotency} — `hit` < `max` means it cleaved
    fewer targets than the window afforded (priced at the per-extra-target potency).

    Two sources: the free-splash casts (`splash_casts`, every cast incl. n=1, so an
    under-target splash shows) and — when `aoe` (the job's dedicated AoE buttons) is
    given — the player's measured >=2-hit casts of those buttons (Scattergun, Flare,
    Tenka Goken, …) from the packetID grouping, so the player/ref lanes dot their
    real AoE casts the same way the idealized lane already does. Splash ids are kept
    on the splash path (they carry n=1 too); AoE-only ids come from the >=2 set."""
    def _tc(t: float):
        for w in windows:
            if w.start_s <= t < w.end_s:
                return w.target_count
        return None
    out: list[dict] = []
    for t, aid, hit in getattr(run, "splash_casts", ()):
        nt = _tc(t)
        if nt is None:
            continue
        lost = splash.get(aid, 0) * max(0, nt - hit)
        out.append({"timeSec": float(t), "abilityId": int(aid),
                    "hit": int(hit), "max": int(nt),
                    "lostPotency": round(float(lost), 1)})
    for t, aid, hit in (getattr(run, "observed_multi_target_casts", ()) if aoe else ()):
        if aid in splash or aid not in aoe:
            continue   # splash handled above; only the dedicated AoE buttons here
        nt = _tc(t)
        if nt is None:
            continue
        lost = aoe.get(aid, 0) * max(0, nt - hit)
        out.append({"timeSec": float(t), "abilityId": int(aid),
                    "hit": int(hit), "max": int(nt),
                    "lostPotency": round(float(lost), 1)})
    return out


def _annotate_track_hits(track: list[dict], hits: list[dict]) -> None:
    """Tag each CastEvent dict in `track` with mtHit/mtMax/mtLost by greedily
    matching one `hits` entry per cast (same abilityId, nearest cast-vs-damage
    time within 2s). In place. The cast stream and the DamageDone packets are
    different streams, so the times differ by the projectile travel — hence the
    nearest-match rather than an exact key."""
    used: set[int] = set()
    for h in hits:
        best, best_dt = -1, 2.0
        for j, ev in enumerate(track):
            if j in used or ev.get("abilityId") != h["abilityId"]:
                continue
            dt = abs(float(ev.get("startSec", 0)) - h["timeSec"])
            if dt <= best_dt:
                best_dt, best = dt, j
        if best >= 0:
            used.add(best)
            track[best]["mtHit"] = h["hit"]
            track[best]["mtMax"] = h["max"]
            track[best]["mtLost"] = h["lostPotency"]


def _annotate_idealized_hits(track: list[dict], windows: list[dict],
                             splash: dict) -> None:
    """Tag the idealized lane's splash casts inside the windows as full hits
    (the ceiling assumes the perfect rotation cleaves every targetable enemy).
    `windows` are the serialized {startSec, endSec, targetCount} dicts."""
    for ev in track:
        aid = ev.get("abilityId")
        if aid not in splash:
            continue
        t = float(ev.get("startSec", 0))
        for w in windows:
            if w["startSec"] <= t < w["endSec"]:
                ev["mtHit"] = w["targetCount"]
                ev["mtMax"] = w["targetCount"]
                ev["mtLost"] = 0.0
                break


def _inject_multi_target(job: str, you: ModuleResult,
                         refs: list[ModuleResult]) -> None:
    """Confirm the user's candidate multi-target windows by ref consensus, then
    credit splash on the user AND every ref over those windows. Lets the
    headline show a fair efficiency (delivered + splash vs ceiling + splash) on
    multi-target pulls instead of the disclaimed single-target number.

    Sets `multi_target_credited` + the serialized windows on the user's Scoring
    state. No-op (pull stays disclaimed) for jobs without splash potencies, runs
    without candidate windows, or when the refs don't confirm. Mirrors
    `_inject_tier_b`; mutates Scoring state in place."""
    from jobs._core.downtime_sources import multi_target_consensus_from_refs

    scoring = you.aspects.get("Scoring")
    if scoring is None:
        return
    data = get_job(job).data
    splash = data.splash_potencies
    candidates = list(getattr(you, "multi_target_windows", ()) or [])
    # A job is multi-target-capable if it has free-splash abilities OR a dedicated
    # AoE kit (aoe_potencies) the AoE-aware sim can cast. (SAM has only the latter.)
    if not (splash or data.aoe_potencies) or not candidates:
        return
    windows = multi_target_consensus_from_refs(
        candidates, [r.observed_multi_target_casts for r in refs],
        data.role_policy)
    if not windows:
        return   # refs didn't confirm — leave the pull disclaimed

    # The floored N(t) schedule feeds the AoE-aware ceiling sim (you + every ref);
    # the observed-reach caps bound each ability's ceiling N at the max anyone
    # actually hit with it (ceiling-only — delivered keeps true measured N).
    schedule = _build_target_schedule(windows, [you, *refs])
    ability_caps = _observed_reach_caps(windows, [you, *refs])
    sim = get_job(job).simulator
    # Warm both ceilings each run needs — its single-target sim AND its AoE-schedule sim
    # — for you + every ref in ONE parallel batch (process pool), so the per-run credit
    # loop below (each calls sim.simulate twice) hits cache instead of ~22 serial sims.
    # No-op without a pool. Mirrors the (duration, downtime, sim_context) each run uses
    # (the caps included — a spec without them would prime a different LRU slot).
    if sim is not None:
        specs = []
        for run in [you, *refs]:
            rs = (run.aspects.get("Scoring") or _Empty()).state
            dur = float(rs.get("fight_duration_s") or run.fight_duration_s)
            dt = tuple(rs.get("downtime_windows") or run.downtime_windows)
            rctx = rs.get("sim_context")
            specs.append((dur, dt, None, rctx))
            specs.append((dur, dt, None,
                          _with_multitarget(rctx, schedule, ability_caps)))
        sim.prime(specs)
    _credit_multi_target_run(you, windows, schedule, data, sim, ability_caps)
    for r in refs:
        _credit_multi_target_run(r, windows, schedule, data, sim, ability_caps)
    # Stash the schedule + caps so the displayed idealized lanes + the
    # improvements diff render/score the same AoE-aware rotation as the credited
    # headline (see `_user_sim_context`) and share its LRU slots. Only
    # meaningful once `multi_target_credited` is set.
    scoring.state["mt_schedule"] = schedule
    scoring.state["mt_ability_caps"] = ability_caps

    # Serialize the confirmed windows on the user's state for the headline, with
    # each window's delivered/ceiling AoE delta so the WindowReview trim UI can deny
    # one window and recompute efficiency two-sidedly. The deltas sum to the run's
    # headline totals, so denying all windows reverts exactly to single target.
    # Each window also carries every ref's (delivered, ceiling) deltas (refs-array
    # order — the same list flows to `_build_response`'s refs summary) plus the
    # refs' average delivered splash, so the frontend's crediting modes (cap the
    # ceiling at the top-10 average / at the player's credited splash) can
    # recompute you AND the displayed ref efficiencies without a round trip.
    pw = _per_window_deltas(you, windows, data)
    ref_pw = [_per_window_deltas(r, windows, data) for r in refs]
    scoring.state["multi_target_windows"] = [
        {"startSec": w.start_s, "endSec": w.end_s, "targetCount": w.target_count,
         "deliveredSplash": round(pw[i][0], 1), "ceilingSplash": round(pw[i][1], 1),
         "refDeliveredSplash": [round(rp[i][0], 1) for rp in ref_pw],
         "refCeilingSplash": [round(rp[i][1], 1) for rp in ref_pw],
         "refAvgDeliveredSplash": (round(sum(rp[i][0] for rp in ref_pw)
                                         / len(ref_pw), 1) if ref_pw else 0.0)}
        for i, w in enumerate(windows)
    ]

    # Per-cast multi-target hits for EVERY lane's timeline (you + each ref):
    # every splash-able cast inside a confirmed window, flagged full (hit the
    # window's target count) or short (hit fewer — the missed targets priced at
    # the splash potency). The idealized lane is annotated separately (assumed
    # full). Drives the per-cast timeline highlights, on all lanes, so the user
    # can compare and judge whether a window was really multi-target.
    for run in [you, *refs]:
        sc = run.aspects.get("Scoring")
        if sc is not None:
            sc.state["multi_target_hits"] = _compute_run_hits(
                run, windows, splash, data.aoe_potencies)


def _annotate_cleave_geometry(client, job: str, code: str, fight_id: int,
                              you: ModuleResult) -> None:
    """Attach an advisory `cleaveGeometry` verdict to each serialized confirmed
    multi-target window on the user's Scoring state (see
    jobs._core.cleave_geometry): could a target-centered cleave ever have hit a
    second enemy on THIS pull, judged from the party's sampled enemy positions?

    User pull only (refs' positioning is their own pull's business), one aliased
    window-scoped bundle round trip. Evidence-gated end to end: any fetch/parse
    failure leaves the windows without the key — the frontend renders no chip
    and applies no auto-deny, byte-identical to today. Never touches the credit
    math (structurally: `_credit_multi_target_run` has no geometry imports)."""
    try:
        scoring = you.aspects.get("Scoring")
        if scoring is None:
            return
        wins = scoring.state.get("multi_target_windows")
        if not wins or not scoring.state.get("multi_target_credited"):
            return
        from jobs._core.actors import find_fight
        from jobs._core.cleave_geometry import (job_reach_yalm,
                                                sample_enemy_positions,
                                                window_verdict)
        report = client.get_report_summary(code)
        fight = find_fight(report, fight_id)
        if not fight:
            return
        reach = job_reach_yalm(get_job(job).data)
        # Own-cleave short-circuit: a window the player demonstrably splashed
        # in (an observed n>=2 cast) was cleaveable by definition — never send
        # it to the position check, and never auto-deny the player's own real
        # delivered splash on a near-threshold geometry call.
        own = list(getattr(you, "observed_multi_target_casts", ()) or ())
        pending = []
        for w in wins:
            s, e = float(w["startSec"]), float(w["endSec"])
            n_own = sum(1 for t, _aid, _n in own if s <= t < e)
            if n_own:
                w["cleaveGeometry"] = {
                    "verdict": "reachable",
                    "detail": f"you cleaved here {n_own}× — no position check needed",
                }
            else:
                pending.append(w)
        if not pending:
            return
        positions = sample_enemy_positions(client, code, fight, pending)
        for w in pending:
            verdict, detail = window_verdict(
                float(w["startSec"]), float(w["endSec"]), positions, reach)
            w["cleaveGeometry"] = {"verdict": verdict, "detail": detail}
    except Exception:
        traceback.print_exc(file=sys.stderr)


def _compare_all_aspects(job: str, you: ModuleResult,
                          refs: list[ModuleResult]) -> dict[str, AspectComparison]:
    out: dict[str, AspectComparison] = {}
    for name in you.aspects.keys():
        try:
            out[name] = compare_aspect(job, name, you, refs)
        except Exception:
            traceback.print_exc(file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Response building
# ---------------------------------------------------------------------------

class _Empty:
    state: dict = {}


def _scoring_state(mr: ModuleResult) -> dict:
    """The canonical scalar source for the dashboard headline. Per-job
    scoring aspects (currently only MCH's MCHScoringAspect) emit a state
    dict with `delivered_potency`, `idealized_potency`, and
    `downtime_windows`. Jobs without one return empty → headline KPIs
    degrade to zero / efficiency '—'."""
    return (mr.aspects.get("Scoring") or _Empty()).state


# The ceiling invariant: idealized ≥ delivered by construction (see
# _inject_melee_downtime / _credit_multi_target_run / _buff_scenarios_for), so
# efficiency should never exceed 100%. Anything genuinely over is a modeling
# bug worth a detailed `ceiling_anomaly` log event; the dashboard nudge
# (`ceilingAnomaly` on the headline) uses a slightly higher bar so hairline
# float noise doesn't nag users. The calibration gates (validate_job_ceiling.py)
# flag at 100.5 — the log threshold is deliberately tighter, a log line is free.
_CEILING_LOG_PCT = 100.0
_CEILING_NUDGE_PCT = 100.05


def _efficiency_for(mr: ModuleResult) -> tuple[float, float, float]:
    """(delivered, idealized, efficiency_pct) — strict variant.

    Strict uses Tier-A-only idealized so the headline is honest about
    forced-downtime ambiguity by default. The lenient variant is
    available alongside via `_lenient_efficiency_for` and shown as a
    secondary badge in the UI when the deltas differ meaningfully.
    """
    state = _scoring_state(mr)
    # On a confirmed multi-target pull, the strict pair carries the splash
    # credit (delivered + ceiling over the same windows) so the headline reads
    # a fair number instead of the single-target understatement.
    if state.get("multi_target_credited"):
        delivered = float(state.get("delivered_multitarget", 0) or 0)
        idealized = float(state.get("idealized_multitarget", 0) or 0)
    else:
        delivered = float(state.get("delivered_potency", 0) or 0)
        idealized = float(state.get("idealized_strict")
                          or state.get("idealized_potency", 0) or 0)
    eff = round(100.0 * delivered / idealized, 2) if idealized > 0 else 0.0
    return delivered, idealized, eff


def _lenient_efficiency_for(mr: ModuleResult) -> tuple[float, float]:
    """(idealized_lenient, efficiency_lenient_pct). Equal to the strict
    pair when no Tier-B windows were produced."""
    state = _scoring_state(mr)
    delivered = float(state.get("delivered_potency", 0) or 0)
    lenient = float(state.get("idealized_lenient")
                     or state.get("idealized_potency", 0) or 0)
    eff = round(100.0 * delivered / lenient, 2) if lenient > 0 else 0.0
    return lenient, eff


def _buff_scenarios_for(mr: ModuleResult) -> dict[str, float]:
    """Raid-buff-aware efficiency scenarios (0 when the job has no Scoring
    aspect / no buff data):

      - observed: delivered (scored under the buffs actually present) vs the
        idealized ceiling under those same buffs. The fair, player-
        accountable number — "given the buffs you got, how close to optimal?"
      - master:   the same delivered vs the idealized ceiling under buffs on
        the perfect 2-minute cadence — "how close to the party-perfect max?"
        Below observed when the party's buffs were late / short.
    """
    state = _scoring_state(mr)
    delivered_obs = float(state.get("delivered_observed")
                          or state.get("delivered_potency", 0) or 0)
    idl_obs = float(state.get("idealized_observed") or 0)
    idl_master = float(state.get("idealized_master") or 0)
    # A buff-scenario ceiling is the OPTIMAL rotation under those buffs — and the
    # player's OWN rotation is a feasible candidate for that optimum, so the ceiling is
    # `max(what the buff-aware search found, the player's delivered)`. This is the same
    # valid construction the strict ceiling uses (`_inject_melee_downtime`'s
    # `max(credited, delivered)`): the optimizer's search candidates plus the one
    # feasible point we already hold. The engine's buff-aware burst alignment
    # (`beam_perfect` -> `canonical_aligned_max_guard`, with the per-anchor-recast hold
    # cap) is what makes the SEARCH competitive — it phases the burst into the raid
    # windows — but a job that stacks several CONCENTRATED self-buffs (DRG: Power Surge
    # x Lance Charge x Life of the Dragon ~ 1.39 in the burst) over IRREGULAR observed
    # windows can still leave a sub-1% buff-alignment search residual the greedy
    # hold can't phase out without a per-burst throughput cost; this `max` closes it
    # exactly. No-op for every scenario where the search already beat delivered.
    idl_obs = max(idl_obs, delivered_obs)
    idl_master = max(idl_master, delivered_obs)
    return {
        "deliveredObserved": delivered_obs,
        "idealizedObserved": idl_obs,
        "idealizedMaster": idl_master,
        "efficiencyPctObserved": (round(100.0 * delivered_obs / idl_obs, 2)
                                  if idl_obs > 0 else 0.0),
        "efficiencyPctMaster": (round(100.0 * delivered_obs / idl_master, 2)
                                if idl_master > 0 else 0.0),
    }


def _run_summary(mr: ModuleResult, include_track: bool = False,
                 ability_meta: dict[int, dict] | None = None) -> dict:
    delivered, idealized, eff = _efficiency_for(mr)
    out = {
        "label": mr.label,
        "fightDurationSec": float(mr.fight_duration_s),
        "deliveredPotency": delivered,
        "idealizedPotency": idealized,
        "efficiencyPct": eff,
        "killTimeSec": int(mr.fight_duration_s),
    }
    if include_track:
        abilities = mr.aspects.get("Abilities")
        if abilities is not None:
            out["abilitiesTrack"] = _serialize_track(abilities.track, ability_meta)
    # Per-run tincture windows for the Timeline (this actor's actual pots). Drawn
    # on this run's own lane, so the player, the sim, and each ref show their own
    # pot timings — not one fight-wide band.
    _sc = mr.aspects.get("Scoring")
    if _sc is not None:
        _st = _sc.state
        out["tinctureWindows"] = [
            {"startSec": float(s), "endSec": float(e),
             "multiplier": float(_st.get("tincture_multiplier") or 1.0)}
            for s, e in (_st.get("observed_tincture_windows") or [])
        ]
        # Which basis this run's delivered/idealized pair is on for a
        # multi-target pull: True = the credited (splash-inclusive) pair,
        # False = the single-target pair (this run's own splash pushed past its
        # ceiling, so `_credit_multi_target_run` left it uncredited). Key absent
        # on single-target pulls — emitted present-only so those stay
        # byte-identical. The frontend's crediting modes reconstruct an
        # uncredited run's splash-inclusive pair from the per-window ref deltas.
        if "multi_target_credited" in _st:
            out["multiTargetCredited"] = bool(_st.get("multi_target_credited"))
    return out


def _ensure_ability_meta(ability_meta: dict[int, dict], aid: int, meta=None) -> None:
    """Seed ability_meta[aid] from the metadata registry if not already present.
    Pass `meta` to reuse an already-fetched AbilityMeta and skip the lookup.
    The single home for the id → {name, iconPath, isOgcd} projection that every
    track/finding/improvement serializer needs."""
    if not aid or aid in ability_meta:
        return
    m = meta if meta is not None else get_metadata(aid)
    if m is not None:
        ability_meta[aid] = {
            "id": aid, "name": m.name, "iconPath": m.icon, "isOgcd": m.is_ogcd,
        }


def _serialize_track(track, ability_meta: dict[int, dict] | None) -> list[dict]:
    events = []
    for e in track.events:
        # Prefer the id the aspect carried directly; the icon reverse-lookup is
        # a fallback for tracks that don't set it (and fails for jobs whose
        # icons aren't in the bundled map, e.g. RPR).
        aid = getattr(e, "ability_id", None) or _extract_ability_id(e)
        if ability_meta is not None:
            _ensure_ability_meta(ability_meta, aid)
        events.append({
            "startSec": float(e.start_s),
            "endSec": float(e.end_s),
            "abilityId": aid,
            "label": e.label,
            "tooltip": e.tooltip,
            "color": e.color,
            "iconPath": e.icon_path or None,
            "yOffset": float(e.y_offset),
        })
    return events


def _align_player_prepull(track: list[dict] | None,
                          display_timeline: list[tuple[float, int]],
                          prepull_ids: frozenset[int]) -> None:
    """Reconstruct a clipped pre-pull begincast for DISPLAY (in place).

    FFLogs logs a player's pre-pull cast at its ~t=0 resolution — the begincast
    happens during the countdown, before the report's fight boundary, so it never
    arrives as negative time. The idealized lane places its precast at the
    begincast (e.g. RDM Verthunder III -5s, MCH Reassemble -5s, RPR Harpe -1.3s).
    To line the two lanes up, shift the player's opener precast (its first cast of
    a pre-pull ability, within the first couple of seconds) back to the simulated
    precast time so it renders in the Timeline's pre-pull zone instead of crammed
    at the pull. Display-only — scoring already counts the cast where it landed.
    """
    if not track:
        return
    sim_prepull = [(t, aid) for t, aid in display_timeline if t < 0]
    if not sim_prepull:
        return
    target_t = min(t for t, _ in sim_prepull)
    # Pre-pull abilities = what the sim precast ∪ the job's declared channels (so
    # an RDM who opened Veraero where the sim precast Verthunder still matches).
    ids = set(prepull_ids) | {aid for _t, aid in sim_prepull}
    for ev in track[:4]:                 # track is time-sorted
        t = float(ev.get("startSec", 0.0))
        if t < -0.05:
            return                       # a real pre-pull cast already present
        if t > 2.5:
            return                       # past the opener — nothing to reconstruct
        if ev.get("abilityId") in ids:
            ev["endSec"] = float(target_t) + (float(ev.get("endSec", t)) - t)
            ev["startSec"] = float(target_t)
            return


def _inject_inferred_prepull(track: list[dict] | None,
                             display_timeline: list[tuple[float, int]],
                             inferred_ids: list[int],
                             ability_meta: dict[int, dict]) -> None:
    """Add reconstructed pre-pull casts (in place) for clipped-instant precasts
    the player provably made (a pre-applied buff — see AbilityTimelineAspect),
    placed at the simulated precast time so they line up with the Sim lane. The
    cast event FFLogs dropped isn't recoverable, so this is the only way to show
    e.g. an MCH Reassemble precast on the player's lane."""
    if not track or not inferred_ids:
        return
    sim_prepull = [(t, aid) for t, aid in display_timeline if t < 0]
    if not sim_prepull:
        return
    earliest = min(t for t, _ in sim_prepull)
    for ability_id in inferred_ids:
        if any(ev.get("abilityId") == ability_id and ev.get("startSec", 0.0) < 0
               for ev in track):
            continue                     # already reconstructed / present
        times = [t for t, aid in sim_prepull if aid == ability_id]
        target_t = min(times) if times else earliest
        m = get_metadata(ability_id)
        _ensure_ability_meta(ability_meta, ability_id, m)
        is_ogcd = bool(m.is_ogcd) if m else False
        name = m.name if m else f"action {ability_id}"
        track.append({
            "startSec": float(target_t),
            "endSec": float(target_t) + 0.5,
            "abilityId": ability_id,
            "label": (name[:3] if name else "?"),
            "tooltip": f"{name}  (pre-pull — reconstructed from buff)",
            "color": "#777777",
            "iconPath": (m.icon if m else "") or None,
            "yOffset": -0.55 if is_ogcd else 0.0,
        })
    track.sort(key=lambda e: e.get("startSec", 0.0))


def _extract_ability_id(event) -> int | None:
    """TrackEvent doesn't carry ability_id directly today; the Abilities
    aspect doesn't tag events with IDs in the dataclass. Resolve via icon
    path when we have it (icon → ability_id is 1:1 in the BUNDLED map)."""
    # The icon path format is `/i/003000/003501.png`. Reverse-lookup
    # against ability_metadata.BUNDLED. Imported lazily to keep the
    # module cheap when only running passthrough handlers.
    from jobs._core.ability_metadata import BUNDLED
    icon = getattr(event, "icon_path", "") or ""
    if not icon:
        return None
    for aid, meta in BUNDLED.items():
        if meta.icon == icon:
            return aid
    return None


def _serialize_comparison(c: AspectComparison) -> dict:
    return {
        "aspectName": c.aspect_name,
        "findings": list(c.findings),
        "detailColumns": list(c.detail_columns),
        "yourDetailRows": [list(row) for row in c.your_detail_rows],
        "yourDetailRowColors": list(c.your_detail_row_colors),
        "summaryLines": list(c.summary_lines),
    }


def _serialize_aspect_state(name: str, state: dict) -> dict:
    """camelCase + dataclass-aware. Skip None/empty by default; lists pass through."""
    return _camelize(state)


def _melee_downtime_for(mr: ModuleResult) -> dict:
    """Headline `meleeDowntime` block: the strict-ceiling credit for forced melee
    disconnects — potency, the % of the pre-credit ceiling it represents, and the
    per-pull windows. Empty (no key emitted, so non-credited pulls stay
    byte-identical) when the pull had no credit."""
    state = _scoring_state(mr)
    credit = float(state.get("melee_downtime_credit") or 0)
    if credit <= 0:
        return {}
    base = float(state.get("idealized_strict") or 0) + credit
    return {"meleeDowntime": {
        "potency": round(credit, 1),
        "pct": round(100.0 * credit / base, 2) if base > 0 else 0.0,
        "windows": [
            {"startSec": float(w["start_s"]), "endSec": float(w["end_s"])}
            for w in (state.get("melee_downtime_windows") or [])],
    }}


def _headline(you: ModuleResult, refs: list[ModuleResult],
              comparisons: dict[str, AspectComparison]) -> dict:
    """Rank you vs refs by efficiency (delivered / idealized@own-duration),
    matching the legacy Execution.compare() ordering. Raw potency by
    itself is misleading because longer fights accumulate more — the
    delivered/idealized ratio normalizes that out."""
    you_potency, you_ideal, you_eff = _efficiency_for(you)
    you_kill = int(you.fight_duration_s)

    ref_potencies: list[float] = []
    ref_idealized: list[float] = []
    ref_effs: list[float] = []
    ref_kills: list[int] = []
    for r in refs:
        rp, ri, re_ = _efficiency_for(r)
        ref_potencies.append(rp)
        ref_idealized.append(ri)
        ref_effs.append(re_)
        ref_kills.append(int(r.fight_duration_s))

    ref_avg_potency = sum(ref_potencies) / len(ref_potencies) if ref_potencies else 0.0
    ref_avg_idealized = sum(ref_idealized) / len(ref_idealized) if ref_idealized else 0.0
    ref_avg_eff = sum(ref_effs) / len(ref_effs) if ref_effs else 0.0
    ref_avg_kill = round(sum(ref_kills) / len(ref_kills)) if ref_kills else 0

    # Rank / percentile by EFFICIENCY (not by raw potency). Higher efficiency
    # ranks better; ties broken by raw potency.
    pop = sorted(ref_effs + [you_eff], reverse=True)
    rank_you = pop.index(you_eff) + 1
    total = len(pop)
    beat_count = sum(1 for e in ref_effs if you_eff > e)
    percentile = round(100.0 * (1 - (rank_you - 1) / max(total - 1, 1))) if total > 1 else 100

    # Effective GCD from the Clipping aspect if registered for this job.
    clip_state = (you.aspects.get("Clipping") or _Empty()).state
    clipping = clip_state.get("clipping")
    eff_gcd = 0.0
    if clipping is not None:
        eff_gcd = float(getattr(clipping, "effective_gcd_s", 0) or 0)

    you_ideal_lenient, you_eff_lenient = _lenient_efficiency_for(you)

    return {
        "percentile": percentile,
        "rank": {"you": rank_you, "total": total},
        "beat": {"count": beat_count, "of": len(ref_potencies)},
        "effectiveGcdSec": eff_gcd,
        "yourPotency": you_potency,
        "yourIdealizedPotency": you_ideal,
        "yourIdealizedPotencyLenient": you_ideal_lenient,
        "refAvgPotency": ref_avg_potency,
        "refAvgIdealizedPotency": ref_avg_idealized,
        "efficiencyPct": you_eff,
        "efficiencyPctStrict": you_eff,           # alias for clarity
        "efficiencyPctLenient": you_eff_lenient,
        # Raid-buff-aware scenarios (observed circumstances + party-optimal).
        **_buff_scenarios_for(you),
        "refEfficiencyPct": ref_avg_eff,
        "killTimeSec": you_kill,
        "refKillTimeSec": ref_avg_kill,
        # Observed party composition (friendly jobs) — seeds the Kill Time
        # Theorizer's comp selector with the comp this pull actually had.
        "partyJobs": list(getattr(you, "party_jobs", ()) or ()),
        # Downtime breakdown — drives the optional DowntimePanel popover.
        "downtimeSource": (_scoring_state(you).get("downtime_source")
                            or "fallback_heuristic"),
        "downtimeTierA": [
            {"startSec": float(s), "endSec": float(e)}
            for s, e in (_scoring_state(you).get("downtime_windows") or [])
        ],
        "downtimeTierB": [
            {"startSec": float(w["start_s"]), "endSec": float(w["end_s"]),
             "nIdle": int(w["n_idle"]), "nTotal": int(w["n_total"])}
            for w in (_scoring_state(you).get("downtime_tier_b") or [])
        ],
        # High-confidence (near-unanimous) sub-cores of Tier B — the genuinely
        # forced stretches the idealized rotation skips. Subset of downtimeTierB;
        # the timeline renders these firmly and the rest as the lighter "suspected"
        # hatch so the ideal casting through the ambiguous edges reads as intended.
        "downtimeTierBHigh": [
            {"startSec": float(w["start_s"]), "endSec": float(w["end_s"]),
             "nIdle": int(w["n_idle"]), "nTotal": int(w["n_total"])}
            for w in (_scoring_state(you).get("downtime_tier_b_high") or [])
        ],
        # Consensus ranged-filler windows (forced melee disconnects bridged
        # with e.g. RPR Harpe) — lenient ceiling only, Tier-B's sibling.
        "rangedWindows": [
            {"startSec": float(w["start_s"]), "endSec": float(w["end_s"]),
             "nCasting": int(w["n_casting"]), "nTotal": int(w["n_total"])}
            for w in (_scoring_state(you).get("ranged_windows") or [])
        ],
        # Forced "melee downtime" credited onto the STRICT/rank ceiling (potency +
        # % of the pre-credit ceiling + the windows). Present only when credited.
        **_melee_downtime_for(you),
    }


def _stamp_ceiling_anomaly(out: dict, you: ModuleResult,
                           refs: list[ModuleResult], job: str, code: str,
                           fight_id: int, encounter_id: int, refs_bucket: str,
                           player_name: str | None) -> None:
    """Watchdog for the ceiling invariant: if you or any reference scored over
    100% efficiency (strict or lenient), write one detailed `ceiling_anomaly`
    event, and — past the nudge threshold — stamp the additive
    `ceilingAnomaly` field onto the headline so the dashboard can ask the user
    to submit the data. Key absent on clean runs (the meleeDowntime pattern),
    so the normal contract shape is unchanged. Never raises — the analysis
    must not fail because its watchdog did."""
    try:
        h = out.get("headline") or {}
        entries: list[dict] = []
        for who, mr in [("you", you)] + [("ref", r) for r in refs]:
            # A mit-plan-locked ceiling is EXPECTED to be exceedable — a player
            # who skips planned heals out-damages the honest maximum (the
            # dashboard frames it explicitly) — so the locked you-run is not an
            # anomaly. Refs are never locked and stay fully checked.
            if who == "you" and _scoring_state(mr).get("heal_locks_applied"):
                continue
            delivered, idealized, eff = _efficiency_for(mr)
            ideal_lenient, eff_lenient = _lenient_efficiency_for(mr)
            if max(eff, eff_lenient) <= _CEILING_LOG_PCT:
                continue
            entries.append({
                "who": who, "label": mr.label,
                "effPct": eff, "effLenientPct": eff_lenient,
                "deliveredPotency": delivered,
                "idealizedPotency": idealized,
                "idealizedPotencyLenient": ideal_lenient,
                "killTimeSec": int(mr.fight_duration_s),
            })
        if not entries:
            return

        enc_name = next(
            (n for i, n in ALL_ENCOUNTERS if i == encounter_id), "")
        event_log.log("warn", "ceiling_anomaly",
                      f"{job} {code}#{fight_id} exceeded the ceiling",
                      {"job": job, "encounterId": encounter_id,
                       "encounterName": enc_name, "reportCode": code,
                       "fightId": fight_id, "playerName": player_name,
                       "refsBucket": refs_bucket,
                       "downtimeSource": h.get("downtimeSource"),
                       "multiTargetCredited": h.get("multiTargetCredited"),
                       "multiTargetDisclaimed": h.get("multiTargetDisclaimed"),
                       "entries": entries})

        over = [e for e in entries
                if max(e["effPct"], e["effLenientPct"]) > _CEILING_NUDGE_PCT]
        if over:
            h["ceilingAnomaly"] = {
                "maxEffPct": max(max(e["effPct"], e["effLenientPct"])
                                 for e in over),
                "entries": [{"who": e["who"], "label": e["label"],
                             "effPct": e["effPct"],
                             "effLenientPct": e["effLenientPct"]}
                            for e in over],
                "job": job,
                "encounterId": encounter_id,
                "encounterName": enc_name,
                "reportCode": code,
                "fightId": fight_id,
            }
    except Exception:
        traceback.print_exc(file=sys.stderr)


def _consensus_downtime_windows(you: ModuleResult) -> list[tuple[float, float]]:
    """HIGH-CONFIDENCE Tier-B windows for this pull — the near-unanimous
    (`consensus_high_pct`) consensus cores, off the Scoring state (populated by
    `_inject_tier_b`, so [] before refs run / for a job without Scoring). These
    are the *genuinely* forced stretches the idealized rotation skips; the wider
    `downtime_tier_b` (suspected) band is deliberately NOT used here so ambiguous
    consensus time still gets a "cast here" nudge."""
    sc = you.aspects.get("Scoring")
    if sc is None:
        return []
    return [(float(w["start_s"]), float(w["end_s"]))
            for w in (sc.state.get("downtime_tier_b_high") or [])]


def _strip_consensus_downtime(timeline: list[tuple[float, int]],
                              you: ModuleResult) -> list[tuple[float, int]]:
    """Drop idealized casts the sim placed inside a HIGH-CONFIDENCE Tier-B window.

    The strict sim only ever receives Tier-A downtime (`you.downtime_windows`);
    Tier B is computed after refs and is never handed to the simulator, so the sim
    fills those windows with a full rotation. We strip only the *high-confidence*
    cores (near-unanimous ref consensus, `consensus_high_pct`), trimmed to where
    the pool truly agrees — casts no real parse makes, that would otherwise render
    straight through the band and become "cast during downtime" improvement cards.
    Sub-threshold *ambiguous* consensus time (and the window edges where a couple
    of top parses squeeze a boundary GCD) is left castable so a genuine
    improvement still surfaces there. Tier-A casts are untouched: the sim already
    skips Tier A, so its intentional boundary squeezes (MCH Flamethrower, RPR
    Soulsow, BRD re-songs, …) stay. Pot markers are kept so the idealized pot band
    is unaffected. No-op when there are no high-confidence windows (every Savage
    pull, any pull analyzed before refs, and pulls whose consensus never reaches
    near-unanimity) → byte-identical."""
    wins = _consensus_downtime_windows(you)
    if not wins:
        return timeline
    from jobs._core.tincture import TINCTURE_ACTION_ID
    return [c for c in timeline
            if c[1] == TINCTURE_ACTION_ID
            or not any(s <= c[0] < e for s, e in wins)]


def _idealized_timeline(job: str, you: ModuleResult) -> list[tuple[float, int]]:
    """The job's idealized perfect-sim cast timeline for this run's duration +
    downtime, BUFF-AGNOSTIC (the strict-throughput basis). This is what the
    improvements panel diffs against — buff alignment is intentionally excluded
    so it isn't double-counted as recoverable potency (it lives in the Raid-buff
    card). [] when the job has no simulator."""
    sim = get_job(job).simulator
    if sim is None:
        return []
    from jobs._core.tincture import TINCTURE_ACTION_ID
    tl = [c for c in sim.simulate(you.fight_duration_s, tuple(you.downtime_windows),
                                  sim_context=_user_sim_context(you)).timeline
          if c[1] != TINCTURE_ACTION_ID]
    return _strip_consensus_downtime(tl, you)


_CTX_DEFAULT = object()   # sentinel: None is a valid sim_context


def _idealized_display_timeline(job: str, you: ModuleResult,
                                sim_context=_CTX_DEFAULT
                                ) -> list[tuple[float, int]]:
    """The timeline rendered on the Timeline's "Idealized" lane. Unlike
    `_idealized_timeline`, this is BUFF-AWARE: the sim is scored under the
    canonical master raid-buff windows (opener phased per provider at ~the 3rd
    GCD), so burst placement reflects the real party-buff multipliers. This is
    the THROUGHPUT-OPTIMAL line — it does NOT *force* burst into the raid window,
    but it does search realistic alignment: e.g. MCH banks Queen battery toward
    the window and evaluates a raid-window-aligned Wildfire, so the opener burst
    lands in the buffs whenever that out-scores firing on cooldown (it keeps
    on-cooldown burst only on the fights where that genuinely wins). The
    always-'hold for the window' variant is the separate canonical lane
    (`_idealized_canonical_timeline`). Falls back to the agnostic timeline when
    there's no simulator or no providers were present (empty intervals)."""
    sim = get_job(job).simulator
    if sim is None:
        return []
    scoring = you.aspects.get("Scoring")
    master_intervals = None
    if scoring is not None:
        mi = scoring.state.get("master_buff_intervals") or None
        if mi:
            master_intervals = [tuple(x) for x in mi]
    # Default context is AoE-aware on a credited pull (the multi-target schedule
    # rides `_user_sim_context`); callers pass the bare base context to render the
    # single-target variant (the cached "reverted" lane the frontend splices into
    # denied windows).
    ctx = _user_sim_context(you) if sim_context is _CTX_DEFAULT else sim_context
    res = sim.simulate(you.fight_duration_s, tuple(you.downtime_windows),
                       buff_intervals=master_intervals, sim_context=ctx)
    return _strip_consensus_downtime(list(res.timeline), you)


def _idealized_canonical_timeline(job: str, you: ModuleResult
                                  ) -> list[tuple[float, int]]:
    """The canonical 'hold burst for the 2-min window' lane — Wildfire + Barrel
    Stabilizer cast inside the master raid-buff windows so their payloads are
    buffed (vs `_idealized_display_timeline`, which fires burst on the sim's
    throughput-optimal cadence). [] when the job has no canonical simulator or
    no party buffs were present (no windows to align to → identical to optimal,
    so the UI just shows the one lane)."""
    sim = get_job(job).simulator
    canon = getattr(sim, "simulate_canonical", None) if sim is not None else None
    if canon is None:
        return []
    scoring = you.aspects.get("Scoring")
    master_intervals = None
    if scoring is not None:
        mi = scoring.state.get("master_buff_intervals") or None
        if mi:
            master_intervals = [tuple(x) for x in mi]
    if not master_intervals:
        return []
    return _strip_consensus_downtime(
        list(canon(you.fight_duration_s, tuple(you.downtime_windows),
                   buff_intervals=master_intervals,
                   sim_context=_user_sim_context(you)).timeline),
        you)


# Potency floor for an improvement to rank as its own card. Below this a
# damaging miss is real but too small to lead with, so it's folded into the
# expandable "Other" breakdown instead. Matches the default in
# improvements.compute_missed_cast_improvements.
_CARD_FLOOR = 150.0


def _build_improvements(job: str, you: ModuleResult,
                        idealized: list[tuple[float, int]]) -> list[dict]:
    """Unified Potential Improvements for the `you` run — a located, ranked
    decomposition of *where the potency went*.

    For a sim-backed job the recoverable potency is exactly
    `idealized_strict - delivered`; the panel attributes that gap rather than
    summing independent estimates that overlap and overshoot it (see
    jobs/_core/improvements.py for the full model). Priced items are direct
    damage pinnable to a cast (missed tools net of filler, Double Check /
    Checkmate, overcap waste, Reassemble misuse); ordering (opener) and enabler
    (Hypercharge / Wildfire / …) findings become zero-priced diagnostics; and
    `reconcile_to_budget` bounds the priced total by the gap with an "other"
    residual so it always sums to the measured loss. Buff alignment is omitted
    here — it is zero under the strict basis and lives in the Raid-buff card.

    Jobs *without* a simulator have no measured gap to reconcile against, so
    they fall back to the aggregate drift heuristic + clipping/overcap, ungated.
    """
    from jobs._core import improvements as imp

    def st(name: str) -> dict:
        return (you.aspects.get(name) or _Empty()).state

    priced: list = []
    diagnostics: list = []
    # Sub-card-floor damaging misses — real, located, but too small to rank as
    # their own card. They become the expandable breakdown under "Other" so the
    # residual isn't an opaque bucket.
    minor: list = []

    death_windows = list(you.death_windows)
    enabler_values = st("Scoring").get("enabler_net_values") or {}

    if idealized:
        # Strict / buff-agnostic valuation to match the recoverable gap shown in
        # the headline (idealized_strict − delivered). Enabler misses are priced
        # at their sim-derived net value (Scoring aspect); below-floor ones come
        # back zero-priced, so split priced vs. diagnostic by the value.
        for im in imp.compute_missed_cast_improvements(
            actual_casts=list(you.norm_casts),
            idealized_timeline=idealized,
            data=get_job(job).data,
            buff_intervals=None,
            fight_duration_s=you.fight_duration_s,
            enabler_values=enabler_values,
        ):
            # A cast missed while the player was dead belongs to the Death card
            # (priced below at full value), not to the missed-cast diff — else
            # it double-counts the same lost cast.
            if imp._in_any(im.time_s, death_windows):
                continue
            (priced if im.lost_potency > 0 else diagnostics).append(im)
        # Second pass with no noise floor harvests the small damaging misses the
        # default floor drops — they're the located detail behind the "Other"
        # residual (left out of the ranked cards, but worth showing on expand).
        for im in imp.compute_missed_cast_improvements(
            actual_casts=list(you.norm_casts),
            idealized_timeline=idealized,
            data=get_job(job).data,
            buff_intervals=None,
            fight_duration_s=you.fight_duration_s,
            min_potency=1.0,
            enabler_values=enabler_values,
        ):
            if (im.kind == "missed_cast" and 0 < im.lost_potency < _CARD_FLOOR
                    and not imp._in_any(im.time_s, death_windows)):
                minor.append(im)
        # Located idle/clip stretches below the aggregate pacing-card gates —
        # real, time-stamped, but too small to card on their own. Surfaced as
        # the residual's located breakdown rather than lost in its diffuse tail.
        minor += imp.minor_pacing_from_clipping(st("Clipping"))
        # Missed Flamethrower squeezes — full-value (downtime-edge, no filler
        # backfill), located. Suppressed inside a death window (the Death card
        # already sums the idealized rotation there).
        for im in imp.improvements_from_flamethrower(
            actual_casts=list(you.norm_casts),
            idealized_timeline=idealized,
            data=get_job(job).data,
            buff_intervals=None,
            fight_duration_s=you.fight_duration_s,
        ):
            if not imp._in_any(im.time_s, death_windows):
                priced.append(im)
        # One priced, located card per death — the idealized rotation over the
        # dead window, which the Clipping aspect no longer claims.
        priced += imp.improvements_from_deaths(
            death_windows, idealized, get_job(job).data,
            enabler_values=enabler_values,
        )
        # Filler-composition loss the cooldown diff can't see — only the
        # interchangeable filler GCDs a job opts into via filler_quality_gcds
        # (RDM's Dualcasted 440s); no-op for jobs that don't declare any.
        priced += imp.improvements_from_gcd_quality(
            actual_casts=list(you.norm_casts),
            idealized_timeline=idealized,
            data=get_job(job).data,
            fight_duration_s=you.fight_duration_s,
        )
        # Loose GCD pacing — the GCD-count shortfall vs the optimal line beyond the
        # discrete idle gaps (no single anchor). A top-level card so group_families
        # can fold it together with idle / over-weaving into "GCD uptime & pacing".
        priced += imp.improvements_from_cadence(
            [(t, a) for t, a in you.norm_casts if t >= 0],
            idealized, get_job(job).data, st("Clipping"))
    else:
        priced += imp.improvements_from_drift(st("Drift"))

    priced += imp.improvements_from_clipping(st("Clipping"))
    priced += imp.improvements_from_overcap(st("Overcap"))
    priced += imp.improvements_from_wildfire_windows(st("Wildfire"))
    # Hypercharge windows you opened but didn't fire a full 5 Blazing Shots into
    # — priced per missing shot at the sim-derived per-shot value.
    priced += imp.improvements_from_hypercharge_windows(
        st("Hypercharge"), enabler_values)
    priced += imp.improvements_from_located(st("Reassemble"), "align")
    # Tincture pot-timing loss (job-agnostic; empty for non-potting jobs / a
    # well-potted pull). reconcile_to_budget bounds it within the strict gap.
    priced += imp.improvements_from_tincture(st("Scoring"))
    # Opener is ordering, not net loss — the sim-diff owns any cast it actually
    # dropped — so it's a zero-priced note, not a priced card.
    diagnostics += imp.diagnostics_from_opener(st("Opener"))

    # Job-specific priced contributors registered on the Job (e.g. RPR's
    # Death's Design downtime, positional misses, Enshroud completion). Additive
    # and registry-driven — None for jobs that don't define any (MCH keeps its
    # inline lines above). reconcile_to_budget still bounds the total to the gap.
    contrib = getattr(get_job(job), "improvement_contributors", None)
    if contrib is not None:
        try:
            priced += list(contrib(you, idealized, enabler_values, death_windows) or [])
        except Exception:
            traceback.print_exc(file=sys.stderr)

    # Multi-target / AoE under-delivery — a grouped top-level card (ranked by its
    # total, so it leads the panel on an add fight) with one located child per
    # confirmed window. Job-agnostic (reads the shared MT state); no-op unless this
    # pull was multi-target-credited. Its total is a slice of the credited gap, so
    # reconcile_to_budget keeps it without over-attributing.
    priced += imp.improvements_from_multi_target(st("Scoring"))

    priced = imp.group_improvements(priced)
    if idealized:
        sc = st("Scoring")
        # On a credited multi-target pull the headline gap is the splash-aware
        # one (idealized_multitarget − delivered_multitarget), so reconcile the
        # cards to THAT so they sum to the recoverable figure the dashboard
        # shows. Falls back to the strict single-target gap otherwise.
        if sc.get("multi_target_credited"):
            recoverable = max(0.0, float(sc.get("idealized_multitarget", 0.0) or 0.0)
                                   - float(sc.get("delivered_multitarget", 0.0) or 0.0))
        else:
            recoverable = max(0.0, float(sc.get("idealized_strict", 0.0) or 0.0)
                                   - float(sc.get("delivered_potency", 0.0) or 0.0))
        priced = imp.reconcile_to_budget(priced, recoverable,
                                         extra_children=minor)
        # Group the causally-linked pacing cards (idle / over-weaving / loose
        # pacing) into one "GCD uptime & pacing" umbrella so related losses read
        # together instead of scattered; ordering stays by potency total. Distinct
        # major cards (overcap, missed cooldowns, tincture) and the "Other" residual
        # are left standalone. Presentation-only — totals are unchanged.
        priced = imp.group_families(priced)

    # Top-level response keys are emitted verbatim (not camelized by _emit),
    # so camelize the dataclass list here: time_s -> timeSec, etc. Priced
    # (ranked, incl. residual) first; zero-priced diagnostics trail.
    return _camelize(priced + diagnostics)


# Display durations for the idealized lane (cosmetic — the lane is a visual
# comparison, not a timing-accurate render).
_IDEAL_GCD_DUR_S = 1.0
_IDEAL_OGCD_DUR_S = 0.6


def _serialize_idealized_track(timeline: list[tuple[float, int]],
                               ability_meta: dict[int, dict]) -> list[dict]:
    """Turn the sim's (t, ability_id) timeline into CastEvents for the
    Timeline's idealized lane, seeding ability_meta along the way. Mirrors the
    color/yOffset scheme the player lane uses (oGCDs amber + lifted).

    Pre-pull casts (t < 0 — a precast channel like RDM Verthunder III / RPR
    Harpe, or MCH's pre-pull Reassemble) ARE included so they render in the
    Timeline's pre-pull zone; the player's Abilities track keeps its own pre-pull
    casts too, and the frontend cast-diff ignores t < 0 so an opener precast
    isn't flagged as a missed/extra cast."""
    from jobs._core.tincture import TINCTURE_ACTION_ID
    out: list[dict] = []
    for t, aid in timeline:
        if aid == TINCTURE_ACTION_ID:
            continue   # the in-sim pot marker drives the idealized pot band, not a cast
        m = get_metadata(aid)
        is_ogcd = bool(m.is_ogcd) if m else False
        _ensure_ability_meta(ability_meta, aid, m)
        name = m.name if m else f"action {aid}"
        out.append({
            "startSec": float(t),
            "endSec": float(t) + (_IDEAL_OGCD_DUR_S if is_ogcd else _IDEAL_GCD_DUR_S),
            "abilityId": aid,
            "label": name[:3],
            "tooltip": f"{name} @ {t:.1f}s (idealized)",
            "color": "#f59e0b" if is_ogcd else "#3b82f6",
            "iconPath": (m.icon if m else "") or None,
            "yOffset": -0.55 if is_ogcd else 0.0,
        })
    return out


def _phase_delivered(mr: ModuleResult) -> dict[int, float]:
    """`{phase_id: delivered_potency}` off a run's Scoring state (empty when the
    job has no simulator or the pull isn't phased)."""
    sc = mr.aspects.get("Scoring")
    return dict((sc.state.get("phase_delivered") or {})) if sc is not None else {}


def _observed_tincture(mr: ModuleResult) -> list:
    sc = mr.aspects.get("Scoring")
    return list(sc.state.get("observed_tincture_windows") or []) if sc is not None else []


def _build_phase_analysis(job: str, you: ModuleResult, refs: list[ModuleResult],
                          ability_meta: dict[int, dict],
                          suppress_deviations: bool) -> dict | None:
    """Per-phase (phasic) analysis for a phased pull (ultimates). `None` unless
    the subject carries phase segments. Combines per-phase execution metrics
    (gauge banking, GCD pace, pot, overcap — from `phase_metrics`) with per-phase
    delivered potency (from each run's Scoring state), aggregates the refs to
    medians/IQR, and detects where the subject saves/spends abnormally vs the top
    clears. Deviations are suppressed for locked-healer runs (rankSuppressed —
    their ceiling/comparisons aren't a fair yardstick)."""
    if not you.phases:
        return None
    from jobs._core.phase_metrics import (aggregate_phase_metrics,
                                          compute_phase_metrics,
                                          detect_deviations)
    gauges = get_job(job).data.gauges

    def _is_gcd(aid: int) -> bool:
        m = get_metadata(aid)
        return m is None or not m.is_ogcd

    scored_end = float(you.fight_duration_s)
    user_metrics = compute_phase_metrics(
        list(you.norm_casts), you.phases, gauges, you.downtime_windows,
        end_s=scored_end, tincture_windows=_observed_tincture(you), is_gcd=_is_gcd)
    you_deliv = _phase_delivered(you)

    # The subject's final PARTIAL phase (a wipe's truncated last phase): each
    # ref's version of it is recomputed over the elapsed-time-matched prefix so a
    # full clear isn't compared against the subject's shorter slice.
    final_partial = next((m for m in reversed(user_metrics) if m.partial), None)
    up_final = (next((p for p in you.phases if p.id == final_partial.phase_id), None)
                if final_partial is not None else None)

    ref_metrics_list: list = []
    ref_deliv_list: list = []
    for r in refs:
        if not r.phases:
            continue
        r_tinc = _observed_tincture(r)
        rm = compute_phase_metrics(
            list(r.norm_casts), r.phases, gauges, r.downtime_windows,
            tincture_windows=r_tinc, is_gcd=_is_gcd)
        r_deliv = _phase_delivered(r)
        if up_final is not None:
            rp = next((p for p in r.phases if p.id == up_final.id), None)
            if rp is not None:
                r_end = rp.start_s + (scored_end - up_final.start_s)
                trunc_all = compute_phase_metrics(
                    list(r.norm_casts), r.phases, gauges, r.downtime_windows,
                    end_s=r_end, tincture_windows=r_tinc, is_gcd=_is_gcd)
                trunc = next((m for m in trunc_all if m.phase_id == up_final.id), None)
                if trunc is not None:
                    rm = [trunc if m.phase_id == up_final.id else m for m in rm]
                # Whole-phase delivered can't be time-matched from the scalar, so
                # drop this partial phase from the delivered comparison.
                r_deliv = {k: v for k, v in r_deliv.items() if k != up_final.id}
        ref_metrics_list.append(rm)
        ref_deliv_list.append(r_deliv)

    ref_count = len(ref_metrics_list)
    agg = aggregate_phase_metrics(ref_metrics_list, delivered_per_ref=ref_deliv_list)

    deviations = ([] if suppress_deviations else detect_deviations(
        user_metrics, agg, you.phases, gauges,
        ref_count=ref_count, user_delivered=you_deliv))

    # --- Serialize (camelCase; this response isn't camelized) ---------------
    user_out = [{
        "phaseId": m.phase_id,
        "partial": m.partial,
        "activeSec": round(m.active_s, 1),
        "gcdCasts": m.gcd_casts,
        "totalCasts": m.total_casts,
        "deliveredPotency": round(float(you_deliv.get(m.phase_id, 0.0))),
        "gauges": [{
            "name": g.name, "entry": g.entry, "exit": g.exit,
            "generated": g.generated, "spent": g.spent, "overcapped": g.overcapped,
        } for g in m.gauges],
        "potUsed": m.pot_used,
    } for m in user_metrics]

    user_by_id = {m.phase_id: m for m in user_metrics}
    refs_out = []
    for pid, a in sorted(agg.items()):
        # notableCasts: per-ability medians only where the subject deviates by
        # >= 2 casts from the ref median (keeps the payload lean).
        um = user_by_id.get(pid)
        notable = []
        if um is not None:
            for aid, ref_med in a.ability_median.items():
                yours = um.casts_by_ability.get(aid, 0)
                if abs(yours - ref_med) >= 2:
                    _ensure_ability_meta(ability_meta, aid)
                    notable.append({
                        "abilityId": aid, "yourCasts": yours,
                        "refMedian": round(ref_med, 1),
                    })
            notable.sort(key=lambda n: -abs(n["yourCasts"] - n["refMedian"]))
        refs_out.append({
            "phaseId": pid,
            "refCount": a.ref_count,
            "gcdCasts": {k: round(v, 1) for k, v in a.gcd_casts.items()},
            "gcdRate": {k: round(v, 3) for k, v in a.gcd_rate.items()},
            "deliveredPotency": {k: round(v) for k, v in a.delivered.items()},
            "gauges": [{
                "name": gname,
                "exit": {k: round(v, 1) for k, v in st["exit"].items()},
                "overcapped": {k: round(v, 1) for k, v in st["overcapped"].items()},
                "spent": {k: round(v, 1) for k, v in st["spent"].items()},
                "generated": {k: round(v, 1) for k, v in st["generated"].items()},
            } for gname, st in a.gauges.items()],
            "potPct": round(a.pot_pct, 2),
            "notableCasts": notable[:6],
        })

    dev_out = [{
        "phaseId": d["phase_id"],
        "kind": d["kind"],
        **({"gauge": d["gauge"]} if d.get("gauge") else {}),
        **({"abilityId": d["abilityId"]} if d.get("abilityId") else {}),
        "yourValue": d["your_value"],
        "refValue": d["ref_value"],
        "text": d["text"],
    } for d in deviations]

    return {"user": user_out, "refs": refs_out, "deviations": dev_out}


def _build_response(job: str, you: ModuleResult, refs: list[ModuleResult],
                    comparisons: dict[str, AspectComparison]) -> dict:
    ability_meta: dict[int, dict] = {}
    you_summary = _run_summary(you, include_track=True, ability_meta=ability_meta)
    # Refs carry their Abilities track too, for the Timeline's reference lanes
    # (they run the same aspect pipeline as `you`). Shares the ability_meta map
    # so ref-only ability icons resolve.
    refs_summary = [
        _run_summary(r, include_track=True, ability_meta=ability_meta)
        for r in refs
    ]

    # Pull ability metadata for IDs referenced by the Drift aspect findings.
    drift_state = (you.aspects.get("Drift") or _Empty()).state
    for d in drift_state.get("findings", []) or []:
        _ensure_ability_meta(ability_meta, int(getattr(d, "ability_id", 0) or 0))

    idealized = _idealized_timeline(job, you)
    # Multi-target pulls. `_inject_multi_target` may have credited splash on both
    # sides (delivered + ceiling over confirmed windows) — if so the efficiency
    # is fair and we DON'T disclaim it; otherwise the single-target number is an
    # understatement and we do. Either way the sim-diff improvements are
    # single-target-based and would mis-rank losses on a multi-target pull, so
    # they're suppressed whenever the pull is substantially multi-target.
    from jobs._core.downtime_sources import is_multi_target_pull
    mt_state = _scoring_state(you)
    is_mt = is_multi_target_pull(list(getattr(you, "multi_target_windows", ())))
    credited = bool(mt_state.get("multi_target_credited"))
    disclaimed = is_mt and not credited
    # Suppress the sim-diff improvements only while the pull is DISCLAIMED — then
    # the numbers are the understated single-target ones and the cards would
    # double-mislead. Once splash is credited the efficiency is fair and the
    # rotation really is single-target + incidental splash (which the sim
    # models), so the missed-cast / clip / overcap cards are valid and shown.
    improvements = [] if disclaimed else _build_improvements(job, you, idealized)
    # Ensure icons resolve for every ability a suggestion references.
    for im in improvements:
        _ensure_ability_meta(ability_meta, int(im.get("abilityId", 0) or 0))
    # Idealized comparison lane for the Timeline (empty when no simulator).
    # Buff-aware (canonical master windows) so burst aligns to the real opener
    # window, unlike the strict `idealized` the improvements panel diffs against.
    display_timeline = _idealized_display_timeline(job, you)
    idealized_track = _serialize_idealized_track(display_timeline, ability_meta)
    # Second lane: the canonical 'hold burst for the 2-min window' line the
    # player can toggle to. [] when no party buffs (UI shows only the optimal).
    idealized_track_canonical = _serialize_idealized_track(
        _idealized_canonical_timeline(job, you), ability_meta)
    # On a CREDITED multi-target pull, also ship the single-target idealized lane
    # (same display sim WITHOUT the multi-target schedule). The frontend caches
    # both and splices this one into any window the user marks "not possible",
    # so the lane visibly reverts to single-target there with no re-sim. [] when
    # not credited (the AoE and single-target lanes are then identical anyway).
    idealized_track_strict: list[dict] = []
    if credited:
        idealized_track_strict = _serialize_idealized_track(
            _idealized_display_timeline(
                job, you, sim_context=_scoring_state(you).get("sim_context")),
            ability_meta)

    # Reconstruct each player's clipped pre-pull begincast so their opener precast
    # renders in the Timeline's pre-pull zone aligned with the simulated one.
    _sc_aspect = next((a for a in get_job(job).aspects
                       if getattr(a, "name", "") == "Scoring"), None)
    _prepull_ids = getattr(_sc_aspect, "prepull_channel_ids", frozenset())
    _align_player_prepull(you_summary.get("abilitiesTrack"), display_timeline, _prepull_ids)
    _inject_inferred_prepull(
        you_summary.get("abilitiesTrack"), display_timeline,
        (you.aspects.get("Abilities") or _Empty()).state.get("prepull_inferred_casts", []),
        ability_meta)
    for r, rsum in zip(refs, refs_summary):
        _align_player_prepull(rsum.get("abilitiesTrack"), display_timeline, _prepull_ids)
        _inject_inferred_prepull(
            rsum.get("abilitiesTrack"), display_timeline,
            (r.aspects.get("Abilities") or _Empty()).state.get("prepull_inferred_casts", []),
            ability_meta)

    # Annotate every lane's casts with their multi-target hit status (full vs
    # under-target) so the timeline flags splash casts on you, each ref, AND the
    # idealized lane — letting the user compare and judge whether each event was
    # really a multi-target opportunity.
    if credited:
        splash = get_job(job).data.splash_potencies
        # The idealized lane now casts dedicated AoE buttons too (credited pulls
        # render the AoE-aware rotation), so annotate those with their per-target
        # dot as well — union the free-splash + AoE ability ids.
        splashable_ids = {**splash, **get_job(job).data.aoe_potencies}
        mt_windows_ser = mt_state.get("multi_target_windows", [])
        if you_summary.get("abilitiesTrack"):
            _annotate_track_hits(you_summary["abilitiesTrack"],
                                 mt_state.get("multi_target_hits", []))
        for r, rsum in zip(refs, refs_summary):
            track = rsum.get("abilitiesTrack")
            if track:
                rstate = (r.aspects.get("Scoring") or _Empty()).state
                _annotate_track_hits(track, rstate.get("multi_target_hits", []))
        _annotate_idealized_hits(idealized_track, mt_windows_ser, splashable_ids)
        _annotate_idealized_hits(idealized_track_canonical, mt_windows_ser, splashable_ids)

    # Headline death indicator — count + per-death timing + total cost (summed
    # from the priced Death cards above, so the number matches the panel).
    headline = _headline(you, refs, comparisons)
    headline["deaths"] = [
        {"timeSec": float(s), "durationSec": float(e - s)}
        for s, e in you.death_windows
    ]
    headline["deathsLostPotency"] = sum(
        float(im.get("lostPotency", 0) or 0)
        for im in improvements if im.get("kind") == "death"
    )
    # Situational ceiling-only squeezes the user confirms/denies (MCH
    # Flamethrower today) — derived from the same display timeline the idealized
    # lane shows, so each window's id matches an idealized-lane cast. Generic on
    # the wire; the frontend renders one WindowReview per group. Job-isolated
    # producer lives in improvements.py next to the FT improvement logic.
    from jobs._core import improvements as _imp
    reviewable_windows = _imp.reviewable_windows_from_idealized(
        display_timeline, get_job(job).data)

    # Multi-target headline fields. `multiTargetDisclaimed` = efficiency is the
    # understated single-target number (no confirmed credit). `multiTargetCredited`
    # = the efficiency now includes window-gated splash on both sides.
    # `multiTargetWindows` = the confirmed windows the credit was applied over.
    headline["multiTargetDisclaimed"] = disclaimed
    headline["multiTargetCredited"] = credited
    headline["multiTargetWindows"] = (
        mt_state.get("multi_target_windows", []) if credited else [])

    # Healer mit-plan headline fields (absent-key pattern — only a healer run
    # carries them). `rankSuppressed` hides the rank/percentile-vs-refs and
    # counts-vs-ref-median comparisons frontend-side: top-parsing healers force
    # DPS into heal windows, so neither is a fair reference; refs still power
    # the Tier-B consensus and reference lanes. `healLocksApplied` marks a
    # ceiling that already pays the plan's heal GCDs — the honest maximum —
    # and gates the >100% "above the honest ceiling" framing.
    if _is_locked_healer_analysis(job):
        headline["rankSuppressed"] = True
        if mt_state.get("heal_locks_applied"):
            headline["healLocksApplied"] = True
            headline["healLockCount"] = int(mt_state.get("heal_lock_count") or 0)
            headline["healLockPotency"] = float(
                mt_state.get("heal_lock_potency") or 0.0)
            headline["mitPlanComp"] = list(mt_state.get("mit_plan_comp") or [])
            headline["mitPlanCompSource"] = str(
                mt_state.get("mit_plan_comp_source") or "defaults")
            if mt_state.get("mit_plan_warnings"):
                headline["mitPlanWarnings"] = list(mt_state["mit_plan_warnings"])

    # Prog (wipe) pull headline fields (absent-key pattern — kills stay
    # byte-identical). The scored window was already truncated at the terminal
    # death inside analyze_pull, so killTimeSec/efficiency describe the played
    # window; these fields carry the full-pull framing plus the projected kill
    # time. `isProgPull` suppresses rank/percentile/vs-refs frontend-side (a
    # truncated wipe against kill refs is never a fair comparison) — distinct
    # from `rankSuppressed`, which implies the healer heal-lock framing.
    if you.prog is not None:
        from jobs._core.kill_projection import (ProjectionInputs,
                                                project_kill_time)
        p = you.prog
        headline["isProgPull"] = True
        headline["pullDurationSec"] = float(p["wipe_duration_s"])
        if p.get("fight_pct") is not None:
            headline["fightPercentage"] = float(p["fight_pct"])
        if p.get("boss_pct") is not None:
            headline["bossPercentage"] = float(p["boss_pct"])
        if p.get("last_phase"):
            headline["lastPhase"] = int(p["last_phase"])
        if p.get("terminal_death_s") is not None:
            headline["terminalDeathSec"] = float(p["terminal_death_s"])
        proj = project_kill_time(ProjectionInputs(
            elapsed_s=float(p["wipe_duration_s"]),
            fight_pct_remaining=p.get("fight_pct"),
            own_downtime_s=sum(
                b - a for a, b in (p.get("full_downtime_windows") or ())),
            ref_downtime_windows=tuple(
                tuple((float(a), float(b)) for a, b in r.downtime_windows)
                for r in refs),
            ref_kill_times=tuple(float(r.fight_duration_s) for r in refs),
            last_phase=p.get("last_phase"),
            phase_transitions=tuple(p.get("phase_transitions") or ()),
        ))
        if proj is not None:
            headline["projectedKillTimeSec"] = float(proj.projected_s)
            headline["projectionMeta"] = {
                "method": proj.method,
                "refCount": proj.ref_count,
                "refKillSec": float(proj.ref_kill_s),
                "activeSec": float(proj.active_s),
                "downtimeBeyondSec": float(proj.downtime_beyond_s),
                "burnedPct": float(proj.burned_pct),
            }

    # Tag each ability as defensive/utility (excluded from the DPS timeline +
    # cast-diff) so the frontend filters off one wire flag instead of a
    # hand-maintained per-job id list. Source of truth: the shared role actions
    # ∪ this job's defensive oGCDs. Single post-pass after every _ensure_ability_meta.
    from jobs._core.role_actions import ROLE_ACTION_IDS
    defensive_ids = ROLE_ACTION_IDS | get_job(job).data.defensive_ids
    for aid, m in ability_meta.items():
        m["isDefensive"] = aid in defensive_ids

    # Where the sim potted (idealized lane band): read straight off the pot markers
    # the sim placed in the displayed idealized timeline. Per-job; [] when no tincture.
    from jobs._core.tincture import spec_for_job, tincture_windows_from_timeline
    _jd = get_job(job).data
    _tspec = spec_for_job(_jd.tincture_main_stat, _jd.tincture_role_coeff)
    # Pot RECOMMENDATION source. For a prog (wipe) pull, place the idealized pots
    # with the KILL in mind: a progger pots on the full-fight cadence (opener,
    # then ~270s), aiming to kill — not re-optimized for the window that happens
    # to end at their death. So run the pot placement over the PROJECTED KILL
    # length and clip to the window they actually played. Kills use the played
    # (== full) timeline unchanged. Only the pot OVERLAY changes here; the
    # idealized cast lane and the missed-cast diff still use the played-window
    # `display_timeline`.
    _pot_timeline = display_timeline
    _played_s = float(you.fight_duration_s)
    _sim = get_job(job).simulator
    if (you.prog is not None and _sim is not None
            and _tspec is not None and _tspec.multiplier > 1.0):
        _kill_s = float(headline.get("projectedKillTimeSec") or 0.0)
        if _kill_s <= _played_s + 1.0:
            # No projection (e.g. no refs): fall back to the full pull length as a
            # floor so pots at least aren't crammed into the death-clamped window.
            _kill_s = max(_played_s,
                          float((you.prog or {}).get("wipe_duration_s") or _played_s))
        if _kill_s > _played_s + 1.0:
            try:
                from jobs._core.buff_windows import (expected_windows,
                                                     multiplier_intervals)
                _kbuffs = multiplier_intervals(
                    expected_windows(_kill_s, list(you.party_jobs))) or None
                _kres = _sim.simulate(_kill_s, tuple(you.downtime_windows),
                                      buff_intervals=_kbuffs,
                                      sim_context=_user_sim_context(you))
                _pot_timeline = list(_kres.timeline)
            except Exception:
                _pot_timeline = display_timeline   # degrade to the played window
    idealized_tincture = [
        {"startSec": float(w.start_s), "endSec": float(w.end_s),
         "multiplier": float(w.multiplier)}
        for w in tincture_windows_from_timeline(_pot_timeline, _tspec)
        # Clip to the played window — show the kill-cadence pots that fall inside
        # the time they actually reached (a no-op for a kill).
        if w.start_s <= _played_s + 0.5]

    response: dict[str, Any] = {
        "you": you_summary,
        "refs": refs_summary,
        "headline": headline,
        "improvements": improvements,
        "idealizedTinctureWindows": idealized_tincture,
        "idealizedTrack": idealized_track,
        "idealizedTrackCanonical": idealized_track_canonical,
        "idealizedTrackStrict": idealized_track_strict,
        "reviewableWindows": reviewable_windows,
        "comparisons": {
            name: _serialize_comparison(c) for name, c in comparisons.items()
        },
        "aspectStates": {
            name: _serialize_aspect_state(name, ar.state)
            for name, ar in you.aspects.items()
        },
        "abilityMeta": ability_meta,
    }

    # Boss phase segments (phasic analysis). Emitted ONLY when the fight carries
    # phases (ultimates and multi-phase encounters); absent for single-phase
    # Savage pulls, which stay byte-identical on the wire. `reached` covers the
    # full wipe span (a progger did reach those phases even if their scored
    # window is clamped earlier at a terminal death); `completed` respects the
    # clamped scored end.
    if you.phases:
        from jobs._core.phases import downtime_overlap_s
        scored_end = float(you.fight_duration_s)
        full_end = float((you.prog or {}).get("wipe_duration_s") or scored_end)
        response["phases"] = [
            {
                "id": ph.id,
                "name": ph.name,
                "startSec": float(ph.start_s),
                "endSec": float(ph.end_s),
                "isIntermission": bool(ph.is_intermission),
                "downtimeSec": float(downtime_overlap_s(ph, you.downtime_windows)),
                "reached": bool(ph.start_s < full_end),
                "completed": bool(ph.end_s <= scored_end + 0.5),
            }
            for ph in you.phases
        ]
        # Per-phase execution + ref-pattern deviations (the phasic panel).
        phase_analysis = _build_phase_analysis(
            job, you, refs, ability_meta,
            suppress_deviations=bool(headline.get("rankSuppressed")))
        if phase_analysis is not None:
            response["phaseAnalysis"] = phase_analysis

    return response


# ---------------------------------------------------------------------------
# Kill Time Theorizer
# ---------------------------------------------------------------------------

# Theorized kill time is clamped to a sane band; the ~7s spread is sampled on a
# 1s grid (each sample is its own perfect-sim cache key, so re-runs are cheap).
_THEORIZE_MIN_S = 30.0
_THEORIZE_MAX_S = 1800.0
_THEORIZE_RANGE_DEFAULT_S = 7.0
_THEORIZE_RANGE_MAX_S = 60.0


def _clip_downtime(windows: list[tuple[float, float]],
                   duration_s: float) -> list[tuple[float, float]]:
    """Keep the pull's observed downtime windows that start before the theorized
    kill, truncating any that straddle it. A longer target keeps every window
    and gains a pure-uptime tail; a shorter one drops late windows. The honest
    'what your fight's constraints imply at time T' — never invents mechanics."""
    return [(s, min(e, duration_s)) for s, e in windows if s < duration_s]


def _avg_kill_s(refs: list[ModuleResult]) -> float:
    """Mean kill time across reference runs (0 when there are none)."""
    durs = [r.fight_duration_s for r in refs if r.fight_duration_s]
    return round(sum(durs) / len(durs), 1) if durs else 0.0


def _encounter_downtime(job: str, encounter_id: int, target: float,
                        progress) -> tuple[list[tuple[float, float]], dict]:
    """An encounter's fight constraints (downtime) derived from the reference
    logs — the closest-by-duration top ref — so the theorizer needs no character
    or analyzed pull. Reuses `_get_refs`: a warmed matrix entry returns instantly,
    a cold one is built (streaming progress) and cached for next time. Also yields
    a representative party comp (that ref's) + the refs' average kill time.
    Returns `(windows, info)`."""
    info = {"source": "none", "count": 0, "killSec": 0.0, "killAvgSec": 0.0,
            "partyJobs": []}
    if not encounter_id:
        return [], info
    try:
        refs = _get_refs(_client(), job, encounter_id, "Top 10", progress)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return [], info
    if not refs:
        return [], info
    # The ref whose kill time is nearest the theorized target — its phase
    # structure best matches the kill we're modeling. Clipping handles the rest.
    best = min(refs, key=lambda r: abs(r.fight_duration_s - target))
    info = {
        "source": "references",
        "count": len(refs),
        "killSec": float(best.fight_duration_s),
        "killAvgSec": _avg_kill_s(refs),
        "partyJobs": list(getattr(best, "party_jobs", ()) or ()),
    }
    return list(best.downtime_windows), info


def theorize_kill_time(req: dict, req_id: str) -> dict:
    """Self-sufficient: the sim's ideal output + cast timeline for a *theorized*
    kill time, under an encounter's real downtime + a user-chosen party comp's
    raid buffs. Needs no character or analyzed pull — given `encounterId` it
    reaches out via the SAME reference pipeline the dashboard uses to derive the
    fight's downtime (`_encounter_downtime`), streaming progress. Tests/advanced
    callers may pass explicit `downtimeWindows` to bypass the fetch.
    Healer note: a theorized run has no pull to plan mitigation against, so a
    healer's theorized ceiling stays LOCK-FREE (the pure damage optimum) —
    only run_analysis bakes the mit plan's heal GCDs in.
    See src/views/KillTimeTheorizer.tsx."""
    # The lazy loader (vs the module-level eager `get_job`) so a theorize request
    # works even if it's the first thing to touch this job's package.
    from jobs import get_job as _get_job
    job: str = req["spec"]
    try:
        sim = _get_job(job).simulator
    except KeyError:
        # Unregistered spec — every registered job now ships a simulator, so the
        # "unsupported" path is reachable only for a job the analyzer doesn't know.
        return {"unsupported": True}
    if sim is None:
        return {"unsupported": True}

    target = float(req.get("targetKillSec") or 0.0)
    target = max(_THEORIZE_MIN_S, min(target, _THEORIZE_MAX_S))
    range_s = max(0.0, min(float(req.get("rangeSec") or _THEORIZE_RANGE_DEFAULT_S),
                           _THEORIZE_RANGE_MAX_S))
    party = [j for j in (req.get("partyJobs") or []) if j]

    def progress(pct: int, stage: str, tasks: list[dict] | None = None) -> None:
        msg: dict[str, Any] = {"stage": stage, "pct": pct}
        if tasks is not None:
            msg["tasks"] = tasks
        _emit({"id": req_id, "progress": msg})

    # Downtime: an explicit override (tests / advanced) wins; otherwise derive it
    # from the encounter's reference logs (the self-sufficient path).
    if req.get("downtimeWindows") is not None:
        observed_dt = [(float(w["startSec"]), float(w["endSec"]))
                       for w in req["downtimeWindows"]]
        ref_info = {"source": "explicit", "count": 0, "killSec": 0.0,
                    "partyJobs": []}
    else:
        progress(8, "Loading reference fight data…")
        observed_dt, ref_info = _encounter_downtime(
            job, int(req.get("encounterId") or 0), target, progress)

    progress(92, "Computing ideal rotation…")

    from jobs._core.buff_windows import expected_windows, multiplier_intervals

    def buffs(duration_s: float):
        if not party:
            return None
        return multiplier_intervals(expected_windows(duration_s, party)) or None

    def ideal_potency(duration_s: float) -> float:
        return sim.simulate(
            duration_s, tuple(_clip_downtime(observed_dt, duration_s)),
            buff_intervals=buffs(duration_s)).delivered_potency

    # ~range_s-wide spread on a 1s grid (incl. the exact target) so the card can
    # show how the ceiling moves across the realistic kill-time band.
    half = range_s / 2.0
    grid = {round(target)}
    d = int(target - half)
    while d <= int(target + half) + 1:
        grid.add(d)
        d += 1
    durations = sorted({max(_THEORIZE_MIN_S, min(float(g), _THEORIZE_MAX_S))
                        for g in grid})
    samples = [{"killSec": dur, "idealizedPotency": round(ideal_potency(dur), 1)}
               for dur in durations]

    # Center (entered) target — the timeline we display.
    center_dt = _clip_downtime(observed_dt, target)
    center_buffs = buffs(target)
    res = sim.simulate(target, tuple(center_dt), buff_intervals=center_buffs)

    ability_meta: dict[int, dict] = {}
    timeline = _serialize_idealized_track(list(res.timeline), ability_meta)

    # In-sim tincture placement on the ideal lane (mirrors _build_response) — read
    # off the pot markers the sim placed in the timeline.
    from jobs._core.tincture import spec_for_job, tincture_windows_from_timeline
    jd = _get_job(job).data
    tspec = spec_for_job(jd.tincture_main_stat, jd.tincture_role_coeff)
    tincture_windows = [
        {"startSec": float(w.start_s), "endSec": float(w.end_s),
         "multiplier": float(w.multiplier)}
        for w in tincture_windows_from_timeline(list(res.timeline), tspec)]

    progress(100, "Done")
    return {
        "targetKillSec": target,
        "idealizedPotency": round(res.delivered_potency, 1),
        "timeline": timeline,
        "downtimeWindows": [{"startSec": float(s), "endSec": float(e)}
                            for s, e in center_dt],
        "buffWindows": [{"startSec": float(s), "endSec": float(e),
                         "multiplier": float(m)}
                        for s, e, m in (center_buffs or [])],
        "tinctureWindows": tincture_windows,
        "samples": samples,
        "abilityMeta": ability_meta,
        # Where the downtime came from + a representative reference comp, so the
        # card can disclose "based on N references (closest kill m:ss)".
        "downtimeSource": ref_info["source"],
        "refCount": ref_info["count"],
        "refKillTimeSec": ref_info["killSec"],
        "refAvgKillSec": ref_info.get("killAvgSec", 0.0),
        "refPartyJobs": ref_info["partyJobs"],
    }


# ---------------------------------------------------------------------------
# Mitigation planner (mitplan/ — encounter-scoped healer-duo plans)
# ---------------------------------------------------------------------------

# The damage model (top-log fetch + classification) is duo-independent, so it
# caches per encounter for the process lifetime; switching duo/comp re-runs
# only the pure planner. Same inflight-collapse idiom as _get_refs.
_mitplan_cache: dict[int, Any] = {}
_mitplan_inflight: dict[int, threading.Event] = {}
_mitplan_lock = threading.Lock()

_MITPLAN_TIER_COLORS = {
    "tank": "#7aa2f7", "healer": "#9ece6a", "dps": "#f7768e",
}
_MITPLAN_SUGGEST_COLOR = "#565f89"


def _get_mitplan_model(client, encounter_id: int, progress):
    import mitplan
    while True:
        with _mitplan_lock:
            model = _mitplan_cache.get(encounter_id)
            if model is not None:
                return model
            ev = _mitplan_inflight.get(encounter_id)
            if ev is None:
                _mitplan_inflight[encounter_id] = threading.Event()
                break
        progress(10, "Waiting for the damage model build in flight…", None)
        ev.wait()
    try:
        model = mitplan.build_damage_model(client, encounter_id,
                                           progress=progress)
        with _mitplan_lock:
            _mitplan_cache[encounter_id] = model
        return model
    finally:
        with _mitplan_lock:
            _mitplan_inflight.pop(encounter_id).set()


def _mmss(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _serialize_mit_plan(model, result) -> dict:
    """Snake_case response body for plan_mitigation; `_camelize` finishes it."""
    from mitplan.library import role_for_job
    ability_meta: dict[int, dict] = {}
    lanes = []
    for slot, job in result.slots:
        lanes.append({"slot": slot, "job": job,
                      "label": f"{slot} · {job}", "casts": []})
    lane_by_slot = {ln["slot"]: ln for ln in lanes}

    mechanics = []
    markers = []
    for pm in result.mechanics:
        m = pm.mech
        for bid in m.boss_ability_ids:
            # Boss-ability icons for the plan board's mechanic rows (XIVAPI
            # resolves boss actions too; rows fall back to a glyph without).
            _ensure_ability_meta(ability_meta, bid)
        mechanics.append({
            "id": m.id, "time_s": m.time_s, "end_s": m.end_s, "name": m.name,
            "boss_ability_ids": list(m.boss_ability_ids),
            "kind": m.kind, "school": m.school,
            "hits": m.hits,
            "unmitigated": m.unmitigated, "unmitigated_p90": m.unmitigated_p90,
            "observed_mit_pct": m.observed_mit_pct,
            "presence_ratio": m.presence_ratio,
            "tank_targets": m.tank_targets,
            "assignments": pm.assignments,
            "gcd_heals": pm.gcd_heals,
            "predicted": pm.predicted, "hp_after": pm.hp_after,
            "status": pm.status, "notes": list(m.notes) + list(pm.notes),
        })
        markers.append({
            "mechanic_id": m.id, "time_s": m.time_s, "end_s": m.end_s,
            "name": m.name, "kind": m.kind, "school": m.school,
            "status": pm.status,
            "unmit_total": sum(float(m.unmitigated.get(r) or 0.0)
                               for r in ("tank", "healer", "dps")),
        })
        for a in pm.assignments:
            _ensure_ability_meta(ability_meta, a.action_id)
            if a.is_carryover:
                continue   # the original cast already renders on its lane
            color = (_MITPLAN_SUGGEST_COLOR if a.is_suggestion
                     else _MITPLAN_TIER_COLORS.get(role_for_job(a.job), "#888"))
            tip = (f"{_mmss(a.cast_at_s)}  {a.name} — {m.name} "
                   f"({_mmss(m.time_s)})")
            if a.is_suggestion:
                tip += " · suggested"
            lane_by_slot[a.slot]["casts"].append({
                "start_s": a.cast_at_s,
                "end_s": a.cast_at_s + max(a.duration_s, 1.5),
                "ability_id": a.action_id, "label": a.name, "tooltip": tip,
                "color": color, "y_offset": 0 if a.is_gcd else -1,
            })
        for gh in pm.gcd_heals:
            _ensure_ability_meta(ability_meta, gh.action_id)
            for k in range(gh.count):
                t = gh.cast_at_s + k * gh.cast_time_s
                lane_by_slot[gh.slot]["casts"].append({
                    "start_s": t, "end_s": t + gh.cast_time_s,
                    "ability_id": gh.action_id, "label": gh.name,
                    "tooltip": (f"{_mmss(t)}  {gh.name} — top-up before "
                                f"{m.name} ({_mmss(m.time_s)})"),
                    "color": _MITPLAN_TIER_COLORS["healer"], "y_offset": 0,
                })
    for ln in lanes:
        ln["casts"].sort(key=lambda c: (c["start_s"], c["ability_id"] or 0))

    return _camelize({
        "encounter_id": model.encounter_id,
        "encounter_name": model.encounter_name,
        "shield_healer": result.slots[2][1],
        "regen_healer": result.slots[3][1],
        "party_jobs": [job for _, job in result.slots],
        "model_kill_s": model.model_kill_s,
        "ref_count": model.ref_count,
        "ref_avg_kill_s": model.ref_avg_kill_s,
        "avoidable_count": model.avoidable_count,
        "role_hp": model.role_hp,
        "hp_source": model.hp_source,
        "summary": result.summary,
        "mechanics": mechanics,
        "lanes": lanes,
        "damage_markers": markers,
        "downtime_windows": [{"start_s": s, "end_s": e}
                             for s, e in model.downtime_windows],
        "ability_meta": ability_meta,
        "warnings": list(result.warnings),
    })


def plan_mitigation(req: dict, req_id: str) -> dict:
    """Encounter-scoped healer-duo mitigation plan. Needs no character or
    analyzed pull — the damage timeline comes from top-ranked kill logs.
    With `reportCode`/`fightId` (and no explicit comp fields) the comp is
    resolved from that pull's actors instead — the healer-flow preselection
    (`spec` = the analyzed player's job, kept in their own slot on
    non-standard duos). See src/views/MitigationPlanner.tsx."""
    from mitplan.library import (DPS_JOBS, REGEN_HEALERS, SHIELD_HEALERS,
                                 TANK_JOBS)
    import mitplan

    shield = str(req.get("shieldHealer") or "Sage")
    regen = str(req.get("regenHealer") or "White Mage")
    tanks = [str(j) for j in (req.get("tanks") or []) if j]
    dps = [str(j) for j in (req.get("dps") or []) if j]
    comp_source = "request" if req.get("shieldHealer") else "defaults"
    comp_warnings: list[str] = []
    if (req.get("reportCode") and req.get("fightId")
            and not req.get("shieldHealer") and not tanks and not dps):
        from jobs._core.actors import find_fight
        from mitplan.comp import resolve_comp_from_fight
        try:
            report = _client().get_report_summary(str(req["reportCode"]))
            fight = find_fight(report, int(req["fightId"]))
            if fight is None:
                raise ValueError("fight not found")
            res = resolve_comp_from_fight(report, fight,
                                          anchor_job=req.get("spec") or None)
            shield, regen = res.shield_healer, res.regen_healer
            tanks, dps = list(res.tanks), list(res.dps)
            comp_source, comp_warnings = res.source, list(res.warnings)
        except Exception as exc:
            comp_source = "defaults"
            comp_warnings = ["Could not read this pull's party — "
                             "planned with the default comp."]
            event_log.log("warn", "mitplan",
                          "pull comp resolution failed",
                          {"reportCode": req.get("reportCode"),
                           "fightId": req.get("fightId"), "error": str(exc)})
    if not tanks:
        tanks = ["Paladin", "Dark Knight"]
    if not dps:
        dps = ["Samurai", "Dragoon", "Bard", "Pictomancer"]
    if shield not in SHIELD_HEALERS:
        raise ValueError(f"shieldHealer must be one of {SHIELD_HEALERS}")
    if regen not in REGEN_HEALERS:
        raise ValueError(f"regenHealer must be one of {REGEN_HEALERS}")
    if len(tanks) != 2 or any(j not in TANK_JOBS for j in tanks):
        raise ValueError("tanks must be two tank jobs")
    if len(dps) != 4 or any(j not in DPS_JOBS for j in dps):
        raise ValueError("dps must be four DPS jobs")
    encounter_id = int(req.get("encounterId") or 0)
    if not encounter_id:
        raise ValueError("encounterId required")

    def progress(pct: int, stage: str, tasks: list[dict] | None = None) -> None:
        msg: dict[str, Any] = {"stage": stage, "pct": pct}
        if tasks is not None:
            msg["tasks"] = tasks
        _emit({"id": req_id, "progress": msg})

    model = _get_mitplan_model(_client(), encounter_id, progress)
    pinned, pf_warnings = _pf_pinned_plan(encounter_id,
                                          bool(req.get("usePfMitPlan")))
    progress(92, "Scheduling mitigation plan…")
    result = mitplan.plan(model, shield, regen, tanks, dps, pinned=pinned)
    progress(97, "Rendering…")
    out = _serialize_mit_plan(model, result)
    # _serialize_mit_plan camelizes internally — these are wire keys.
    out["compSource"] = comp_source
    if comp_warnings:
        out["compWarnings"] = comp_warnings
    # `usePfMitPlan` was requested but no premade applied (non-ultimate / no file)
    # ⇒ pfPlanApplied False so the UI can fall back to the auto plan honestly.
    out["pfPlanApplied"] = pinned is not None
    if pf_warnings:
        # Load-time (unknown-ability) warnings; the planner's PF match/comp-
        # mismatch warnings already ride out["warnings"] via result.warnings.
        out["warnings"] = list(out.get("warnings") or []) + pf_warnings
    progress(100, "Done")
    return out


# ---------------------------------------------------------------------------
# FFLogs sign-in (OAuth PKCE — see fflogs_auth.py)
# ---------------------------------------------------------------------------

# Single active sign-in attempt; a new begin cancels the prior one. The UI
# drives begin → (browser round-trip) → poll loop → done/expired/error.
_auth_session: fflogs_auth.AuthSession | None = None
_auth_session_lock = threading.Lock()


def _auth_status() -> dict:
    tokens = fflogs_auth.AuthStore().load()
    if tokens is not None:
        return {"mode": "user", "userName": tokens.get("user_name", "")}
    cfg = load_config()
    if cfg.get("client_id") and cfg.get("client_secret"):
        return {"mode": "client_credentials"}
    return {"mode": "none"}


def get_auth_status(req: dict) -> dict:
    return _auth_status()


def fflogs_auth_begin(req: dict) -> dict:
    global _auth_session
    cid = fflogs_auth.public_client_id(load_config())
    if not cid:
        raise RuntimeError(
            "No FFLogs OAuth client id is configured in this build "
            "(fflogs_auth.FFLOGS_PUBLIC_CLIENT_ID, or config.json "
            "'oauth_client_id' for dev)."
        )
    with _auth_session_lock:
        if _auth_session is not None:
            _auth_session.cancel()
        _auth_session = fflogs_auth.AuthSession(cid)
        info = _auth_session.begin()
    return {
        "authorizeUrl": info.authorize_url,
        "port": info.port,
        "expiresInSec": fflogs_auth.LOGIN_TIMEOUT_S,
    }


def fflogs_auth_poll(req: dict) -> dict:
    with _auth_session_lock:
        session = _auth_session
    if session is None:
        return {"status": "error", "message": "no sign-in in progress"}
    status = session.status
    if status == "pending":
        return {"status": "pending"}
    if status == "done":
        # Rebuild the session client in user mode (idempotent — polling done
        # more than once just re-nulls an already-null singleton).
        _reset_session_client()
        event_log.log("info", "auth", "FFLogs sign-in completed")
        return {"status": "done", "userName": session.user_name}
    if status in ("expired", "cancelled"):
        return {"status": "expired"}
    return {"status": "error", "message": session.error or "sign-in failed"}


def fflogs_auth_cancel(req: dict) -> dict:
    global _auth_session
    with _auth_session_lock:
        if _auth_session is not None:
            _auth_session.cancel()
            _auth_session = None
    return {"cancelled": True}


def fflogs_logout(req: dict) -> dict:
    fflogs_auth.AuthStore().delete()
    _reset_session_client()
    event_log.log("info", "auth", "signed out")
    return _auth_status()


def list_user_characters(req: dict) -> dict:
    """Characters claimed on the signed-in FFLogs account — powers the
    character picker (replaces the old XIVAuth character list). Only
    meaningful in user mode; legacy client-credentials mode has no user,
    so the picker falls back to manual search on an empty list."""
    if fflogs_auth.AuthStore().load() is None:
        return {"characters": []}
    data = _client().query(
        """
        { userData { currentUser { characters {
            name
            lodestoneID
            server { name region { compactName } subregion { name } }
        } } } }
        """
    )
    cur = (data.get("userData") or {}).get("currentUser") or {}
    out = []
    for c in cur.get("characters") or []:
        srv = c.get("server") or {}
        lodestone = c.get("lodestoneID") or 0
        if not lodestone:
            continue
        out.append({
            "name": c.get("name", ""),
            "server": srv.get("name", ""),
            "region": ((srv.get("region") or {}).get("compactName") or "NA").upper(),
            "dataCenter": (srv.get("subregion") or {}).get("name") or "",
            "lodestoneId": int(lodestone),
        })
    if out:
        # Portraits come off the Lodestone (FFLogs exposes no images) —
        # parallel, disk-cached, best-effort (absent → initials chip).
        from lodestone import portrait_url
        with ThreadPoolExecutor(max_workers=4) as ex:
            urls = list(ex.map(lambda ch: portrait_url(ch["lodestoneId"]), out))
        for ch, url in zip(out, urls):
            if url:
                ch["avatarUrl"] = url
    return {"characters": out}


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------

HANDLERS = {
    "handshake":        lambda req, _id: {"ok": True, "data": handshake(req)},
    "lookup_character": lambda req, _id: {"ok": True, "data": lookup_character(req)},
    "list_encounters":  lambda req, _id: {"ok": True, "data": list_encounters(req)},
    "list_pulls":       lambda req, _id: {"ok": True, "data": list_pulls(req)},
    "list_prog_pulls":  lambda req, _id: {"ok": True, "data": list_prog_pulls(req)},
    "list_setup":       lambda req, _id: {"ok": True, "data": list_setup(req)},
    "list_rankings":    lambda req, _id: {"ok": True, "data": list_rankings(req)},
    "get_catalog":      lambda req, _id: {"ok": True, "data": get_catalog(req)},
    "cache_stats":      lambda req, _id: {"ok": True, "data": cache_stats(req)},
    "set_cache_cap":    lambda req, _id: {"ok": True, "data": set_cache_cap(req)},
    "clear_cache":      lambda req, _id: {"ok": True, "data": clear_cache(req)},
    "log_event":        lambda req, _id: {"ok": True, "data": log_event(req)},
    "get_recent_events": lambda req, _id: {"ok": True, "data": get_recent_events(req)},
    "export_feedback_bundle": lambda req, _id: {"ok": True, "data": export_feedback_bundle(req)},
    "prefetch_refs":    lambda req, _id: {"ok": True, "data": prefetch_refs(req, _id)},
    "run_analysis":     lambda req, _id: {"ok": True, "data": run_analysis(req, _id)},
    "theorize_kill_time": lambda req, _id: {"ok": True, "data": theorize_kill_time(req, _id)},
    "plan_mitigation":  lambda req, _id: {"ok": True, "data": plan_mitigation(req, _id)},
    "get_auth_status":    lambda req, _id: {"ok": True, "data": get_auth_status(req)},
    "fflogs_auth_begin":  lambda req, _id: {"ok": True, "data": fflogs_auth_begin(req)},
    "fflogs_auth_poll":   lambda req, _id: {"ok": True, "data": fflogs_auth_poll(req)},
    "fflogs_auth_cancel": lambda req, _id: {"ok": True, "data": fflogs_auth_cancel(req)},
    "fflogs_logout":      lambda req, _id: {"ok": True, "data": fflogs_logout(req)},
    "list_user_characters": lambda req, _id: {"ok": True, "data": list_user_characters(req)},
    "prepare_update":   lambda req, _id: {"ok": True, "data": prepare_update(req)},
}


def prepare_update(_req: dict) -> dict:
    """Drain the sim-pool worker processes before the app updates.

    The pool prestarts at launch, so several worker *processes* keep the onedir
    `_internal/*.dll` files mapped for the whole session. A bare kill of the main
    sidecar can't reach those grandchildren, and their parent-death watchdog
    fires asynchronously — so the NSIS updater would race them and fail every
    locked file ("error opening file for writing"). The frontend calls this right
    before `shutdownSidecar()` so the workers (and their handles) are gone first.
    Synchronous and best-effort; returns once the workers have been joined."""
    event_log.log("info", "lifecycle", "prepare_update: draining sim pool")
    try:
        from sidecar import sim_pool
        sim_pool.drain()
    except Exception:
        traceback.print_exc(file=sys.stderr)
    return {}


def handle(line: str) -> None:
    try:
        req = json.loads(line)
    except json.JSONDecodeError as e:
        event_log.log("warn", "protocol", "invalid JSON on stdin",
                      {"preview": line[:200]})
        _emit({"id": None, "ok": False, "error": f"invalid JSON: {e}"})
        return
    rid = req.get("id", "?")
    kind = req.get("kind", "?")
    handler = HANDLERS.get(kind)
    if not handler:
        event_log.log("warn", "protocol", f"unknown kind: {kind}")
        _emit({"id": rid, "ok": False, "error": f"unknown kind: {kind}"})
        return
    try:
        result = handler(req, rid)
        _emit({"id": rid, **result})
    except AuthExpiredError as e:
        # Typed so the UI can show a re-sign-in prompt instead of a generic
        # error banner (see errorCode in contract.ts).
        _emit({"id": rid, "ok": False, "error": str(e), "errorCode": "auth_expired"})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        # log_event is exempt: a failure logging a log would loop.
        if kind != "log_event":
            event_log.log("error", "sidecar_error", f"{type(e).__name__}: {e}",
                          {"kind": kind, "reqId": rid,
                           "traceback": traceback.format_exc()})
        _emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})


def _detach_stdin_for_spawn() -> None:
    """Re-plumb stdin so spawned pool workers never inherit the live NDJSON pipe.

    On Windows + CPython 3.14, `multiprocessing` spawn workers wedge in bootstrap
    (alive, zero CPU, tasks never picked up) when the parent's stdin is a PIPE that
    another thread is blocking-read on — exactly the sidecar's shape under Tauri or
    any driver (stdout piping is harmless; probed A/B/C matrix 2026-07-02). Keep
    reading the real stdin via a private dup'd fd, and hand fd 0 + the Win32
    STD_INPUT_HANDLE to NUL so child processes see a boring stdin. Best-effort: on
    any failure the pool's prestart canary still catches a wedge and degrades
    in-process (sim_pool.SimPool)."""
    try:
        dup_fd = os.dup(0)
        sys.stdin = os.fdopen(dup_fd, "r", encoding="utf-8", errors="replace")
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)
        if sys.platform == "win32":
            import ctypes
            import msvcrt
            ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))
    except Exception:
        traceback.print_exc(file=sys.stderr)


def main() -> None:
    # Detach the live stdin pipe from fd 0 BEFORE any pool work — spawn workers
    # inherit fd 0, and a live piped stdin wedges their bootstrap (see
    # _detach_stdin_for_spawn). The NDJSON loop below reads the dup'd handle.
    _detach_stdin_for_spawn()
    event_log.install_logging_bridge()
    event_log.log("info", "lifecycle", "sidecar started")
    # Install the process pool for the GIL-bound perfect-sim and PRESTART it from
    # this still-single-threaded moment: creating the executor lazily under the
    # mid-analysis thread fan-out can wedge worker spawn-bootstrap on Windows
    # (observed on CPython 3.14), so the pool is built + canary-health-checked on a
    # background thread now, off the handshake path (SIDECAR_SIM_WORKERS=0 disables
    # → fully in-process). Behind the scoring cache, so the ref-warm + the per-pull
    # sweeps fan out across cores with byte-identical output. Best-effort: any pool
    # failure degrades to in-process compute (see sim_pool.SimPool).
    try:
        from sidecar import sim_pool
        pool = sim_pool.install()
        if pool is not None:
            pool.prestart_async()
    except Exception:
        traceback.print_exc(file=sys.stderr)
    # Dispatch each request on its own daemon thread so a long-running
    # background warm (prefetch_refs across the whole tier matrix) never
    # blocks interactive requests (lookups, the blocking priority warm,
    # run_analysis). `_emit` is serialized by `_io_lock`; the shared caches
    # and the underlying FFLogsClient are all thread-safe.
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            threading.Thread(target=handle, args=(line,), daemon=True).start()
    finally:
        try:
            from sidecar import sim_pool
            sim_pool.shutdown()
        except Exception:
            pass


def _selftest_pool() -> int:
    """Offline check that the process pool runs the perfect-sim byte-identically — the
    one frozen-exe path that needs no network/credentials. Invoke the built exe with
    `--selftest-pool` to confirm Windows/PyInstaller spawn works (or cleanly falls back)
    before trusting it in the app. SAM @ 60s exercises the deterministic DP path.

    Compares the pool-dispatched RAW sim against the same in-process call —
    apples-to-apples. (The scoring-layer `perfect_sim_timeline` additionally
    re-places tincture pot markers, so comparing raw-vs-scoring-layer, as this
    selftest once did, fails structurally with a healthy pool.)"""
    from jobs.samurai import simulator as sam
    from sidecar import sim_pool

    ref = sam.simulate_idealized_perfect(60.0, [])
    pool = sim_pool.SimPool(max_workers=2)
    try:
        got = pool.run("jobs.samurai.simulator", "simulate_idealized_perfect",
                       (60.0, []), {})
    finally:
        used_pool = not pool._broken
        pool.shutdown()
    ok = (list(got[0]), got[1]) == (list(ref[0]), ref[1])
    print(f"selftest-pool: workers={pool.max_workers} casts={len(got[0])} "
          f"match={ok} used_real_pool={used_pool}")
    return 0 if ok else 1


if __name__ == "__main__":
    # Required so a spawned pool worker (Windows / frozen exe) re-runs the worker
    # bootstrap instead of main(). Must precede any pool creation.
    multiprocessing.freeze_support()
    if "--selftest-pool" in sys.argv:
        sys.exit(_selftest_pool())
    main()
