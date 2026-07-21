"""Generate the synthetic VPR fixture for the pipeline / contract snapshot test.

NOT a real log — a deterministic cast stream produced ONCE from the idealized Viper
simulator (a representative 300 s single-target rotation: the ST combo + Death
Rattle, the Vicewinder coil cycle with its Twin Bite weaves, Uncoiled Fury dumps,
the Reawaken -> Generations -> Ouroboros combo, and the 2-minute Serpent's Ire
burst). The output JSON is committed and thereafter acts as a fixed delivered
stream; rerun only to intentionally change the fixture (then regenerate the
snapshot with UPDATE_SNAPSHOT=1).

All ability ids are VPR-only from jobs/viper/data.py, so ability_metadata stays
consistent. The generator drops the sim's pre-pull / tincture markers so the
fixture looks like a plain cast stream.

Run from python/:  python scripts/gen_viper_synthetic_fixture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.tincture import TINCTURE_ACTION_ID               # noqa: E402
from jobs.viper.simulator import simulate_idealized             # noqa: E402

OUT = (Path(__file__).resolve().parent.parent
       / "tests" / "fixtures" / "vpr" / "synthetic.json")

START_MS = 1_000_000
DURATION_S = 300.0
SOURCE_ID = 25


def main() -> int:
    timeline, _ = simulate_idealized(DURATION_S, [])
    casts = [(t, aid) for t, aid in timeline
             if t >= 0.0 and aid != TINCTURE_ACTION_ID]
    cast_events = [
        {"timestamp": START_MS + int(round(ct * 1000)), "type": "cast",
         "sourceID": SOURCE_ID, "abilityGameID": aid, "fight": 1}
        for ct, aid in casts
    ]
    fixture = {
        "_comment": (
            "Synthetic VPR fixture for end-to-end pipeline / contract testing. "
            "NOT a real log — deterministic output of "
            "scripts/gen_viper_synthetic_fixture.py (idealized VPR rotation: the "
            "ST combo + Death Rattle, the Vicewinder coil cycle, Uncoiled Fury "
            "dumps, the Reawaken -> Generations -> Ouroboros combo, and the "
            "2-minute Serpent's Ire burst). All ids are VPR-only from "
            "jobs/viper/data.py."),
        "label": "vpr_synthetic",
        "report_code": "VPR_SYNTH_001",
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
