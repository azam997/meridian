"""Probe #3 (tiny): implied potency of EVERY Radiant Encore hit, per cast, with
flags — settles whether the opener (1-Coda) Encore lands at 700 or 1100.

Run from python/:  python scripts/probe_bard_encore.py [--enc 103] [--top 5]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

# Flat buffs (crit/DH-rate ones excluded; DH events divided by 1.25 explicitly).
FLAT = {1000125: 1.15, 1002964: None, 1001878: 1.06, 1003685: 1.05,
        1001297: 1.04, 1000049: 1.08, 1002217: 1.01, 1002912: 1.05}
RF = 1002964


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    client = _client()
    blob = client.get_rankings(args.enc, class_name="Bard", spec_name="Bard",
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")][:args.top]
    for r in ranks:
        code, fid = r["report"]["code"], r["report"]["fightID"]
        report = client.get_report_summary(code)
        fight = next(f for f in report["fights"] if f["id"] == fid)
        friendly = set(fight.get("friendlyPlayers") or [])
        actors = [a for a in report["masterData"]["actors"]
                  if a["type"] == "Player" and a.get("subType") == "Bard"
                  and a["id"] in friendly]
        if not actors:
            continue
        aid = actors[0]["id"]
        abil = {a["gameID"]: a["name"]
                for a in (report["masterData"].get("abilities") or [])}
        s, e = fight["startTime"], fight["endTime"]
        buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
        wins: dict[int, list[tuple[float, float]]] = {}
        open_t: dict[int, float] = {}
        for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
            bid = ev.get("abilityGameID")
            if bid not in FLAT:
                continue
            t = (ev.get("timestamp", s) - s) / 1000.0
            if ev.get("type") in ("applybuff", "refreshbuff"):
                open_t.setdefault(bid, t)
            elif ev.get("type") == "removebuff" and bid in open_t:
                wins.setdefault(bid, []).append((open_t.pop(bid), t))
        for bid, t0 in open_t.items():
            wins.setdefault(bid, []).append((t0, t0 + 30.0))

        # Song casts -> coda count at each Radiant Finale (distinct songs since last RF).
        raw = client.get_events(code, s, e, aid, data_type="Casts")
        songs, rfs = [], []
        for ev in raw:
            if ev.get("type") != "cast":
                continue
            nm = abil.get(ev.get("abilityGameID"), "")
            t = (ev.get("timestamp", s) - s) / 1000.0
            if nm in ("The Wanderer's Minuet", "Mage's Ballad", "Army's Paeon"):
                songs.append((t, nm))
            elif nm == "Radiant Finale":
                rfs.append(t)
        coda_at_rf: list[tuple[float, int]] = []
        last_rf = -1e9
        for rt in rfs:
            played = {nm for t, nm in songs if last_rf < t < rt}
            coda_at_rf.append((rt, max(1, len(played))))
            last_rf = rt

        def coda_for(t: float) -> int:
            prior = [c for rt, c in coda_at_rf if rt <= t]
            return prior[-1] if prior else 0

        def mult_at(t: float, rf_coda: int) -> float:
            m = 1.0
            for bid, ws in wins.items():
                if any(a <= t < b for a, b in ws):
                    m *= {2: 1.04, 3: 1.06}.get(rf_coda, 1.02) if bid == RF \
                        else FLAT[bid]
            return m

        dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
        print(f"\n=== {r.get('name')}  {code}#{fid} ===")
        for ev in dmg:
            if abil.get(ev.get("abilityGameID")) != "Radiant Encore" or ev.get("tick"):
                continue
            t = (ev.get("timestamp", s) - s) / 1000.0
            amt = ev.get("amount", 0)
            ht, dh = ev.get("hitType"), bool(ev.get("directHit"))
            coda = coda_for(t)
            base = amt / mult_at(t, coda)
            if dh:
                base /= 1.25
            crit = " CRIT" if ht == 2 else ""
            # 98.4 dmg/potency anchor from probe #2.
            print(f"  t={t:6.1f}  coda={coda}  amt={amt:7d}{crit}{' DH' if dh else '':4s}"
                  f" -> implied {base / 98.4:6.0f}p{'  (crit: /1.55 ~ ' + format(base / 98.4 / 1.55, '.0f') + 'p)' if crit else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
