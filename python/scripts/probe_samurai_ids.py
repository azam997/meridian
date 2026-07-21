"""One-off: dump authoritative SAM action ids + buff-status ids from a real
top-SAM pull's masterData.abilities, so data.py can be rewritten against ground
truth (the scaffold has id collisions). Run from python/:
    python scripts/probe_samurai_ids.py [--enc 103]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Samurai"
SUBTYPE = "Samurai"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    args = ap.parse_args()
    client = _client()

    blob = client.get_rankings(args.enc, class_name=JOB, spec_name=JOB,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")]
    if not ranks:
        print("no rankings")
        return 1
    r = ranks[0]
    code, fid = r["report"]["code"], r["report"]["fightID"]
    print(f"top SAM: {r.get('name')}  report={code}#{fid}  parse={r.get('rankPercent')}")

    report = client.get_report_summary(code)
    fight = next(f for f in report["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in report["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == SUBTYPE
              and a["id"] in friendly]
    sam = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = sam["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]

    print("\n=== CASTS (id  name  xN) ===")
    casts = client.get_events(code, s, e, aid, data_type="Casts")
    cc = Counter(ev.get("abilityGameID") for ev in casts)
    for cid, n in cc.most_common():
        print(f"  {cid:7d}  {abil.get(cid, '?'):28s} x{n}")

    print("\n=== BUFFS ON PLAYER (applybuff id  name  xN) ===")
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    for bid, n in bc.most_common():
        print(f"  {bid:7d}  {abil.get(bid, '?'):28s} x{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
