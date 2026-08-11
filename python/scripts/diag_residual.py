"""Diagnostic: dissect the "Other" residual for one specific pull.

Reproduces the EXACT production analysis (full refs + Tier-B + multi-target
crediting, then `_idealized_timeline` -> `_build_improvements`) for a pull given by
report code + fight id, then decomposes the "Other" residual into named sources:

  * multi-target / AoE under-delivery, per confirmed window (the dominant piece on a
    credited pull) — already computed as ceilingSplash - deliveredSplash per window;
  * single-target diffuse — GCD cadence + Queen/burst under-delivery;
  * a Reassemble guaranteed-crit-DH leak table (card's stale 0.30 basis vs the
    scorer's true GUARANTEED_CRIT_DH_MULT basis).

IMPORTANT: bare `analyze_pull` shows the single-target SHADOW (no AoE credit) and is
misleading on a multi-target fight; this runs the full pipeline like the sidecar does.

Run from python/:
    python scripts/diag_residual.py --report <REPORT_CODE> --fight 1 --enc 102
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull                                  # noqa: E402
from jobs._core import ability_metadata                        # noqa: E402
from jobs._core.job import get_job                             # noqa: E402
from jobs.machinist import data as md                          # noqa: E402
from jobs.machinist.reassemble import (                        # noqa: E402
    _VALID_REASSEMBLE_TARGETS, _FMF_ID, _REASSEMBLE_ID,
)
from sidecar.main import (                                     # noqa: E402
    _client, _get_refs, _inject_tier_b, _inject_multi_target,
    _idealized_timeline, _build_improvements,
)

JOB = "Machinist"
_BEST_TARGET_POT = 660.0   # Drill / Air Anchor / Chain Saw / Excavator
_PROG = lambda *a, **k: None   # noqa: E731


def _is_gcd(aid: int) -> bool:
    m = ability_metadata.get_metadata(aid)
    return m is not None and not m.is_ogcd


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def per_ability_diff(player_tl, ideal_tl):
    pc = Counter(a for t, a in player_tl if t >= 0)
    cc = Counter(a for t, a in ideal_tl if t >= 0)
    rows = []
    for a in set(pc) | set(cc):
        d = cc.get(a, 0) - pc.get(a, 0)
        if d == 0:
            continue
        pot = md.POTENCIES.get(a, 0)
        rows.append((d * pot, _name(a), pc.get(a, 0), cc.get(a, 0), pot, d))
    return sorted(rows, key=lambda x: -abs(x[0]))


def reassemble_table(norm_casts):
    mult_bonus = md.GUARANTEED_CRIT_DH_MULT - 1.0
    casts = list(norm_casts)
    rows = []
    for i, (t, aid) in enumerate(casts):
        if aid != _REASSEMBLE_ID:
            continue
        target = None
        for j in range(i + 1, len(casts)):
            t2, a2 = casts[j]
            if t2 - t > 5.0:
                break
            m = ability_metadata.get_metadata(a2)
            if m is None or m.is_ogcd:
                continue
            target = a2
            break
        pot = md.POTENCIES.get(target, 0) if target is not None else 0
        if target is None or target in _VALID_REASSEMBLE_TARGETS:
            card = true = 0.0
        elif target == _FMF_ID:
            card, true = 200.0, _BEST_TARGET_POT * mult_bonus
        else:
            card = max(0.0, (_BEST_TARGET_POT - pot) * 0.30)
            true = max(0.0, (_BEST_TARGET_POT - pot) * mult_bonus)
        rows.append((t, _name(target) if target else "(none)", pot, card, true))
    return rows


def run(code, fid, enc):
    c = _client()
    you = analyze_pull(JOB, c, code, fid, ranking_name=None, label="You")
    refs = _get_refs(c, JOB, enc, "Top 10", _PROG)
    _inject_tier_b(JOB, you, refs)
    _inject_multi_target(JOB, you, refs)

    s = you.aspects["Scoring"].state
    dur = s["fight_duration_s"]
    deliv, idl = s["delivered_potency"], s["idealized_strict"]
    cred = bool(s.get("multi_target_credited"))
    dmt = s.get("delivered_multitarget", deliv)
    imt = s.get("idealized_multitarget", idl)
    gap = (imt - dmt) if cred else (idl - deliv)

    print(f"\n=== {code}#{fid}  dur={dur:.0f}s  refs={len(refs)}  "
          f"{'MT-CREDITED' if cred else 'single-target'} ===")
    print(f"ST: delivered={deliv:.0f} idealized={idl:.0f} ({100*deliv/idl:.2f}%)")
    if cred:
        print(f"MT: delivered={dmt:.0f} idealized={imt:.0f} gap={gap:.0f} "
              f"({100*dmt/imt:.2f}%)")
        aoe = s.get("mt_ceiling_delta", 0) - s.get("mt_delivered_delta", 0)
        print(f"    AoE under-delivery = ceiling_delta {s.get('mt_ceiling_delta',0):.0f}"
              f" - delivered_delta {s.get('mt_delivered_delta',0):.0f} = {aoe:.0f}")

    ideal = _idealized_timeline(JOB, you)
    cards = _build_improvements(JOB, you, ideal)
    located = sum(float(cd["lostPotency"] or 0) for cd in cards
                  if cd.get("kind") != "residual" and (cd["lostPotency"] or 0) > 0)
    res = next((cd for cd in cards if cd.get("kind") == "residual"), None)
    other = float(res["lostPotency"]) if res else 0.0
    print(f"\n--- cards: located={located:.0f}  OTHER={other:.0f}  "
          f"(located+other={located+other:.0f} vs gap {gap:.0f}) ---")
    for cd in cards:
        lp = float(cd["lostPotency"] or 0)
        if lp > 0 or cd.get("kind") == "residual":
            print(f"  {cd.get('kind'):<13}{lp:8.0f}  {(cd.get('summary') or '')[:66]}")

    # --- bucket 0: per-window AoE under-delivery ---
    print("\n--- [bucket 0] multi-target / AoE under-delivery (per window) ---")
    aoe_sum = 0.0
    for w in s.get("multi_target_windows", []):
        short = w["ceilingSplash"] - w["deliveredSplash"]
        aoe_sum += short
        print(f"  {_mmss(w['startSec'])}-{_mmss(w['endSec'])}  tgt={w['targetCount']}  "
              f"delivered={w['deliveredSplash']:.0f} ceiling={w['ceilingSplash']:.0f} "
              f"-> short={short:.0f}")
    print(f"  AoE bucket total = {aoe_sum:.0f}")

    # --- ST diffuse: cadence + Queen ---
    pg = sum(1 for t, a in you.norm_casts if _is_gcd(a) and t >= 0)
    ig = sum(1 for t, a in ideal if _is_gcd(a) and 0 <= t <= dur)
    clip = (you.aspects.get("Clipping").state if you.aspects.get("Clipping") else {})
    f = clip.get("clipping")
    eff = getattr(f, "effective_gcd_s", 2.5) or 2.5
    idle_s = getattr(f, "total_idle_s", 0.0) or 0.0
    filler = get_job(JOB).data.filler_gcd_potency
    cadence_gcds = max(0.0, (ig - pg) - idle_s / eff)
    print(f"\n--- [buckets 1/3] ST diffuse ~{other - aoe_sum:.0f} (Other - AoE) ---")
    print(f"  GCDs player={pg} ideal={ig} eff={eff:.3f}s  cadence~{cadence_gcds:.1f} "
          f"GCDs x {filler}p = ~{cadence_gcds*filler:.0f}p")
    pq = Counter(a for t, a in you.norm_casts if t >= 0).get(16501, 0)
    iq = Counter(a for t, a in ideal if t >= 0).get(16501, 0)
    print(f"  Queen summons player={pq} ideal={iq} (burst under-delivery)")

    print("\n--- Reassemble crit-DH leak (card 0.30 vs true x%.2f) ---"
          % md.GUARANTEED_CRIT_DH_MULT)
    rt = reassemble_table(you.norm_casts)
    leak = sum(true - card for _t, _n, _p, card, true in rt)
    bad = [r for r in rt if r[3] != r[4]]
    print(f"  {len(rt)} Reassembles, {len(bad)} misplaced, total leak into Other = {leak:.0f}")
    for t, tn, p, card, true in bad:
        print(f"    {_mmss(t)} ->{tn} {p}p  card={card:.0f} true={true:.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--fight", type=int, required=True)
    ap.add_argument("--enc", type=int, required=True, help="encounter id (e.g. 102)")
    a = ap.parse_args()
    run(a.report, a.fight, a.enc)


if __name__ == "__main__":
    main()
