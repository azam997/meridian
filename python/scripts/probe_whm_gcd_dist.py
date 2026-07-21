"""Histogram a WHM pull's clean GCD-pair intervals (gcd-inference diagnosis).

Run from python/:  python scripts/probe_whm_gcd_dist.py <enc> <name-substring>
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path
from statistics import quantiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE                  # noqa: E402
from jobs._core.ability_metadata import get_metadata      # noqa: E402
from jobs._core.cached_client import CachedEventsClient   # noqa: E402
from jobs._core.casts import fetch_norm_casts             # noqa: E402
from sidecar.main import _client                          # noqa: E402


def main() -> int:
    enc = int(sys.argv[1]) if len(sys.argv) > 1 else 105
    who = (sys.argv[2] if len(sys.argv) > 2 else "").lower()
    client = _client()
    blob = client.get_rankings(enc, class_name="White Mage",
                               spec_name="White Mage",
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    r = next(x for x in blob["rankings"]
             if who in (x.get("name") or "").lower())
    code, fid = r["report"]["code"], r["report"]["fightID"]
    rep = client.get_report_summary(code)
    fight = next(f for f in rep["fights"] if f["id"] == fid)
    friendly = set(fight.get("friendlyPlayers") or [])
    actor = next(a for a in rep["masterData"]["actors"]
                 if a["type"] == "Player" and a.get("subType") == "WhiteMage"
                 and a["id"] in friendly)
    cc = CachedEventsClient(client)
    norm = fetch_norm_casts(cc, code, fight, actor)

    def is_gcd(aid):
        m = get_metadata(aid)
        return m is not None and not m.is_ogcd

    casts = sorted((t, a) for t, a in norm if t >= 0)
    clean: list[float] = []
    prev = None
    weaves = 0
    for t, aid in casts:
        if is_gcd(aid):
            if prev is not None and weaves <= 1:
                gap = t - prev
                if 2.0 <= gap <= 2.8:
                    clean.append(gap)
            prev = t
            weaves = 0
        else:
            weaves += 1
    clean.sort()
    print(f"{r.get('name')}: n = {len(clean)}")
    qs = quantiles(clean, n=20)
    for i, q in enumerate(qs, 1):
        print(f"  p{i * 5:>2}: {q:.3f}")
    hist = collections.Counter(round(g, 2) for g in clean)
    for k in sorted(hist):
        print(f"  {k:.2f}: {'#' * hist[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
