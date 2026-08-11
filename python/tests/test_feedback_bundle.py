"""Tests for the diagnostics request kinds in sidecar/main.py:
log_event / get_recent_events / export_feedback_bundle.

The bundle is the user-submitted half of crash reporting: a zip of the event
log + environment + analysis context that the user attaches to a prefilled
GitHub issue. It must never contain credentials (auth.json), must prune
itself to the newest few zips, and the issue body must stay under the
URL-length cap and tell the user to attach the zip.

Run from python/:  python tests/test_feedback_bundle.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sidecar import event_log
from sidecar import main as sidecar_main


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


def _with_scratch(test):
    """Point both the event log and FEEDBACK_DIR at a temp scratch space."""
    saved_dir = event_log._log_dir
    saved_active = event_log._active
    saved_size = event_log._approx_size
    saved_feedback = sidecar_main.FEEDBACK_DIR
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        event_log._set_dir_for_tests(scratch_path / "logs")
        sidecar_main.FEEDBACK_DIR = scratch_path / "feedback"
        try:
            test(scratch_path)
        finally:
            event_log._log_dir = saved_dir
            event_log._active = saved_active
            event_log._approx_size = saved_size
            sidecar_main.FEEDBACK_DIR = saved_feedback


def test_log_event_handler() -> None:
    print()
    print("Test: log_event lands with the ui. prefix, level coerced")

    def body(_scratch: Path) -> None:
        out = sidecar_main.log_event(
            {"level": "error", "cat": "sidecar", "msg": "boom",
             "data": {"stack": "trace"}})
        _check("handler returns {}", out == {})
        sidecar_main.log_event({"level": "hax", "cat": "x", "msg": "coerced",
                                "data": "not-a-dict"})

        events = event_log.recent_events()
        _check("two events written", len(events) == 2, f"got {len(events)}")
        _check("ui. prefix + payload intact",
               events[0]["cat"] == "ui.sidecar" and events[0]["lv"] == "error"
               and events[0]["msg"] == "boom"
               and events[0]["data"] == {"stack": "trace"},
               f"got {events[0]}")
        _check("bogus level coerced, non-dict data dropped",
               events[1]["lv"] == "info" and "data" not in events[1],
               f"got {events[1]}")

    _with_scratch(body)


def test_get_recent_events_roundtrip() -> None:
    print()
    print("Test: get_recent_events round-trip + limit clamp")

    def body(_scratch: Path) -> None:
        for i in range(20):
            event_log.log("info", "unit", f"ev {i}")

        got = sidecar_main.get_recent_events({"limit": 5})["events"]
        _check("limit respected, log order",
               [e["msg"] for e in got] == [f"ev {i}" for i in range(15, 20)],
               f"got {[e['msg'] for e in got]}")
        _check("garbled limit falls back",
               len(sidecar_main.get_recent_events({"limit": "x"})["events"])
               == 20)

    _with_scratch(body)


def test_bundle_contents() -> None:
    print()
    print("Test: bundle zip contents (logs + env + context, no auth.json)")

    def body(_scratch: Path) -> None:
        event_log.set_context(app_version="0.6.0-test")
        event_log.log("warn", "ceiling_anomaly", "over", {"job": "Samurai"})
        event_log.log("info", "unit", "plain event")

        res = sidecar_main.export_feedback_bundle({
            "category": "anomaly",
            "description": "It went over 100%",
            "analysisContext": {
                "job": "Samurai", "encounterName": "Some Boss",
                "reportCode": "AbCd1234", "fightId": 7,
                "efficiencyPct": 100.42, "efficiencyPctLenient": 100.61,
                "ceilingAnomaly": {"job": "Samurai",
                                   "encounterName": "Some Boss",
                                   "maxEffPct": 100.61},
            },
        })

        zip_path = Path(res["path"])
        _check("zip written where reported", zip_path.exists(),
               str(zip_path))
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            _check("event log inside under logs/",
                   "logs/events.ndjson" in names, f"got {names}")
            _check("environment.json + context.json inside",
                   "environment.json" in names and "context.json" in names)
            _check("no auth.json / credentials member",
                   not any("auth" in n or "config.json" in n for n in names),
                   f"got {names}")
            env = json.loads(zf.read("environment.json"))
            _check("environment carries the handshake app version",
                   env["appVersion"] == "0.6.0-test"
                   and env["protocolVersion"] >= 1)
            ctx = json.loads(zf.read("context.json"))
            _check("context carries category/description/analysisContext",
                   ctx["category"] == "anomaly"
                   and ctx["description"] == "It went over 100%"
                   and ctx["analysisContext"]["reportCode"] == "AbCd1234")
            _check("recent ceiling anomalies filtered in",
                   len(ctx["recentCeilingAnomalies"]) == 1
                   and ctx["recentCeilingAnomalies"][0]["data"]["job"]
                   == "Samurai")

        _check("anomaly title carries job/encounter/max%",
               res["issueTitle"]
               == "[Anomaly] Samurai · Some Boss — efficiency 100.61%",
               f"got {res['issueTitle']}")
        body_text = res["issueBody"]
        _check("body under the URL cap",
               len(body_text) <= sidecar_main._ISSUE_BODY_CAP,
               f"len={len(body_text)}")
        _check("body names the zip path + attach instruction",
               str(zip_path) in body_text
               and "attach that zip" in body_text)
        _check("body carries the run context",
               "AbCd1234#7" in body_text
               and "100.42%" in body_text and "Samurai" in body_text,
               body_text)

    _with_scratch(body)


def test_title_variants_and_caps() -> None:
    print()
    print("Test: bug/feedback titles, long description capped")

    def body(_scratch: Path) -> None:
        res = sidecar_main.export_feedback_bundle({
            "category": "bug",
            "description": "Crash when I  click\nrun twice " + "x" * 200,
        })
        _check("bug title is [Bug] + collapsed desc[:60]",
               res["issueTitle"].startswith("[Bug] Crash when I click run "
                                            "twice")
               and len(res["issueTitle"]) <= len("[Bug] ") + 60,
               f"got {res['issueTitle']!r}")

        res2 = sidecar_main.export_feedback_bundle({"category": "feedback"})
        _check("feedback title falls back on empty description",
               res2["issueTitle"] == "[Feedback] (no description)",
               f"got {res2['issueTitle']}")
        _check("empty description placeholder in body",
               "(no description provided)" in res2["issueBody"])

        res3 = sidecar_main.export_feedback_bundle({
            "category": "feedback", "description": "y" * 20_000})
        _check("giant description still under the body cap",
               len(res3["issueBody"]) <= sidecar_main._ISSUE_BODY_CAP,
               f"len={len(res3['issueBody'])}")

    _with_scratch(body)


def test_prune_to_newest() -> None:
    print()
    print("Test: FEEDBACK_DIR pruned to the newest bundles")

    def body(_scratch: Path) -> None:
        sidecar_main.FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            (sidecar_main.FEEDBACK_DIR
             / f"meridian-feedback-2026010{i}-000000.zip").write_bytes(b"old")

        sidecar_main.export_feedback_bundle({"category": "feedback",
                                             "description": "prune me"})

        remaining = sorted(
            p.name for p in sidecar_main.FEEDBACK_DIR.glob("*.zip"))
        _check("pruned to _FEEDBACK_KEEP",
               len(remaining) == sidecar_main._FEEDBACK_KEEP,
               f"got {remaining}")
        _check("oldest bundles evicted first",
               "meridian-feedback-20260100-000000.zip" not in remaining
               and "meridian-feedback-20260101-000000.zip" not in remaining,
               f"got {remaining}")

    _with_scratch(body)


def main() -> int:
    test_log_event_handler()
    test_get_recent_events_roundtrip()
    test_bundle_contents()
    test_title_variants_and_caps()
    test_prune_to_newest()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
