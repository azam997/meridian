"""One-off: dump authoritative Bard action ids + mechanics from real top-BRD pulls,
for building jobs/bard/data.py. Prints:

  * cast counts (id  name  xN) — the id table, straight from masterData,
  * the opening cast sequence (canonical-opener consensus),
  * begincast->cast deltas (BRD should be all-instant; anything else surfaces),
  * the song cycle: WM/MB/AP cast times, per-song window lengths, and per-window
    Pitch Perfect / Heartbreak Shot / Empyreal Arrow counts (repertoire economy:
    deterministic 3s ticks -> PP3 every ~9s under WM; extra HB charges under MB),
  * GCD cadence per song window (Army's Paeon haste + Army's Muse check),
  * Pitch Perfect damage-amount clusters (the 1/2/3-stack potency split),
  * Apex Arrow damage distribution (Soul Voice scaling) + Blast Arrow pairing,
  * buff applies on the player (Raging Strikes, Hawk's Eye, Resonant Arrow Ready,
    Radiant Encore Ready, Blast Arrow Ready, Army's Muse, Medicated ...) with
    first apply->remove durations,
  * an ACROSS-PULLS per-potency damage-rate table (non-crit/non-DH mean amount /
    candidate potency, median-normalized per pull) — flags potency drift. DoT
    ticks (Caustic Bite / Stormbite) get their own rows.

Run from python/:
    python scripts/probe_bard_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Bard"
FFLOGS_SUBTYPE = "Bard"

# Candidate potencies keyed by ABILITY NAME (ids come from masterData at runtime).
# ⚠️ all values are pre-verification guesses (DT 7.x level 100).
CANDIDATE_POTENCIES: dict[str, int] = {
    "Burst Shot":       220,
    "Refulgent Arrow":  280,
    "Caustic Bite":     150,
    "Stormbite":        100,
    "Iron Jaws":        100,
    "Apex Arrow":       600,   # at 100 Soul Voice (scales down)
    "Blast Arrow":      600,
    "Pitch Perfect":    360,   # at 3 stacks (100/220/360)
    "Empyreal Arrow":   260,
    "Sidewinder":       400,
    "Heartbreak Shot":  180,
    "Bloodletter":      180,
    "Resonant Arrow":   600,
    "Radiant Encore":   900,   # at 3 Coda (verify the by-Coda split)
    "Ladonsbite":       110,
    "Shadowbite":       270,
    "Rain of Death":    100,
    "Wide Volley":      140,
}
DOT_TICKS: dict[str, int] = {
    "Caustic Bite": 20,
    "Stormbite":    25,
}
SONG_NAMES = ("The Wanderer's Minuet", "Mage's Ballad", "Army's Paeon")


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
        print("  (no BRD actor in this fight)")
        return
    who = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = who["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    s, e = fight["startTime"], fight["endTime"]
    dur = (e - s) / 1000.0
    print(f"  duration={dur:.0f}s")

    raw = client.get_events(code, s, e, aid, data_type="Casts")
    casts = [ev for ev in raw if ev.get("type") == "cast"]
    begins = [ev for ev in raw if ev.get("type") == "begincast"]

    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {abil.get(cid, '?'):28s} x{n}")

    print("\n  === OPENER (first 30 casts, t in s) ===")
    for ev in casts[:30]:
        t = (ev.get("timestamp", s) - s) / 1000.0
        cid = ev.get("abilityGameID")
        print(f"    {t:7.2f}  {cid:7d}  {abil.get(cid, '?')}")
    if begins:
        print(f"    ({len(begins)} begincast events — BRD should be all-instant; "
              f"ids: {sorted({ev.get('abilityGameID') for ev in begins})})")
    else:
        print("    (no begincast events — all instant, as expected)")

    # --- Song cycle ----------------------------------------------------------
    name_of = lambda cid: abil.get(cid, "?")
    song_casts = [((ev.get("timestamp", s) - s) / 1000.0, name_of(ev.get("abilityGameID")))
                  for ev in casts if name_of(ev.get("abilityGameID")) in SONG_NAMES]
    print("\n  === SONG CYCLE (cast t, name, gap since previous song) ===")
    windows: list[tuple[float, float, str]] = []   # (start, end, song)
    for i, (t, nm) in enumerate(song_casts):
        gap = t - song_casts[i - 1][0] if i else 0.0
        print(f"    {t:7.2f}  {nm:24s} (+{gap:5.1f}s)")
        end = song_casts[i + 1][0] if i + 1 < len(song_casts) else min(t + 45.0, dur)
        windows.append((t, end, nm))

    def win_of(t: float) -> str:
        for a, b, nm in windows:
            if a <= t < b:
                return nm
        return "(none)"

    # Per-window repertoire economy: PP/HB/EA counts inside each song window.
    print("\n  === PER-SONG-WINDOW COUNTS (Pitch Perfect / Heartbreak / Empyreal) ===")
    for a, b, nm in windows:
        inside = [name_of(ev.get("abilityGameID")) for ev in casts
                  if a <= (ev.get("timestamp", s) - s) / 1000.0 < b]
        c = Counter(inside)
        print(f"    {a:6.1f}-{b:6.1f} {nm:24s} len={b - a:5.1f}s  "
              f"PP x{c.get('Pitch Perfect', 0)}  HB x{c.get('Heartbreak Shot', 0) + c.get('Bloodletter', 0)}  "
              f"EA x{c.get('Empyreal Arrow', 0)}  ApexBlast x{c.get('Apex Arrow', 0)}+{c.get('Blast Arrow', 0)}")

    # Pitch Perfect spend cadence under WM (deterministic 3s repertoire -> ~9s PP3).
    pp_ts = [(ev.get("timestamp", s) - s) / 1000.0 for ev in casts
             if name_of(ev.get("abilityGameID")) == "Pitch Perfect"]
    pp_gaps = [round(b - a, 1) for a, b in zip(pp_ts, pp_ts[1:]) if b - a < 20.0]
    print(f"\n  === PITCH PERFECT GAPS (<20s): {Counter(pp_gaps).most_common(8)} ===")

    # GCD cadence per song window (Army's Paeon haste check). GCD = anything that
    # is not in the "likely oGCD" name set (approximate; refined by data.py later).
    ogcd_names = {"Pitch Perfect", "Empyreal Arrow", "Sidewinder", "Heartbreak Shot",
                  "Bloodletter", "Rain of Death", "Barrage", "Raging Strikes",
                  "Battle Voice", "Radiant Finale", "The Wanderer's Minuet",
                  "Mage's Ballad", "Army's Paeon", "Troubadour", "Nature's Minne",
                  "The Warden's Paean", "Repelling Shot", "Sprint", "Medicated"}
    gts = [((ev.get("timestamp", s) - s) / 1000.0) for ev in casts
           if name_of(ev.get("abilityGameID")) not in ogcd_names]
    by_win: dict[str, list[float]] = {}
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if 1.5 <= g <= 3.2:
            by_win.setdefault(win_of(gts[i - 1]), []).append(g)

    def med(v):
        return sorted(v)[len(v) // 2] if v else float("nan")
    print("\n  === GCD CADENCE BY SONG WINDOW (median clean gap) ===")
    for nm, gaps in by_win.items():
        print(f"    {nm:24s} median {med(gaps):.3f}s  (n={len(gaps)})")

    # --- Buffs on player -----------------------------------------------------
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, n in bc.most_common():
        print(f"    {bid:7d}  {abil.get(bid, '?'):28s} x{n}")

    print("\n  === BUFF DURATIONS (first apply->remove per status) ===")
    open_t: dict[int, float] = {}
    seen: set[int] = set()
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        bid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        typ = ev.get("type", "")
        if typ == "applybuff" and bid not in open_t:
            open_t[bid] = t
        elif typ == "removebuff" and bid in open_t:
            if bid not in seen:
                print(f"    {bid:7d}  {abil.get(bid, '?'):28s} "
                      f"{open_t[bid]:7.2f} -> {t:7.2f}  ({t - open_t[bid]:5.2f}s)")
                seen.add(bid)
            del open_t[bid]

    # --- Damage --------------------------------------------------------------
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    # Pitch Perfect amount clusters (stack levels) + Apex Arrow scaling.
    for probe_name in ("Pitch Perfect", "Apex Arrow", "Radiant Encore"):
        amts = sorted(ev.get("amount", 0) for ev in dmg
                      if name_of(ev.get("abilityGameID")) == probe_name
                      and ev.get("hitType") == 1 and not ev.get("directHit")
                      and not ev.get("tick"))
        if amts:
            print(f"\n  === {probe_name} clean amounts (clusters -> potency levels) ===")
            print(f"    {amts}")

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
    """Across-pulls potency-rate table (name-keyed candidate potencies)."""
    names = agg.pop("__names__", {})
    per_pull_ratio: dict[tuple, list[float]] = {}
    per_pull_n: Counter = Counter()
    for pull, rows in agg.items():
        ratios: dict[tuple, float] = {}
        for (cid, kind), rec in rows.items():
            nm = names.get(cid, "?")
            p = DOT_TICKS.get(nm) if kind == "dot" else CANDIDATE_POTENCIES.get(nm)
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
