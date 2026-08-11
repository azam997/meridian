"""Probe #2: deconvolved Summoner potency table (player + pet).

The cast-count probe's rate table was contaminated by buff windows (SMN
concentrates Sunflare/Searing Flash/Necrotize inside the 2-min burst, while
the Bahamut/Phoenix pets fire on un-buffed odd minutes). This probe divides
each damage event's amount by the FFLogs `multiplier` field (the product of
the active damage auras on the SOURCE, incl. Medicated), so every ability —
player-cast or pet-dealt — lands on the same per-potency scale.

Prints, per ability: n(clean), mean(amount/multiplier), and the implied true
potency anchored to (Ruin III 360 / Topaz Rite 340 / Mountain Buster 160) —
three independent anchors; disagreement between them exposes anchor error.

Also settles the pet "x2 events" question from probe #1 (Akh Morn/Exodus/
Inferno... all logged 2 hits per cast with same-timestamp equal amounts):
for each pet ability, groups events by timestamp and prints group size +
unique targetIDs per group, plus the fight's enemyNPCs — duplicated event vs
genuine double-hit vs second target.

Run from python/:
    python scripts/probe_summoner_potency.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "Summoner"

CANDIDATES: dict[str, int] = {
    "Ruin III": 360, "Ruin IV": 490,
    "Astral Impulse": 500, "Umbral Impulse": 620, "Fountain of Fire": 580,
    "Deathflare": 500, "Sunflare": 600,
    "Ruby Rite": 630, "Topaz Rite": 340, "Emerald Rite": 240,
    "Crimson Cyclone": 490, "Crimson Strike": 590,
    "Mountain Buster": 160, "Slipstream": 490,
    "Energy Drain": 200, "Necrotize": 460,
    "Searing Flash": 600,
    # pet-dealt
    "Wyrmwave": 150, "Scarlet Flame": 150, "Luxwave": 160,
    "Akh Morn": 1300, "Revelation": 1300, "Exodus": 1300,
    "Inferno": 800, "Earthen Fury": 800, "Aerial Blast": 800,
}
ANCHOR_NAMES = ("Ruin III", "Topaz Rite", "Mountain Buster")
PET_NAMES = ("Wyrmwave", "Scarlet Flame", "Luxwave", "Akh Morn", "Revelation",
             "Exodus", "Inferno", "Earthen Fury", "Aerial Blast")


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

    rates: dict[str, list[float]] = defaultdict(list)      # clean player hits
    pet_rates: dict[str, list[float]] = defaultdict(list)  # clean pet hits
    no_mult = 0
    group_sizes: dict[str, Counter] = defaultdict(Counter)     # name -> {size: n}
    group_targets: dict[str, Counter] = defaultdict(Counter)   # name -> {unique targets in group: n}
    casts_per_pull: dict[str, Counter] = defaultdict(Counter)  # payoff-events vs enkindle casts

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
        abil = {a["gameID"]: a["name"]
                for a in (report["masterData"].get("abilities") or [])}
        name_of = lambda cid: abil.get(cid, "?")  # noqa: E731
        s, e = fight["startTime"], fight["endTime"]

        enemies = fight.get("enemyNPCs") or []
        print(f"pull {code}#{fid}: enemyNPCs={[(en.get('id'), en.get('gameID')) for en in enemies]}")

        def eat(events, into):
            nonlocal no_mult
            for ev in events:
                nm = name_of(ev.get("abilityGameID"))
                amt = ev.get("amount", 0)
                if amt <= 0 or ev.get("tick"):
                    continue
                mult = ev.get("multiplier")
                if not mult:
                    no_mult += 1
                    continue
                if ev.get("hitType") == 1 and not ev.get("directHit"):
                    into[nm].append(amt / mult)

        eat(client.get_events(code, s, e, aid, data_type="DamageDone"), rates)

        pets = [a for a in report["masterData"]["actors"]
                if a["type"] == "Pet" and a.get("petOwner") == aid]
        pet_dmg: list[dict] = []
        for p in pets:
            pet_dmg.extend(client.get_events(code, s, e, p["id"],
                                             data_type="DamageDone"))
        eat(pet_dmg, pet_rates)

        # Same-timestamp grouping: duplicate vs double-hit vs multi-target.
        by_key: dict[tuple, list[dict]] = defaultdict(list)
        for ev in pet_dmg:
            if ev.get("amount", 0) <= 0 or ev.get("tick"):
                continue
            nm = name_of(ev.get("abilityGameID"))
            if nm in PET_NAMES:
                by_key[(nm, ev.get("timestamp"))].append(ev)
        for (nm, _), evs in by_key.items():
            group_sizes[nm][len(evs)] += 1
            group_targets[nm][len({(ev.get("targetID"), ev.get("targetInstance"))
                                   for ev in evs})] += 1

        # payoff events per triggering cast
        raw = client.get_events(code, s, e, aid, data_type="Casts")
        cc = Counter(name_of(ev.get("abilityGameID")) for ev in raw
                     if ev.get("type") == "cast")
        pc = Counter(nm for ev in pet_dmg if ev.get("amount", 0) > 0
                     and not ev.get("tick")
                     for nm in [name_of(ev.get("abilityGameID"))])
        for trigger, payoff in (("Enkindle Bahamut", "Akh Morn"),
                                ("Enkindle Phoenix", "Revelation"),
                                ("Enkindle Solar Bahamut", "Exodus"),
                                ("Summon Ifrit II", "Inferno"),
                                ("Summon Titan II", "Earthen Fury"),
                                ("Summon Garuda II", "Aerial Blast"),
                                ("Summon Bahamut", "Wyrmwave"),
                                ("Summon Phoenix", "Scarlet Flame"),
                                ("Summon Solar Bahamut", "Luxwave")):
            if cc.get(trigger):
                casts_per_pull[payoff][f"{pc.get(payoff, 0)}ev/{cc[trigger]}cast"] += 1

    def mean(v):
        return sum(v) / len(v) if v else float("nan")

    anchor_rates = {nm: mean(rates[nm]) / CANDIDATES[nm]
                    for nm in ANCHOR_NAMES if rates[nm]}
    k = mean(list(anchor_rates.values()))
    print(f"\nanchor K (dmg per potency): {k:.2f}   per-anchor: "
          f"{ {nm: round(v, 2) for nm, v in anchor_rates.items()} }   "
          f"(events lacking multiplier: {no_mult})")

    print(f"\n{'ability':22s} {'n':>4s}  {'amt/mult':>9s}  {'norm':>6s}  implied_p (cand)")
    for nm, cand in sorted(CANDIDATES.items(), key=lambda kv: kv[0]):
        src = pet_rates if nm in PET_NAMES else rates
        if not src[nm]:
            continue
        m = mean(src[nm])
        tag = " [pet]" if nm in PET_NAMES else ""
        print(f"{nm:22s} {len(src[nm]):4d}  {m:9.1f}  {m / (k * cand):6.3f}  "
              f"{m / k:7.1f}  ({cand}){tag}")

    print("\n=== PET SAME-TIMESTAMP GROUPS (size xN | unique-targets xN) ===")
    for nm in PET_NAMES:
        if group_sizes[nm]:
            print(f"    {nm:16s} sizes={dict(group_sizes[nm])}  "
                  f"uniq_targets={dict(group_targets[nm])}")

    print("\n=== PAYOFF EVENTS PER TRIGGER CAST (per pull) ===")
    for nm, c in casts_per_pull.items():
        print(f"    {nm:16s} {dict(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
