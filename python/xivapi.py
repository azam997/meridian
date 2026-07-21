"""Shared XIVAPI access — base URL + a pooled requests Session.

Both the ability-metadata lookup (`jobs/_core/ability_metadata.py`) and the
icon disk cache (`icon_cache.py`) fetch from XIVAPI. They share one Session
here so the ~20 icon downloads + N metadata lookups on a cold machine reuse a
single keep-alive connection instead of opening a fresh socket per request
(mirrors what `fflogs_api.py` already does for its GraphQL client).

The relative icon path XIVAPI returns looks like `/i/003000/003501.png`; the
full URL is `f"{BASE}{path}"`.
"""
from __future__ import annotations

import requests

BASE = "https://xivapi.com"

# Module-level so connection pooling persists across calls. Safe to share
# across worker threads — requests.Session is thread-safe for plain GETs.
SESSION = requests.Session()
