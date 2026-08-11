"""One-off: dump authoritative Viper action ids + buff-status ids from a real
top-Viper pull's masterData.abilities, so data.py can be written against ground
truth. Also prints the opening cast sequence (for the canonical opener) and the
GCD/oGCD split where ability metadata is available. Run from python/:
    python scripts/probe_viper_ids.py [--enc 101] [--top 1]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Viper"
SUBTYPE = "Viper"


def _dump_one(client, r) -> None:
    code, fid = r["report"]["code"], r["report"]["fightID"]
    print(f"\n########## top {JOB}: {r.get('name')}  report={code}#{fid}  "
          f"parse={r.get('rankPercent')} ##########")

    report = client.get_report_summary(code)
    fight = next(f for f in report["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in report["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == SUBTYPE
              and a["id"] in friendly]
    if not actors:
        print("  (no Viper actor in this fight)")
        return
    vpr = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = vpr["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]
    print(f"  duration={(e - s) / 1000:.0f}s")

    casts = client.get_events(code, s, e, aid, data_type="Casts")
    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {abil.get(cid, '?'):28s} x{n}")

    print("\n  === OPENER (first 40 casts, t in s) ===")
    for ev in casts[:40]:
        t = (ev.get("timestamp", s) - s) / 1000.0
        cid = ev.get("abilityGameID")
        print(f"    {t:6.1f}  {cid:7d}  {abil.get(cid, '?')}")

    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, n in bc.most_common():
        print(f"    {bid:7d}  {abil.get(bid, '?'):28s} x{n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=101)
    ap.add_argument("--top", type=int, default=1)
    args = ap.parse_args()
    client = _client()

    blob = client.get_rankings(args.enc, class_name=JOB, spec_name=JOB,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")]
    if not ranks:
        print("no rankings")
        return 1
    for r in ranks[:args.top]:
        _dump_one(client, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
