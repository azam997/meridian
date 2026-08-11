"""Throwaway probe: where do top Reapers cast mid-fight Harpe?

Phase-2 evidence for the RPR ceiling-looseness decomposition (plan
rpr-calibration.md): the M10S top-10 gap is dominated by forced-disconnect
ranged filler (Harpe at 300p replacing ~600p melee GCDs) that Tier-B's
idle-consensus can never credit (the player keeps GCDing). This prints, per
top-10 pull on the chosen encounters, the Harpe cast times (fight-relative,
pre-pull excluded) so we can see (a) whether they cluster at consensus
mechanic times within an encounter and (b) whether the gate-critical
M12S-P2 pulls carry any.

Run from python/:
    python scripts/probe_rpr_harpe.py            # enc 101 102 105
    python scripts/probe_rpr_harpe.py --enc 102
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from jobs.reaper.data import HARPE                                     # noqa: E402
from sidecar.main import _client                                       # noqa: E402

JOB = "Reaper"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, nargs="*", default=[101, 102, 105])
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    client = _client()
    enc_names = dict(AAC_HEAVYWEIGHT_ENCOUNTERS)
    for enc in args.enc:
        blob = client.get_rankings(enc, class_name=JOB, spec_name=JOB,
                                   difficulty=DIFFICULTY_SAVAGE, metric="rdps",
                                   page=1)
        ranks = [r for r in ((blob or {}).get("rankings") or [])
                 if r.get("report", {}).get("code")][:args.top]
        print(f"\n=== {enc_names.get(enc, enc)} (enc {enc}) ===")
        all_times: list[float] = []
        for r in ranks:
            code, fid = r["report"]["code"], r["report"]["fightID"]
            nm = r.get("name", "")
            try:
                mr = analyze_pull(JOB, client, code, fid, ranking_name=nm,
                                  label=nm)
            except Exception as e:  # noqa: BLE001
                print(f"  {nm:<18} ERR {type(e).__name__}: {e}")
                continue
            times = [round(t, 1) for t, a in mr.norm_casts
                     if a == HARPE and t >= 0]
            all_times.extend(times)
            dur = mr.aspects["Scoring"].state["fight_duration_s"]
            print(f"  {nm:<18} dur={dur:5.0f}  harpe={len(times):>2}  {times}")
        if all_times:
            # 10s-bucket histogram across the pool — consensus clusters pop out.
            from collections import Counter
            hist = Counter(int(t // 10) * 10 for t in all_times)
            print("  pool histogram (10s buckets with >=3 casts):")
            for b in sorted(hist):
                if hist[b] >= 3:
                    print(f"    t={b:>4}-{b + 10:<4} n={hist[b]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
