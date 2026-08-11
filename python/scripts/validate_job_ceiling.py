"""Validate a job's idealized ceiling against REAL top-ranking pulls (network).

The primary calibration tool when adding or tuning a job simulator: fetch the
top-N ranked pulls per encounter, run the analysis pipeline, and report
efficiency (delivered / idealized_strict). A well-calibrated ceiling sits
~97-100% for top parses and never far over 100% — pulls much over 100% mean the
ceiling is too LOW (a potency, buff, or uptime term is under-modeled). See the
`add-job` skill for the full workflow and how to read the decomposition.

Run from python/ (needs FFLogs creds in ~/.fflogs_efficiency_analyzer/config.json):
    python scripts/validate_job_ceiling.py "Paladin"
    python scripts/validate_job_ceiling.py "Paladin" --enc 104 --top 10
    python scripts/validate_job_ceiling.py "Paladin" --decompose Rudeus

`--decompose <name-substring>` localizes WHY a pull is over 100%: it compares the
player's cast stream to the idealized sim (GCD/oGCD counts, raw table potency,
opener/ender timing, and any player casts that fall inside a downtime window —
which would be a fairness bug). The remaining gap after those line up is the
buff-weighting / greedy-vs-optimum residual.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import (  # noqa: E402
    AAC_HEAVYWEIGHT_ENCOUNTERS,
    ALL_ENCOUNTERS,
    encounter_difficulty,
)
from jobs import analyze_pull, get_job                                 # noqa: E402
from jobs._core.ability_metadata import get_metadata                   # noqa: E402
from sidecar.main import _client                                       # noqa: E402


def _resolve_src(client, code, fight_id, name, subtype):
    rep = client.get_report_summary(code)
    fight = next((f for f in rep["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in rep["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == subtype
              and a["id"] in friendly]
    by_name = [a for a in actors if a["name"].lower() == (name or "").lower()]
    pick = by_name or actors
    return pick[0]["id"] if pick else None


def _top_rankings(client, job, enc, top):
    blob = client.get_rankings(enc, class_name=job, spec_name=job,
                               difficulty=encounter_difficulty(enc),
                               metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")]
    ranks.sort(key=lambda r: r.get("rankPercent") or r.get("percentile") or 0,
               reverse=True)
    return ranks[:top]


def _is_gcd(aid):
    m = get_metadata(aid)
    return m is not None and not m.is_ogcd


def sweep(job, encounters, top):
    client = _client()
    subtype = job.replace(" ", "")
    enc_names = dict(ALL_ENCOUNTERS)
    all_eff = []
    over = 0
    for enc in encounters:
        ranks = _top_rankings(client, job, enc, top)
        label = enc_names.get(enc, f"enc{enc}")
        print(f"\n=== {label} (enc {enc}) — top {len(ranks)} ===")
        print(f"{'#':>2} {'name':<18}{'dur':>6}{'eff%':>8}{'p/s':>6}  src")
        effs = []
        for i, r in enumerate(ranks, 1):
            code, fid = r["report"]["code"], r["report"]["fightID"]
            nm = r.get("name", "")
            try:
                src = _resolve_src(client, code, fid, nm, subtype)
                if src is None:
                    print(f"{i:>2} {nm[:18]:<18} (no {job} actor)")
                    continue
                mr = analyze_pull(job, client, code, fid, ranking_name=nm, label=nm)
                st = mr.aspects["Scoring"].state
                dl, idl = st["delivered_potency"], st["idealized_strict"]
                dur = st["fight_duration_s"]
                eff = 100 * dl / idl if idl > 0 else 0
                effs.append(eff)
                all_eff.append(eff)
                if eff > 100.5:
                    over += 1
                flag = "  <-- OVER" if eff > 100.5 else ""
                print(f"{i:>2} {nm[:18]:<18}{dur:6.0f}{eff:8.2f}{dl/dur:6.0f}  "
                      f"{st['downtime_source'][:4]}{flag}")
            except Exception as e:  # noqa: BLE001
                print(f"{i:>2} {nm[:18]:<18} ERR {type(e).__name__}: {e}")
        if effs:
            print(f"   {label}: min={min(effs):.2f} max={max(effs):.2f} "
                  f"mean={sum(effs)/len(effs):.2f}")
    if all_eff:
        print(f"\nSUMMARY: {len(all_eff)} pulls, {over} over 100.5%, "
              f"max={max(all_eff):.2f}% min={min(all_eff):.2f}% "
              f"mean={sum(all_eff)/len(all_eff):.2f}%")
        if over:
            print("  -> ceiling too LOW on those pulls. Run --decompose <name> on "
                  "the worst to localize (uptime / mix / buff-weighting).")


def decompose(job, encounters, substr):
    client = _client()
    subtype = job.replace(" ", "")
    jd = get_job(job).data
    sim_model = get_job(job).simulator
    # Find the first pull whose ranking name contains `substr`.
    for enc in encounters:
        for r in _top_rankings(client, job, enc, 25):
            if substr.lower() not in r.get("name", "").lower():
                continue
            code, fid, nm = r["report"]["code"], r["report"]["fightID"], r["name"]
            src = _resolve_src(client, code, fid, nm, subtype)
            if src is None:
                continue
            mr = analyze_pull(job, client, code, fid, ranking_name=nm, label=nm)
            st = mr.aspects["Scoring"].state
            dur, dt = st["fight_duration_s"], st["downtime_windows"]
            eff = 100 * st["delivered_potency"] / st["idealized_strict"]
            sim = [(t, a) for t, a in sim_model.simulate(dur, dt).timeline if t >= 0]
            pc = [(t, a) for t, a in mr.norm_casts if t >= 0]
            pot = jd.potencies.get

            def raw(cs):
                return sum(pot(a, 0) for _t, a in cs)

            def gcds(cs):
                return [c for c in cs if _is_gcd(c[1])]
            print(f"\n{nm}  ({dict(ALL_ENCOUNTERS).get(enc, enc)})  "
                  f"dur={dur:.0f}s  EFF={eff:.2f}%")
            print(f"  downtime: {[(round(s,1),round(e,1)) for s,e in dt]} "
                  f"({sum(e-s for s,e in dt):.0f}s)")
            indt = [(round(t, 1), a) for t, a in pc
                    if pot(a, 0) > 0 and any(s <= t < e for s, e in dt)]
            print(f"  player scored casts INSIDE downtime: {len(indt)} "
                  f"{'(FAIRNESS BUG if >0)' if indt else ''}")
            print(f"  GCDs:  player {len(gcds(pc))}  sim {len(gcds(sim))}  "
                  f"(theoretical max @2.5s ~ {dur/2.5:.0f})")
            print(f"  casts: player {len(pc)}  sim {len(sim)}")
            print(f"  raw table potency: player {raw(pc)}  sim {raw(sim)}  "
                  f"(sim-player {raw(sim)-raw(pc):+d})")
            print(f"  first cast t: player {pc[0][0]:.2f}  sim {sim[0][0]:.2f}")
            # Per-ability count diff (raw-potency-weighted), biggest swings first.
            pcc, scc = Counter(a for _t, a in pc), Counter(a for _t, a in sim)
            rows = []
            for a in set(pcc) | set(scc):
                d = (pcc.get(a, 0) - scc.get(a, 0)) * pot(a, 0)
                if d:
                    m = get_metadata(a)
                    rows.append((d, m.name if m else str(a),
                                 pcc.get(a, 0), scc.get(a, 0), pot(a, 0)))
            print("  per-ability count diff (player vs sim), by raw potency swing:")
            for d, name, p, s, po in sorted(rows, key=lambda x: -abs(x[0]))[:10]:
                print(f"    {name:<20} pot={po:<5} player={p:<3} sim={s:<3} p*d={d:+d}")
            print("  -> if uptime/mix line up, the residual is buff-weighting "
                  "(sequencing high-potency GCDs into buff windows).")
            return
    print(f"no top-ranking pull matching {substr!r} found for {job}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", help='canonical job name, e.g. "Paladin" or "Red Mage"')
    ap.add_argument("--enc", type=int, nargs="*",
                    help="encounter ids (default: whole current tier)")
    ap.add_argument("--top", type=int, default=10, help="pulls per encounter")
    ap.add_argument("--decompose", metavar="NAME",
                    help="decompose the pull whose ranking name contains NAME")
    args = ap.parse_args()
    encs = args.enc or [eid for eid, _name in AAC_HEAVYWEIGHT_ENCOUNTERS]
    if args.decompose:
        decompose(args.job, encs, args.decompose)
    else:
        sweep(args.job, encs, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
