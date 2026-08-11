"""Probe #2: settle Bard potency questions the coarse rate table couldn't.

For each top pull:
  * builds the player's FLAT-damage buff windows from their own aura stream
    (Raging Strikes x1.15, Radiant Finale x1.06/1.04/1.02, Divination x1.06,
    Starry Muse x1.05, Embolden x1.04, Medicated x~1.08, Mage's Ballad x1.01) and
    divides each damage event by the product at its time -> buff-NORMALIZED
    per-potency rates (crit/DH-rate buffs don't move non-crit non-DH amounts),
  * prints tick-amount HISTOGRAMS for Caustic/Stormbite (bimodal => the "clean"
    hitType filter is letting crit ticks through; the low cluster is truth),
    with the DoT snapshot multiplier divided out (last application before tick),
  * counts Refulgent Arrow DAMAGE EVENTS per cast (Barrage: one 3x event or
    three 1x events?), and prints barraged vs normal Refulgent normalized rates,
  * GCD cadence inside Army's Muse windows (the 10s after Army's Paeon ends) and
    across the Army's Paeon ramp (first 15s vs rest),
  * normalized per-potency table for every damage ability, anchored on Burst
    Shot / Iron Jaws (certain potencies), so Apex max / Blast / Resonant /
    Radiant Encore read directly in potency units.

Run from python/:
    python scripts/probe_bard_rates.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "Bard"

# Status gameID -> flat damage multiplier (crit/DH-rate buffs intentionally absent:
# they don't change non-crit non-DH amounts).
FLAT_BUFFS: dict[int, float] = {
    1000125: 1.15,   # Raging Strikes
    1002964: 1.06,   # Radiant Finale (assume 3 coda; opener 1-coda handled below)
    1001878: 1.06,   # Divination
    1003685: 1.05,   # Starry Muse
    1001297: 1.04,   # Embolden
    1000049: 1.08,   # Medicated (~tier tincture)
    1002217: 1.01,   # Mage's Ballad (party +1%)
    1002912: 1.05,   # Searing Light
    1000786: 1.0,    # Battle Litany (crit rate — no clean-amount effect)
}
# The opener Radiant Finale is played with only 1 coda (+2%), later ones 3 (+6%).
RF_STATUS = 1002964
RF_FIRST_MULT = 1.02

DOT_APPLICATORS = {"Caustic Bite": "Caustic Bite", "Stormbite": "Stormbite"}
CANDIDATES = {  # name -> assumed potency for the normalized table
    "Burst Shot": 220, "Refulgent Arrow": 280, "Iron Jaws": 100,
    "Caustic Bite": 150, "Stormbite": 100, "Heartbreak Shot": 180,
    "Empyreal Arrow": 260, "Sidewinder": 400, "Pitch Perfect": 360,
    "Apex Arrow": 700, "Blast Arrow": 700, "Resonant Arrow": 640,
    "Radiant Encore": 1100,
}


def _windows_from_auras(buffs, s, name_of):
    """status_id -> list[(start_s, end_s)] for the flat buffs."""
    out: dict[int, list[tuple[float, float]]] = defaultdict(list)
    open_t: dict[int, float] = {}
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        bid = ev.get("abilityGameID")
        if bid not in FLAT_BUFFS:
            continue
        t = (ev.get("timestamp", s) - s) / 1000.0
        typ = ev.get("type", "")
        if typ in ("applybuff", "refreshbuff"):
            open_t.setdefault(bid, t)
        elif typ == "removebuff" and bid in open_t:
            out[bid].append((open_t.pop(bid), t))
    for bid, t in open_t.items():
        out[bid].append((t, t + 30.0))
    return out


def _mult_at(t: float, wins: dict[int, list[tuple[float, float]]]) -> float:
    m = 1.0
    for bid, ws in wins.items():
        for i, (a, b) in enumerate(ws):
            if a <= t < b:
                f = FLAT_BUFFS[bid]
                if bid == RF_STATUS and i == 0:
                    f = RF_FIRST_MULT
                m *= f
                break
    return m


def _dump_one(client, r, table: dict) -> None:
    code, fid = r["report"]["code"], r["report"]["fightID"]
    report = client.get_report_summary(code)
    fight = next(f for f in report["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in report["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
              and a["id"] in friendly]
    if not actors:
        return
    who = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = who["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    name_of = lambda cid: abil.get(cid, "?")
    s, e = fight["startTime"], fight["endTime"]
    print(f"\n########## {r.get('name')}  {code}#{fid}  dur={(e - s) / 1000:.0f}s ##########")

    raw = client.get_events(code, s, e, aid, data_type="Casts")
    casts = [((ev.get("timestamp", s) - s) / 1000.0, name_of(ev.get("abilityGameID")))
             for ev in raw if ev.get("type") == "cast"]
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    wins = _windows_from_auras(buffs, s, name_of)
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")

    # --- DoT snapshots: last application (dot cast or Iron Jaws) before each tick.
    apps: dict[str, list[float]] = {"Caustic Bite": [], "Stormbite": []}
    for t, nm in casts:
        if nm in apps:
            apps[nm].append(t)
        elif nm == "Iron Jaws":
            for k in apps:
                apps[k].append(t)
    for k in apps:
        apps[k].sort()

    def snap_t(dot: str, tick_t: float) -> float | None:
        prev = [a for a in apps[dot] if a <= tick_t]
        return prev[-1] if prev else None

    # --- Tick histograms (snapshot-normalized) + hitType breakdown.
    print("\n  === DoT TICKS (snapshot-buff-normalized amounts; hitType/DH mix) ===")
    for dot in ("Caustic Bite", "Stormbite"):
        norm: list[float] = []
        hts: Counter = Counter()
        for ev in dmg:
            if name_of(ev.get("abilityGameID")) != dot or not ev.get("tick"):
                continue
            t = (ev.get("timestamp", s) - s) / 1000.0
            hts[(ev.get("hitType"), bool(ev.get("directHit")))] += 1
            if ev.get("hitType") == 1 and not ev.get("directHit"):
                st = snap_t(dot, t)
                if st is not None:
                    norm.append(ev.get("amount", 0) / _mult_at(st, wins))
        norm.sort()
        n = len(norm)
        if n:
            q = lambda p: norm[int(p * (n - 1))]
            print(f"    {dot}: n={n}  p10={q(.1):.0f} p25={q(.25):.0f} p50={q(.5):.0f} "
                  f"p75={q(.75):.0f} p90={q(.9):.0f}  (bimodal if p75/p25 >> 1.1)")
            print(f"      hitType mix (all ticks): {dict(hts)}")

    # --- Refulgent: damage events per cast (Barrage 3x check).
    ref_casts = [t for t, nm in casts if nm == "Refulgent Arrow"]
    barrage_ts = [t for t, nm in casts if nm == "Barrage"]
    ref_events = [((ev.get("timestamp", s) - s) / 1000.0, ev)
                  for ev in dmg if name_of(ev.get("abilityGameID")) == "Refulgent Arrow"
                  and not ev.get("tick")]
    per_cast = Counter()
    for ct in ref_casts:
        n = sum(1 for t, _e in ref_events if abs(t - ct) <= 1.2)
        per_cast[n] += 1
    barraged = {ct for ct in ref_casts
                if any(0 <= ct - bt <= 3.0 for bt in barrage_ts)}
    print(f"\n  === REFULGENT: dmg events per cast {dict(per_cast)}; "
          f"{len(barraged)} barraged casts ===")
    for label, sel in (("normal", [t for t in ref_casts if t not in barraged]),
                       ("barraged", sorted(barraged))):
        vals = [ev.get("amount", 0) / _mult_at(t, wins)
                for t, ev in ref_events
                if ev.get("hitType") == 1 and not ev.get("directHit")
                and any(abs(t - ct) <= 1.2 for ct in sel)]
        if vals:
            print(f"    {label}: n={len(vals)} mean_norm={sum(vals) / len(vals):.0f}")

    # --- Army's Paeon ramp + Army's Muse cadence.
    songs = [(t, nm) for t, nm in casts
             if nm in ("The Wanderer's Minuet", "Mage's Ballad", "Army's Paeon")]
    ap_windows = []
    for i, (t, nm) in enumerate(songs):
        if nm == "Army's Paeon":
            end = songs[i + 1][0] if i + 1 < len(songs) else t + 45.0
            ap_windows.append((t, end))
    ogcd_names = {"Pitch Perfect", "Empyreal Arrow", "Sidewinder", "Heartbreak Shot",
                  "Rain of Death", "Barrage", "Raging Strikes", "Battle Voice",
                  "Radiant Finale", "The Wanderer's Minuet", "Mage's Ballad",
                  "Army's Paeon", "Troubadour", "Nature's Minne", "The Warden's Paean",
                  "Repelling Shot", "Sprint", "Second Wind", "Head Graze",
                  "Grade 4 Gemdraught of Dexterity [HQ]"}
    gts = [t for t, nm in casts if nm not in ogcd_names]
    ramp, late, muse = [], [], []
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if not (1.5 <= g <= 3.2):
            continue
        for a, b in ap_windows:
            if a <= gts[i - 1] and gts[i] < b:
                (ramp if gts[i] - a <= 15.0 else late).append(g)
            elif b <= gts[i - 1] and gts[i] < b + 10.0:
                muse.append(g)

    def med(v):
        return sorted(v)[len(v) // 2] if v else float("nan")
    print(f"\n  === AP ramp (first 15s) median {med(ramp):.3f}s n={len(ramp)}; "
          f"late-AP {med(late):.3f}s n={len(late)}; MUSE {med(muse):.3f}s n={len(muse)} ===")

    # --- Buff-normalized per-potency table rows.
    for ev in dmg:
        nm = name_of(ev.get("abilityGameID"))
        if nm not in CANDIDATES or ev.get("tick"):
            continue
        if ev.get("hitType") != 1 or ev.get("directHit"):
            continue
        t = (ev.get("timestamp", s) - s) / 1000.0
        table.setdefault(nm, []).append(ev.get("amount", 0) / _mult_at(t, wins))


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
             if r.get("report", {}).get("code")][:args.top]
    table: dict[str, list[float]] = {}
    for r in ranks:
        _dump_one(client, r, table)

    print("\n########## BUFF-NORMALIZED PER-POTENCY (anchor: Burst Shot / Iron Jaws) ##########")
    anchor_vals = []
    for nm in ("Burst Shot", "Iron Jaws"):
        for v in table.get(nm, []):
            anchor_vals.append(v / CANDIDATES[nm])
    anchor = sorted(anchor_vals)[len(anchor_vals) // 2] if anchor_vals else float("nan")
    print(f"    anchor rate: {anchor:.1f} damage/potency  (n={len(anchor_vals)})")
    for nm, vals in sorted(table.items()):
        vals.sort()
        n = len(vals)
        p50 = vals[n // 2]
        p90 = vals[int(0.9 * (n - 1))]
        print(f"    {nm:18s} n={n:4d}  p50={p50 / anchor:6.1f}p  p90={p90 / anchor:6.1f}p"
              f"   (candidate {CANDIDATES[nm]}p)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
