"""Probe MCH data questions against real logs (network; dev cache makes it cheap).

1. Wildfire damage action ID — data.py carries a best-guess `11638: 240`; find
   what ability IDs actually deal damage in the 12s after each Wildfire cast
   (2878) and whether 11638 / any "Wildfire" damage event exists.
2. QUEEN_RECAST_S masking — the modeled 12.5s recast (12s pet + wind-down) vs
   the FFXIV wiki's server-side 6s: measure the MINIMUM gap between consecutive
   Queen summons (16501) across top parses.

Run from python/:  python scripts/probe_mch_ids.py [--top 5]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client                                       # noqa: E402

JOB = "Machinist"
WILDFIRE = 2878
QUEEN = 16501


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    client = _client()

    wf_window_ids: Counter = Counter()
    wf_named: dict[int, str] = {}
    queen_gaps: list[float] = []
    pulls = 0

    for enc, enc_name in AAC_HEAVYWEIGHT_ENCOUNTERS:
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
            start, end = fight["startTime"], fight["endTime"]
            casts = client.get_events(code, start, end, src, data_type="Casts")
            wf_ts = [e["timestamp"] for e in casts
                     if e.get("abilityGameID") == WILDFIRE
                     and e.get("type") == "cast"]
            queen_ts = [e["timestamp"] for e in casts
                        if e.get("abilityGameID") == QUEEN
                        and e.get("type") == "cast"]
            for a, b in zip(queen_ts, queen_ts[1:]):
                queen_gaps.append((b - a) / 1000.0)
            dmg = client.get_events(code, start, end, src,
                                    data_type="DamageDone")
            for e in dmg:
                aid = e.get("abilityGameID")
                if aid is None:
                    continue
                ts = e["timestamp"]
                if any(0 < ts - w <= 12_000 for w in wf_ts):
                    wf_window_ids[aid] += 1
                if aid not in wf_named and "ability" in e:
                    wf_named[aid] = e["ability"].get("name", "")

    print(f"\n{pulls} pulls probed.")
    print("\nDamage ability IDs in the 12s after a Wildfire cast (top 20):")
    from jobs._core.ability_metadata import get_metadata
    for aid, n in wf_window_ids.most_common(20):
        m = get_metadata(aid)
        name = (m.name if m else None) or wf_named.get(aid, "?")
        flag = "  <-- best-guess 11638" if aid == 11638 else ""
        print(f"  {aid:>7}  {name:<24} x{n}{flag}")
    hit_11638 = wf_window_ids.get(11638, 0)
    wf_dmg = [aid for aid in wf_window_ids
              if "wildfire" in ((get_metadata(aid).name if get_metadata(aid)
                                 else wf_named.get(aid, "")) or "").lower()]
    print(f"\n  11638 seen: {hit_11638} times; ids named 'Wildfire': {wf_dmg}")

    if queen_gaps:
        import statistics
        print(f"\nQueen summon gaps across {len(queen_gaps)} pairs: "
              f"min={min(queen_gaps):.2f}s  p5={sorted(queen_gaps)[len(queen_gaps)//20]:.2f}s  "
              f"median={statistics.median(queen_gaps):.2f}s")
        print("  (model QUEEN_RECAST_S=12.5 holds iff min >= ~12.5)")


if __name__ == "__main__":
    main()
