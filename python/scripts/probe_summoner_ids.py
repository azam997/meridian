"""One-off: dump authoritative Summoner action ids + mechanics from real
top-SMN pulls, for building jobs/summoner/data.py. Prints:

  * cast counts (id  name  xN) — the id table, straight from masterData,
  * the opening cast sequence (canonical-opener consensus) incl. the pre-pull
    Ruin III hardcast + first in-fight GCD time (pins PREPULL_RESIDUAL_S),
  * begincast->cast deltas per ability (verifies the cast-time table: Ruin III
    1.5s, Ruby Rite 2.8s, Slipstream 3.0s; instants surface as absent),
  * per-ability RECASTS from mono-phase consecutive-cast gaps (Emerald ~1.5s,
    Topaz ~2.5s, Ruby ~3.0s — the gcd_recast_mult table),
  * the demi cycle: ordered demi-summon casts + gaps (shared 60s recast?),
    expected order Solar -> Bahamut -> Solar -> Phoenix,
  * per demi window: impulse / Enkindle / flare counts, PET auto counts +
    auto timestamp spacing (pins the k×auto_potency fold per demi kind),
  * pet actors (petOwner-filtered) with per-ability damage totals — settles
    which ids log under the pet (Wyrmwave/Akh Morn/Inferno...) vs the player,
  * primal phases: rites per summon (2/4/4?), Mountain Buster == Topaz?,
    Cyclone/Strike/Slipstream favor economy, primal order per cycle,
  * economy closures: Necrotize == 2x Energy Drain, Ruin IV <= Energy Drain,
    Searing Flash == Searing Light, primal summons == 3x demi summons,
  * GCD cadence inside vs outside demi windows (any hidden haste?),
  * buff applies on the player + first apply->remove durations (attunement,
    favors, Further Ruin, Searing Light, Rekindle...),
  * pet buff inheritance: pet-auto clean damage rate inside vs outside the
    player's Searing Light windows (the snapshot-bias question),
  * multi-target falloff ratios + an ACROSS-PULLS per-potency damage-rate
    table (player AND pet abilities, name-keyed candidates).

Run from python/:
    python scripts/probe_summoner_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Summoner"
FFLOGS_SUBTYPE = "Summoner"

# Candidate potencies keyed by ABILITY NAME (ids come from masterData at
# runtime). ⚠️ wiki 7.x L100 values, pre-verification. Pet-dealt abilities
# included — the rate table works off damage events regardless of source.
CANDIDATE_POTENCIES: dict[str, int] = {
    # filler / procs
    "Ruin III":                 360,
    "Ruin IV":                  490,
    "Tri-disaster":             240,
    # demi window GCD fillers
    "Astral Impulse":           500,
    "Umbral Impulse":           620,
    "Fountain of Fire":         580,
    "Astral Flare":             180,
    "Umbral Flare":             280,
    "Brand of Purgatory":       240,
    # demi oGCDs (player-cast)
    "Deathflare":               500,
    "Sunflare":                 600,
    # Enkindle payoffs (pet-dealt)
    "Akh Morn":                1300,
    "Revelation":              1300,
    "Exodus":                  1300,
    # demi autos (pet-dealt)
    "Wyrmwave":                 150,
    "Scarlet Flame":            150,
    "Luxwave":                  160,
    # primal summon payoffs
    "Inferno":                  800,
    "Earthen Fury":             800,
    "Aerial Blast":             800,
    # attunement rites
    "Ruby Rite":                630,
    "Topaz Rite":               340,
    "Emerald Rite":             240,
    "Ruby Catastrophe":         210,
    "Topaz Catastrophe":        140,
    "Emerald Catastrophe":      100,
    # favors
    "Crimson Cyclone":          490,
    "Crimson Strike":           590,
    "Mountain Buster":          160,
    "Slipstream":               490,
    # aetherflow
    "Energy Drain":             200,
    "Energy Siphon":            100,
    "Necrotize":                460,
    "Painflare":                150,
    # Searing
    "Searing Flash":            600,
}

DEMI_SUMMONS = ("Summon Solar Bahamut", "Summon Bahamut", "Summon Phoenix")
PRIMAL_SUMMONS = ("Summon Ifrit II", "Summon Titan II", "Summon Garuda II",
                  "Summon Ifrit", "Summon Titan", "Summon Garuda")
RITES = ("Ruby Rite", "Topaz Rite", "Emerald Rite",
         "Ruby Catastrophe", "Topaz Catastrophe", "Emerald Catastrophe")
IMPULSES = ("Astral Impulse", "Umbral Impulse", "Fountain of Fire",
            "Astral Flare", "Umbral Flare", "Brand of Purgatory")
ENKINDLES = ("Enkindle Bahamut", "Enkindle Phoenix", "Enkindle Solar Bahamut")
FLARES = ("Deathflare", "Sunflare", "Rekindle")
PET_AUTOS = ("Wyrmwave", "Scarlet Flame", "Luxwave")
PET_PAYOFFS = ("Akh Morn", "Revelation", "Exodus",
               "Inferno", "Earthen Fury", "Aerial Blast")

# Names that are NOT GCDs (for the cadence scan). Summons, rites, impulses,
# Cyclone/Strike, Slipstream, Ruin III/IV ARE GCDs and stay in.
OGCD_NAMES = {
    "Enkindle Bahamut", "Enkindle Phoenix", "Enkindle Solar Bahamut",
    "Deathflare", "Sunflare", "Rekindle",
    "Energy Drain", "Energy Siphon", "Necrotize", "Painflare", "Fester",
    "Mountain Buster", "Searing Light", "Searing Flash",
    "Radiant Aegis", "Lux Solaris", "Aethercharge",
    "Swiftcast", "Addle", "Lucid Dreaming", "Surecast",
    "Sprint", "Medicated",
}

DEMI_WINDOW_S = 18.0     # generous scan window; the real length prints below
SEARING_WINDOW_S = 20.0


def med(v):
    return sorted(v)[len(v) // 2] if v else float("nan")


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

    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print("\n  === CASTS (id  name  xN) ===")
    for cid, n in cc.most_common():
        print(f"    {cid:7d}  {name_of(cid):28s} x{n}")

    print("\n  === OPENER (first 40 casts, t in s; b=begincast) ===")
    opener = sorted(raw, key=lambda ev: ev.get("timestamp", s))[:60]
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

    # --- per-ability RECASTS from consecutive same-ability runs ---------------
    # Mono-phase kits (4x Topaz in a row) expose the recast as the gap.
    print("\n  === RECASTS (consecutive same-ability cast gaps, mono-phase runs) ===")
    times_by_name: dict[str, list[float]] = {}
    for ev in casts:
        nm = name_of(ev.get("abilityGameID"))
        times_by_name.setdefault(nm, []).append((ev.get("timestamp", s) - s) / 1000.0)
    for nm in ("Emerald Rite", "Topaz Rite", "Ruby Rite", "Ruin III",
               "Astral Impulse", "Umbral Impulse", "Fountain of Fire",
               "Emerald Catastrophe", "Topaz Catastrophe", "Ruby Catastrophe"):
        ts = sorted(times_by_name.get(nm, []))
        gaps = [b - a for a, b in zip(ts, ts[1:]) if 0.5 <= b - a <= 6.0]
        if gaps:
            print(f"    {nm:24s} n={len(gaps):3d}  median={med(gaps):.2f}s  "
                  f"min={min(gaps):.2f}  max={max(gaps):.2f}")

    # --- Demi cycle ------------------------------------------------------------
    demis = sorted(((ev.get("timestamp", s) - s) / 1000.0,
                    name_of(ev.get("abilityGameID")))
                   for ev in casts if name_of(ev.get("abilityGameID")) in DEMI_SUMMONS)
    print("\n  === DEMI CYCLE (t  name; gap to previous) ===")
    prev = None
    for t, nm in demis:
        gap = f"  (+{t - prev:.1f}s)" if prev is not None else ""
        print(f"    {t:7.1f}  {nm}{gap}")
        prev = t
    order = [nm.replace("Summon ", "") for _, nm in demis]
    print(f"    order: {' -> '.join(order)}")

    # --- Pet actors + per-ability damage ---------------------------------------
    pets = [a for a in report["masterData"]["actors"]
            if a["type"] == "Pet" and a.get("petOwner") == aid]
    print(f"\n  === PET ACTORS (petOwner={aid}): "
          f"{[(p['id'], p['name']) for p in pets]} ===")
    pet_dmg: list[dict] = []
    for p in pets:
        pet_dmg.extend(client.get_events(code, s, e, p["id"], data_type="DamageDone"))
    pdc = Counter(name_of(ev.get("abilityGameID")) for ev in pet_dmg
                  if ev.get("amount", 0) > 0)
    print("  === PET DAMAGE EVENTS (name xN) ===")
    for nm, n in pdc.most_common():
        print(f"    {nm:28s} x{n}")

    # --- Demi windows: player + pet content per summon --------------------------
    print("\n  === DEMI WINDOWS (per summon: impulses/Enkindle/flare + pet events) ===")
    pet_ts = sorted((ev.get("timestamp", s) - s) / 1000.0
                    for ev in pet_dmg if ev.get("amount", 0) > 0)
    for w_i, (t, nm) in enumerate(demis):
        kind = nm.replace("Summon ", "")
        inside = [name_of(ev.get("abilityGameID")) for ev in casts
                  if t <= (ev.get("timestamp", s) - s) / 1000.0 < t + DEMI_WINDOW_S]
        c = Counter(inside)
        n_imp = sum(v for k, v in c.items() if k in IMPULSES)
        n_enk = sum(v for k, v in c.items() if k in ENKINDLES)
        n_flare = sum(v for k, v in c.items() if k in FLARES)
        pin = [pt for pt in pet_ts if t <= pt < t + DEMI_WINDOW_S]
        pnames = Counter(name_of(ev.get("abilityGameID")) for ev in pet_dmg
                         if ev.get("amount", 0) > 0
                         and t <= (ev.get("timestamp", s) - s) / 1000.0 < t + DEMI_WINDOW_S)
        autos = sum(v for k, v in pnames.items() if k in PET_AUTOS)
        payoffs = {k: v for k, v in pnames.items() if k in PET_PAYOFFS}
        last_imp = max(((ev.get("timestamp", s) - s) / 1000.0 for ev in casts
                        if name_of(ev.get("abilityGameID")) in IMPULSES
                        and t <= (ev.get("timestamp", s) - s) / 1000.0 < t + DEMI_WINDOW_S),
                       default=t)
        print(f"    {t:7.1f}  {kind:14s} impulses x{n_imp}  enkindle x{n_enk}  "
              f"flare x{n_flare}  pet-autos x{autos}  payoffs={dict(payoffs)}  "
              f"last-impulse +{last_imp - t:.1f}s")
        if w_i == 0 and pin:
            spacing = [round(b - a, 2) for a, b in zip(pin, pin[1:])]
            print(f"      first-window pet event offsets: "
                  f"{[round(pt - t, 2) for pt in pin]}  spacing={spacing}")

    # --- Primal phases -----------------------------------------------------------
    prims = sorted(((ev.get("timestamp", s) - s) / 1000.0,
                    name_of(ev.get("abilityGameID")))
                   for ev in casts if name_of(ev.get("abilityGameID")) in PRIMAL_SUMMONS)
    print("\n  === PRIMAL PHASES (t  summon; rites/favors until next summon) ===")
    bounds = [t for t, _ in prims] + [dur + 1]
    for i, (t, nm) in enumerate(prims):
        nxt = bounds[i + 1]
        inside = Counter(name_of(ev.get("abilityGameID")) for ev in casts
                         if t < (ev.get("timestamp", s) - s) / 1000.0 < nxt)
        rites = {k: v for k, v in inside.items() if k in RITES}
        fav = {k: v for k, v in inside.items()
               if k in ("Crimson Cyclone", "Crimson Strike", "Slipstream",
                        "Mountain Buster")}
        print(f"    {t:7.1f}  {nm:18s} rites={rites}  favors={fav}")
    per_cycle: list[str] = []
    d_bounds = [t for t, _ in demis] + [dur + 1]
    for i in range(len(demis)):
        cyc = [nm.replace("Summon ", "").replace(" II", "")
               for t, nm in prims if d_bounds[i] < t < d_bounds[i + 1]]
        per_cycle.append("/".join(cyc))
    print(f"    primal order per demi cycle: {per_cycle}")

    # --- Economy closures ---------------------------------------------------------
    n = lambda *names: sum(cnt for cid, cnt in cc.items() if name_of(cid) in names)  # noqa: E731
    print("\n  === ECONOMY CLOSURES ===")
    print(f"    demi x{len(demis)}  primal-summons x{len(prims)} (expect 3x demi-ish)")
    print(f"    Ruby Rite x{n('Ruby Rite', 'Ruby Catastrophe')} vs Ifrit x{n('Summon Ifrit II', 'Summon Ifrit')} (expect 2:1)")
    print(f"    Topaz Rite x{n('Topaz Rite', 'Topaz Catastrophe')} vs Titan x{n('Summon Titan II', 'Summon Titan')} (expect 4:1)")
    print(f"    Emerald Rite x{n('Emerald Rite', 'Emerald Catastrophe')} vs Garuda x{n('Summon Garuda II', 'Summon Garuda')} (expect 4:1)")
    print(f"    Mountain Buster x{n('Mountain Buster')} vs Topaz Rite x{n('Topaz Rite', 'Topaz Catastrophe')} (expect ==)")
    print(f"    Cyclone x{n('Crimson Cyclone')} <= Ifrit x{n('Summon Ifrit II', 'Summon Ifrit')};  "
          f"Strike x{n('Crimson Strike')} <= Cyclone")
    print(f"    Slipstream x{n('Slipstream')} <= Garuda x{n('Summon Garuda II', 'Summon Garuda')}")
    print(f"    Energy Drain x{n('Energy Drain')} + Siphon x{n('Energy Siphon')} -> "
          f"Necrotize x{n('Necrotize', 'Fester')} + Painflare x{n('Painflare')} (expect 2:1)")
    print(f"    Ruin IV x{n('Ruin IV')} <= Energy Drain+Siphon x{n('Energy Drain', 'Energy Siphon')}")
    print(f"    Searing Light x{n('Searing Light')} -> Searing Flash x{n('Searing Flash')} (proc if ==)")
    print(f"    Enkindle x{n(*ENKINDLES)} vs demi x{len(demis)} (expect ==)")

    # --- GCD cadence inside vs outside demi windows -------------------------------
    demi_ts = [t for t, _ in demis]
    in_demi = lambda t: any(a <= t < a + 15.0 for a in demi_ts)  # noqa: E731
    gts = [((ev.get("timestamp", s) - s) / 1000.0) for ev in casts
           if name_of(ev.get("abilityGameID")) not in OGCD_NAMES]
    gaps_in, gaps_out = [], []
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if 1.0 <= g <= 4.5:
            (gaps_in if in_demi(gts[i - 1]) else gaps_out).append(g)
    print(f"\n  === GCD CADENCE: inside demi median {med(gaps_in):.3f}s (n={len(gaps_in)}), "
          f"outside {med(gaps_out):.3f}s (n={len(gaps_out)}) ===")

    # --- Buffs on player ------------------------------------------------------------
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    bc = Counter(ev.get("abilityGameID") for ev in buffs
                 if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, cnt in bc.most_common():
        print(f"    {bid:7d}  {name_of(bid):28s} x{cnt}")

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

    # --- Pet buff inheritance (Searing Light snapshot question) --------------------
    sl_ts = [(ev.get("timestamp", s) - s) / 1000.0 for ev in casts
             if name_of(ev.get("abilityGameID")) == "Searing Light"]
    in_sl = lambda t: any(a <= t < a + SEARING_WINDOW_S for a in sl_ts)  # noqa: E731
    print("\n  === PET AUTO RATE inside vs outside Searing Light (clean hits) ===")
    for nm in PET_AUTOS:
        ins, outs = [], []
        for ev in pet_dmg:
            if name_of(ev.get("abilityGameID")) != nm or ev.get("amount", 0) <= 0:
                continue
            if ev.get("hitType") != 1 or ev.get("directHit"):
                continue
            t = (ev.get("timestamp", s) - s) / 1000.0
            (ins if in_sl(t) else outs).append(ev.get("amount", 0))
        if ins or outs:
            mi, mo = (sum(ins) / len(ins)) if ins else float("nan"), \
                     (sum(outs) / len(outs)) if outs else float("nan")
            ratio = mi / mo if ins and outs else float("nan")
            print(f"    {nm:16s} in n={len(ins):3d} mean={mi:9.1f} | "
                  f"out n={len(outs):3d} mean={mo:9.1f} | in/out={ratio:.3f} "
                  f"(1.05 = inherits)")

    # --- Damage: falloff + rate table -----------------------------------------------
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    all_dmg = dmg + pet_dmg

    hits_by_cast: dict[tuple, list[int]] = {}
    for ev in all_dmg:
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
    for ev in all_dmg:
        cid = ev.get("abilityGameID")
        amt = ev.get("amount", 0)
        if amt <= 0 or ev.get("tick"):
            continue
        clean = ev.get("hitType") == 1 and not ev.get("directHit")
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
