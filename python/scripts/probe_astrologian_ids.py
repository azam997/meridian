"""Probe XIVAPI to verify Astrologian action-id candidates (one-off, add-job step).

Run from python/:  python scripts/probe_astrologian_ids.py
Prints OK/MISMATCH per id plus a BUNDLED-ready line for each. 36/36 verified.

Potencies still need live-pull verification (Combust III tick, Oracle, Lord of
Crowns, Earthly Star / Stellar Explosion, Gravity II) via a real top-parse
event probe during calibration — this script only pins the id -> name mapping.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core import ability_metadata as am  # noqa: E402

CANDIDATES = {
    # damage / rotation
    25871: "Fall Malefic",
    16554: "Combust III",
    25872: "Gravity II",
    16552: "Divination",
    37029: "Oracle",
    7439: "Earthly Star",
    7444: "Lord of Crowns",
    37022: "Minor Arcana",
    37017: "Astral Draw",
    37018: "Umbral Draw",
    3606: "Lightspeed",
    # cards (ally buffs / heal cards)
    37019: "Play I",
    37020: "Play II",
    37021: "Play III",
    37024: "The Arrow",
    37025: "The Spire",
    37026: "The Spear",
    37027: "The Bole",
    7445: "Lady of Crowns",
    # GCD heals
    3594: "Benefic",
    3610: "Benefic II",
    3595: "Aspected Benefic",
    3600: "Helios",
    3601: "Aspected Helios",
    37030: "Helios Conjunction",
    # oGCD heals / mit
    3614: "Essential Dignity",
    16556: "Celestial Intersection",
    16553: "Celestial Opposition",
    3613: "Collective Unconscious",
    25873: "Exaltation",
    25874: "Macrocosmos",
    16559: "Neutral Sect",
    37031: "Sun Sign",
    3612: "Synastry",
    16557: "Horoscope",
    16558: "Horoscope",
}


def main() -> None:
    bad = 0
    for aid, exp in sorted(CANDIDATES.items()):
        m = am._fetch_from_xivapi(aid)
        got = m.name if m else None
        # XIVAPI lowercases the leading article on the cards ("the Bole"); accept
        # a case-insensitive match so the probe stays green.
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
