"""Throwaway live check: strict vs lenient RPR efficiency with the consensus
ranged-filler (Harpe) windows live, per top-10 pull on selected encounters.

Mirrors the sidecar's lenient path: analyze the top-10 as the ref pool, then
`_inject_tier_b(job, you, refs)` per pull (Tier B + ranged windows together,
exactly what run_analysis does). Prints eff_strict / eff_lenient / the
window seconds so the M10S recovery and the M12S-P2 gate sanity are visible.

Run from python/:  python scripts/check_rpr_lenient.py [--enc 101 102 105]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from sidecar.main import _client, _inject_tier_b                       # noqa: E402

JOB = "Reaper"


def main() -> None:
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
        pulls = []
        for r in ranks:
            code, fid = r["report"]["code"], r["report"]["fightID"]
            nm = r.get("name", "")
            try:
                pulls.append(analyze_pull(JOB, client, code, fid,
                                          ranking_name=nm, label=nm))
            except Exception as e:  # noqa: BLE001
                print(f"  {nm:<18} ERR {type(e).__name__}: {e}")
        print(f"\n=== {enc_names.get(enc, enc)} (enc {enc}) — {len(pulls)} pulls ===")
        print(f"{'name':<18}{'dur':>6}{'strict%':>9}{'lenient%':>9}"
              f"{'delta':>6}{'tierB s':>9}{'ranged s':>9}")
        for you in pulls:
            _inject_tier_b(JOB, you, pulls)
            st = you.aspects["Scoring"].state
            dl = st["delivered_potency"]
            strict = st["idealized_strict"]
            lenient = st.get("idealized_lenient") or strict
            es = 100 * dl / strict if strict else 0.0
            el = 100 * dl / lenient if lenient else 0.0
            tb = sum(w["end_s"] - w["start_s"]
                     for w in (st.get("downtime_tier_b") or []))
            rw = sum(w["end_s"] - w["start_s"]
                     for w in (st.get("ranged_windows") or []))
            flag = "  <-- LENIENT OVER" if el > 100.5 else ""
            print(f"{you.label[:18]:<18}{st['fight_duration_s']:6.0f}"
                  f"{es:9.2f}{el:9.2f}{el - es:6.2f}{tb:9.1f}{rw:9.1f}{flag}")


if __name__ == "__main__":
    main()
