"""Validate a job's MULTI-TARGET (AoE-aware) ceiling against real top pulls.

The single-target counterpart (`validate_job_ceiling.py`) reads
`idealized_strict` straight off `analyze_pull`, so on a multi-target fight it
reports the DISCLAIMED single-target number — not the AoE-aware efficiency the
app actually shows. This script mirrors the sidecar's `run_analysis` path: it
analyzes the top-N pulls, then for each pull runs `_inject_multi_target` with the
others as the reference pool (same consensus + crediting + floored N(t) schedule
the app uses), and reports the CREDITED efficiency
(delivered_multitarget / idealized_multitarget).

The acceptance bar is the same as everywhere in the analyzer: **0/N over 100.5%**
on credited pulls. It also prints the single-target -> multi-target efficiency
lift, so the BLM M10S "AoE unmodeled ~90%" canary should visibly close.

Run from python/ (needs FFLogs creds in ~/.fflogs_efficiency_analyzer/config.json):
    python scripts/validate_multitarget_ceiling.py "Black Mage"
    python scripts/validate_multitarget_ceiling.py "Samurai" --enc 102 --top 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS                     # noqa: E402
from jobs import analyze_pull                                        # noqa: E402
from jobs._core.downtime_sources import is_multi_target_pull        # noqa: E402
from sidecar.main import _client, _inject_multi_target              # noqa: E402

# Reuse the single-target script's ranking + actor-resolution helpers.
from scripts.validate_job_ceiling import _resolve_src, _top_rankings  # noqa: E402

# M10S "Red Hot and Deep Blue" — two co-bosses targetable the whole fight, the
# canonical multi-target encounter in the current tier.
_DEFAULT_ENC = 102


def sweep(job: str, enc: int, top: int) -> None:
    client = _client()
    subtype = job.replace(" ", "")
    enc_names = dict(AAC_HEAVYWEIGHT_ENCOUNTERS)
    label = enc_names.get(enc, f"enc{enc}")
    ranks = _top_rankings(client, job, enc, top)
    print(f"\n=== {job} — {label} (enc {enc}) — top {len(ranks)} ===")

    # Analyze every ranked pull once; cross-use as each other's reference pool.
    runs: list[tuple[str, object]] = []
    for r in ranks:
        code, fid, nm = r["report"]["code"], r["report"]["fightID"], r["name"]
        try:
            src = _resolve_src(client, code, fid, nm, subtype)
            if src is None:
                print(f"   {nm[:18]:<18} (no {job} actor)")
                continue
            mr = analyze_pull(job, client, code, fid, ranking_name=nm, label=nm)
            runs.append((nm, mr))
        except Exception as e:  # noqa: BLE001
            print(f"   {nm[:18]:<18} ERR {type(e).__name__}: {e}")

    print(f"{'#':>2} {'name':<18}{'dur':>6}{'mt?':>4}{'cred':>5}"
          f"{'ST%':>8}{'MT%':>8}{'lift':>7}")
    over = 0
    credited_effs: list[float] = []
    for i, (nm, you) in enumerate(runs):
        refs = [m for j, (_, m) in enumerate(runs) if j != i]
        _inject_multi_target(job, you, refs)
        st = you.aspects["Scoring"].state
        dl = float(st.get("delivered_potency", 0) or 0)
        idl = float(st.get("idealized_strict") or st.get("idealized_potency", 0) or 0)
        dur = float(st.get("fight_duration_s", 0) or 0)
        st_eff = 100 * dl / idl if idl > 0 else 0.0
        is_mt = is_multi_target_pull(list(getattr(you, "multi_target_windows", ())))
        credited = bool(st.get("multi_target_credited"))
        if credited:
            dlm = float(st.get("delivered_multitarget", 0) or 0)
            idlm = float(st.get("idealized_multitarget", 0) or 0)
            mt_eff = 100 * dlm / idlm if idlm > 0 else 0.0
            credited_effs.append(mt_eff)
        else:
            mt_eff = st_eff
        lift = mt_eff - st_eff
        flag = "  <-- OVER" if (credited and mt_eff > 100.5) else ""
        if credited and mt_eff > 100.5:
            over += 1
        print(f"{i + 1:>2} {nm[:18]:<18}{dur:6.0f}{('Y' if is_mt else 'n'):>4}"
              f"{('Y' if credited else 'n'):>5}{st_eff:8.2f}{mt_eff:8.2f}"
              f"{lift:+7.2f}{flag}")

    print(f"\nSUMMARY ({job}, {label}): {len(runs)} pulls, "
          f"{len(credited_effs)} credited, {over} credited-over-100.5%")
    if credited_effs:
        print(f"  credited MT eff: min={min(credited_effs):.2f} "
              f"max={max(credited_effs):.2f} "
              f"mean={sum(credited_effs) / len(credited_effs):.2f}")
        if over:
            print("  -> ceiling too LOW on those pulls: the AoE rotation / falloff "
                  "is under-modeled. Inspect the over pulls' AoE cast mix.")
        else:
            print("  -> 0 over the gate. AoE ceiling holds.")
    else:
        print("  (no pulls credited — refs didn't confirm any multi-target window, "
              "or the job has no AoE/splash kit)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", help='canonical job name, e.g. "Black Mage"')
    ap.add_argument("--enc", type=int, default=_DEFAULT_ENC,
                    help=f"encounter id (default {_DEFAULT_ENC} = M10S)")
    ap.add_argument("--top", type=int, default=10, help="pulls per encounter")
    args = ap.parse_args()
    sweep(args.job, args.enc, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
