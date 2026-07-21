"""Probe XIVAPI to verify White Mage action-id candidates (one-off, add-job step).

Run from python/:  python scripts/probe_whm_ids.py
Prints OK/MISMATCH per id plus a BUNDLED-ready line for each.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core import ability_metadata as am  # noqa: E402

CANDIDATES = {
    # damage / rotation
    25859: "Glare III",
    37009: "Glare IV",
    16532: "Dia",
    3571: "Assize",
    136: "Presence of Mind",
    16535: "Afflatus Misery",
    25860: "Holy III",
    139: "Holy",
    # lily heals (dual-purpose: nourish the Blood Lily)
    16531: "Afflatus Solace",
    16534: "Afflatus Rapture",
    # defensives / utility heals
    7430: "Thin Air",
    3570: "Tetragrammaton",
    140: "Benediction",
    137: "Regen",
    16536: "Temperance",
    25861: "Aquaveil",
    25862: "Liturgy of the Bell",
    7432: "Divine Benison",
    7433: "Plenary Indulgence",
    3569: "Asylum",
    131: "Cure III",
    135: "Cure II",
    120: "Cure",
    124: "Medica",
    133: "Medica II",
    37010: "Medica III",
    37011: "Divine Caress",
    37008: "Aetherial Shift",
    # role actions
    7561: "Swiftcast",
    7562: "Lucid Dreaming",
    7559: "Surecast",
    7571: "Rescue",
    7568: "Esuna",
}


def main() -> None:
    bad = 0
    for aid, exp in sorted(CANDIDATES.items()):
        m = am._fetch_from_xivapi(aid)
        got = m.name if m else None
        ok = got == exp
        bad += 0 if ok else 1
        flag = "OK      " if ok else "MISMATCH"
        print(f"{flag} {aid:>6} expected={exp!r} got={got!r} "
              f"ogcd={getattr(m, 'is_ogcd', None)}")
        if m:
            print(f"    {aid}: AbilityMeta({m.name!r}, {m.icon!r}, {m.is_ogcd}),")
    print(f"\n{bad} mismatches / {len(CANDIDATES)} ids")


if __name__ == "__main__":
    main()
