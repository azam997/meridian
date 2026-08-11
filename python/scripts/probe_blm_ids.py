"""One-off: dump authoritative Black Mage action ids + buff-status ids from real
top-BLM pulls, for the patch-currency audit of jobs/blackmage/data.py. Prints:

  * cast counts (id verification; any unexpected 7.x id surfaces here),
  * the opening cast sequence (canonical-opener consensus),
  * begincast->cast deltas per ability (verifies CAST_TIMES: Fire IV 2.0,
    Blizzard III 3.5 hardcast vs instant-under-Swift/Triple, Fire III),
  * buff apply->remove durations (Ley Lines / Circle of Power 20s, Firestarter,
    Thunderhead, Triplecast, Medicated) + all applybuff ids seen,
  * GCD cadence inside vs outside Circle of Power (the LEY_LINES_HASTE=0.85 check),
  * fire-phase decomposition between ice entries (the "6x Fire IV + Paradox per
    phase" MP-economy invariant + the Manafont second batch),
  * an ACROSS-PULLS per-potency damage-rate table (non-crit/non-DH mean amount /
    table potency, median-normalized per pull) — flags potency drift vs data.py.

Run from python/:
    python scripts/probe_blm_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402
from jobs.blackmage import data as blm  # noqa: E402

JOB = "Black Mage"
FFLOGS_SUBTYPE = "BlackMage"

BLIZZARD_III = blm.BLIZZARD_III
PHASE_IDS = {
    blm.FIRE_IV: "F4", blm.PARADOX: "Pdx", blm.DESPAIR: "Dsp",
    blm.FLARE_STAR: "FS", blm.XENOGLOSSY: "Xeno", blm.FIRE_III: "F3",
    blm.MANAFONT: "MF", blm.BLIZZARD_IV: "B4", blm.HIGH_THUNDER: "HT",
    blm.FOUL: "Foul", blm.TRANSPOSE: "Tp", blm.FLARE: "Flare",
}


def _dump_one(client, r, agg: dict) -> None:
    code, fid = r["report"]["code"], r["report"]["fightID"]
    print(f"\n########## top {JOB}: {r.get('name')}  report={code}#{fid}  "
          f"parse={r.get('rankPercent')} ##########")

    report = client.get_report_summary(code)
    fight = next(f for f in report["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in report["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
              and a["id"] in friendly]
    if not actors:
        print("  (no BLM actor in this fight)")
        return
    who = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = who["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]
    print(f"  duration={(e - s) / 1000:.0f}s")

    raw = client.get_events(code, s, e, aid, data_type="Casts")
    casts = [ev for ev in raw if ev.get("type") == "cast"]
    begins = [ev for ev in raw if ev.get("type") == "begincast"]

    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {abil.get(cid, '?'):28s} x{n}")

    print("\n  === OPENER (first 25 casts, t in s; * = hardcast begin) ===")
    begun = {(ev.get("abilityGameID"), ev.get("timestamp")) for ev in begins}
    for ev in casts[:25]:
        t = (ev.get("timestamp", s) - s) / 1000.0
        cid = ev.get("abilityGameID")
        print(f"    {t:7.2f}  {cid:7d}  {abil.get(cid, '?')}")
    if begins:
        t0 = (begins[0].get("timestamp", s) - s) / 1000.0
        print(f"    (first begincast at {t0:.2f}s: "
              f"{abil.get(begins[0].get('abilityGameID'), '?')})")

    # begincast -> cast deltas per ability (true cast-time distribution).
    print("\n  === CAST TIMES (begincast->cast deltas, 0.05 bins) ===")
    last_begin: dict[int, float] = {}
    deltas: dict[int, Counter] = {}
    for ev in sorted(raw, key=lambda x: x.get("timestamp", 0)):
        cid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        if ev.get("type") == "begincast":
            last_begin[cid] = t
        elif ev.get("type") == "cast" and cid in last_begin:
            d = t - last_begin.pop(cid)
            if 0.0 < d <= 6.0:
                deltas.setdefault(cid, Counter())[round(d * 20) / 20] += 1
    for cid, ctr in sorted(deltas.items()):
        top = ", ".join(f"{d:.2f}s x{n}" for d, n in ctr.most_common(4))
        print(f"    {cid:7d}  {abil.get(cid, '?'):28s} {top}")
    hard = {ev.get("abilityGameID") for ev in begins}
    inst = [cid for cid in cc if cid not in hard]
    print("    (never begincast -> instant: "
          + ", ".join(abil.get(c, str(c)) for c in sorted(inst)) + ")")

    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, n in bc.most_common():
        print(f"    {bid:7d}  {abil.get(bid, '?'):28s} x{n}")

    print("\n  === BUFF DURATIONS (first apply->remove per status) ===")
    open_t: dict[int, float] = {}
    seen: set[int] = set()
    cop_windows: list[tuple[float, float]] = []   # Circle of Power (Ley Lines)
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        bid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        typ = ev.get("type", "")
        if typ == "applybuff" and bid not in open_t:
            open_t[bid] = t
        elif typ == "removebuff" and bid in open_t:
            if "circle of power" in abil.get(bid, "").lower():
                cop_windows.append((open_t[bid], t))
            if bid not in seen:
                print(f"    {bid:7d}  {abil.get(bid, '?'):28s} "
                      f"{open_t[bid]:7.2f} -> {t:7.2f}  ({t - open_t[bid]:5.2f}s)")
                seen.add(bid)
            del open_t[bid]

    # GCD cadence inside vs outside Circle of Power -> LEY_LINES_HASTE check.
    gcd_ids = {cid for cid in cc if cid not in blm.OGCD_IDS}
    gts = [(ev.get("timestamp", s) - s) / 1000.0 for ev in casts
           if ev.get("abilityGameID") in gcd_ids]
    def in_cop(t: float) -> bool:
        return any(a <= t <= b for a, b in cop_windows)
    gaps_in, gaps_out = [], []
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if 1.0 <= g <= 3.2:
            (gaps_in if in_cop(gts[i]) and in_cop(gts[i - 1]) else gaps_out).append(g)
    def med(v):
        return sorted(v)[len(v) // 2] if v else float("nan")
    print(f"\n  === GCD CADENCE: in-LeyLines median {med(gaps_in):.3f}s (n={len(gaps_in)}) "
          f"vs outside {med(gaps_out):.3f}s (n={len(gaps_out)}) "
          f"ratio={med(gaps_in) / med(gaps_out):.3f} ===")
    print(f"    Circle of Power windows: {len(cop_windows)}, "
          f"durations: {[f'{b - a:.1f}' for a, b in cop_windows]}")

    # Fire-phase decomposition: segments between ice entries (Blizzard III casts).
    print("\n  === PHASE SEGMENTS (split at Blizzard III; counts per segment) ===")
    seg: Counter = Counter()
    seg_start = 0.0
    segs: list[tuple[float, Counter]] = []
    for ev in casts:
        cid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        if cid == BLIZZARD_III and seg:
            segs.append((seg_start, seg))
            seg, seg_start = Counter(), t
        if cid in PHASE_IDS:
            seg[PHASE_IDS[cid]] += 1
    if seg:
        segs.append((seg_start, seg))
    for t0, sg in segs:
        parts = " ".join(f"{k}x{v}" for k, v in sg.most_common())
        print(f"    t={t0:6.1f}  {parts}")

    # Damage: aggregate per-ability CLEAN (non-crit, non-DH) direct amounts and
    # DoT tick amounts across pulls for the final potency-rate table.
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    pull_key = f"{code}#{fid}"
    for ev in dmg:
        cid = ev.get("abilityGameID")
        amt = ev.get("amount", 0)
        if amt <= 0:
            continue
        clean = ev.get("hitType") == 1 and not ev.get("directHit")
        kind = "dot" if ev.get("tick") else "direct"
        rec = agg.setdefault(pull_key, {}).setdefault((cid, kind),
                                                      {"clean": [], "all": []})
        rec["all"].append(amt)
        if clean:
            rec["clean"].append(amt)
    agg.setdefault("__names__", {}).update(abil)


def _rate_table(agg: dict) -> None:
    """Across-pulls potency-rate table: per pull, mean clean amount / table
    potency, normalized by that pull's median ratio; then averaged across pulls.
    A |dev| > ~8-10% on a well-sampled row = potency drift vs data.py."""
    names = agg.pop("__names__", {})
    pot = dict(blm.POTENCIES)
    dots = {blm.HIGH_THUNDER: blm.HIGH_THUNDER_DOT_TICK_P,
            blm.THUNDER_III: blm.THUNDER_III_DOT_TICK_P}
    per_pull_ratio: dict[tuple, list[float]] = {}
    per_pull_n: Counter = Counter()
    for pull, rows in agg.items():
        ratios: dict[tuple, float] = {}
        for (cid, kind), rec in rows.items():
            p = dots.get(cid) if kind == "dot" else pot.get(cid)
            if not p:
                continue
            vals = rec["clean"] if len(rec["clean"]) >= 4 else rec["all"]
            if len(vals) < 4:
                continue
            ratios[(cid, kind)] = (sum(vals) / len(vals)) / p
            per_pull_n[(cid, kind)] += len(vals)
        if not ratios:
            continue
        m = sorted(ratios.values())[len(ratios) // 2]
        for k, v in ratios.items():
            per_pull_ratio.setdefault(k, []).append(v / m)
    print("\n########## ACROSS-PULLS POTENCY RATE (normalized; 1.00 = median) ##########")
    print(f"    {'id':>7s}  {'ability':28s} {'kind':6s} {'n':>5s}  {'norm':>6s}  dev")
    for (cid, kind), rs in sorted(per_pull_ratio.items(),
                                  key=lambda kv: -abs(sum(kv[1]) / len(kv[1]) - 1)):
        r = sum(rs) / len(rs)
        flag = "  <-- CHECK" if abs(r - 1) > 0.10 and per_pull_n[(cid, kind)] >= 8 else ""
        print(f"    {cid:7d}  {names.get(cid, '?'):28s} {kind:6s} "
              f"{per_pull_n[(cid, kind)]:5d}  {r:6.3f}  {r - 1:+.1%}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()
    client = _client()

    blob = client.get_rankings(args.enc, class_name=FFLOGS_SUBTYPE,
                               spec_name=FFLOGS_SUBTYPE,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")]
    if not ranks:
        print("no rankings")
        return 1
    agg: dict = {}
    for r in ranks[:args.top]:
        _dump_one(client, r, agg)
    _rate_table(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
