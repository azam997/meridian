"""Calibrate the sub-GCD cadence factor (`gcd_speed._SUBGCD_SWEEP_FACTORS`) per tier.

A GCD's nominal recast (the gear value, e.g. 2.50s) is NOT the cadence a real top parse
achieves: the server resolves a queued GCD on the tick its recast completes, landing a
few hundredths early, so clean tight play fits a fractionally-faster EFFECTIVE GCD over a
fight. The idealized ceiling is scored over a small band of cadences (gear GCD down to
`factor x gear`) and the BEST (highest) ceiling kept, so elite play approaches but can't
exceed it. This script picks the band floor empirically: for each candidate factor it
runs the live top parses through the analyzer (the ceiling already `max`-sweeps over the
band internally is NOT used here — we score each factor in isolation to see its own
effect) and reports the WORST efficiency, so you can choose the floor that puts every live
parse under the gate with margin, without over-correcting the field mean.

It is a diagnostic / per-tier calibration tool (network), in the spirit of
calibrate_crit_dh.py / calibrate_buff_timing.py — not on the hot path.

Run from python/:
    python scripts/calibrate_subgcd_cadence.py
    python scripts/calibrate_subgcd_cadence.py --jobs Paladin Machinist --n 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE   # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from jobs._core.ability_metadata import get_metadata                  # noqa: E402
from jobs._core.gcd_speed import (                                    # noqa: E402
    CeilingContext, effective_gcd_for, unwrap_ceiling_context)
from sidecar.main import _client                                      # noqa: E402

# job -> (scoring module, GCD constant). SAM's GCD is its Fuka self-haste value.
_JOBS = {
    "Paladin": ("jobs.paladin.scoring", 2.50),
    "Machinist": ("jobs.machinist.scoring", 2.50),
    "Reaper": ("jobs.reaper.scoring", 2.50),
    "Samurai": ("jobs.samurai.scoring", 2.14),
    "Warrior": ("jobs.warrior.scoring", 2.50),
}
_FACTORS = (1.000, 0.992, 0.990, 0.988, 0.986, 0.984)


def _is_gcd(aid: int) -> bool:
    m = get_metadata(aid)
    return m is not None and not m.is_ogcd


def run(jobs: list[str], n: int) -> None:
    import importlib
    client = _client()
    enc, encname = AAC_HEAVYWEIGHT_ENCOUNTERS[-1]
    print(f"=== {encname} — worst live efficiency per sub-GCD factor (gear x factor) ===")
    print("job        const  " + "  ".join(f"f={f:.3f}" for f in _FACTORS))
    for job in jobs:
        sm, const = _JOBS[job]
        sc = importlib.import_module(sm)
        blob = client.get_rankings(enc, class_name=job, spec_name=job,
                                   difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
        worst = {f: 0.0 for f in _FACTORS}
        for r in ((blob or {}).get("rankings") or [])[:n]:
            rep = r.get("report") or {}
            if not rep.get("code"):
                continue
            try:
                mr = analyze_pull(job, client, rep["code"], rep["fightID"],
                                  ranking_name=r["name"], label=r["name"])
            except Exception:
                continue
            st = mr.aspects["Scoring"].state
            dur, dt = st["fight_duration_s"], st["downtime_windows"]
            delivered = st["delivered_potency"]
            _g, payload = unwrap_ceiling_context(st.get("sim_context"))
            gear = effective_gcd_for(mr.norm_casts, _is_gcd, const, dt)
            for f in _FACTORS:
                ceil = sc._FNS.idealized_at_duration(
                    dur, dt, sim_context=CeilingContext(gcd_base_s=gear * f, payload=payload))
                if ceil > 0:
                    worst[f] = max(worst[f], 100.0 * delivered / ceil)
        print(f"{job:9} {const:5.2f}  "
              + "  ".join(f"{worst[f]:6.2f}%" for f in _FACTORS))
    print("\nPick the band floor as the loosest factor that holds every job's worst <= ~99.7%")
    print("(NOTE: the production ceiling MAX-sweeps gear..floor, so a non-monotonic dip is")
    print("absorbed — this table shows each factor in isolation to localize the floor).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", nargs="*", default=list(_JOBS))
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()
    run(args.jobs, args.n)


if __name__ == "__main__":
    main()
