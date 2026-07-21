"""Probe XIVAPI to verify Scholar action-id candidates (one-off, add-job step).

Run from python/:  python scripts/probe_scholar_ids.py
Prints OK/MISMATCH per id plus a BUNDLED-ready line for each. 32/32 verified.

Potencies still need live-pull verification (Broil IV, Biolysis tick/duration,
Art of War II, Energy Drain, Baneful Impaction folded DoT) via a real top-parse
event probe during calibration — this script only pins the id -> name mapping.
Watch the id-trait upgrades (gotcha #5): a level-100 SCH casts Broil IV (25865),
Art of War II (25866), Biolysis (16540), and Concitation (37013) — NOT their
lower-level base ids. And SCH's Energy Drain is 167 (NOT the SMN id 16508).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core import ability_metadata as am  # noqa: E402

CANDIDATES = {
    # damage / rotation (level-100 upgraded ids)
    25865: "Broil IV",
    16540: "Biolysis",
    25866: "Art of War II",
    16539: "Art of War",       # base (bundled for low-level sync)
    17870: "Ruin II",
    7436: "Chain Stratagem",
    37012: "Baneful Impaction",
    167: "Energy Drain",       # SCH's — NOT the SMN 16508
    166: "Aetherflow",
    # GCD heals (costed-heal currency)
    185: "Adloquium",
    186: "Succor",
    37013: "Concitation",
    190: "Physick",
    # oGCD heals / mit
    189: "Lustrate",
    3583: "Indomitability",
    7434: "Excogitation",
    188: "Sacred Soil",
    25868: "Expedient",
    25867: "Protraction",
    16542: "Recitation",
    3587: "Dissipation",
    3586: "Emergency Tactics",
    3585: "Deployment Tactics",
    # fairy (heal-only pet) commands
    16537: "Whispering Dawn",
    16538: "Fey Illumination",
    16543: "Fey Blessing",
    16546: "Consolation",
    16545: "Summon Seraph",
    37014: "Seraphism",
    17215: "Summon Eos",
    17216: "Summon Selene",
    7423: "Aetherpact",
}


def main() -> None:
    bad = 0
    for aid, exp in sorted(CANDIDATES.items()):
        m = am._fetch_from_xivapi(aid)
        got = m.name if m else None
        ok = got is not None and got.lower() == exp.lower()
        bad += 0 if ok else 1
        flag = "OK      " if ok else "MISMATCH"
        print(f"{flag} {aid:>6} expected={exp!r} got={got!r} "
              f"ogcd={getattr(m, 'is_ogcd', None)}")
        if m:
            print(f"    {aid}: AbilityMeta({m.name!r}, {m.icon!r}, {m.is_ogcd}),")
    print(f"\n{bad} mismatches / {len(CANDIDATES)} ids")


if __name__ == "__main__":
    main()
