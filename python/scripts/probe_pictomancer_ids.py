"""One-off: dump authoritative Pictomancer action ids + mechanics from real
top-PCT pulls, for building jobs/pictomancer/data.py. Prints:

  * cast counts (id  name  xN) — the id table, straight from masterData,
  * the opening cast sequence (canonical-opener consensus) incl. the pre-pull
    Rainbow Drip hardcast + first in-fight GCD time (pins PREPULL_RESIDUAL_S),
  * begincast->cast deltas per ability (verifies the cast-time table: RGB 1.5s,
    CMY 2.3s, motifs 3.0s, Rainbow Drip 4.0s; instants surface as absent),
  * Starry Muse burst windows: per-window counts (CMY / Comet / hammers /
    Star Prism / Rainbow Drip) + GCD cadence inside vs outside the window
    (Inspiration -25% haste check),
  * hammer economy: Stamp/Brush/Polishing counts vs Striking Muse count (3:1?)
    and the guaranteed crit+DH check on every hammer damage event,
  * muse economy: motif casts vs Living Muse variant casts (repaint 1:1?),
    Mog/Retribution counts vs muse pairs, and Mog->Retribution gaps (do they
    share the 30s portrait recast?),
  * gauge closure: palette (Waters x25 vs paid Subtractives x50) and white
    paint (Waters+Thunders+Drips vs Holy+Comet+conversions) arithmetic,
  * buff applies on the player (Starry Muse, Hyperphantasia, Inspiration,
    Starstruck, Rainbow Bright, Monochrome Tones, Subtractive Palette,
    Hammer Time, Aetherhues, Medicated ...) with first apply->remove durations,
  * an ACROSS-PULLS per-potency damage-rate table (non-crit/non-DH mean amount /
    candidate potency, median-normalized per pull) — flags potency drift.
    Hammers are excluded from the clean-rate table (always crit+DH — they get
    their own section instead).

Run from python/:
    python scripts/probe_pictomancer_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Pictomancer"
FFLOGS_SUBTYPE = "Pictomancer"

# Candidate potencies keyed by ABILITY NAME (ids come from masterData at runtime).
# ⚠️ wiki 7.5 L100 values, pre-verification.
CANDIDATE_POTENCIES: dict[str, int] = {
    "Fire in Red":               490,
    "Aero in Green":             530,
    "Water in Blue":             570,
    "Fire II in Red":            180,
    "Aero II in Green":          200,
    "Water II in Blue":          220,
    "Blizzard in Cyan":          860,
    "Stone in Yellow":           900,
    "Thunder in Magenta":        940,
    "Blizzard II in Cyan":       360,
    "Stone II in Yellow":        380,
    "Thunder II in Magenta":     400,
    "Holy in White":             570,
    "Comet in Black":            940,
    "Hammer Stamp":              560,   # always crit+DH — excluded from rate table
    "Hammer Brush":              580,   # always crit+DH
    "Polishing Hammer":          600,   # always crit+DH
    "Pom Muse":                  800,
    "Winged Muse":               800,
    "Clawed Muse":               800,
    "Fanged Muse":               800,
    "Mog of the Ages":          1000,
    "Retribution of the Madeen": 1100,
    "Star Prism":               1100,
    "Rainbow Drip":             1000,
}
ALWAYS_CRIT_DH_NAMES = {"Hammer Stamp", "Hammer Brush", "Polishing Hammer"}

MOTIF_HINT = "Motif"          # motif casts are matched by substring
MUSE_NAMES = ("Pom Muse", "Winged Muse", "Clawed Muse", "Fanged Muse")
PORTRAIT_NAMES = ("Mog of the Ages", "Retribution of the Madeen")

# Names that are NOT GCDs (for the cadence scan). Motifs/hammers/Star Prism/
# Holy/Comet ARE GCDs and stay in.
OGCD_NAMES = {
    "Pom Muse", "Winged Muse", "Clawed Muse", "Fanged Muse",
    "Mog of the Ages", "Retribution of the Madeen",
    "Striking Muse", "Starry Muse", "Subtractive Palette",
    "Smudge", "Tempera Coat", "Tempera Grassa",
    "Swiftcast", "Addle", "Lucid Dreaming", "Surecast",
    "Sprint", "Medicated",
}

STARRY_WINDOW_S = 20.0


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
        print(f"  (no {JOB} actor in this fight)")
        return
    who = next((a for a in actors if a["name"].lower() == (r.get("name") or "").lower()),
               actors[0])
    aid = who["id"]
    abil = {a["gameID"]: a["name"]
            for a in (report["masterData"].get("abilities") or [])}
    name_of = lambda cid: abil.get(cid, "?")  # noqa: E731
    s, e = fight["startTime"], fight["endTime"]
    dur = (e - s) / 1000.0
    print(f"  duration={dur:.0f}s")

    raw = client.get_events(code, s, e, aid, data_type="Casts")
    casts = [ev for ev in raw if ev.get("type") == "cast"]
    begins = [ev for ev in raw if ev.get("type") == "begincast"]

    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {name_of(cid):28s} x{n}")

    print("\n  === OPENER (first 40 casts, t in s; b=begincast) ===")
    opener = sorted(raw, key=lambda ev: ev.get("timestamp", s))[:55]
    shown = 0
    for ev in opener:
        if shown >= 40:
            break
        t = (ev.get("timestamp", s) - s) / 1000.0
        cid = ev.get("abilityGameID")
        tag = "b" if ev.get("type") == "begincast" else " "
        print(f"    {t:7.2f} {tag} {cid:7d}  {name_of(cid)}")
        if ev.get("type") == "cast":
            shown += 1

    # First in-fight GCD (pins PREPULL_RESIDUAL_S).
    gcd_casts = [ev for ev in casts if name_of(ev.get("abilityGameID")) not in OGCD_NAMES]
    if gcd_casts:
        t0 = (gcd_casts[0].get("timestamp", s) - s) / 1000.0
        second = (gcd_casts[1].get("timestamp", s) - s) / 1000.0 if len(gcd_casts) > 1 else float("nan")
        print(f"\n  === FIRST GCDs: first={t0:.2f}s ({name_of(gcd_casts[0].get('abilityGameID'))}), "
              f"second={second:.2f}s ===")

    # --- begincast->cast deltas (the cast-time table) -------------------------
    print("\n  === CAST TIMES (begincast->cast median per ability) ===")
    open_b: dict[int, float] = {}
    deltas: dict[int, list[float]] = {}
    for ev in sorted(raw, key=lambda x: x.get("timestamp", 0)):
        cid = ev.get("abilityGameID")
        t = (ev.get("timestamp", s) - s) / 1000.0
        if ev.get("type") == "begincast":
            open_b[cid] = t
        elif ev.get("type") == "cast" and cid in open_b:
            deltas.setdefault(cid, []).append(t - open_b.pop(cid))
    for cid, ds in sorted(deltas.items(), key=lambda kv: -len(kv[1])):
        ds = sorted(ds)
        print(f"    {cid:7d}  {name_of(cid):28s} n={len(ds):3d}  median={ds[len(ds)//2]:.2f}s  "
              f"min={ds[0]:.2f}  max={ds[-1]:.2f}")
    hardcast_ids = set(deltas)
    inst = [cid for cid, n in cc.most_common()
            if cid not in hardcast_ids and name_of(cid) not in OGCD_NAMES and n >= 2]
    print(f"    (GCDs with NO begincast — instant: "
          f"{[(cid, name_of(cid)) for cid in inst]})")

    # --- Starry Muse burst windows --------------------------------------------
    starry_ts = [(ev.get("timestamp", s) - s) / 1000.0 for ev in casts
                 if name_of(ev.get("abilityGameID")) == "Starry Muse"]
    print(f"\n  === STARRY MUSE casts at: {[round(t, 1) for t in starry_ts]} ===")
    in_starry = lambda t: any(a <= t < a + STARRY_WINDOW_S for a in starry_ts)  # noqa: E731
    for a in starry_ts:
        inside = [name_of(ev.get("abilityGameID")) for ev in casts
                  if a <= (ev.get("timestamp", s) - s) / 1000.0 < a + STARRY_WINDOW_S]
        c = Counter(inside)
        cmy = sum(v for k, v in c.items() if k in ("Blizzard in Cyan", "Stone in Yellow",
                                                   "Thunder in Magenta", "Blizzard II in Cyan",
                                                   "Stone II in Yellow", "Thunder II in Magenta"))
        ham = sum(v for k, v in c.items() if k in ALWAYS_CRIT_DH_NAMES)
        print(f"    {a:6.1f}-{a + STARRY_WINDOW_S:6.1f}  CMY x{cmy}  Comet x{c.get('Comet in Black', 0)}  "
              f"hammer x{ham}  StarPrism x{c.get('Star Prism', 0)}  "
              f"Drip x{c.get('Rainbow Drip', 0)}  Holy x{c.get('Holy in White', 0)}  "
              f"SubPalette x{c.get('Subtractive Palette', 0)}")

    # GCD cadence inside vs outside Starry (Inspiration haste check).
    gts = [((ev.get("timestamp", s) - s) / 1000.0) for ev in casts
           if name_of(ev.get("abilityGameID")) not in OGCD_NAMES]
    gaps_in, gaps_out = [], []
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if 1.0 <= g <= 4.5:
            (gaps_in if in_starry(gts[i - 1]) else gaps_out).append(g)

    def med(v):
        return sorted(v)[len(v) // 2] if v else float("nan")
    print(f"\n  === GCD CADENCE: inside Starry median {med(gaps_in):.3f}s (n={len(gaps_in)}), "
          f"outside {med(gaps_out):.3f}s (n={len(gaps_out)}) ===")

    # --- Hammer / muse / portrait economy --------------------------------------
    n_striking = sum(n for cid, n in cc.items() if name_of(cid) == "Striking Muse")
    n_hammer = sum(n for cid, n in cc.items() if name_of(cid) in ALWAYS_CRIT_DH_NAMES)
    n_motif = {name_of(cid): n for cid, n in cc.items() if MOTIF_HINT in name_of(cid)}
    n_muse = {nm: sum(n for cid, n in cc.items() if name_of(cid) == nm) for nm in MUSE_NAMES}
    n_portrait = {nm: sum(n for cid, n in cc.items() if name_of(cid) == nm)
                  for nm in PORTRAIT_NAMES}
    print(f"\n  === ECONOMY: Striking x{n_striking} -> hammers x{n_hammer} "
          f"(expect 3:1)  motifs={n_motif}  muses={n_muse}  portraits={n_portrait} ===")

    # Mog/Retribution shared-recast probe: gaps between consecutive portrait casts.
    pts = sorted((ev.get("timestamp", s) - s) / 1000.0 for ev in casts
                 if name_of(ev.get("abilityGameID")) in PORTRAIT_NAMES)
    pgaps = [round(b - a, 1) for a, b in zip(pts, pts[1:])]
    print(f"  === PORTRAIT GAPS (shared 30s recast if min >= ~30): {pgaps} ===")

    # --- Gauge closure ----------------------------------------------------------
    n_water = sum(n for cid, n in cc.items()
                  if name_of(cid) in ("Water in Blue", "Water II in Blue"))
    n_thunder = sum(n for cid, n in cc.items()
                    if name_of(cid) in ("Thunder in Magenta", "Thunder II in Magenta"))
    n_drip = sum(n for cid, n in cc.items() if name_of(cid) == "Rainbow Drip")
    n_sub = sum(n for cid, n in cc.items() if name_of(cid) == "Subtractive Palette")
    n_starry = len(starry_ts)
    n_holy = sum(n for cid, n in cc.items() if name_of(cid) == "Holy in White")
    n_comet = sum(n for cid, n in cc.items() if name_of(cid) == "Comet in Black")
    print(f"\n  === GAUGE CLOSURE ===")
    print(f"    palette: waters x{n_water} (+{25 * n_water}) vs subtractives x{n_sub} "
          f"(paid <= x{n_sub - n_starry} free~x{n_starry}; paid cost {50 * max(0, n_sub - n_starry)})")
    print(f"    white paint: gained ~{n_water + n_thunder + n_drip} "
          f"(W x{n_water} + T x{n_thunder} + Drip x{n_drip}) vs spent "
          f"{n_holy + n_comet} (Holy x{n_holy} + Comet x{n_comet}; Comet also eats 1 white via conversion)")

    # --- Buffs on player -----------------------------------------------------
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, n in bc.most_common():
        print(f"    {bid:7d}  {name_of(bid):28s} x{n}")

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
                print(f"    {bid:7d}  {name_of(bid):28s} "
                      f"{open_t[bid]:7.2f} -> {t:7.2f}  ({t - open_t[bid]:5.2f}s)")
                seen.add(bid)
            del open_t[bid]

    # --- Damage --------------------------------------------------------------
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")

    # Hammer guaranteed crit+DH check: hitType==2 is crit; directHit flag.
    print("\n  === HAMMER CRIT+DH CHECK (expect 100% crit, 100% DH) ===")
    for nm in sorted(ALWAYS_CRIT_DH_NAMES):
        evs = [ev for ev in dmg if name_of(ev.get("abilityGameID")) == nm]
        if not evs:
            continue
        crit = sum(1 for ev in evs if ev.get("hitType") == 2)
        dh = sum(1 for ev in evs if ev.get("directHit"))
        print(f"    {nm:20s} n={len(evs):3d}  crit={crit}  DH={dh}")

    # Multi-target falloff probe: per-ability, same-timestamp multi-hit ratio.
    hits_by_cast: dict[tuple, list[int]] = {}
    for ev in dmg:
        if ev.get("tick") or ev.get("amount", 0) <= 0:
            continue
        key = (ev.get("abilityGameID"), ev.get("timestamp"))
        hits_by_cast.setdefault(key, []).append(ev.get("amount", 0))
    fall: dict[str, list[float]] = {}
    for (cid, _), amts in hits_by_cast.items():
        if len(amts) >= 2:
            amts = sorted(amts, reverse=True)
            fall.setdefault(name_of(cid), []).extend(a / amts[0] for a in amts[1:])
    if fall:
        print("\n  === FALLOFF (secondary/primary amount ratios; crit noise ~±5%) ===")
        for nm, rs in sorted(fall.items()):
            print(f"    {nm:28s} n={len(rs):3d}  median={med(rs):.3f}")

    pull_key = f"{code}#{fid}"
    for ev in dmg:
        cid = ev.get("abilityGameID")
        amt = ev.get("amount", 0)
        if amt <= 0 or name_of(cid) in ALWAYS_CRIT_DH_NAMES:
            continue
        clean = ev.get("hitType") == 1 and not ev.get("directHit")
        # Only primary hits (max amount per timestamp) belong in the rate table;
        # cheap approximation: skip events that are a secondary of a multi-hit.
        rec = agg.setdefault(pull_key, {}).setdefault((cid, "direct"),
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
            p = CANDIDATE_POTENCIES.get(nm)
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
