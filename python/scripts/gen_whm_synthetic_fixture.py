"""Generate the synthetic WHM fixture for the pipeline / contract snapshot test.

NOT a real log — a deterministic hand-designed 360 s rotation that exercises:
  * the pre-pull Glare III channel (begincast-anchored at -1.5 s),
  * a near-canonical opener (precast Glare -> Dia -> Glare, PoM + Assize weaved
    after the 2nd in-fight GCD, then the 3 Glare IVs inside the haste window),
  * the PoM haste cadence (2.0 s GCD spacing for 15 s after each PoM),
  * Dia refreshes on a ~30 s cadence,
  * the lily economy (3 Afflatus Solace + an Afflatus Misery each ~minute),
  * one obvious Assize cooldown drift (cast once at ~5.4 s, then never again
    across 360 s -> a clean Drift finding),
  * a second PoM at ~125 s (on cooldown, no drift).

All ability ids are WHM-only from jobs/whitemage/data.py. Deterministic: the
output JSON is committed; rerun only to intentionally change the fixture (then
regenerate the snapshot with UPDATE_SNAPSHOT=1).

Run from python/:  python scripts/gen_whm_synthetic_fixture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.whitemage import data as wd  # noqa: E402

OUT = (Path(__file__).resolve().parent.parent
       / "tests" / "fixtures" / "whm" / "synthetic.json")

START_MS = 1_000_000
DURATION_S = 360.0
SOURCE_ID = 31


def main() -> int:
    casts: list[tuple[float, int]] = []

    # Pre-pull Glare III channel (begincast-anchored).
    casts.append((-1.5, wd.GLARE_III))

    pom_until = 0.0
    sacred = 0
    dia_end = 0.0
    lilies = 0
    blood = 0
    next_lily = wd.LILY_INTERVAL_S
    pom_casts = 0
    assize_done = False

    t = 1.0  # first in-fight GCD (recast rolled from the -1.5 s begincast)
    while t < DURATION_S - 0.1:
        # Accrue lilies (timer; cap 3).
        while next_lily <= t:
            lilies = min(wd.LILY_CAP, lilies + 1)
            next_lily += wd.LILY_INTERVAL_S

        # Pick this GCD.
        if dia_end - t <= 2.5:
            aid = wd.DIA
            dia_end = t + wd.DIA_DOT_DURATION_S
        elif blood >= wd.BLOOD_LILY_CAP:
            aid = wd.AFFLATUS_MISERY
            blood = 0
        elif sacred > 0 and t < pom_until + wd.SACRED_SIGHT_DURATION_S:
            aid = wd.GLARE_IV
            sacred -= 1
        elif lilies >= wd.LILY_CAP:
            aid = wd.AFFLATUS_SOLACE
            lilies -= 1
            blood += 1
        else:
            aid = wd.GLARE_III
        casts.append((t, aid))

        # Weaves after the 2nd in-fight GCD and on the 2-minute PoM cadence.
        if pom_casts == 0 and t >= 3.0:
            casts.append((t + 1.2, wd.PRESENCE_OF_MIND))
            casts.append((t + 1.9, wd.ASSIZE))   # the one and only Assize (drift)
            assize_done = True
            pom_until = t + 1.2 + wd.POM_DURATION_S
            sacred = wd.SACRED_SIGHT_STACKS
            pom_casts = 1
        elif pom_casts == 1 and t >= 125.0:
            casts.append((t + 1.2, wd.PRESENCE_OF_MIND))
            pom_until = t + 1.2 + wd.POM_DURATION_S
            sacred = wd.SACRED_SIGHT_STACKS
            pom_casts = 2

        gcd = 2.0 if t < pom_until else 2.5
        t += gcd

    assert assize_done
    cast_events = [
        {"timestamp": START_MS + int(round(ct * 1000)), "type": "cast",
         "sourceID": SOURCE_ID, "abilityGameID": aid, "fight": 1}
        for ct, aid in casts
    ]
    fixture = {
        "_comment": (
            "Synthetic WHM fixture for end-to-end pipeline / contract testing. "
            "NOT a real log — deterministic output of "
            "scripts/gen_whm_synthetic_fixture.py (see its docstring for what "
            "it exercises: pre-pull Glare channel, PoM haste cadence + Glare "
            "IVs, ~30s Dia refreshes, the lily->Misery economy, and a "
            "deliberate one-cast Assize drift)."),
        "label": "whm_synthetic",
        "report_code": "WHM_SYNTH_001",
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
