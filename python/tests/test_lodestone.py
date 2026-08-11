"""Tests for lodestone.py — portrait URL parsing + disk cache.

Hermetic: the page HTML is a fixture snippet; network fetch is stubbed.

Run from python/:  python tests/test_lodestone.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lodestone

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


# Trimmed from a real Lodestone character page (2026-07): the face image the
# parser targets, amid sibling markup that must not confuse it.
_PAGE = """
<div class="frame__chara__link">
  <div class="frame__chara__face"><img
    src="https://img2.finalfantasyxiv.com/f/abc123_def456fc0.jpg?1784158088"
    alt="Sample Character"></div>
  <div class="frame__chara__box">
    <p class="frame__chara__name">Sample Character</p>
  </div>
</div>
<img src="https://img2.finalfantasyxiv.com/f/unrelated_banner.jpg">
"""


def test_parse() -> None:
    print()
    print("Test: face-image parse")
    url = lodestone.parse_portrait_url(_PAGE)
    _check("extracts the face img src",
           url == "https://img2.finalfantasyxiv.com/f/abc123_def456fc0.jpg?1784158088",
           str(url))
    _check("no match on unrelated markup",
           lodestone.parse_portrait_url("<div><img src='x.jpg'></div>") is None)


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def test_cache_roundtrip() -> None:
    print()
    print("Test: portrait_url fetch + disk cache + TTL")
    saved_path = lodestone.PORTRAIT_CACHE_PATH
    saved_get = lodestone.requests.get
    saved_mem = lodestone._cache
    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(200, _PAGE)

    with tempfile.TemporaryDirectory() as scratch:
        try:
            lodestone.PORTRAIT_CACHE_PATH = Path(scratch) / "portrait_cache.json"
            lodestone._cache = None
            lodestone.requests.get = fake_get

            url1 = lodestone.portrait_url(12345678)
            _check("first call fetches and parses",
                   bool(url1) and url1.startswith("https://img2."), str(url1))
            _check("one network call", len(calls) == 1, str(len(calls)))

            url2 = lodestone.portrait_url(12345678)
            _check("second call served from cache",
                   url2 == url1 and len(calls) == 1, f"{url2} calls={len(calls)}")
            _check("cache persisted to disk",
                   lodestone.PORTRAIT_CACHE_PATH.exists())

            # Cold process (memory dropped) still hits the disk cache.
            lodestone._cache = None
            url3 = lodestone.portrait_url(12345678)
            _check("disk cache survives a reload",
                   url3 == url1 and len(calls) == 1, str(len(calls)))

            # Expired entry refetches.
            lodestone._cache = None
            cache = lodestone._load_cache()
            cache["12345678"][0] = time.time() - lodestone._TTL_S - 1
            lodestone._save_cache(cache)
            url4 = lodestone.portrait_url(12345678)
            _check("expired entry refetches",
                   url4 == url1 and len(calls) == 2, str(len(calls)))

            # A failed fetch is not cached (no blank avatar pinned for a week).
            lodestone.requests.get = lambda *a, **k: _FakeResponse(404, "")
            _check("404 yields None", lodestone.portrait_url(999) is None)
            lodestone.requests.get = fake_get
            _check("failure was not cached (retry succeeds)",
                   lodestone.portrait_url(999) == url1)
        finally:
            lodestone.PORTRAIT_CACHE_PATH = saved_path
            lodestone.requests.get = saved_get
            lodestone._cache = saved_mem


def main() -> int:
    test_parse()
    test_cache_roundtrip()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
