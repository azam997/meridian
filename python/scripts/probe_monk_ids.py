"""One-off: dump authoritative Monk action ids + buff-status ids from real
top-Monk pulls' masterData.abilities, so data.py can be written against ground
truth. Also prints the opening cast sequence (for the canonical opener + the
pre-pull Form Shift / Meditation timing), the GCD cadence between consecutive
weaponskills (the Greased Lightning flat ~2.0s GCD), per-window burst cast
counts (Perfect Balance / Blitz / Riddle of Fire cadence), and the damage
bonusPercent bytes (positional / Fury-bonus cross-check). Run from python/:
    python scripts/probe_monk_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Monk"
SUBTYPE = "Monk"


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
        print("  (no Monk actor in this fight)")
        return
    mnk = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = mnk["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]
    print(f"  duration={(e - s) / 1000:.0f}s")

    casts = client.get_events(code, s, e, aid, data_type="Casts")
    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {abil.get(cid, '?'):28s} x{n}")

    print("\n  === OPENER (first 55 casts, t in s) ===")
    for ev in casts[:55]:
        t = (ev.get("timestamp", s) - s) / 1000.0
        cid = ev.get("abilityGameID")
        print(f"    {t:6.2f}  {cid:7d}  {abil.get(cid, '?')}")

    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, n in bc.most_common():
        print(f"    {bid:7d}  {abil.get(bid, '?'):28s} x{n}")

    # Buff durations (first apply->remove pair per status) — Riddle of Fire,
    # Brotherhood, Perfect Balance, Formless Fist, Fire's/Wind's Rumination,
    # Medicated.
    print("\n  === BUFF DURATIONS (first apply->remove per status) ===")
    open_t: dict[int, float] = {}
    seen: set[int] = set()
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        bid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        typ = ev.get("type", "")
        if typ == "applybuff" and bid not in seen and bid not in open_t:
            open_t[bid] = t
        elif typ == "removebuff" and bid in open_t and bid not in seen:
            print(f"    {bid:7d}  {abil.get(bid, '?'):28s} "
                  f"{open_t[bid]:7.2f} -> {t:7.2f}  ({t - open_t[bid]:5.2f}s)")
            seen.add(bid)
            del open_t[bid]

    # GCD cadence: consecutive-cast gaps histogram (rounded to 0.05s) — separates
    # the Greased Lightning ~2.0s weaponskill GCD, the 1.0s Meditation, the ~4s
    # Six-sided Star, and weave-stretched gaps.
    times = [(ev.get("timestamp", s) - s) / 1000.0 for ev in casts]
    gaps = Counter()
    for i in range(1, len(times)):
        g = round((times[i] - times[i - 1]) * 20) / 20
        if 0.2 <= g <= 4.5:
            gaps[g] += 1
    print("\n  === CAST-GAP HISTOGRAM (s x N, 0.05 bins, top 25) ===")
    for g, n in sorted(gaps.most_common(25)):
        print(f"    {g:5.2f}s x{n}")

    # Damage events: per-ability mean amount + the bonusPercent byte (positional /
    # combo / Fury cross-check) + crit rate (guaranteed-crit check on Leaping Opo).
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    by_ab: dict[int, list] = {}
    for ev in dmg:
        by_ab.setdefault(ev.get("abilityGameID"), []).append(ev)
    print("\n  === DAMAGE (id  name  hits  mean  crit%  bonusPercent set) ===")
    for cid, evs in sorted(by_ab.items(), key=lambda kv: -len(kv[1])):
        amounts = [ev.get("amount", 0) for ev in evs]
        crits = sum(1 for ev in evs if ev.get("hitType") == 2)
        bps = Counter(ev.get("bonusPercent") for ev in evs
                      if ev.get("bonusPercent") is not None)
        mean = sum(amounts) / max(1, len(amounts))
        print(f"    {cid:7d}  {abil.get(cid, '?'):26s} x{len(evs):3d} "
              f"mean={mean:9.0f} crit={crits / max(1, len(evs)):4.0%} "
              f"bp={dict(bps.most_common(5))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    ap.add_argument("--top", type=int, default=3)
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
