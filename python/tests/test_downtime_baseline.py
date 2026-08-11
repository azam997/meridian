"""Pin the legacy detect_downtime_windows output (Tier-C fallback only).

The legacy cast-gap heuristic is no longer the primary downtime source —
Tier A (`targetabilityupdate` events) and Tier B (consensus from refs)
are. It's kept as the last-ditch fallback when Tier A fails (network
error) and the ref pool is too small for Tier B. This file pins its
output across every fixture so we'd notice if a refactor silently
changes the fallback's behavior.

Threshold was raised from 5.0s to 8.0s as part of the rewrite — at the
old 5.0s the heuristic was relabeling single-GCD drops as downtime and
pardoning real clips. 8.0s only catches gaps that are clearly structural
(multiple lost GCDs in a row). On the MCH fixture set this means zero
fallback windows, because top-quartile players don't drop that many
GCDs and bottom-quartile players don't either — the original 5s windows
were artifacts of the heuristic itself, not real downtime.

Run from python/:  python tests/test_downtime_baseline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime import detect_downtime_windows


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _norm_casts(fix: dict) -> list[tuple[float, int]]:
    start = fix["fight_start_ms"]
    out: list[tuple[float, int]] = []
    for ev in fix["cast_events"]:
        if ev.get("type") != "cast":
            continue
        aid = ev.get("abilityGameID")
        if not aid:
            continue
        out.append(((ev["timestamp"] - start) / 1000.0, aid))
    out.sort(key=lambda t: t[0])
    return out


# Locked baseline: legacy 5s cast-gap heuristic output as of pre-rewrite.
# Each entry: fixture_name -> list of (start_s, end_s) windows. Generated
# by running the helper at the bottom of this file once.
LOCKED: dict[str, list[tuple[float, float]]] = {
    "botq_1":  [],
    "botq_2":  [],
    "q2_1":    [],
    "q2_2":    [],
    "q3_1":    [],
    "q3_2":    [],
    "topq_1":  [],
    "topq_2":  [],
    "user_tyrant_recent": [],
}


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


def test_legacy_heuristic_locked() -> None:
    print()
    print("Test: legacy detect_downtime_windows pinned per fixture")
    for name, expected in LOCKED.items():
        path = FIXTURES_DIR / f"{name}.json"
        if not path.exists():
            _check(f"{name}: fixture present", False, f"missing {path}")
            continue
        fix = json.loads(path.read_text(encoding="utf-8"))
        norm = _norm_casts(fix)
        dur = (fix["fight_end_ms"] - fix["fight_start_ms"]) / 1000.0
        got = detect_downtime_windows(norm, dur)
        got_r = [(round(s, 1), round(e, 1)) for s, e in got]
        _check(f"{name}: legacy windows match locked baseline",
               got_r == expected,
               f"got {got_r}  expected {expected}")


def test_role_tuned_threshold() -> None:
    """detect_downtime_windows accepts a per-call threshold so the
    role-tuned Tier-C fallback in resolve_downtime can pass through the
    policy's value without the default biting. Uses casts spaced to
    isolate one specific 10s gap from any trailing-to-fight-end gap."""
    print()
    print("Test: detect_downtime_windows respects threshold_s arg")
    # Casts every 2.5s except a 10s gap between t=10 and t=20.
    norm = [(0.0, 7411), (2.5, 7411), (5.0, 7411), (7.5, 7411), (10.0, 7411),
            (20.0, 7411), (22.5, 7411), (25.0, 7411), (27.5, 7411)]
    out_default = detect_downtime_windows(norm, 30.0)
    out_strict = detect_downtime_windows(norm, 30.0, threshold_s=12.0)
    _check("default (8s) catches the 10s gap",
           any(s == 10.0 and e == 20.0 for s, e in out_default),
           f"got {out_default}")
    _check("12s threshold rejects the 10s gap",
           not any(s == 10.0 and e == 20.0 for s, e in out_strict),
           f"got {out_strict}")


def main() -> int:
    # Optional helper: if asked, print the live values so it's easy to
    # update LOCKED after a deliberate change.
    if "--print" in sys.argv:
        for path in sorted(FIXTURES_DIR.glob("*.json")):
            if "/" in path.stem:  # skip nested
                continue
            try:
                fix = json.loads(path.read_text(encoding="utf-8"))
                if "cast_events" not in fix:
                    continue
                norm = _norm_casts(fix)
                dur = (fix["fight_end_ms"] - fix["fight_start_ms"]) / 1000.0
                windows = detect_downtime_windows(norm, dur)
                rounded = [(round(s, 1), round(e, 1)) for s, e in windows]
                print(f'    "{path.stem}": {rounded},')
            except Exception as e:
                print(f'    # {path.stem}: skipped ({e})')
        return 0

    test_legacy_heuristic_locked()
    test_role_tuned_threshold()
    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
