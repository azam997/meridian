"""Probe XIVAPI to verify Sage action-id candidates (one-off, add-job step).

Run from python/:  python scripts/probe_sage_ids.py
Prints OK/MISMATCH per id plus a BUNDLED-ready line for each.

Potencies still need live-pull verification (Dosis III, Eukrasian Dosis III
tick/duration, Phlegma III, Toxikon II, Pneuma, Psyche) via a real top-parse
event probe during calibration — this script only pins the id -> name mapping.
Watch the id-trait upgrades (gotcha #5): a level-100 SGE casts Dosis III (24312),
Eukrasian Dosis III (24314), Phlegma III (24313), Toxikon II (24316), and
Dyskrasia II (24315) — NOT their lower-level base ids. Psyche (37033) and
Eukrasian Prognosis II (37034) are the Dawntrail additions (adjacent block).

Note the GCD/oGCD truth the sim needs (§2, corrected when pasting into BUNDLED):
Eukrasia + the Eukrasian follow-ups (Dosis/Prognosis/Diagnosis/Dyskrasia) + Pneuma
+ Toxikon are GCDs (is_ogcd=False); Psyche + all the Addersgall/utility oGCDs are
oGCDs (is_ogcd=True). XIVAPI's category is usually right here, but verify.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core import ability_metadata as am  # noqa: E402

CANDIDATES = {
    # --- damage / rotation (level-100 upgraded ids) ---
    24312: "Dosis III",
    24290: "Eukrasia",
    24314: "Eukrasian Dosis III",
    24313: "Phlegma III",
    24316: "Toxikon II",
    24315: "Dyskrasia II",
    37032: "Eukrasian Dyskrasia",
    24318: "Pneuma",
    37033: "Psyche",
    # base ids (bundled for low-level sync robustness)
    24283: "Dosis",
    24306: "Dosis II",
    24293: "Eukrasian Dosis",
    24308: "Eukrasian Dosis II",
    24289: "Phlegma",
    24307: "Phlegma II",
    24304: "Toxikon",
    24297: "Dyskrasia",
    # --- GCD heals / shields (costed-heal currency + locked heal) ---
    37034: "Eukrasian Prognosis II",   # the mit-plan LOCKED heal GCD (AoE shield)
    24292: "Eukrasian Prognosis",      # AoE shield (base)
    24286: "Prognosis",                # AoE heal
    24291: "Eukrasian Diagnosis",      # ST shield
    24284: "Diagnosis",                # ST heal
    24287: "Egeiro",                   # raise
    # --- Addersgall oGCD heals / mit ---
    24296: "Druochole",
    24298: "Kerachole",
    24299: "Ixochole",
    24303: "Taurochole",
    24302: "Physis II",
    24310: "Holos",
    24311: "Panhaima",
    24305: "Haima",
    24301: "Pepsis",
    # --- utility / buffs ---
    24285: "Kardia",
    24294: "Soteria",
    24295: "Icarus",
    24309: "Rhizomata",
    24317: "Krasis",
    24300: "Zoe",
    37035: "Philosophia",
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
