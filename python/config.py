"""Credential/config JSON at ~/.fflogs_efficiency_analyzer/config.json.

The first time the sidecar starts after the 0.4-era rename, this module
also migrates an existing ~/.fflogs_mch_compare/ directory to the new
path so previously-saved API credentials carry over without user action.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Pre-rename location. Kept here (not as a constant elsewhere) because
# only the migration helper needs to know about it.
_LEGACY_DIR = Path.home() / ".fflogs_mch_compare"

CONFIG_DIR = Path.home() / ".fflogs_efficiency_analyzer"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Persisted FFLogs user sign-in (OAuth PKCE tokens) — written/read by
# fflogs_auth.AuthStore, never by this module. Lives beside config.json at
# the same trust level: the user's own token on the user's own machine.
AUTH_PATH = CONFIG_DIR / "auth.json"

# Dev-only on-disk cache of raw FFLogs responses. Lives outside the project
# tree (under the user config dir) and is only consulted when `is_dev` is
# truthy in config.json — see sidecar/dev_cache.py. Versioned so a shape
# change can be busted by bumping the suffix; safe to delete wholesale.
DEV_CACHE_DIR = CONFIG_DIR / "dev_cache" / "v1"

# Production on-disk cache of raw FFLogs responses (separate dir from the dev
# one so the two never collide). Always consulted on the prod path, with a
# short TTL on the mutable rankings query and a size cap — see
# sidecar/main.py::_client. Versioned; safe to delete wholesale to bust.
CACHE_DIR = CONFIG_DIR / "cache" / "v1"

# Self-cleaning in-app event log (edge cases, errors, >100% anomalies) — see
# sidecar/event_log.py. Size-rotated, so safe to delete wholesale.
LOG_DIR = CONFIG_DIR / "logs"

# Exported diagnostics bundles (Submit Feedback). Pruned to the newest few by
# sidecar/main.py::export_feedback_bundle; safe to delete wholesale.
FEEDBACK_DIR = CONFIG_DIR / "feedback"


def ensure_config_dir_migrated() -> None:
    """If the new dir is missing AND the legacy dir exists, rename it.
    Idempotent: subsequent calls do nothing once the new dir exists.

    Failure is non-fatal — we log and leave the user to copy credentials
    manually via SettingsView if the rename trips on permissions.
    """
    if CONFIG_DIR.exists():
        return
    if not _LEGACY_DIR.exists():
        return
    try:
        shutil.move(str(_LEGACY_DIR), str(CONFIG_DIR))
        log.info("Migrated config dir %s -> %s", _LEGACY_DIR, CONFIG_DIR)
    except Exception:
        log.exception("Config dir migration failed; old data remains at %s",
                      _LEGACY_DIR)


def load_config() -> dict:
    ensure_config_dir_migrated()
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    ensure_config_dir_migrated()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
