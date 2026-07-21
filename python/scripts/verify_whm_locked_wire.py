"""Verify the WHM mit-plan LOCKED wire response end-to-end (network).

The healer-flow companion to verify_whm_wire.py: drives a real top WHM pull
through the full locked pipeline — `_heal_lock_payload` (real mitplan damage
model + plan, comp resolved from the pull's actors) → `analyze_pull` with the
staged `__heal_locks__` → `_build_response` — and asserts what the dashboard
walkthrough would: heal-lock headline fields present, rankSuppressed set, the
locked ceiling at-or-below the unlocked one, locked heal casts on the
idealized lane, and the >100% case exempt from the anomaly stamp.

Run from python/:  python scripts/verify_whm_locked_wire.py [encounterId]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE                    # noqa: E402
from jobs import analyze_pull                                # noqa: E402
from jobs.whitemage import data as wd                        # noqa: E402
from sidecar.main import (                                   # noqa: E402
    _build_response, _client, _compare_all_aspects, _heal_lock_payload,
    _stamp_ceiling_anomaly,
)

JOB = "White Mage"


def main() -> int:
    enc_id = int(sys.argv[1]) if len(sys.argv) > 1 else 105
    client = _client()
    blob = client.get_rankings(enc_id, class_name=JOB, spec_name=JOB,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in (blob or {}).get("rankings", [])
             if r.get("report", {}).get("code")]
    if not ranks:
        print(f"no {JOB} rankings for encounter {enc_id}")
        return 1
    r = ranks[0]
    code, fid = r["report"]["code"], r["report"]["fightID"]
    print(f"pull: {r.get('name')} {code}/{fid} enc={enc_id}")

    def progress(pct, stage, tasks=None, step=None):
        print(f"  [{pct:3d}%] {stage}")

    fails: list[str] = []

    # 1. The lock payload from the real mitplan model + this pull's comp.
    payload = _heal_lock_payload(client, JOB, code, fid, enc_id, None, progress)
    if payload is None:
        print("FAIL: _heal_lock_payload returned None")
        return 1
    print(f"\npayload: source={payload['source']} comp={payload['comp']}")
    print(f"  locks={len(payload['locks'])} count={payload['count']} "
          f"potency={payload['potency']} costed={payload['plan_costed_count']}")
    for lk in payload["locks"][:8]:
        print(f"    {lk.ability_id} x{lk.count} in [{lk.start_s:.1f}, {lk.end_s:.1f})")
    if payload["source"] != "pull":
        fails.append(f"comp source {payload['source']} != pull")

    # 2. Locked vs unlocked analysis of the same pull.
    you_locked = analyze_pull(JOB, client, code, fid,
                              ranking_name=r.get("name"), label="You",
                              extra_report={"__heal_locks__": payload})
    you_unlocked = analyze_pull(JOB, client, code, fid,
                                ranking_name=r.get("name"), label="You")
    sl = you_locked.aspects["Scoring"].state
    su = you_unlocked.aspects["Scoring"].state
    eff_locked = 100 * sl["delivered_potency"] / sl["idealized_strict"]
    eff_unlocked = 100 * su["delivered_potency"] / su["idealized_strict"]
    print(f"\nunlocked: ideal={su['idealized_strict']:.0f} eff={eff_unlocked:.2f}%")
    print(f"locked:   ideal={sl['idealized_strict']:.0f} eff={eff_locked:.2f}%")
    if not sl.get("heal_locks_applied"):
        fails.append("heal_locks_applied missing from locked scoring state")
    if sl["idealized_strict"] > su["idealized_strict"] + 1e-6:
        fails.append("locked ceiling ABOVE unlocked (locks must only cost)")
    if abs(sl["delivered_potency"] - su["delivered_potency"]) > 1e-6:
        fails.append("delivered changed under locks")

    # 3. The wire response.
    comparisons = _compare_all_aspects(JOB, you_locked, [])
    resp = _build_response(JOB, you_locked, [], comparisons)
    h = resp["headline"]
    print(f"\nheadline: eff={h['efficiencyPctStrict']:.2f}% "
          f"healLocks={h.get('healLocksApplied')} n={h.get('healLockCount')} "
          f"p={h.get('healLockPotency')} rankSuppressed={h.get('rankSuppressed')} "
          f"compSource={h.get('mitPlanCompSource')}")
    if not h.get("rankSuppressed"):
        fails.append("rankSuppressed missing")
    if payload["locks"] and not h.get("healLocksApplied"):
        fails.append("healLocksApplied missing from headline")
    if h.get("mitPlanCompSource") != "pull":
        fails.append("mitPlanCompSource != pull")

    # 4. Locked heal casts render on the idealized lane.
    if payload["locks"]:
        lane_ids = {c.get("abilityId") for c in resp["idealizedTrack"]}
        lock_ids = {lk.ability_id for lk in payload["locks"]}
        # Rapture locks may resolve as Solace/Rapture; Medica III as itself.
        heal_ids = lock_ids | {wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE}
        shown = lane_ids & heal_ids
        print(f"idealized lane heal casts: {sorted(shown)}")
        if wd.MEDICA_III in lock_ids and wd.MEDICA_III not in lane_ids:
            fails.append("Medica III lock missing from the idealized lane")

    # 5. Anomaly stamp: a locked you-run must never stamp (even over 100%).
    out = {"headline": h}
    _stamp_ceiling_anomaly(out, you_locked, [], JOB, code, fid, enc_id,
                           "Top 10", r.get("name"))
    if "ceilingAnomaly" in h:
        fails.append("ceilingAnomaly stamped on a locked you-run")
    print(f"anomaly stamp on locked run: {'ceilingAnomaly' in h} "
          f"(eff {h['efficiencyPctStrict']:.2f}%)")

    print("\n" + "=" * 50)
    if fails:
        print(f"FAIL ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS — locked WHM wire response verified end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
