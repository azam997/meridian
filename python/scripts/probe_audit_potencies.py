"""Probe: v1.1 audit potency checks — AST Stellar Explosion, DNC Saber Dance.

Settles two verify-first items from the simulator audit:
  * AST EARTHLY_STAR: POTENCIES says 310 ("wiki-verified full-grown"), the
    SPLASH_POTENCIES entry says 540 — measure the real per-potency rate of the
    Stellar Explosion damage events (whatever id they land under) against the
    Fall Malefic 270 anchor.
  * DNC SABER_DANCE: id comment says 520, POTENCIES says 540 — measure against
    the Cascade 220 anchor.

Same method as probe_darkknight_potency.py: dedupe calculateddamage/damage
pairs, divide amount by the FFLogs `multiplier`, keep clean hits only
(hitType==1, no directHit), anchor on a known single-potency id.

Run from python/:
    python scripts/probe_audit_potencies.py [--enc 103] [--top 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOBS = {
    "Astrologian": {
        "anchor": (25871, "Fall Malefic", 270),
        "watch": {7439: ("Earthly Star place-cast", (310, 540))},
        "watch_names": ("stellar", "star"),
    },
    "Dancer": {
        "anchor": (15989, "Cascade", 220),
        "watch": {16005: ("Saber Dance", (520, 540))},
        "watch_names": ("saber",),
    },
}


def dedup(events: list[dict]) -> list[dict]:
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


def mean(v):
    return sum(v) / len(v) if v else float("nan")


def probe_job(client, subtype: str, spec: dict, enc: int, top: int) -> None:
    blob = client.get_rankings(enc, class_name=subtype, spec_name=subtype,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")][:top]

    rates: dict[int, list[float]] = defaultdict(list)
    counts: dict[int, int] = Counter()
    names: dict[int, str] = {}

    for r in ranks:
        code, fid = r["report"]["code"], r["report"]["fightID"]
        report = client.get_report_summary(code)
        fight = next(f for f in report["fights"] if f["id"] == fid)
        friendly = set(fight.get("friendlyPlayers") or [])
        actors = [a for a in report["masterData"]["actors"]
                  if a["type"] == "Player" and a.get("subType") == subtype
                  and a["id"] in friendly]
        if not actors:
            continue
        who = next((a for a in actors
                    if a["name"].lower() == (r.get("name") or "").lower()), actors[0])
        names.update({a["gameID"]: a["name"]
                      for a in (report["masterData"].get("abilities") or [])})
        s, e = fight["startTime"], fight["endTime"]
        dmg = dedup(client.get_events(code, s, e, who["id"], data_type="DamageDone"))
        for ev in dmg:
            cid = ev.get("abilityGameID")
            amt, mult = ev.get("amount", 0), ev.get("multiplier")
            if amt <= 0 or not mult or ev.get("tick"):
                continue
            counts[cid] += 1
            if ev.get("hitType") == 1 and not ev.get("directHit"):
                rates[cid].append(amt / mult)

    aid, aname, ap = spec["anchor"]
    if not rates[aid]:
        print(f"  !! no clean {aname} hits — cannot anchor")
        return
    k = mean(rates[aid]) / ap
    print(f"  anchor: {aname} {ap} -> K={k:.2f} dmg/potency (n={len(rates[aid])})")
    print(f"  {'id':>6s} {'ability':26s} {'hits':>5s} {'clean':>5s}  "
          f"{'implied_p':>9s}  note")
    for cid in sorted(rates, key=lambda c: -counts[c]):
        if not rates[cid]:
            continue
        m = mean(rates[cid]) / k
        nm = names.get(cid, "?")
        note = ""
        if cid in spec["watch"]:
            wname, cands = spec["watch"][cid]
            best = min(cands, key=lambda c: abs(m - c))
            note = f"<-- {wname}: candidates {cands}, closest {best}"
        elif any(w in nm.lower() for w in spec["watch_names"]):
            note = "<-- name-matched watch"
        print(f"  {cid:6d} {nm:26s} {counts[cid]:5d} {len(rates[cid]):5d}  "
              f"{m:9.1f}  {note}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=103)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()
    client = _client()
    for subtype, spec in JOBS.items():
        print(f"\n=== {subtype} (enc {args.enc}) ===")
        probe_job(client, subtype, spec, args.enc, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
