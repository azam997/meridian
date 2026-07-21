"""Probe #2: deconvolved Pictomancer potency table.

The cast-count probe's rate table was contaminated by buff windows (PCT
concentrates its big oGCDs inside the 6-buff burst). This probe divides each
damage event's amount by the FFLogs `multiplier` field (the product of the
active damage auras, incl. Medicated), so every ability lands on the same
per-potency scale regardless of when it was cast.

Prints, per ability: n(clean), mean(amount/multiplier), and the implied true
potency anchored to the RGB trio (Fire/Aero/Water assumed 490/530/570 — if the
anchor itself is off, the K column exposes it as a uniform drift).

Hammers (always crit+DH) get their own section: their per-potency rate divided
by the clean baseline = the empirical GUARANTEED_CRIT_DH multiplier, and the
560:580:600 split is checked within the trio.

Also prints CMY begincast->begincast gaps split by Starry window (recast
3.3s unhasted vs 2.475s hasted check) and confirms 34682 (Star Prism follow-up)
deals no damage.

Run from python/:
    python scripts/probe_pictomancer_potency.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

FFLOGS_SUBTYPE = "Pictomancer"

CANDIDATES: dict[str, int] = {
    "Fire in Red": 490, "Aero in Green": 530, "Water in Blue": 570,
    "Blizzard in Cyan": 860, "Stone in Yellow": 900, "Thunder in Magenta": 940,
    "Holy in White": 570, "Comet in Black": 940,
    "Pom Muse": 800, "Winged Muse": 800, "Clawed Muse": 800, "Fanged Muse": 800,
    "Mog of the Ages": 1000, "Retribution of the Madeen": 1100,
    "Star Prism": 1100, "Rainbow Drip": 1000,
    "Hammer Stamp": 560, "Hammer Brush": 580, "Polishing Hammer": 600,
}
ANCHOR_NAMES = ("Fire in Red", "Aero in Green", "Water in Blue")
HAMMERS = ("Hammer Stamp", "Hammer Brush", "Polishing Hammer")
CMY_NAMES = ("Blizzard in Cyan", "Stone in Yellow", "Thunder in Magenta")


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

    rates: dict[str, list[float]] = defaultdict(list)   # clean, non-hammer
    ham_rates: dict[str, list[float]] = defaultdict(list)
    no_mult: int = 0
    sp_followup_dmg = 0
    cmy_gaps_in: list[float] = []
    cmy_gaps_out: list[float] = []

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
        s, e = fight["startTime"], fight["endTime"]

        dmg = client.get_events(code, s, e, aid, data_type="DamageDone")
        for ev in dmg:
            nm = abil.get(ev.get("abilityGameID"), "?")
            amt = ev.get("amount", 0)
            if ev.get("abilityGameID") == 34682 and amt > 0:
                sp_followup_dmg += 1
            if amt <= 0 or ev.get("tick"):
                continue
            mult = ev.get("multiplier")
            if not mult:
                no_mult += 1
                continue
            base = amt / mult
            if nm in HAMMERS:
                ham_rates[nm].append(base)          # always crit+DH; keep all
            elif ev.get("hitType") == 1 and not ev.get("directHit"):
                rates[nm].append(base)

        # CMY begincast cadence, split by Starry window.
        raw = client.get_events(code, s, e, aid, data_type="Casts")
        name_of = lambda cid: abil.get(cid, "?")  # noqa: E731
        starry = [(ev.get("timestamp", s) - s) / 1000.0
                  for ev in raw if ev.get("type") == "cast"
                  and name_of(ev.get("abilityGameID")) == "Starry Muse"]
        in_starry = lambda t: any(a <= t < a + 20.0 for a in starry)  # noqa: E731
        bts = [((ev.get("timestamp", s) - s) / 1000.0)
               for ev in sorted(raw, key=lambda x: x.get("timestamp", 0))
               if ev.get("type") == "begincast"
               and name_of(ev.get("abilityGameID")) in CMY_NAMES]
        for a, b in zip(bts, bts[1:]):
            g = b - a
            if 2.0 <= g <= 4.0:
                (cmy_gaps_in if in_starry(a) else cmy_gaps_out).append(g)

    def mean(v):
        return sum(v) / len(v) if v else float("nan")

    anchor_vals = [mean(rates[nm]) / CANDIDATES[nm] for nm in ANCHOR_NAMES if rates[nm]]
    k = mean(anchor_vals)  # damage per potency, RGB-anchored
    print(f"anchor K (dmg per potency, RGB): {k:.2f}   (events lacking multiplier: {no_mult})")
    print(f"\n{'ability':28s} {'n':>4s}  {'amt/mult':>9s}  {'norm':>6s}  implied_p (cand)")
    for nm, cand in sorted(CANDIDATES.items(), key=lambda kv: kv[0]):
        if nm in HAMMERS or not rates[nm]:
            continue
        m = mean(rates[nm])
        print(f"{nm:28s} {len(rates[nm]):4d}  {m:9.1f}  {m / (k * cand):6.3f}  "
              f"{m / k:7.1f}  ({cand})")

    print("\n=== HAMMERS (all events crit+DH; rate ÷ K = crit-DH mult × p) ===")
    for nm in HAMMERS:
        if not ham_rates[nm]:
            continue
        m = mean(ham_rates[nm])
        print(f"{nm:28s} {len(ham_rates[nm]):4d}  {m:9.1f}  implied mult vs cand "
              f"{CANDIDATES[nm]}p: {m / (k * CANDIDATES[nm]):.3f}")

    def med(v):
        return sorted(v)[len(v) // 2] if v else float("nan")
    print(f"\nCMY begincast gaps: outside Starry median {med(cmy_gaps_out):.3f}s "
          f"(n={len(cmy_gaps_out)}, expect ~3.3+queue), inside {med(cmy_gaps_in):.3f}s "
          f"(n={len(cmy_gaps_in)}, expect ~2.475+queue)")
    print(f"Star Prism follow-up (34682) damage events: {sp_followup_dmg} (expect 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
