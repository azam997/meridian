"""Probe #3 (one-off): raw pet event dump for the first two demi windows of one
top-SMN pull — settles the '2 events per Enkindle payoff' question (genuine
double-hit vs calculateddamage+damage pair vs second target).

Prints every pet Casts event and every pet DamageDone event with
(t, type, name, amount, hitType, directHit, targetID, targetInstance, mult).

Run from python/:
    python scripts/probe_summoner_petdump.py [--enc 103]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "Summoner"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    args = ap.parse_args()
    client = _client()

    blob = client.get_rankings(args.enc, class_name=FFLOGS_SUBTYPE,
                               spec_name=FFLOGS_SUBTYPE,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    r = next(r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code"))
    code, fid = r["report"]["code"], r["report"]["fightID"]
    report = client.get_report_summary(code)
    fight = next(f for f in report["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    who = next(a for a in report["masterData"]["actors"]
               if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
               and a["id"] in friendly)
    aid = who["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]
    print(f"pull {code}#{fid}  player={who['name']}({aid})")

    pets = [a for a in report["masterData"]["actors"]
            if a["type"] == "Pet" and a.get("petOwner") == aid]
    print(f"pets: {[(p['id'], p['name']) for p in pets]}")

    cutoff = 130.0  # first two demi windows + primal phase
    for p in pets:
        casts = client.get_events(code, s, e, p["id"], data_type="Casts")
        dmg = client.get_events(code, s, e, p["id"], data_type="DamageDone")
        rows = []
        for ev in casts:
            t = (ev.get("timestamp", s) - s) / 1000.0
            if t > cutoff:
                continue
            rows.append((t, f"cast:{ev.get('type')}", abil.get(ev.get("abilityGameID"), "?"),
                         "", "", "", ev.get("targetID"), ""))
        for ev in dmg:
            t = (ev.get("timestamp", s) - s) / 1000.0
            if t > cutoff:
                continue
            rows.append((t, f"dmg:{ev.get('type')}", abil.get(ev.get("abilityGameID"), "?"),
                         ev.get("amount"), ev.get("hitType"), ev.get("directHit"),
                         ev.get("targetID"), ev.get("multiplier")))
        if not rows:
            continue
        print(f"\n=== pet {p['name']} ({p['id']}) events to t={cutoff:.0f}s ===")
        for row in sorted(rows):
            t, typ, nm, amt, ht, dh, tgt, mult = row
            print(f"  {t:7.2f}  {typ:22s} {nm:18s} amt={amt!s:>8s} hit={ht!s:>4s} "
                  f"dh={dh!s:>5s} tgt={tgt!s:>4s} mult={mult}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
