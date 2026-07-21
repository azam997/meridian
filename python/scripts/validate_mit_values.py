"""Validate the mitigation library against real logs (network, not pytest).

Three checks over an encounter's top kill logs:
  1. ID audit — every library action/status resolves against masterData
     (wrong hand-authored ids surface immediately by name mismatch).
  2. Mit% audit — for damage-taken hits whose FFLogs `buffs` snapshot contains
     exactly ONE known mit status (plus food), the observed multiplier must
     match (1 - eff) within tolerance. The probe (probe_damage_taken.py)
     established that `buffs` lists exactly the statuses that entered the
     hit's calculation — both boss-side debuffs and victim-side buffs.
  3. Magnitude report — observed shield pools / heal sizes vs the library's
     potency-based estimates (informational).

Run from python/:
    python scripts/validate_mit_values.py [--enc 101] [--tol 0.03] [--logs 6]
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fflogs_api import BundleStream  # noqa: E402
from sidecar.main import _client  # noqa: E402
from mitplan.classify import school_for  # noqa: E402
from mitplan.damage import _pick_logs  # noqa: E402
from mitplan.library import ACTIONS, Target  # noqa: E402
from mitplan.planner import eff_mit  # noqa: E402

AURA_OFFSET = 1_000_000  # FFLogs aura-form ids = 1e6 + status id
IGNORED_STATUS_NAMES = {"Well Fed"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=101)
    ap.add_argument("--tol", type=float, default=0.03)
    ap.add_argument("--logs", type=int, default=6)
    ap.add_argument("--min-samples", type=int, default=20)
    args = ap.parse_args()

    client = _client()
    picks = _pick_logs(client, args.enc)[: args.logs]
    summaries = client.get_report_summaries([c for c, _ in picks])

    # name -> library rows (several jobs share Reprisal/Feint/Addle rows)
    rows_by_status: dict[str, list] = defaultdict(list)
    for a in ACTIONS:
        for s in a.status_names:
            rows_by_status[s].append(a)

    # ---- pass over logs: collect (status_name, school) -> multipliers,
    # observed shield pools, and the id audit.
    mults: dict[tuple[str, str], list[float]] = defaultdict(list)
    pools: dict[str, list[float]] = defaultdict(list)
    id_hits: dict[int, str] = {}      # action_id -> masterData name (if seen)
    known_status_ids_global: set[str] = set()

    for code, fid in picks:
        summary = summaries.get(code) or {}
        fight = next((f for f in summary.get("fights") or []
                      if f.get("id") == fid), None)
        if fight is None:
            continue
        start, end = fight["startTime"], fight["endTime"]
        names = {ab["gameID"]: ab.get("name") or ""
                 for ab in (summary.get("masterData") or {}).get("abilities") or []}
        types = {ab["gameID"]: ab.get("type")
                 for ab in (summary.get("masterData") or {}).get("abilities") or []}
        for a in ACTIONS:
            nm = names.get(a.action_id)
            if nm:
                id_hits.setdefault(a.action_id, nm)
        # aura-form id -> known mit status name
        aura_names: dict[int, str] = {}
        for gid, nm in names.items():
            if gid >= AURA_OFFSET and nm in rows_by_status:
                aura_names[gid] = nm
        known_status_ids_global |= {str(g) for g in aura_names}

        actors = (summary.get("masterData") or {}).get("actors") or []
        by_id = {x["id"]: x for x in actors}
        friendly = [i for i in (fight.get("friendlyPlayers") or [])
                    if by_id.get(i, {}).get("type") == "Player"]

        streams = [BundleStream("DamageTaken", start, end, source_id=i)
                   for i in friendly[:8]]
        streams.append(BundleStream("Buffs", start, end))
        bundles = client.get_event_bundle(code, streams)
        buffs_evs = bundles[-1]
        for ev in buffs_evs:
            absorb = ev.get("absorb")
            if absorb and ev.get("type") in ("applybuff", "refreshbuff"):
                nm = names.get(ev.get("abilityGameID") or 0, "")
                if nm in rows_by_status:
                    pools[nm].append(float(absorb))

        food_ids = {str(gid) for gid, nm in names.items()
                    if gid >= AURA_OFFSET and nm in IGNORED_STATUS_NAMES}
        for evs in bundles[:-1]:
            for ev in evs:
                if ev.get("type") != "damage" or not ev.get("buffs"):
                    continue
                mult = ev.get("multiplier")
                if not isinstance(mult, (int, float)):
                    continue
                ids = [x for x in str(ev["buffs"]).split(".")
                       if x and x not in food_ids]
                known = [aura_names.get(int(x)) for x in ids
                         if int(x) in aura_names]
                unknown = [x for x in ids if int(x) not in aura_names]
                # exactly one KNOWN mit status and nothing else unaccounted
                if len(known) == 1 and not unknown:
                    school = school_for([ev.get("abilityGameID") or 0], types)
                    mults[(known[0], school)].append(float(mult))

    # ---- report ---------------------------------------------------------
    print("=== ID audit (action ids vs masterData) ===")
    bad_ids = 0
    for a in sorted({(x.job, x.name, x.action_id) for x in ACTIONS}):
        job, name, aid = a
        seen = id_hits.get(aid)
        if seen is None:
            print(f"  [?   ] {job:<13} {name:<24} id {aid} never seen in these logs")
        elif seen != name:
            bad_ids += 1
            print(f"  [FAIL] {job:<13} {name:<24} id {aid} is '{seen}' in logs!")
    print(f"  ({len(id_hits)} ids confirmed by name; {bad_ids} mismatches)\n")

    print("=== Mitigation % audit (single-status hits, tol "
          f"+/-{args.tol:.0%}, n >= {args.min_samples}) ===")
    fails = passes = thin = 0
    for (status, school), vals in sorted(mults.items()):
        rows = rows_by_status[status]
        expected = 1.0 - max(eff_mit(a, school) for a in rows)
        med = statistics.median(vals)
        n = len(vals)
        if n < args.min_samples:
            thin += 1
            verdict = "thin"
        elif abs(med - expected) <= args.tol:
            passes += 1
            verdict = "OK  "
        else:
            fails += 1
            verdict = "FAIL"
        print(f"  [{verdict}] {status:<22} {school:<9} observed {med:.3f} "
              f"expected {expected:.3f}  (n={n})")
    print(f"  ({passes} pass, {fails} fail, {thin} thin)\n")

    print("=== Shield pools observed vs potency estimate ===")
    hp_per_pot = 80.0  # ballpark; the model calibrates per log set
    for name, vals in sorted(pools.items()):
        est = next((a.shield_potency * hp_per_pot for a in rows_by_status[name]
                    if a.shield_potency > 0), None)
        med = statistics.median(vals)
        est_s = f"potency est ~{int(est)}" if est else "maxHP-based"
        print(f"  {name:<24} observed median {int(med):>8}  {est_s}  (n={len(vals)})")

    if bad_ids or fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
