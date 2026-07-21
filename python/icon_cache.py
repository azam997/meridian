"""Disk cache for FFXIV action icons (XIVAPI source).

Icons are downloaded once per user and cached at
`~/.fflogs_efficiency_analyzer/icons/`. Subsequent runs hit the local
cache only. Each job declares its own ability metadata map; this module
is job-agnostic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from config import CONFIG_DIR, ensure_config_dir_migrated
from xivapi import BASE as _XIVAPI_BASE, SESSION as _SESSION

log = logging.getLogger(__name__)

# Run the rename before computing the cache dir so an existing
# ~/.fflogs_mch_compare/icons/ carries over.
ensure_config_dir_migrated()
_CACHE_DIR = CONFIG_DIR / "icons"


class IconCache:
    """Synchronous icon cache. Safe to call from worker threads (HTTP only)."""

    def __init__(self, dir: Path = _CACHE_DIR):
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def ensure_local(self, icon_url_path: str) -> Optional[Path]:
        """Download to disk if missing. Returns the local path or None on failure."""
        if not icon_url_path:
            return None
        local = self._local_path(icon_url_path)
        if local.exists():
            return local
        try:
            url = f"{_XIVAPI_BASE}{icon_url_path}"
            r = _SESSION.get(url, timeout=12)
            r.raise_for_status()
            tmp = local.with_suffix(local.suffix + ".part")
            tmp.write_bytes(r.content)
            tmp.replace(local)
            return local
        except Exception as e:
            log.warning("icon download failed: %s -> %s", icon_url_path, e)
            return None

    def warm(self, icon_paths: Iterable[str]) -> None:
        """Pre-download a batch of icons. Safe to call from a worker thread."""
        for p in icon_paths:
            self.ensure_local(p)

    def _local_path(self, icon_url_path: str) -> Path:
        # Flatten the XIVAPI path /i/003000/003501.png -> 003000_003501.png
        flat = icon_url_path.lstrip("/").replace("/", "_")
        if flat.startswith("i_"):
            flat = flat[2:]
        return self.dir / flat


# Module-level singleton — simpler than passing the cache around.
ICONS = IconCache()
