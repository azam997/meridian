"""Probe a real WHM pull's lily economy vs the sim (entry-gauge diagnosis).

Run from python/:  python scripts/probe_whm_entry.py <enc> <name-substring>
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE                   # noqa: E402
from jobs._core.casts import fetch_norm_casts             # noqa: E402
from jobs._core.cached_client import CachedEventsClient   # noqa: E402
from jobs.whitemage import data as wd                      # noqa: E402
from jobs.whitemage import simulator as sim                # noqa: E402
from jobs.whitemage.scoring import measure_entry_lily_state  # noqa: E402
from sidecar.main import _client                           # noqa: E402

NAMES = {wd.GLARE_III: "Glare3", wd.GLARE_IV: "Glare4", wd.DIA: "Dia",
         wd.ASSIZE: "Assize", wd.PRESENCE_OF_MIND: "PoM",
         wd.AFFLATUS_MISERY: "Misery", wd.AFFLATUS_SOLACE: "Solace",
         wd.AFFLATUS_RAPTURE: "Rapture"}


def main() -> int:
    enc = int(sys.argv[1]) if len(sys.argv) > 1 else 105
    who = (sys.argv[2] if len(sys.argv) > 2 else "").lower()
    client = _client()
    blob = client.get_rankings(enc, class_name="White Mage",
                               spec_name="White Mage",
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in (blob or {}).get("rankings", [])
             if r.get("report", {}).get("code")]
    r = next((x for x in ranks if who in (x.get("name") or "").lower()), None)
    if r is None:
        print("not found")
        return 1
    code, fid = r["report"]["code"], r["report"]["fightID"]
    rep = client.get_report_summary(code)
    fight = next(f for f in rep["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actor = next(a for a in rep["masterData"]["actors"]
                 if a["type"] == "Player" and a.get("subType") == "WhiteMage"
                 and a["id"] in friendly)
    cc = CachedEventsClient(client)
    norm = fetch_norm_casts(cc, code, fight, actor)
    dur = (fight["endTime"] - fight["startTime"]) / 1000.0

    lily_casts = [(round(t, 1), NAMES.get(a, a)) for t, a in norm
                  if a in (wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE,
                           wd.AFFLATUS_MISERY)]
    print(f"{r.get('name')} dur={dur:.0f}s")
    print(f"lily-economy casts ({len(lily_casts)}):")
    for t, n in lily_casts:
        print(f"   {t:>7.1f}  {n}")
    entry = measure_entry_lily_state(norm)
    print(f"measured entry: lilies={entry[0]} blood={entry[1]}")

    ctx = sim.WhmContext(entry_lilies=entry[0], entry_blood=entry[1])
    tl, _ = sim.simulate_idealized_perfect(dur, [], sim_context=ctx if ctx else None)
    c = Counter(a for _t, a in tl)
    print(f"sim with entry ctx: Misery={c[wd.AFFLATUS_MISERY]} "
          f"Solace={c[wd.AFFLATUS_SOLACE]} PoM={c[wd.PRESENCE_OF_MIND]} "
          f"Glare4={c[wd.GLARE_IV]} Dia={c[wd.DIA]}")
    pc = Counter(a for _t, a in norm)
    print(f"player:            Misery={pc[wd.AFFLATUS_MISERY]} "
          f"Solace={pc[wd.AFFLATUS_SOLACE]} Rapture={pc[wd.AFFLATUS_RAPTURE]} "
          f"PoM={pc[wd.PRESENCE_OF_MIND]} Glare4={pc[wd.GLARE_IV]} Dia={pc[wd.DIA]}")
    # First GCD timing
    print(f"player first cast t={norm[0][0]:.2f} ({NAMES.get(norm[0][1], norm[0][1])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
