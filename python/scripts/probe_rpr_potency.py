"""Throwaway probe: verify the RPR data.py "verify" flags against real logs.

Phase-3 of plan rpr-calibration.md:
1. Plentiful Harvest 1000p ("full-party value -- verify") and Harpe 300p
   ("verify live"): estimate each ability's base-potency ratio vs a
   known-potency anchor (Communio 1100) from top-parse DamageDone events.
   Per event we normalize `unmitigatedAmount / multiplier` (FFLogs folds the
   active buff multipliers, incl. Medicated + Death's Design, into
   `multiplier`), keep hitType==1 (non-crit), and take a low quantile per
   ability to dodge direct-hit inflation (+25% tail); the +/-5% damage
   variance stays. implied_potency = 1100 * (q25_ability / q25_communio).
2. Canonical opener: print the first 12 in-fight GCDs of a few top pulls
   next to data.CANONICAL_OPENER (zero-priced diagnostic; cosmetic).

Run from python/:  python scripts/probe_rpr_potency.py [--top 5]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from jobs._core.ability_metadata import get_metadata                   # noqa: E402
from jobs.reaper import data as rd                                     # noqa: E402
from sidecar.main import _client                                       # noqa: E402

JOB = "Reaper"
# anchor first; then the verify targets + two more knowns as sanity controls
PROBE_IDS = {
    rd.COMMUNIO: ("Communio (ANCHOR)", 1100),
    rd.PLENTIFUL_HARVEST: ("Plentiful Harvest", 1000),
    rd.HARPE: ("Harpe", 300),
    rd.PERFECTIO: ("Perfectio (control)", 1300),
    rd.SOUL_SLICE: ("Soul Slice (control)", 520),
}


def _actor(client, code, fid, name):
    rep = client.get_report_summary(code)
    fight = next((f for f in rep["fights"] if f["id"] == fid), None)
    if fight is None:
        return None, None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in rep["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == JOB
              and a["id"] in friendly]
    by_name = [a for a in actors if a["name"].lower() == (name or "").lower()]
    pick = by_name or actors
    return (pick[0]["id"] if pick else None), fight


def _q(sorted_vals: list[float], frac: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * frac))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    client = _client()

    norm: dict[int, list[float]] = defaultdict(list)   # aid -> normalized bases
    raw_n: dict[int, int] = defaultdict(int)
    openers: list[tuple[str, list[str]]] = []
    pulls = 0

    for enc, _enc_name in AAC_HEAVYWEIGHT_ENCOUNTERS:
        blob = client.get_rankings(enc, class_name=JOB, spec_name=JOB,
                                   difficulty=DIFFICULTY_SAVAGE, metric="rdps",
                                   page=1)
        ranks = [r for r in ((blob or {}).get("rankings") or [])
                 if r.get("report", {}).get("code")][:args.top]
        for r in ranks:
            code, fid = r["report"]["code"], r["report"]["fightID"]
            src, fight = _actor(client, code, fid, r.get("name"))
            if src is None:
                continue
            pulls += 1
            dmg = client.get_events(code, fight["startTime"], fight["endTime"],
                                    src, data_type="DamageDone")
            seen: set[tuple] = set()
            for e in dmg:
                aid = e.get("abilityGameID")
                if aid not in PROBE_IDS:
                    continue
                key = (e.get("packetID"), aid, e.get("targetID"))
                if key in seen:        # calculateddamage/damage pairs
                    continue
                seen.add(key)
                raw_n[aid] += 1
                amt = e.get("unmitigatedAmount") or e.get("amount") or 0
                mult = e.get("multiplier") or 1.0
                if e.get("hitType") != 1 or not amt or mult <= 0:
                    continue           # crits (hitType 2) out; DH stays in tail
                norm[aid].append(amt / mult)
            # Opener: first 12 in-fight GCDs (M9S pulls only, fresh-pull opener)
            if enc == 101 and len(openers) < 4:
                mr = analyze_pull(JOB, client, code, fid,
                                  ranking_name=r.get("name"), label=r.get("name"))
                gcds = []
                for t, a in mr.norm_casts:
                    if t < 0:
                        continue
                    m = get_metadata(a)
                    if m is not None and not m.is_ogcd:
                        gcds.append((m.name or str(a)))
                    if len(gcds) >= 12:
                        break
                openers.append((r.get("name", "?"), gcds))

    print(f"\n{pulls} pulls probed.\n")
    anchor_vals = sorted(norm.get(rd.COMMUNIO, []))
    if not anchor_vals:
        print("no Communio anchor events — abort")
        return
    a25 = _q(anchor_vals, 0.25)
    print(f"{'ability':<22}{'table':>7}{'n(evt)':>8}{'n(norm)':>9}"
          f"{'q25 norm':>10}{'implied pot':>12}{'vs table':>9}")
    for aid, (label, table_pot) in PROBE_IDS.items():
        vals = sorted(norm.get(aid, []))
        if not vals:
            print(f"{label:<22}{table_pot:>7}{raw_n[aid]:>8}{0:>9}{'—':>10}")
            continue
        q25 = _q(vals, 0.25)
        implied = 1100.0 * q25 / a25
        print(f"{label:<22}{table_pot:>7}{raw_n[aid]:>8}{len(vals):>9}"
              f"{q25:>10.0f}{implied:>12.0f}{implied / table_pot:>9.2f}")
    print("\n(implied pot within ~±8% of table => table value verified; the "
          "±5% damage-variance floor + DH tail bound the method.)")

    print("\n--- Openers: first 12 in-fight GCDs (M9S) vs CANONICAL_OPENER ---")
    canon = [(get_metadata(a).name if get_metadata(a) else str(a))
             for a in rd.CANONICAL_OPENER]
    print(f"  CANON: {' > '.join(canon)}")
    for nm, gcds in openers:
        print(f"  {nm:<18}: {' > '.join(gcds)}")


if __name__ == "__main__":
    main()
