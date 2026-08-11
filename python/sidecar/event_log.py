"""Compact, self-cleaning NDJSON event log for the sidecar.

One line per event: `{"t": <epoch ms>, "lv": "info|warn|error", "cat": ...,
"msg": ..., "data": {...}?}` — `data` omitted when empty. The active file is
LOG_DIR/events.ndjson; once it exceeds ~1 MB it is gzipped into a timestamped
`events-<epoch_ms>.ndjson.gz` segment and recreated, keeping only the newest
few segments (~1.5 MB worst-case footprint). Everything here swallows its own
failures — logging must never break a request.

Consumers: main.py wires request errors / breadcrumbs / ceiling anomalies in,
the `log_event` request forwards frontend events, and `export_feedback_bundle`
zips `log_paths()` into the user-submitted diagnostics bundle.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from config import LOG_DIR

_LOCK = threading.Lock()

# Module-level (not constants) so tests can repoint via _set_dir_for_tests.
_log_dir: Path = LOG_DIR
_active: Path = LOG_DIR / "events.ndjson"

_MAX_ACTIVE_BYTES = 1_000_000   # rotate the active file past ~1 MB
_MAX_SEGMENTS = 4               # keep this many rotated .gz segments
_MSG_CAP = 500                  # chars per event message
_STR_CAP = 8192                 # chars per string inside data (tracebacks)
_DATA_CAP = 10240               # bytes of serialized data per event — above
                                # _STR_CAP so one capped traceback + small
                                # context keys survives without collapsing

_LEVELS = ("info", "warn", "error")

# Running size of the active file, seeded from os.stat on first write so a
# restart resumes the rotation clock without re-scanning per append.
_approx_size: int | None = None

# Session context (e.g. the app version from the UI handshake) — stamped into
# the export bundle's environment.json, not onto every line (keeps lines small).
_context: dict[str, str] = {}


def log(level: str, cat: str, msg: str, data: dict | None = None) -> None:
    """Append one event. Never raises; never imports/uses `logging` (the
    bridge handler below calls in here, so that would recurse)."""
    try:
        event: dict = {
            "t": int(time.time() * 1000),
            "lv": level if level in _LEVELS else "info",
            "cat": str(cat),
            "msg": str(msg)[:_MSG_CAP],
        }
        if data:
            event["data"] = _capped_data(data)
        _append(json.dumps(event, separators=(",", ":"), default=str))
    except Exception:
        pass


def set_context(app_version: str | None = None) -> None:
    try:
        if app_version:
            _context["appVersion"] = str(app_version)
    except Exception:
        pass


def get_context() -> dict[str, str]:
    return dict(_context)


def recent_events(limit: int = 100) -> list[dict]:
    """The last `limit` events, oldest first (log order). Reads the active
    file (≤ ~1 MB by construction) and, if that comes up short, the newest
    rotated segment. Unparseable lines are skipped."""
    try:
        limit = max(1, int(limit))
        with _LOCK:
            lines = _read_lines_locked(limit)
        events: list[dict] = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if isinstance(ev, dict):
                events.append(ev)
        return events[-limit:]
    except Exception:
        return []


def log_paths() -> list[Path]:
    """Existing log files, newest first (active file, then rotated segments) —
    the set the feedback bundle zips up."""
    paths: list[Path] = []
    try:
        if _active.exists():
            paths.append(_active)
        paths.extend(sorted(_log_dir.glob("events-*.ndjson.gz"), reverse=True))
    except OSError:
        pass
    return paths


class _BridgeHandler(logging.Handler):
    """Routes stdlib-logging WARNING+ records (config.py migration failures,
    ability_metadata/icon_cache fetch warnings) into the event log."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            lv = "error" if record.levelno >= logging.ERROR else "warn"
            data: dict | None = None
            if record.exc_info and record.exc_info[0] is not None:
                data = {"traceback":
                        "".join(traceback.format_exception(*record.exc_info))}
            log(lv, record.name, record.getMessage(), data)
        except Exception:
            pass


def install_logging_bridge() -> None:
    """Attach the bridge to the root logger. Also adds an explicit stderr
    handler: attaching ANY root handler disables `logging.lastResort` (the
    WARNING+→stderr fallback), and stderr is what surfaces as the dev
    console's `[sidecar]` lines — keep that visible. Idempotent."""
    try:
        root = logging.getLogger()
        if any(isinstance(h, _BridgeHandler) for h in root.handlers):
            return
        bridge = _BridgeHandler(level=logging.WARNING)
        root.addHandler(bridge)
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setLevel(logging.WARNING)
        root.addHandler(stderr)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _capped_data(data: dict) -> dict:
    """Bound the per-event payload: long strings (tracebacks) are cut to
    _STR_CAP, and if the whole serialized dict still exceeds _DATA_CAP it
    collapses to a truncated preview."""
    capped: dict = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > _STR_CAP:
            v = v[:_STR_CAP] + "…"
        capped[str(k)] = v
    payload = json.dumps(capped, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) > _DATA_CAP:
        return {"truncated": True, "preview": payload[:_DATA_CAP]}
    return capped


def _append(line: str) -> None:
    global _approx_size
    encoded = (line + "\n").encode("utf-8")
    with _LOCK:
        try:
            if _approx_size is None:
                try:
                    _approx_size = _active.stat().st_size
                except OSError:
                    _approx_size = 0
            if _approx_size + len(encoded) > _MAX_ACTIVE_BYTES:
                _rotate_locked()
            _log_dir.mkdir(parents=True, exist_ok=True)
            with open(_active, "ab") as f:
                f.write(encoded)
            _approx_size = (_approx_size or 0) + len(encoded)
        except Exception:
            pass


def _rotate_locked() -> None:
    """Gzip the active file into a timestamped segment (tmp + os.replace, the
    dev_cache idiom), reset the active file, prune old segments. Caller holds
    _LOCK. Epoch-ms names sort lexically == chronologically."""
    global _approx_size
    try:
        raw = _active.read_bytes()
    except OSError:
        _approx_size = 0
        return
    ts = int(time.time() * 1000)
    dest = _log_dir / f"events-{ts}.ndjson.gz"
    while dest.exists():  # two rotations in one ms would silently overwrite
        ts += 1
        dest = _log_dir / f"events-{ts}.ndjson.gz"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_bytes(gzip.compress(raw, compresslevel=6))
        os.replace(tmp, dest)
        try:
            _active.unlink()
        except OSError:
            # Couldn't remove (e.g. a concurrent reader on Windows) — truncate
            # in place so rotation doesn't re-archive the same content.
            with open(_active, "wb"):
                pass
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
    _approx_size = 0
    try:
        segments = sorted(_log_dir.glob("events-*.ndjson.gz"))
        for old in segments[:-_MAX_SEGMENTS]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _read_lines_locked(limit: int) -> list[str]:
    lines: list[str] = []
    try:
        lines = _active.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
    except OSError:
        lines = []
    if len(lines) < limit:
        try:
            segments = sorted(_log_dir.glob("events-*.ndjson.gz"), reverse=True)
        except OSError:
            segments = []
        if segments:
            try:
                older = gzip.decompress(segments[0].read_bytes()) \
                    .decode("utf-8", errors="replace").splitlines()
                lines = older[-(limit - len(lines)):] + lines
            except (OSError, gzip.BadGzipFile, EOFError):
                pass
    return lines[-limit:]


def _set_dir_for_tests(path: Path) -> None:
    """Repoint the log at a temp dir and reset the size counter (tests only)."""
    global _log_dir, _active, _approx_size
    _log_dir = Path(path)
    _active = _log_dir / "events.ndjson"
    _approx_size = None
