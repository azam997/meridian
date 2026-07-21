"""Probe: deconvolved Dark Knight potency table (player + Esteem), ID-KEYED.

Divides each damage event's amount by the FFLogs `multiplier` field so every
ability lands on the same per-potency scale. Keyed by ability ID (names
collide: Esteem's Bloodspiller/Edge/Shadowbringer/Disesteem share names with
player actions under different ids), with calculateddamage+damage PAIRS
deduped in BOTH streams (gotcha #11 — and the owner stream also carries the
pet's events, so pet ids are excluded from the player table).

Anchored on Edge of Shadow 460 (the cleanest single-potency player id).
Also prints a DoT tick table (Salted Earth: ticks per cast + per-tick rate)
and the pet-vs-player K ratio (the fold-bias lever).

Run from python/:
    python scripts/probe_darkknight_potency.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "DarkKnight"

# ⚠️ candidates (wiki 7.x + first-probe adjustments), keyed by id.
PLAYER_CANDIDATES: dict[int, tuple[str, int]] = {
    3617:  ("Hard Slash", 300),
    3623:  ("Syphon Strike", 380),        # combo'd
    3632:  ("Souleater", 480),            # combo'd (measured ~487 — watch)
    7392:  ("Bloodspiller", 600),
    36928: ("Scarlet Delirium", 620),
    36929: ("Comeuppance", 720),
    36930: ("Torcleaver", 820),
    36932: ("Disesteem", 1000),
    3624:  ("Unmend", 150),
    16470: ("Edge of Shadow", 460),       # ANCHOR
    25756: ("Salt and Darkness", 500),    # damage id (cast id 25755)
    3643:  ("Carve and Spit", 540),
    25757: ("Shadowbringer", 600),
}
PET_CANDIDATES: dict[int, tuple[str, int]] = {
    17904: ("Abyssal Drain (pet)", 420),
    25881: ("Shadowbringer (pet)", 570),
    17908: ("Edge of Shadow (pet)", 420),
    17909: ("Bloodspiller (pet)", 420),
    36933: ("Disesteem (pet)", 620),
}
ANCHOR_ID = 16470  # Edge of Shadow 460


def dedup(events: list[dict]) -> list[dict]:
    """Drop calculateddamage/damage duplicates: same (id, ts, target, amount)."""
    seen: set[tuple] = set()
    out = []
    for ev in sorted(events, key=lambda x: (x.get("timestamp", 0),
                                            0 if x.get("type") == "damage" else 1)):
        key = (ev.get("abilityGameID"), ev.get("timestamp"),
               ev.get("targetID"), ev.get("targetInstance"), ev.get("amount"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


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

    rates: dict[int, list[float]] = defaultdict(list)      # clean player hits
    pet_rates: dict[int, list[float]] = defaultdict(list)  # clean pet hits
    tick_rates: dict[int, list[float]] = defaultdict(list)
    ticks_per_cast: dict[int, list[int]] = defaultdict(list)
    names: dict[int, str] = {}
    counts: dict[int, int] = Counter()      # deduped player direct-hit counts
    cast_counts: dict[int, int] = Counter()

    for r in ranks:
        code, fid = r["report"]["code"], r["report"]["fightID"]
        report = client.get_report_summary(code)
        fight = next(f for f in report["fights"] if f["id"] == fid)
        friendly = set(fight.get("friendlyPlayers") or [])
        actors = [a for a in report["masterData"]["actors"]
                  if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
                  and a["id"] in friendly]
        if not actors:
            continue
        who = next((a for a in actors
                    if a["name"].lower() == (r.get("name") or "").lower()), actors[0])
        aid = who["id"]
        names.update({a["gameID"]: a["name"]
                      for a in (report["masterData"].get("abilities") or [])})
        s, e = fight["startTime"], fight["endTime"]

        raw_casts = client.get_events(code, s, e, aid, data_type="Casts")
        for ev in raw_casts:
            if ev.get("type") == "cast":
                cast_counts[ev.get("abilityGameID")] += 1

        pets = [a for a in report["masterData"]["actors"]
                if a["type"] == "Pet" and a.get("petOwner") == aid]
        pet_dmg: list[dict] = []
        for p in pets:
            pet_dmg.extend(client.get_events(code, s, e, p["id"],
                                             data_type="DamageDone"))
        pet_dmg = dedup(pet_dmg)
        pet_ids = {ev.get("abilityGameID") for ev in pet_dmg}

        def eat(events, into, *, skip_ids=frozenset()):
            for ev in events:
                cid = ev.get("abilityGameID")
                if cid in skip_ids:
                    continue
                amt = ev.get("amount", 0)
                mult = ev.get("multiplier")
                if amt <= 0 or not mult:
                    continue
                if ev.get("tick"):
                    tick_rates[cid].append(amt / mult)
                    continue
                counts[cid] += 1
                if ev.get("hitType") == 1 and not ev.get("directHit"):
                    into[cid].append(amt / mult)

        player_dmg = dedup(client.get_events(code, s, e, aid, data_type="DamageDone"))
        eat(player_dmg, rates, skip_ids=pet_ids)
        eat(pet_dmg, pet_rates)

        # ticks per Salted Earth cast (fold count)
        se_casts = sorted((ev.get("timestamp", s) - s) / 1000.0
                          for ev in raw_casts
                          if ev.get("type") == "cast" and ev.get("abilityGameID") == 3639)
        tick_ts = sorted((ev.get("timestamp", s) - s) / 1000.0
                         for ev in player_dmg
                         if ev.get("tick") and ev.get("amount", 0) > 0)
        for i, ct in enumerate(se_casts):
            nxt = se_casts[i + 1] if i + 1 < len(se_casts) else float("inf")
            ticks_per_cast[3639].append(sum(1 for t in tick_ts if ct <= t < min(nxt, ct + 16.0)))

    def mean(v):
        return sum(v) / len(v) if v else float("nan")

    k = mean(rates[ANCHOR_ID]) / PLAYER_CANDIDATES[ANCHOR_ID][1]
    print(f"anchor: Edge of Shadow 460 -> K={k:.2f} dmg/potency "
          f"(n={len(rates[ANCHOR_ID])})")

    print(f"\n=== PLAYER (id-keyed, deduped) ===\n"
          f"{'id':>6s} {'ability':22s} {'casts':>5s} {'hits':>5s} {'clean':>5s}  "
          f"{'implied_p':>9s}  (cand)  dev")
    for cid, (nm, cand) in sorted(PLAYER_CANDIDATES.items(), key=lambda kv: kv[1][0]):
        if not rates[cid]:
            continue
        m = mean(rates[cid])
        print(f"{cid:6d} {nm:22s} {cast_counts.get(cid, 0):5d} {counts[cid]:5d} "
              f"{len(rates[cid]):5d}  {m / k:9.1f}  ({cand:4d})  "
              f"{m / k / cand - 1:+.1%}")
    others = [cid for cid in rates if cid not in PLAYER_CANDIDATES]
    for cid in others:
        m = mean(rates[cid])
        print(f"{cid:6d} {names.get(cid, '?'):22s} {cast_counts.get(cid, 0):5d} "
              f"{counts[cid]:5d} {len(rates[cid]):5d}  {m / k:9.1f}  (  ? )")

    print(f"\n=== ESTEEM (pet K vs player K = fold-bias lever) ===\n"
          f"{'id':>6s} {'ability':22s} {'clean':>5s}  {'implied_p@K':>11s}  (cand)  petK/K")
    for cid, (nm, cand) in sorted(PET_CANDIDATES.items(), key=lambda kv: kv[1][0]):
        if not pet_rates[cid]:
            continue
        m = mean(pet_rates[cid])
        print(f"{cid:6d} {nm:22s} {len(pet_rates[cid]):5d}  {m / k:11.1f}  "
              f"({cand:4d})  {m / k / cand:.3f}")

    print("\n=== DOT TICKS ===")
    for cid, vals in tick_rates.items():
        tpc = ticks_per_cast.get(cid) or ticks_per_cast.get(3639) or []
        print(f"    {cid:6d} {names.get(cid, '?'):22s} ticks={len(vals)}  "
              f"per-tick implied_p={mean(vals) / k:.1f}  "
              f"ticks/cast={tpc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
