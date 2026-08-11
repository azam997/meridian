"""Empirically derive the guaranteed-crit-direct-hit multiplier for the
current gear tier, from real top-parse damage.

Why: our scoring is in raw potency (a non-crit, non-DH hit = base potency).
A few MCH effects *guarantee* a critical direct hit — Reassemble's buffed
weaponskill, and Full Metal Field's innate guarantee. Those deterministically
beat the floor by `crit_mult x 1.25`, so to value them correctly we need the
crit multiplier at current gear.

Method (buff-unbiased): pull DamageDone for the 660-potency tools (Drill /
Air Anchor / Chain Saw / Excavator — interchangeable, identical potency) from
several top parses. Each FFLogs damage event carries `multiplier` (the buff
multiplier, *excluding* crit/DH), so dividing it out removes raid-buff
contamination. Bucket hits by crit (`hitType == 2`) and direct hit
(`directHit is True`):

    crit_mult = mean(crit, non-DH) / mean(non-crit, non-DH)
    dh_mult   = mean(non-crit, DH)  / mean(non-crit, non-DH)   (sanity ~1.25)
    M         = crit_mult * 1.25     (DH is a fixed +25%)

`M` is the number to put in `jobs/machinist/data.py::GUARANTEED_CRIT_DH_MULT`.

Re-run when a new gear tier unlocks (crit scales, so M drifts up slowly).
It does not move fast — a re-run every major tier is plenty.

Run from python/:
    python scripts/calibrate_crit_dh.py [n_reports] [encounter_id]
"""
from __future__ import annotations

import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config            # noqa: E402
from fflogs_api import FFLogsClient       # noqa: E402

# Drill, Air Anchor, Chain Saw, Excavator — all 660p, identical crit mechanics.
TOOLS = [16498, 16500, 25788, 36981]
DEFAULT_ENCOUNTER = 103   # The Tyrant
DEFAULT_N_REPORTS = 12


def _mch_source(client: FFLogsClient, code: str, fight_id: int,
                name: str) -> int | None:
    report = client.get_report_summary(code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = report["masterData"]["actors"]
    by_name = [a for a in actors if a["type"] == "Player"
               and a.get("subType") == "Machinist" and a["id"] in friendly
               and a["name"].lower() == name.lower()]
    any_mch = [a for a in actors if a["type"] == "Player"
               and a.get("subType") == "Machinist" and a["id"] in friendly]
    pick = by_name or any_mch
    return pick[0]["id"] if pick else None


def main() -> int:
    n_reports = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_N_REPORTS
    encounter = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ENCOUNTER

    cfg = load_config()
    client = FFLogsClient(cfg["client_id"], cfg["client_secret"])

    blob = client.get_rankings(encounter, class_name="Machinist",
                               spec_name="Machinist", metric="rdps", page=1)
    rankings = [r for r in ((blob or {}).get("rankings") or [])
                if r.get("report", {}).get("code")][:n_reports]

    floor: list[float] = []
    crit_only: list[float] = []
    dh_only: list[float] = []
    crit_dh: list[float] = []
    n_crit = n_dh = n_tot = 0

    for r in rankings:
        code = r["report"]["code"]
        fid = r["report"]["fightID"]
        src = _mch_source(client, code, fid, r.get("name", ""))
        if src is None:
            continue
        rep = client.get_report_summary(code)
        fight = next((f for f in rep["fights"] if f["id"] == fid), None)
        if fight is None:
            continue
        s, e = fight["startTime"], fight["endTime"]
        for aid in TOOLS:
            try:
                evs = client.get_events(code, s, e, src,
                                        data_type="DamageDone", ability_id=aid)
            except Exception:
                continue
            for ev in evs:
                if ev.get("type") != "calculateddamage":   # dedupe paired events
                    continue
                amt = ev.get("unmitigatedAmount") or ev.get("amount")
                m = ev.get("multiplier") or 1.0
                if not amt:
                    continue
                norm = amt / m
                crit = ev.get("hitType") == 2
                dh = ev.get("directHit") is True
                n_tot += 1
                n_crit += crit
                n_dh += dh
                if crit and dh:
                    crit_dh.append(norm)
                elif crit:
                    crit_only.append(norm)
                elif dh:
                    dh_only.append(norm)
                else:
                    floor.append(norm)

    if not floor or not crit_only:
        print("Insufficient sample. Try more reports.")
        return 1

    fl = mean(floor)
    crit_mult = mean(crit_only) / fl
    dh_mult = mean(dh_only) / fl if dh_only else float("nan")
    direct = mean(crit_dh) / fl if crit_dh else float("nan")
    M = crit_mult * 1.25

    print(f"reports={len(rankings)}  tool hits={n_tot}  "
          f"crit_rate={n_crit/n_tot:.3f}  dh_rate={n_dh/n_tot:.3f}")
    print(f"buckets: floor={len(floor)} crit={len(crit_only)} "
          f"dh={len(dh_only)} crit+dh={len(crit_dh)}")
    print(f"crit_mult = {crit_mult:.4f}")
    print(f"dh_mult   = {dh_mult:.4f}   (sanity vs fixed 1.25)")
    print(f"direct crit+dh / floor = {direct:.4f}   (sanity vs crit_mult*1.25)")
    print()
    print(f">>> GUARANTEED_CRIT_DH_MULT = {M:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
