"""Tests for sidecar/event_log.py — the compact self-cleaning NDJSON log.

Covers:
  - line shape (epoch-ms t, level coercion, data omitted when empty)
  - msg / data truncation caps
  - size-based rotation into gzipped segments + prune to _MAX_SEGMENTS
  - recent_events tail order, bad-line skipping, newest-segment fallback
  - OSError swallowing (an unwritable log dir must never raise)
  - concurrency smoke (parallel writers, every line parses)

Run from python/:  python tests/test_event_log.py
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sidecar import event_log


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


def _with_scratch(test, max_active: int | None = None,
                  max_segments: int | None = None):
    """Run `test(scratch_dir)` with the event log pointed at a temp dir
    (and optionally shrunk caps); restore everything on exit."""
    saved_dir = event_log._log_dir
    saved_active = event_log._active
    saved_size = event_log._approx_size
    saved_max = event_log._MAX_ACTIVE_BYTES
    saved_segs = event_log._MAX_SEGMENTS
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        event_log._set_dir_for_tests(scratch_path / "logs")
        if max_active is not None:
            event_log._MAX_ACTIVE_BYTES = max_active
        if max_segments is not None:
            event_log._MAX_SEGMENTS = max_segments
        try:
            test(scratch_path)
        finally:
            event_log._log_dir = saved_dir
            event_log._active = saved_active
            event_log._approx_size = saved_size
            event_log._MAX_ACTIVE_BYTES = saved_max
            event_log._MAX_SEGMENTS = saved_segs


def _read_active_events() -> list[dict]:
    text = event_log._active.read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines()]


def test_line_shape() -> None:
    print()
    print("Test: event line shape")

    def body(_scratch: Path) -> None:
        before = int(time.time() * 1000)
        event_log.log("warn", "unit", "hello world")
        event_log.log("error", "unit", "with data", {"a": 1, "b": "x"})
        event_log.log("bogus-level", "unit", "coerced")
        after = int(time.time() * 1000)

        events = _read_active_events()
        _check("three lines written", len(events) == 3, f"got {len(events)}")

        first = events[0]
        _check("t is an epoch-ms int in range",
               isinstance(first["t"], int) and before <= first["t"] <= after,
               f"t={first.get('t')} window=[{before},{after}]")
        _check("lv/cat/msg round-trip",
               first["lv"] == "warn" and first["cat"] == "unit"
               and first["msg"] == "hello world")
        _check("data omitted when empty", "data" not in first)
        _check("data present when given",
               events[1]["data"] == {"a": 1, "b": "x"},
               f"got {events[1].get('data')}")
        _check("unknown level coerced to info",
               events[2]["lv"] == "info", f"got {events[2]['lv']}")

    _with_scratch(body)


def test_truncation() -> None:
    print()
    print("Test: msg and data caps")

    def body(_scratch: Path) -> None:
        event_log.log("info", "unit", "m" * 2000)
        event_log.log("info", "unit", "big string",
                      {"traceback": "x" * 50_000, "kind": "run_analysis"})
        event_log.log("info", "unit", "big dict",
                      {f"k{i}": "v" * 200 for i in range(200)})

        events = _read_active_events()
        _check("msg capped at _MSG_CAP",
               len(events[0]["msg"]) == event_log._MSG_CAP,
               f"len={len(events[0]['msg'])}")
        tb = events[1]["data"]["traceback"]
        _check("long string in data capped near _STR_CAP",
               len(tb) <= event_log._STR_CAP + 1, f"len={len(tb)}")
        _check("short sibling key survives the cap",
               events[1]["data"]["kind"] == "run_analysis")
        _check("oversized dict collapses to truncated preview",
               events[2]["data"].get("truncated") is True
               and "preview" in events[2]["data"],
               f"got keys {sorted(events[2]['data'])}")

    _with_scratch(body)


def test_rotation_and_prune() -> None:
    print()
    print("Test: rotation into .gz segments + prune")

    def body(_scratch: Path) -> None:
        for i in range(60):
            event_log.log("info", "unit", f"event number {i:03d}",
                          {"pad": "p" * 40})

        segments = sorted(event_log._log_dir.glob("events-*.ndjson.gz"))
        _check("rotation produced gz segments", len(segments) >= 1,
               f"got {len(segments)}")
        _check("segments pruned to _MAX_SEGMENTS",
               len(segments) <= event_log._MAX_SEGMENTS,
               f"got {len(segments)} > {event_log._MAX_SEGMENTS}")
        _check("active file exists and is under cap + one line",
               event_log._active.exists()
               and event_log._active.stat().st_size
               <= event_log._MAX_ACTIVE_BYTES + 200,
               f"size={event_log._active.stat().st_size}")

        # Every surviving line (segments + active) still parses, and the
        # newest events are the last ones written.
        lines: list[str] = []
        for seg in segments:
            lines += gzip.decompress(seg.read_bytes()).decode().splitlines()
        lines += event_log._active.read_text().splitlines()
        parsed = [json.loads(ln) for ln in lines]
        _check("all surviving lines parse", len(parsed) == len(lines))
        _check("newest event survived",
               parsed[-1]["msg"] == "event number 059",
               f"got {parsed[-1]['msg']}")

        _check("log_paths lists active first, newest-first overall",
               event_log.log_paths()[0] == event_log._active
               and len(event_log.log_paths()) == 1 + len(segments))

    _with_scratch(body, max_active=400, max_segments=2)


def test_recent_events() -> None:
    print()
    print("Test: recent_events order, bad lines, segment fallback")

    def body(_scratch: Path) -> None:
        for i in range(10):
            event_log.log("info", "unit", f"ev {i}")
        # Corrupt line in the middle must be skipped, not fatal.
        with open(event_log._active, "ab") as f:
            f.write(b"{not json}\n")
        event_log.log("info", "unit", "ev last")

        got = event_log.recent_events(5)
        _check("returns the last N in log order",
               [e["msg"] for e in got]
               == ["ev 7", "ev 8", "ev 9", "ev last"],
               f"got {[e['msg'] for e in got]}")

        got_all = event_log.recent_events(100)
        _check("bad line skipped, others intact", len(got_all) == 11,
               f"got {len(got_all)}")

    _with_scratch(body)

    def body_gz(_scratch: Path) -> None:
        for i in range(40):
            event_log.log("info", "unit", f"gz ev {i:02d}", {"pad": "p" * 40})
        active_count = len(_read_active_events())
        got = event_log.recent_events(active_count + 5)
        _check("segment fallback tops up past the active file",
               len(got) > active_count,
               f"active={active_count} got={len(got)}")
        _check("fallback preserves chronological order",
               got[-1]["msg"] == "gz ev 39"
               and [e["msg"] for e in got] == sorted([e["msg"] for e in got]),
               f"tail={[e['msg'] for e in got[-3:]]}")

    _with_scratch(body_gz, max_active=400, max_segments=3)


def test_oserror_swallowed() -> None:
    print()
    print("Test: unwritable log dir never raises")

    def body(scratch: Path) -> None:
        # Point the log "dir" at an existing FILE: mkdir/open/read all fail.
        blocker = scratch / "blocker"
        blocker.write_text("i am a file")
        event_log._set_dir_for_tests(blocker)

        event_log.log("error", "unit", "into the void", {"x": 1})  # must not raise
        _check("log() swallowed the failure", True)
        _check("recent_events returns [] on failure",
               event_log.recent_events() == [])
        _check("log_paths returns [] on failure",
               event_log.log_paths() == [])

    _with_scratch(body)


def test_concurrency_smoke() -> None:
    print()
    print("Test: parallel writers, every line parses")

    def body(_scratch: Path) -> None:
        n_threads, n_events = 8, 200

        def writer(tid: int) -> None:
            for i in range(n_events):
                event_log.log("info", "unit", f"t{tid} e{i}")

        threads = [threading.Thread(target=writer, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = _read_active_events()  # raises on any corrupt line
        _check("all events landed intact",
               len(events) == n_threads * n_events,
               f"got {len(events)}")

    _with_scratch(body)


def main() -> int:
    test_line_shape()
    test_truncation()
    test_rotation_and_prune()
    test_recent_events()
    test_oserror_swallowed()
    test_concurrency_smoke()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
