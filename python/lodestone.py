"""Lodestone character portraits.

The FFLogs API exposes no character images, but every character's public
Lodestone page carries a face/avatar image. One GET + one regex per
character, disk-cached under the config dir (same trust model as the XIVAPI
icon cache). Best-effort throughout: any failure yields None and the UI
falls back to its generated-initials chip.

Lodestone ids are global across regions, and the NA subdomain serves every
character's page, so one host suffices.
"""
from __future__ import annotations

import json
import re
import threading
import time

import requests

from config import CONFIG_DIR

PORTRAIT_CACHE_PATH = CONFIG_DIR / "portrait_cache.json"

# Appearance changes are rare; refresh weekly. Only successes are cached —
# a transient fetch failure retries on the next call instead of pinning a
# blank avatar for a week.
_TTL_S = 7 * 24 * 3600.0

_CHARACTER_URL = "https://na.finalfantasyxiv.com/lodestone/character/{lodestone_id}/"
_UA = "fflogs-efficiency-analyzer/desktop"

# The face thumbnail at the top of the character page:
#   <div class="frame__chara__face"><img src="https://img2.finalfantasyxiv.com/f/..._96x96.jpg" ...>
_FACE_RE = re.compile(
    r'class="frame__chara__face"[^>]*>\s*<img[^>]+src="([^"]+)"',
    re.S,
)

_lock = threading.Lock()
# lodestone_id (str) -> [fetched_at_epoch, url]
_cache: dict[str, list] | None = None


def _load_cache() -> dict[str, list]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(PORTRAIT_CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except Exception:
            _cache = {}
    return _cache


def _save_cache(cache: dict[str, list]) -> None:
    try:
        PORTRAIT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PORTRAIT_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def parse_portrait_url(page_html: str) -> str | None:
    """Extract the face-image URL from a Lodestone character page."""
    m = _FACE_RE.search(page_html)
    return m.group(1) if m else None


def portrait_url(lodestone_id: int) -> str | None:
    """Cached avatar URL for a character, or None. Thread-safe; the network
    fetch happens outside the lock so parallel lookups don't serialize."""
    key = str(lodestone_id)
    now = time.time()
    with _lock:
        cache = _load_cache()
        hit = cache.get(key)
        if hit and now - float(hit[0]) < _TTL_S:
            return hit[1] or None
    try:
        r = requests.get(
            _CHARACTER_URL.format(lodestone_id=lodestone_id),
            headers={"User-Agent": _UA},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        url = parse_portrait_url(r.text)
    except requests.RequestException:
        return None
    if not url:
        return None
    with _lock:
        cache = _load_cache()
        cache[key] = [now, url]
        _save_cache(cache)
    return url
