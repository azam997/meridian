"""Tests for the ceiling-invariant watchdog (sidecar/main.py::
_stamp_ceiling_anomaly).

The ceiling is ≥ delivered by construction, so efficiency > 100% means a
modeling bug. The watchdog must:
  - write one detailed `ceiling_anomaly` event when you OR any ref exceeds
    100.0 (strict or lenient)
  - stamp the additive `ceilingAnomaly` headline field only past the nudge
    threshold (100.05) — hairline float noise logs but never nags
  - leave clean runs byte-identical (no event, no headline key)

Run from python/:  python tests/test_ceiling_anomaly.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import ALL_ENCOUNTERS
from jobs._core.aspect import AspectResult, Track
from jobs._core.module_result import ModuleResult
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


def _mr(label: str, delivered: float, idealized: float,
        lenient: float | None = None,
        duration: float = 300.0) -> ModuleResult:
    """Minimal ModuleResult with just a Scoring aspect — enough for
    _efficiency_for / _lenient_efficiency_for / _headline."""
    state = {
        "delivered_potency": delivered,
        "idealized_strict": idealized,
        "idealized_potency": idealized,
        "idealized_lenient": lenient if lenient is not None else idealized,
    }
    return ModuleResult(
        label=label, fight_duration_s=duration,
        aspects={"Scoring": AspectResult(name="Scoring",
                                         track=Track("Scoring"),
                                         state=state)})


ENC_ID, ENC_NAME = ALL_ENCOUNTERS[0]


def _stamp(you: ModuleResult, refs: list[ModuleResult]) -> tuple[dict, list[dict]]:
    """Run the watchdog against a scratch event log; return (headline, events)."""
    out = {"headline": sidecar_main._headline(you, refs, {}),
           "refs": [sidecar_main._run_summary(r) for r in refs]}
    saved_dir = event_log._log_dir
    saved_active = event_log._active
    saved_size = event_log._approx_size
    with tempfile.TemporaryDirectory() as scratch:
        event_log._set_dir_for_tests(Path(scratch) / "logs")
        try:
            sidecar_main._stamp_ceiling_anomaly(
                out, you, refs, "Machinist", "ABC123xy", 12, ENC_ID,
                "Top 10", "Player One")
            events = event_log.recent_events()
        finally:
            event_log._log_dir = saved_dir
            event_log._active = saved_active
            event_log._approx_size = saved_size
    return out["headline"], events


def test_you_over_ceiling() -> None:
    print()
    print("Test: your run over the ceiling -> event + headline nudge")

    you = _mr("You", delivered=1050.0, idealized=1000.0)
    refs = [_mr("Ref A", 950.0, 1000.0), _mr("Ref B", 900.0, 1000.0)]
    headline, events = _stamp(you, refs)

    _check("one ceiling_anomaly event written",
           len(events) == 1 and events[0]["cat"] == "ceiling_anomaly",
           f"got {events}")
    data = events[0]["data"]
    _check("event carries the run identifiers",
           data["job"] == "Machinist" and data["reportCode"] == "ABC123xy"
           and data["fightId"] == 12 and data["encounterId"] == ENC_ID
           and data["encounterName"] == ENC_NAME
           and data["playerName"] == "Player One"
           and data["refsBucket"] == "Top 10")
    _check("event entry is you at 105%",
           len(data["entries"]) == 1
           and data["entries"][0]["who"] == "you"
           and data["entries"][0]["effPct"] == 105.0,
           f"got {data['entries']}")
    _check("entry carries the potency pair",
           data["entries"][0]["deliveredPotency"] == 1050.0
           and data["entries"][0]["idealizedPotency"] == 1000.0)

    ca = headline.get("ceilingAnomaly")
    _check("headline ceilingAnomaly stamped", ca is not None)
    _check("nudge payload identifies the run",
           ca["maxEffPct"] == 105.0 and ca["job"] == "Machinist"
           and ca["reportCode"] == "ABC123xy" and ca["fightId"] == 12
           and ca["encounterName"] == ENC_NAME,
           f"got {ca}")
    _check("nudge entries are slim (who/label/effPct/effLenientPct)",
           sorted(ca["entries"][0]) == ["effLenientPct", "effPct",
                                        "label", "who"],
           f"got {sorted(ca['entries'][0])}")


def test_ref_only_over_ceiling() -> None:
    print()
    print("Test: a reference over the ceiling also triggers")

    you = _mr("You", 900.0, 1000.0)
    refs = [_mr("Ref A", 1002.0, 1000.0), _mr("Ref B", 950.0, 1000.0)]
    headline, events = _stamp(you, refs)

    _check("event written for the ref", len(events) == 1)
    entry = events[0]["data"]["entries"][0]
    _check("entry is the ref at 100.2%",
           entry["who"] == "ref" and entry["label"] == "Ref A"
           and entry["effPct"] == 100.2,
           f"got {entry}")
    _check("headline nudge stamped for ref anomaly",
           headline.get("ceilingAnomaly") is not None
           and headline["ceilingAnomaly"]["entries"][0]["who"] == "ref")


def test_clean_run_untouched() -> None:
    print()
    print("Test: clean run -> no event, no headline key")

    you = _mr("You", 950.0, 1000.0)
    refs = [_mr("Ref A", 1000.0, 1000.0)]  # exactly 100.0 is NOT over
    headline, events = _stamp(you, refs)

    _check("no event on a clean run", events == [], f"got {events}")
    _check("no ceilingAnomaly key on a clean run",
           "ceilingAnomaly" not in headline)


def test_hairline_logs_without_nudge() -> None:
    print()
    print("Test: 100.0 < eff <= 100.05 -> event but no nudge")

    you = _mr("You", 100_030.0, 100_000.0)  # rounds to 100.03
    headline, events = _stamp(you, [_mr("Ref A", 90_000.0, 100_000.0)])

    _check("hairline anomaly still logged",
           len(events) == 1
           and events[0]["data"]["entries"][0]["effPct"] == 100.03,
           f"got {events}")
    _check("no nudge below the threshold",
           "ceilingAnomaly" not in headline)


def test_lenient_only_anomaly() -> None:
    print()
    print("Test: lenient-only breach triggers via the lenient pair")

    # Strict fine (95.45%), lenient ceiling dipped below delivered (106.06%).
    you = _mr("You", 1050.0, 1100.0, lenient=990.0)
    headline, events = _stamp(you, [_mr("Ref A", 900.0, 1000.0)])

    _check("lenient breach logged",
           len(events) == 1
           and events[0]["data"]["entries"][0]["effLenientPct"] == 106.06,
           f"got {events}")
    ca = headline.get("ceilingAnomaly")
    _check("nudge maxEffPct reflects the lenient value",
           ca is not None and ca["maxEffPct"] == 106.06,
           f"got {ca}")


def test_heal_locked_you_exempt_refs_still_checked() -> None:
    print()
    print("Test: mit-plan-locked you-run over 100% is EXPECTED (no event/nudge);"
          " an unlocked ref anomaly still stamps")

    # A locked healer run above the honest ceiling — planned heals sacrificed
    # for damage. Framed by the dashboard, never an anomaly.
    you = _mr("You", delivered=1050.0, idealized=1000.0)
    you.aspects["Scoring"].state["heal_locks_applied"] = True
    headline, events = _stamp(you, [_mr("Ref A", 950.0, 1000.0)])
    _check("locked you-run over 100% writes no event", events == [],
           f"got {events}")
    _check("locked you-run over 100% stamps no nudge",
           "ceilingAnomaly" not in headline)

    # Refs are never locked — a ref breach on the same run must still stamp.
    headline2, events2 = _stamp(you, [_mr("Ref A", 1020.0, 1000.0)])
    _check("unlocked ref anomaly still logged beside a locked you",
           len(events2) == 1
           and events2[0]["data"]["entries"][0]["who"] == "ref",
           f"got {events2}")
    _check("ref nudge still stamped beside a locked you",
           headline2.get("ceilingAnomaly") is not None
           and headline2["ceilingAnomaly"]["entries"][0]["who"] == "ref")


def test_unknown_encounter_name_empty() -> None:
    print()
    print("Test: unknown encounter id -> empty encounterName, no crash")

    you = _mr("You", 1050.0, 1000.0)
    out = {"headline": sidecar_main._headline(you, [], {}), "refs": []}
    saved_dir = event_log._log_dir
    saved_active = event_log._active
    saved_size = event_log._approx_size
    with tempfile.TemporaryDirectory() as scratch:
        event_log._set_dir_for_tests(Path(scratch) / "logs")
        try:
            sidecar_main._stamp_ceiling_anomaly(
                out, you, [], "Machinist", "ZZZ", 1, 999_999, "Top 10", None)
            events = event_log.recent_events()
        finally:
            event_log._log_dir = saved_dir
            event_log._active = saved_active
            event_log._approx_size = saved_size

    _check("event written with empty encounterName",
           len(events) == 1 and events[0]["data"]["encounterName"] == "",
           f"got {events}")
    _check("nudge stamped with empty encounterName",
           out["headline"]["ceilingAnomaly"]["encounterName"] == "")


def main() -> int:
    test_you_over_ceiling()
    test_ref_only_over_ceiling()
    test_clean_run_untouched()
    test_hairline_logs_without_nudge()
    test_lenient_only_anomaly()
    test_heal_locked_you_exempt_refs_still_checked()
    test_unknown_encounter_name_empty()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
