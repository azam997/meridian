"""Generate the synthetic AST fixture for the pipeline / contract snapshot test.

NOT a real log — the deterministic default-sim 360s rotation (pre-pull Fall
Malefic channel, Combust upkeep, Fall Malefic filler, the Divination/Oracle/
Earthly Star/Minor Arcana/Lord oGCD economy) with a deliberate **Divination
drift**: every Divination after the first is dropped, so the 120s cooldown sits
capped and DriftAspect produces one clean finding.

All ability ids are AST-only from jobs/astrologian/data.py. Deterministic: the
output JSON is committed; rerun only to intentionally change the fixture (then
regenerate the snapshot with UPDATE_SNAPSHOT=1).

Run from python/:  python scripts/gen_astrologian_synthetic_fixture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.astrologian import data as ad  # noqa: E402
from jobs.astrologian.simulator import simulate_idealized  # noqa: E402

OUT = (Path(__file__).resolve().parent.parent
       / "tests" / "fixtures" / "ast" / "synthetic.json")

START_MS = 1_000_000
DURATION_S = 360.0
SOURCE_ID = 31


def main() -> int:
    timeline, _ = simulate_idealized(DURATION_S, [])

    # Deliberate Divination drift: keep only the FIRST Divination cast, drop the
    # rest, so its 120s cooldown sits capped -> a clean DriftAspect finding.
    casts: list[tuple[float, int]] = []
    seen_divination = False
    for t, aid in timeline:
        if aid == ad.DIVINATION:
            if seen_divination:
                continue
            seen_divination = True
        casts.append((t, aid))
    assert seen_divination, "expected at least one Divination in the sim timeline"

    cast_events = [
        {"timestamp": START_MS + int(round(ct * 1000)), "type": "cast",
         "sourceID": SOURCE_ID, "abilityGameID": aid, "fight": 1}
        for ct, aid in casts if aid > 0
    ]
    fixture = {
        "_comment": (
            "Synthetic AST fixture for end-to-end pipeline / contract testing. "
            "NOT a real log — the deterministic default-sim rotation from "
            "scripts/gen_astrologian_synthetic_fixture.py (Combust upkeep, Fall "
            "Malefic filler, the Divination/Oracle/Earthly Star/Minor Arcana/Lord "
            "oGCD economy) with every Divination after the first dropped -> a "
            "clean Divination cooldown drift."),
        "label": "ast_synthetic",
        "report_code": "AST_SYNTH_001",
        "fight_id": 1,
        "source_id": SOURCE_ID,
        "fight_start_ms": START_MS,
        "fight_end_ms": START_MS + int(DURATION_S * 1000),
        "duration_s": DURATION_S,
        "parse_pct": None,
        "cast_events": cast_events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(cast_events)} casts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
