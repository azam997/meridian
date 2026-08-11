"""Shared pytest configuration for the analyzer test suite.

Two jobs:

1. Put the `python/` package root on sys.path so `from jobs import ...` and
   `from sidecar.main import ...` resolve no matter where pytest is invoked.
2. Make every test hermetic: stub `ability_metadata._fetch_from_xivapi` so a
   test can never make a live HTTP call to XIVAPI. Fixtures reference a few
   fabricated FFLogs ability IDs that aren't in the bundled map or disk cache;
   without this stub each one triggers a real (slow, flaky) network request
   from the hot scoring path. That single un-stubbed dependency was ~90% of
   the suite's wall-clock. Tests must resolve unknown IDs to None instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# python/ — the package root (parent of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs._core.ability_metadata as ability_metadata  # noqa: E402

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> dict[str, dict]:
    """All recorded pull fixtures, keyed by file stem. Matches the dict each
    test file's own `_load_fixtures()` builds, so test functions that take a
    `fixtures` parameter resolve to it under pytest. sam/ lives in a subdir
    and is intentionally excluded (the top-level glob doesn't recurse)."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_FIXTURES_DIR.glob("*.json"))
    }


@pytest.fixture(autouse=True)
def _offline_ability_metadata(monkeypatch):
    """Tests never hit XIVAPI. Unknown ability IDs resolve to None (the same
    behavior a real run gets once the negative cache records the miss)."""
    monkeypatch.setattr(
        ability_metadata, "_fetch_from_xivapi", lambda ability_id: None
    )
    # Drop any negative-cache entries a prior test recorded so each test
    # starts from a clean lookup state.
    ability_metadata._negative_cache.clear()
