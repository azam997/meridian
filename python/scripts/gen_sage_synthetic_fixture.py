"""Generate the synthetic SGE fixture for the pipeline / contract snapshot test.

NOT a real log — the deterministic default-sim 360s rotation (pre-pull Dosis III
channel, the Eukrasia -> Eukrasian Dosis III DoT sequence, Dosis III filler, the
Phlegma III charge dumps and Psyche oGCD) with a deliberate **Psyche drift**: every
Psyche after the first is dropped, so its 60s cooldown sits capped and DriftAspect
produces one clean finding.

All ability ids are SGE-only from jobs/sage/data.py. Deterministic: the output JSON
is committed; rerun only to intentionally change the fixture (then regenerate the
snapshot with UPDATE_SNAPSHOT=1).

Run from python/:  python scripts/gen_sage_synthetic_fixture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.sage import data as gd  # noqa: E402
from jobs.sage.simulator import simulate_idealized  # noqa: E402

OUT = (Path(__file__).resolve().parent.parent
       / "tests" / "fixtures" / "sge" / "synthetic.json")

START_MS = 1_000_000
DURATION_S = 360.0
SOURCE_ID = 31


def main() -> int:
    timeline, _ = simulate_idealized(DURATION_S, [])

    # Deliberate Psyche drift: keep only the FIRST Psyche cast, drop the rest, so its
    # 60s cooldown sits capped -> a clean DriftAspect finding.
    casts: list[tuple[float, int]] = []
    seen_psyche = False
    for t, aid in timeline:
        if aid == gd.PSYCHE:
            if seen_psyche:
                continue
            seen_psyche = True
        casts.append((t, aid))
    assert seen_psyche, "expected at least one Psyche in the sim timeline"

    cast_events = [
        {"timestamp": START_MS + int(round(ct * 1000)), "type": "cast",
         "sourceID": SOURCE_ID, "abilityGameID": aid, "fight": 1}
        for ct, aid in casts if aid > 0
    ]
    fixture = {
        "_comment": (
            "Synthetic SGE fixture for end-to-end pipeline / contract testing. "
            "NOT a real log — the deterministic default-sim rotation from "
            "scripts/gen_sage_synthetic_fixture.py (the Eukrasia->Eukrasian Dosis "
            "III DoT sequence, Dosis III filler, Phlegma III charge dumps, Psyche "
            "oGCD) with every Psyche after the first dropped -> a clean Psyche "
            "cooldown drift."),
        "label": "sge_synthetic",
        "report_code": "SGE_SYNTH_001",
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
