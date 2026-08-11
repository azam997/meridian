"""One-off: find where Salted Earth's DoT damage actually logs.

The potency probe's tick table came out empty (no `tick`-flagged events in
the player's deduped DamageDone). This scans one cached top-M11S pull and
prints EVERY distinct (abilityGameID, name, type, tick) combo in the player's
DamageDone stream with counts, plus every damage event inside the first two
Salted Earth windows, plus any report-wide ability whose name mentions
"Salted" (in case the ticks ride a separate actor/id).

Run from python/:
    python scripts/probe_darkknight_ticks.py [--enc 103]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "DarkKnight"


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

    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    combos = Counter((ev.get("abilityGameID"), abil.get(ev.get("abilityGameID"), "?"),
                      ev.get("type"), bool(ev.get("tick")))
                     for ev in dmg)
    print("\n=== PLAYER DamageDone (id, name, type, tick) -> count ===")
    for (cid, nm, typ, tick), n in sorted(combos.items(), key=lambda kv: -kv[1]):
        print(f"    {cid:9d}  {nm:28s} {typ:18s} tick={tick!s:5s} x{n}")

    casts = client.get_events(code, s, e, aid, data_type="Casts")
    se_ts = sorted((ev.get("timestamp", s) - s) / 1000.0 for ev in casts
                   if ev.get("type") == "cast" and ev.get("abilityGameID") == 3639)
    print(f"\nSalted Earth casts at: {[round(t, 1) for t in se_ts]}")
    for wt in se_ts[:2]:
        print(f"\n=== ALL player damage events in [{wt:.1f}, {wt + 16:.1f}] ===")
        for ev in sorted(dmg, key=lambda x: x.get("timestamp", 0)):
            t = (ev.get("timestamp", s) - s) / 1000.0
            if wt <= t < wt + 16.0:
                cid = ev.get("abilityGameID")
                print(f"    {t:7.2f}  {ev.get('type'):18s} {cid:9d} "
                      f"{abil.get(cid, '?'):24s} amt={ev.get('amount')!s:>7s} "
                      f"tick={bool(ev.get('tick'))!s:5s} mult={ev.get('multiplier')}")

    named = [(gid, nm) for gid, nm in abil.items() if "salt" in (nm or "").lower()]
    print(f"\nabilities named *salt*: {named}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
