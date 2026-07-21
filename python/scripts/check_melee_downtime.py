"""Live check: the STRICT melee-downtime credit per top-N pull (network).

`_inject_melee_downtime` (sidecar) promotes the consensus forced-disconnect windows
onto the strict/rank ceiling, self-limited per pull. The existing validate_job_ceiling
sweep calls `analyze_pull` only, so it never sees this credit. This harness mirrors the
sidecar: analyze the top-N as the pool, run `_inject_tier_b` then `_inject_melee_downtime`
over the pool, and print strict efficiency BEFORE vs AFTER the credit + the credited
potency, flagging any pull now over 100.5% (the guard).

Run from python/:  python scripts/check_melee_downtime.py Reaper --enc 101 102 105
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from sidecar.main import (                                             # noqa: E402
    _client, _inject_melee_downtime, _inject_tier_b)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", nargs="?", default="Reaper")
    ap.add_argument("--enc", type=int, nargs="*", default=[101, 102, 105])
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    client = _client()
    enc_names = dict(AAC_HEAVYWEIGHT_ENCOUNTERS)
    grand_over = 0
    grand_n = 0

    for enc in args.enc:
        blob = client.get_rankings(enc, class_name=args.job, spec_name=args.job,
                                   difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
        ranks = [r for r in ((blob or {}).get("rankings") or [])
                 if r.get("report", {}).get("code")][:args.top]
        pulls = []
        for r in ranks:
            code, fid = r["report"]["code"], r["report"]["fightID"]
            nm = r.get("name", "")
            try:
                pulls.append(analyze_pull(args.job, client, code, fid,
                                          ranking_name=nm, label=nm))
            except Exception as e:  # noqa: BLE001
                print(f"  {nm:<18} ERR {type(e).__name__}: {e}")
        if not pulls:
            continue
        # Capture pre-credit strict, then apply the real sidecar injections.
        before = {id(p): float(p.aspects["Scoring"].state["idealized_strict"])
                  for p in pulls}
        for p in pulls:
            _inject_tier_b(args.job, p, pulls)
        _inject_melee_downtime(args.job, pulls[0], pulls)  # symmetric: mutates all

        print(f"\n=== {enc_names.get(enc, enc)} (enc {enc}) — {len(pulls)} pulls ===")
        print(f"{'name':<18}{'dur':>6}{'eff_before':>11}{'eff_after':>10}"
              f"{'credit p':>10}{'cred%':>7}{'wins':>5}")
        effs_a = []
        for p in pulls:
            st = p.aspects["Scoring"].state
            dl = float(st["delivered_potency"])
            b = before[id(p)]
            a = float(st["idealized_strict"])
            credit = float(st.get("melee_downtime_credit") or 0.0)
            nwins = len(st.get("melee_downtime_windows") or [])
            eff_b = 100 * dl / b if b else 0.0
            eff_a = 100 * dl / a if a else 0.0
            effs_a.append(eff_a)
            grand_n += 1
            over = eff_a > 100.5
            grand_over += int(over)
            flag = "  <-- OVER" if over else ""
            credpct = 100 * credit / b if b else 0.0
            print(f"{p.label[:18]:<18}{st['fight_duration_s']:6.0f}"
                  f"{eff_b:11.2f}{eff_a:10.2f}{credit:10.0f}{credpct:7.2f}"
                  f"{nwins:5d}{flag}")
        print(f"   after-credit: min={min(effs_a):.2f} max={max(effs_a):.2f} "
              f"mean={sum(effs_a)/len(effs_a):.2f}")
    print(f"\nSUMMARY: {grand_n} pulls, {grand_over} over 100.5%")


if __name__ == "__main__":
    main()
