"""One-off: dump authoritative Dark Knight action ids + mechanics from real
top-DRK pulls, for building jobs/darkknight/data.py. Prints:

  * cast counts (id  name  xN) — the id table, straight from masterData,
  * the opening cast sequence (canonical-opener consensus) + first in-fight GCD,
  * begincast->cast deltas (expect NONE — DRK is all-instant; confirms InstantGCD),
  * per-ability RECASTS from consecutive-cast gaps (Delirium 60, Living Shadow
    120, Salted Earth 90, Carve and Spit 60, Shadowbringer 60x2 charges,
    Salt and Darkness ?; Carve/Abyssal Drain shared-recast check),
  * DELIRIUM WINDOWS: the GCD sequence around each Delirium cast — chain
    composition (Scarlet Delirium -> Comeuppance -> Torcleaver), whether the
    basic combo survives interleaving, stack expiry,
  * LIVING SHADOW WINDOWS: pet (Esteem) actor resolution via masterData
    petOwner, per-window pet ability sequence + ids + timing (the fixed-count
    fold), Disesteem placement (Scorn), whether LS still costs Blood (economy),
  * BLOOD ECONOMY CLOSURE: 20x(Souleater+Stalwart) vs 50x(Bloodspiller+Quietus)
    [+ LS?]; the per-Delirium slack solves the 7.x Blood-Weapon-fold question,
  * MP ECONOMY CLOSURE: 3000x(Edge+Flood) vs 10000 + ticks + 600x(Syphon+
    Stalwart+Carve) + X x(chain GCDs) + 3000 x DarkArts — solved against the
    TBN-pop count to pin X (per-chain-GCD MP grant) and Dark Arts sourcing,
  * TBN: each application apply->remove duration; early removal (<6.9s) = pop
    (the Dark Arts grant candidate), expiry (~7s) = no pop,
  * DARKSIDE: per-token coverage % over the player's own DamageDone `buffs`
    snapshots (the ~100% token IS the Darkside status id), and the same scan
    over Esteem's hits (pet inheritance question),
  * buff applies + first apply->remove durations (Scorn, Delirium, Darkside...),
  * GCD cadence inside vs outside Delirium windows (expect flat ~2.5 gear GCD),
  * multi-target falloff ratios + an ACROSS-PULLS per-potency damage-rate table.

Run from python/:
    python scripts/probe_darkknight_ids.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Dark Knight"
FFLOGS_SUBTYPE = "DarkKnight"

# Candidate potencies keyed by ABILITY NAME (ids come from masterData at
# runtime). ⚠️ wiki 7.x L100 values, pre-verification. Esteem-dealt abilities
# share names with player actions — the rate table keeps player and pet
# streams separate, so the collision is harmless here.
CANDIDATE_POTENCIES: dict[str, int] = {
    # single-target combo
    "Hard Slash":            340,
    "Syphon Strike":         380,   # combo'd (240 uncombo'd)
    "Souleater":             500,   # combo'd (280 uncombo'd)
    # blood spenders
    "Bloodspiller":          600,
    "Quietus":               240,
    # Delirium chain
    "Scarlet Delirium":      620,
    "Comeuppance":           720,
    "Torcleaver":            820,
    "Impalement":            320,
    # AoE combo
    "Unleash":               120,
    "Stalwart Soul":         160,   # combo'd (120 uncombo'd)
    # ranged / misc GCD
    "Unmend":                150,
    "Disesteem":            1000,
    # oGCDs
    "Edge of Shadow":        460,
    "Flood of Shadow":       160,
    "Salted Earth":           50,   # per tick
    "Salt and Darkness":     500,
    "Carve and Spit":        540,
    "Abyssal Drain":         240,
    "Shadowbringer":         600,
}

CHAIN_GCDS = ("Scarlet Delirium", "Comeuppance", "Torcleaver", "Impalement")
COMBO_GCDS = ("Hard Slash", "Syphon Strike", "Souleater")
BLOOD_SPENDERS = ("Bloodspiller", "Quietus")

# Names that are NOT GCDs (for the cadence scan). Combo, spenders, chain,
# Unmending, Disesteem ARE GCDs and stay in.
OGCD_NAMES = {
    "Edge of Shadow", "Flood of Shadow", "Delirium", "Living Shadow",
    "Salted Earth", "Salt and Darkness", "Carve and Spit", "Abyssal Drain",
    "Shadowbringer", "The Blackest Night", "Oblation", "Dark Mind",
    "Dark Missionary", "Shadow Wall", "Shadowed Vigil", "Living Dead",
    "Grit", "Release Grit", "Shadowstride", "Rampart", "Reprisal", "Provoke",
    "Shirk", "Interject", "Low Blow", "Arm's Length", "Sprint", "Medicated",
}

DELIRIUM_WINDOW_S = 16.0
LS_WINDOW_S = 24.0        # generous; the real Esteem span prints below
TBN_EXPIRY_S = 7.0
MP_TICK_P_3S = 200        # candidate passive tick (verified by the closure)


def med(v):
    return sorted(v)[len(v) // 2] if v else float("nan")


def _token_coverage(dmg_events: list[dict], label: str) -> None:
    """Per-aura-token coverage % over a stream's non-tick hits — the ~100%
    token on the player's own hits is the Darkside status id."""
    hits = [ev for ev in dmg_events if ev.get("amount", 0) > 0 and not ev.get("tick")]
    if not hits:
        print(f"    ({label}: no hits)")
        return
    tok_n: Counter = Counter()
    for ev in hits:
        for tok in str(ev.get("buffs") or "").split("."):
            if tok:
                tok_n[tok] += 1
    print(f"    {label}: {len(hits)} hits; token coverage (top 12):")
    for tok, cnt in tok_n.most_common(12):
        sid = int(tok) - 1000000 if tok.isdigit() and int(tok) >= 1000000 else tok
        print(f"      token={tok}  status_id={sid}  {100.0 * cnt / len(hits):5.1f}%")


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
    t_of = lambda ev: (ev.get("timestamp", s) - s) / 1000.0  # noqa: E731

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
        cid = ev.get("abilityGameID")
        tag = "b" if ev.get("type") == "begincast" else " "
        print(f"    {t_of(ev):7.2f} {tag} {cid:7d}  {name_of(cid)}")
        if ev.get("type") == "cast":
            shown += 1

    gcd_casts = [ev for ev in casts if name_of(ev.get("abilityGameID")) not in OGCD_NAMES]
    if gcd_casts:
        t0, t1 = t_of(gcd_casts[0]), (t_of(gcd_casts[1]) if len(gcd_casts) > 1 else float("nan"))
        print(f"\n  === FIRST GCDs: first={t0:.2f}s ({name_of(gcd_casts[0].get('abilityGameID'))}), "
              f"second={t1:.2f}s ===")

    # --- begincast->cast deltas (expect empty — DRK is all-instant) ----------
    n_begin = sum(1 for ev in raw if ev.get("type") == "begincast")
    print(f"\n  === BEGINCAST EVENTS: {n_begin} (expect 0 — all-instant kit) ===")

    # --- per-ability RECASTS from consecutive same-ability gaps ---------------
    print("\n  === RECASTS (consecutive same-ability cast gaps) ===")
    times_by_name: dict[str, list[float]] = {}
    for ev in casts:
        times_by_name.setdefault(name_of(ev.get("abilityGameID")), []).append(t_of(ev))
    for nm, lo, hi in (("Delirium", 30.0, 200.0), ("Living Shadow", 60.0, 300.0),
                       ("Salted Earth", 45.0, 250.0), ("Salt and Darkness", 5.0, 200.0),
                       ("Carve and Spit", 30.0, 200.0), ("Shadowbringer", 1.0, 200.0),
                       ("Edge of Shadow", 0.5, 120.0), ("The Blackest Night", 5.0, 200.0),
                       ("Hard Slash", 0.5, 6.0), ("Bloodspiller", 0.5, 60.0)):
        ts = sorted(times_by_name.get(nm, []))
        gaps = [round(b - a, 1) for a, b in zip(ts, ts[1:]) if lo <= b - a <= hi]
        if gaps:
            print(f"    {nm:20s} n={len(gaps):3d}  median={med(gaps):.1f}s  "
                  f"min={min(gaps):.1f}  max={max(gaps):.1f}  all={gaps[:14]}")
    # Carve/Abyssal shared recast: interleaved gap analysis
    cas = sorted((t, nm) for nm in ("Carve and Spit", "Abyssal Drain")
                 for t in times_by_name.get(nm, []))
    if len(cas) > 1:
        gaps = [round(b[0] - a[0], 1) for a, b in zip(cas, cas[1:])]
        print(f"    Carve+Abyssal interleaved gaps: {gaps[:16]} "
              f"(all >=60 => shared recast)")

    # --- Delirium windows ------------------------------------------------------
    del_ts = sorted(times_by_name.get("Delirium", []))
    print("\n  === DELIRIUM WINDOWS (GCD seq from 1 before to +16s; *=chain) ===")
    gcd_seq = [(t_of(ev), name_of(ev.get("abilityGameID"))) for ev in casts
               if name_of(ev.get("abilityGameID")) not in OGCD_NAMES]
    for dt in del_ts[:6]:
        before = [nm for t, nm in gcd_seq if t < dt][-2:]
        inside = [(t, nm) for t, nm in gcd_seq if dt <= t < dt + DELIRIUM_WINDOW_S]
        seq = " -> ".join(f"{'*' if nm in CHAIN_GCDS else ''}{nm}" for _, nm in inside)
        print(f"    {dt:7.1f}  [...{' -> '.join(before)}] -> {seq}")
    # chain-step gap check: does the chain complete back-to-back?
    chain_ts = sorted((t, nm) for t, nm in gcd_seq if nm in CHAIN_GCDS)
    runs: list[list[str]] = []
    prev_t = None
    for t, nm in chain_ts:
        if prev_t is None or t - prev_t > 13.0:
            runs.append([])
        runs[-1].append(nm)
        prev_t = t
    print(f"    chain runs: {Counter(tuple(r) for r in runs)}")

    # --- Living Shadow windows + Esteem ----------------------------------------
    pets = [a for a in report["masterData"]["actors"]
            if a["type"] == "Pet" and a.get("petOwner") == aid]
    print(f"\n  === PET ACTORS (petOwner={aid}): "
          f"{[(p['id'], p['name']) for p in pets]} ===")
    pet_dmg: list[dict] = []
    for p in pets:
        pet_dmg.extend(client.get_events(code, s, e, p["id"], data_type="DamageDone"))
    # calculateddamage+damage pairs inflate counts — keep one type only.
    pet_types = Counter(ev.get("type") for ev in pet_dmg)
    keep_type = "damage" if pet_types.get("damage") else "calculateddamage"
    pet_hits = [ev for ev in pet_dmg
                if ev.get("type") == keep_type and ev.get("amount", 0) > 0]
    print(f"  pet event types={dict(pet_types)} (keeping '{keep_type}')")
    pdc = Counter((ev.get("abilityGameID"), name_of(ev.get("abilityGameID")))
                  for ev in pet_hits)
    print("  === PET DAMAGE (id  name  xN, deduped) ===")
    for (cid, nm), n in pdc.most_common():
        print(f"    {cid:7d}  {nm:28s} x{n}")

    ls_ts = sorted(times_by_name.get("Living Shadow", []))
    print("\n  === LIVING SHADOW WINDOWS (per summon: Esteem seq + Disesteem) ===")
    dis_ts = sorted(times_by_name.get("Disesteem", []))
    for lt in ls_ts:
        inside = sorted((t_of(ev), ev.get("abilityGameID"),
                         name_of(ev.get("abilityGameID")), ev.get("amount", 0))
                        for ev in pet_hits if lt <= t_of(ev) < lt + LS_WINDOW_S)
        seq = ", ".join(f"+{t - lt:.1f}s {nm}" for t, _, nm, _ in inside)
        dis = next((d for d in dis_ts if lt <= d < lt + LS_WINDOW_S + 6.0), None)
        dis_s = f"  player-Disesteem +{dis - lt:.1f}s" if dis is not None else "  NO Disesteem"
        print(f"    {lt:7.1f}  pet-hits x{len(inside)}: [{seq}]{dis_s}")
    print(f"    Disesteem x{len(dis_ts)} vs Living Shadow x{len(ls_ts)} (expect ==)")

    # --- Economy closures --------------------------------------------------------
    n = lambda *names: sum(cnt for cid, cnt in cc.items() if name_of(cid) in names)  # noqa: E731
    print("\n  === BLOOD ECONOMY ===")
    gen = 20 * n("Souleater", "Stalwart Soul")
    spend = 50 * n(*BLOOD_SPENDERS)
    n_del = n("Delirium")
    n_ls = n("Living Shadow")
    print(f"    known gen 20x(Souleater+Stalwart)={gen}  spend 50x(spiller+quietus)={spend}")
    print(f"    Delirium x{n_del}  LivingShadow x{n_ls}  chain-GCDs x{n(*CHAIN_GCDS)}")
    for ls_cost in (0, 50):
        slack = spend + ls_cost * n_ls - gen
        per_del = slack / n_del if n_del else float("nan")
        print(f"    if LS costs {ls_cost}: spend-gen slack={slack}  "
              f"=> per-Delirium blood grant ~{per_del:.0f}")

    print("\n  === MP ECONOMY ===")
    mp_spend = 3000 * n("Edge of Shadow", "Flood of Shadow")
    ticks = MP_TICK_P_3S * dur / 3.0
    known = 10000 + ticks + 600 * n("Syphon Strike", "Stalwart Soul", "Carve and Spit")
    n_chain = n(*CHAIN_GCDS)
    n_tbn = n("The Blackest Night")
    print(f"    spend 3000x(Edge+Flood)={mp_spend}   known income=10000+ticks({ticks:.0f})"
          f"+600x(syphon+stalwart+carve)={known:.0f}")
    print(f"    chain GCDs x{n_chain}  TBN casts x{n_tbn}")
    for chain_mp in (200, 500, 600, 800):
        da = (mp_spend - known - chain_mp * n_chain) / 3000.0
        print(f"    if chain grants {chain_mp} MP: implied Dark Arts frees ~{da:.1f} "
              f"(vs TBN casts x{n_tbn})")

    # --- TBN pops (Dark Arts sourcing) ---------------------------------------------
    buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
    print("\n  === TBN APPLICATIONS (apply->remove; <6.9s = popped => Dark Arts) ===")
    tbn_open: dict[int, float] = {}
    pops = expiry = 0
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        nm = name_of(ev.get("abilityGameID"))
        if "Blackest Night" not in nm:
            continue
        t = t_of(ev)
        if ev.get("type") == "applybuff":
            tbn_open[ev.get("abilityGameID")] = t
        elif ev.get("type") == "removebuff" and ev.get("abilityGameID") in tbn_open:
            d = t - tbn_open.pop(ev.get("abilityGameID"))
            popped = d < TBN_EXPIRY_S - 0.1
            pops += popped
            expiry += not popped
            print(f"    {t - d:7.1f} -> {t:7.1f}  ({d:4.2f}s)  {'POP' if popped else 'expired'}")
    print(f"    pops={pops}  expiries={expiry}  (pops ~ Dark Arts grants)")

    # --- Darkside token scan ----------------------------------------------------------
    dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
    print("\n  === DARKSIDE TOKEN SCAN (aura tokens on own hits; ~100% = Darkside) ===")
    _token_coverage(dmg, "player hits")
    _token_coverage(pet_hits, "Esteem hits (inheritance check)")

    # --- Buffs on player -----------------------------------------------------------
    bc = Counter(ev.get("abilityGameID") for ev in buffs if ev.get("type") == "applybuff")
    print("\n  === BUFFS ON PLAYER (applybuff id  name  xN) ===")
    for bid, cnt in bc.most_common():
        print(f"    {bid:7d}  {name_of(bid):28s} x{cnt}")

    print("\n  === BUFF DURATIONS (first apply->remove per status) ===")
    open_t: dict[int, float] = {}
    seen: set[int] = set()
    for ev in sorted(buffs, key=lambda x: x.get("timestamp", 0)):
        bid = ev.get("abilityGameID")
        t = t_of(ev)
        typ = ev.get("type", "")
        if typ == "applybuff" and bid not in open_t:
            open_t[bid] = t
        elif typ == "removebuff" and bid in open_t:
            if bid not in seen:
                print(f"    {bid:7d}  {name_of(bid):28s} "
                      f"{open_t[bid]:7.2f} -> {t:7.2f}  ({t - open_t[bid]:5.2f}s)")
                seen.add(bid)
            del open_t[bid]

    # --- GCD cadence inside vs outside Delirium windows ------------------------------
    in_del = lambda t: any(a <= t < a + DELIRIUM_WINDOW_S for a in del_ts)  # noqa: E731
    gts = [t for t, _ in gcd_seq]
    gaps_in, gaps_out = [], []
    for i in range(1, len(gts)):
        g = gts[i] - gts[i - 1]
        if 1.0 <= g <= 4.5:
            (gaps_in if in_del(gts[i - 1]) else gaps_out).append(g)
    print(f"\n  === GCD CADENCE: inside Delirium median {med(gaps_in):.3f}s "
          f"(n={len(gaps_in)}), outside {med(gaps_out):.3f}s (n={len(gaps_out)}) ===")

    # --- Damage: falloff + rate table ---------------------------------------------------
    all_dmg = dmg + pet_hits
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
