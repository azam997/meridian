"""Kill Time Theorizer — the `theorize_kill_time` sidecar handler.

Offline (no network): every check passes an EXPLICIT `downtimeWindows` override,
so the handler skips the reference fetch and only calls the registered job's
simulator (purely in-process). The self-sufficient path — deriving an
encounter's downtime from its reference logs by `encounterId` — needs network /
credentials and is covered by app verification, not here. Exercises the design
invariants:

  * unregistered job (no simulator) → `unsupported`
  * downtime is CLIPPED to the target (late windows dropped, straddlers cut)
  * a longer target never scores below a shorter one (uptime tail)
  * a buff-bringing comp raises the ceiling vs no raid buffs
  * the ~range-second spread is sampled around the target
  * the target is clamped to a sane band

Run from python/:  python tests/test_theorize.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sidecar.main import theorize_kill_time

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def _ideal(job: str, target: float, *, party=None, downtime=None,
           range_s: float = 7.0) -> dict:
    # Always pass an explicit downtimeWindows override (even empty) so the
    # handler stays offline (no reference fetch). req_id is a dummy — progress
    # events are emitted to stdout and ignored by these assertions.
    return theorize_kill_time({
        "spec": job,
        "targetKillSec": target,
        "rangeSec": range_s,
        "partyJobs": party or [],
        "downtimeWindows": [{"startSec": s, "endSec": e}
                            for s, e in (downtime or [])],
    }, "test")


def main() -> int:
    print()
    print("Test: unsupported job is reported unsupported")
    # Every registered job now ships a simulator, so the unsupported path is
    # reachable only via an UNREGISTERED spec. (Dancer, Black Mage, Summoner,
    # Scholar, then Sage were once the stand-in here; all 20 combat jobs are
    # registered now, so use a job with no analyzer support — Blue Mage, a
    # limited job the analyzer will never model.)
    unsup = theorize_kill_time({"spec": "Blue Mage", "targetKillSec": 400}, "test")
    _check("Blue Mage -> unsupported", unsup.get("unsupported") is True, str(unsup))

    print()
    print("Test: Machinist returns a well-formed result")
    r = _ideal("Machinist", 400.0)
    for key in ("targetKillSec", "idealizedPotency", "timeline",
                "downtimeWindows", "buffWindows", "tinctureWindows",
                "samples", "abilityMeta", "downtimeSource", "refCount",
                "refKillTimeSec", "refPartyJobs"):
        _check(f"has key {key!r}", key in r)
    _check("idealizedPotency > 0", r["idealizedPotency"] > 0, str(r["idealizedPotency"]))
    _check("timeline non-empty", len(r["timeline"]) > 0)
    _check("not flagged unsupported", not r.get("unsupported"))
    _check("explicit-downtime override → downtimeSource 'explicit'",
           r["downtimeSource"] == "explicit", str(r["downtimeSource"]))

    print()
    print("Test: the ~7s spread is sampled around the target")
    ks = [s["killSec"] for s in r["samples"]]
    _check("samples cover target-3 .. target+3",
           min(ks) <= 397.0 and max(ks) >= 403.0, str((min(ks), max(ks))))
    _check(">= 7 samples", len(r["samples"]) >= 7, str(len(r["samples"])))
    _check("every sample has positive potency",
           all(s["idealizedPotency"] > 0 for s in r["samples"]))

    print()
    print("Test: downtime is clipped to the target")
    rc = _ideal("Machinist", 200.0,
                downtime=[(50, 70), (190, 260), (300, 320)])
    got = [(w["startSec"], w["endSec"]) for w in rc["downtimeWindows"]]
    _check("late window dropped, straddler truncated to [(50,70),(190,200)]",
           got == [(50.0, 70.0), (190.0, 200.0)], str(got))

    print()
    print("Test: a longer target never scores below a shorter one (uptime tail)")
    short = _ideal("Machinist", 400.0)["idealizedPotency"]
    long = _ideal("Machinist", 460.0)["idealizedPotency"]
    _check("ideal@460 >= ideal@400", long >= short, f"{long} vs {short}")

    print()
    print("Test: a buff-bringing comp raises the ceiling")
    no_buff = _ideal("Machinist", 400.0, party=[])["idealizedPotency"]
    buffed = _ideal("Machinist", 400.0,
                    party=["Bard", "Dragoon", "Ninja"])["idealizedPotency"]
    _check("buffed > no-buff", buffed > no_buff, f"{buffed} vs {no_buff}")
    _check("buffWindows present when comp given",
           len(_ideal("Machinist", 400.0, party=["Bard"])["buffWindows"]) > 0)

    print()
    print("Test: the target is clamped to [30, 1800]")
    _check("tiny target clamps to 30",
           _ideal("Machinist", 5.0)["targetKillSec"] == 30.0)
    _check("huge target clamps to 1800",
           _ideal("Machinist", 5000.0)["targetKillSec"] == 1800.0)

    print()
    print("Test: a second sim-backed job (Red Mage, sim_context=None path) works")
    rdm = _ideal("Red Mage", 400.0, party=["Bard"])
    _check("RDM idealizedPotency > 0", rdm["idealizedPotency"] > 0)
    _check("RDM timeline non-empty", len(rdm["timeline"]) > 0)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for name, detail in _FAILED:
        print(f"  FAILED: {name}  {detail}")
    return 0 if not _FAILED else 1


def test_theorize_kill_time() -> None:
    """pytest entry: the theorizer handler's design invariants."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
