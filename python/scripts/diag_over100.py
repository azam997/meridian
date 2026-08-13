"""Diagnose an over-100% pull against the REAL production ceiling.

Unlike validate_job_ceiling's --decompose (which compares against the bare
gear-point sim with NO sim_context — misleading for budget jobs like DNC),
this reproduces the production strict ceiling exactly: the measured
sim_context payload, the full sub-GCD cadence band, and the perfect sim per
band point. Prints, per band cadence: score, GCD count, and the scored-cast
totals — so an over-100 pull can be localized to (a) a band point that fits
the player's line but under-scores it (fast-point under-search), (b) a
cadence the band misses, or (c) a placement/packing shortfall at every point.

Run from python/:
    python scripts/diag_over100.py Gunbreaker --enc 104 --name Fuseir
    python scripts/diag_over100.py Dancer --enc 105 --name Viola
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import ALL_ENCOUNTERS, encounter_difficulty   # noqa: E402
from jobs import analyze_pull, get_job                        # noqa: E402
from jobs._core.ability_metadata import get_metadata          # noqa: E402
from jobs._core.gcd_speed import unwrap_ceiling_context       # noqa: E402
from sidecar.main import _client                              # noqa: E402


def _is_gcd(aid: int) -> bool:
    m = get_metadata(aid)
    return m is not None and not m.is_ogcd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--enc", type=int, required=True)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()

    client = _client()
    subtype = args.job.replace(" ", "")
    blob = client.get_rankings(args.enc, class_name=subtype, spec_name=subtype,
                               difficulty=encounter_difficulty(args.enc),
                               metric="rdps", page=1)
    r = next(x for x in (blob or {}).get("rankings", [])
             if args.name.lower() in (x.get("name") or "").lower()
             and x.get("report", {}).get("code"))
    code, fid, nm = r["report"]["code"], r["report"]["fightID"], r["name"]
    mr = analyze_pull(args.job, client, code, fid, ranking_name=nm, label=nm)
    st = mr.aspects["Scoring"].state

    dur = st["fight_duration_s"]
    dt = st["downtime_windows"]
    delivered = st["delivered_potency"]
    wgap = float(st.get("ceiling_witness_gap") or 0.0)
    idl_raw = st["idealized_strict"] - wgap
    print(f"{nm}  ({dict(ALL_ENCOUNTERS).get(args.enc, args.enc)})  dur={dur:.1f}s")
    print(f"delivered={delivered:.1f}  idealized_raw={idl_raw:.1f}  "
          f"eff={100 * delivered / idl_raw:.2f}%  witness_gap={wgap:.1f}")

    # The stashed sim_context is the ARG-max band point; peel to get the
    # measured payload, then rebuild the whole band around the gear inference.
    argmax_ctx = st.get("sim_context")
    argmax_gcd, payload = unwrap_ceiling_context(argmax_ctx)
    print(f"argmax band point: {argmax_gcd}   payload: {type(payload).__name__}")

    pkg = args.job.replace(" ", "").lower()
    sim = importlib.import_module(f"jobs.{pkg}.simulator")
    scoring = importlib.import_module(f"jobs.{pkg}.scoring")

    # Rebuild the band the aspect swept: gear inference + the constant band +
    # the demonstrated anchor (gcd_speed internals, same inputs).
    from jobs._core.gcd_speed import (
        CeilingContext, demonstrated_cadence, effective_gcd_for, subgcd_gcd_sweep,
    )
    aspect = next(a for a in get_job(args.job).aspects
                  if getattr(a, "name", "") == "Scoring")
    norm_casts = mr.norm_casts
    haste_excl = aspect.gcd_inference_exclusions(norm_casts)
    infer_excl = list(dt) + haste_excl
    gear = effective_gcd_for(norm_casts, _is_gcd, aspect.gcd_constant, infer_excl)
    cadences = list(subgcd_gcd_sweep(gear, aspect.gcd_constant))
    if aspect.demonstrated_cadence_anchor:
        dem = demonstrated_cadence(norm_casts, _is_gcd, dur, dt, haste_excl)
        if dem is not None:
            dem = max(dem, aspect.gcd_constant * 0.95)
            print(f"demonstrated cadence: {dem:.4f} "
                  f"({'appended' if dem < min(cadences) - 1e-6 else 'covered by band'})")
            if dem < min(cadences) - 1e-6:
                cadences.append(dem)
    print(f"gear inference: {gear:.4f}   band: {[round(c, 4) for c in cadences]}")

    # The replay-seeded ceiling leg's cast channel — without it the rebuilt band
    # would run the bare beam and under-read the production ceiling for an
    # opted-in job (GNB).
    dem_casts = tuple((float(t), int(a)) for t, a in norm_casts) \
        if (getattr(aspect, "replay_seeded_ceiling", False) and norm_casts) else None
    if dem_casts:
        print(f"replay-seeded leg: on ({len(dem_casts)} demonstrated casts)")

    pc = [(t, a) for t, a in norm_casts if t >= 0]
    p_gcds = sum(1 for _t, a in pc if _is_gcd(a))
    print(f"\nplayer: {p_gcds} GCDs / {len(pc)} casts / delivered {delivered:.1f}")
    print(f"{'cadence':>8s} {'score':>10s} {'vs dlv':>8s} {'GCDs':>5s} {'casts':>6s}")
    best = None
    for g in cadences:
        ctx = CeilingContext(gcd_base_s=g, payload=payload,
                             demonstrated=dem_casts)
        score = scoring.idealized_at_duration(dur, dt, None, sim_context=ctx)
        tl, _aux = sim.simulate_idealized_perfect(dur, dt, None, sim_context=ctx)
        casts = [(t, a) for t, a in tl if t >= 0]
        n_g = sum(1 for _t, a in casts if _is_gcd(a))
        print(f"{g:8.4f} {score:10.1f} {score - delivered:+8.1f} "
              f"{n_g:5d} {len(casts):6d}")
        if best is None or score > best[1]:
            best = (g, score, casts)

    # Per-ability scored-count diff at the best band point.
    g, score, casts = best
    print(f"\nbest point {g:.4f} (score {score:.1f}); per-ability count diff "
          f"(player vs best-point sim), by raw potency swing:")
    jd = get_job(args.job).data
    pot = jd.potencies.get
    pcc, scc = Counter(a for _t, a in pc), Counter(a for _t, a in casts)
    rows = []
    for a in set(pcc) | set(scc):
        d = (pcc.get(a, 0) - scc.get(a, 0)) * pot(a, 0)
        if d:
            m = get_metadata(a)
            rows.append((d, m.name if m else str(a), pcc.get(a, 0), scc.get(a, 0)))
    for d, name, p, s in sorted(rows, key=lambda x: -abs(x[0]))[:12]:
        print(f"    {name:<22} player={p:<3} sim={s:<3} p*d={d:+d}")
    if not rows:
        print("    (identical potency-bearing mix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
