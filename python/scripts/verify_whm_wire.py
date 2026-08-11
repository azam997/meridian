"""Verify the WHM wire response the UI consumes (run-app companion, network).

Drives a real top WHM pull through analyze_pull + the sidecar's _build_response
— the exact path the desktop window renders — and asserts the things the visual
check confirms: efficiency headline populated, every abilityMeta entry has a
resolved name + iconPath (no blue "act" placeholders), isDefensive tagging is
correct (healing kit defensive; lily heals / Misery / Glare / Assize NOT), and
the lily economy (Solace/Rapture/Misery) rides the DPS Abilities track.

Run from python/:  python scripts/verify_whm_wire.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE                  # noqa: E402
from jobs import analyze_pull                              # noqa: E402
from jobs.whitemage import data as wd                      # noqa: E402
from sidecar.main import (                                 # noqa: E402
    _build_response, _client, _compare_all_aspects,
)

JOB = "White Mage"


def main() -> int:
    client = _client()
    blob = client.get_rankings(105, class_name=JOB, spec_name=JOB,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in (blob or {}).get("rankings", [])
             if r.get("report", {}).get("code")]
    r = ranks[0]
    code, fid = r["report"]["code"], r["report"]["fightID"]
    print(f"pull: {r.get('name')} {code}/{fid}")

    you = analyze_pull(JOB, client, code, fid,
                       ranking_name=r.get("name"), label="You")
    comparisons = _compare_all_aspects(JOB, you, [])
    resp = _build_response(JOB, you, [], comparisons)

    fails: list[str] = []

    # 1. Headline efficiency.
    h = resp["headline"]
    eff = h.get("efficiencyPct") or h.get("efficiencyPctStrict")
    print(f"\nheadline: eff={eff:.2f}%  potency={h.get('yourPotency'):.0f}  "
          f"ideal={h.get('yourIdealizedPotency'):.0f}  kill={h.get('killTimeSec')}s")
    if not eff or eff <= 0:
        fails.append("headline efficiency not populated")

    # 2. abilityMeta — every entry resolves (no "act <id>" / blank icon).
    meta = resp["abilityMeta"]
    unresolved = [aid for aid, m in meta.items()
                  if not m.get("iconPath") or not m.get("name")
                  or str(m.get("name", "")).lower().startswith(("act ", "action", "unknown"))]
    print(f"\nabilityMeta: {len(meta)} ids, {len(unresolved)} unresolved")
    if unresolved:
        for aid in unresolved:
            print(f"   UNRESOLVED {aid}: {meta[aid]}")
        fails.append(f"{len(unresolved)} unresolved ability icons")

    # 3. isDefensive tagging.
    def is_def(aid):
        return meta.get(int(aid), {}).get("isDefensive")
    rotational = {wd.GLARE_III: "Glare III", wd.GLARE_IV: "Glare IV",
                  wd.DIA: "Dia", wd.ASSIZE: "Assize",
                  wd.AFFLATUS_MISERY: "Misery", wd.AFFLATUS_SOLACE: "Solace",
                  wd.AFFLATUS_RAPTURE: "Rapture", wd.PRESENCE_OF_MIND: "PoM"}
    print("\nisDefensive (rotational must be False):")
    for aid, nm in rotational.items():
        flag = is_def(aid)
        present = aid in meta
        mark = "" if (flag is False or not present) else "  <-- WRONG"
        print(f"   {nm:10s} present={present} isDefensive={flag}{mark}")
        if present and flag:
            fails.append(f"{nm} wrongly tagged defensive")
    # A healing-kit id, if present, must be defensive.
    for aid, nm in ((wd.CURE_III, "Cure III"), (wd.MEDICA_III, "Medica III"),
                    (wd.TETRAGRAMMATON, "Tetragrammaton")):
        if aid in meta and not is_def(aid):
            fails.append(f"{nm} present but NOT tagged defensive")
            print(f"   {nm} present but isDefensive=False  <-- WRONG")

    # 4. Lily economy on the DPS Abilities track.
    abilities = you.aspects.get("Abilities")
    track_ids = {e.ability_id for e in abilities.track.events} if abilities else set()
    print("\nlily economy on the You Abilities track:")
    for aid, nm in ((wd.AFFLATUS_SOLACE, "Solace"), (wd.AFFLATUS_RAPTURE, "Rapture"),
                    (wd.AFFLATUS_MISERY, "Misery"), (wd.GLARE_IV, "Glare IV")):
        present = aid in track_ids
        print(f"   {nm:10s} on track: {present}")
    # At least Misery + one lily heal must show (a 500s+ M12S-P2 pull has them).
    if wd.AFFLATUS_MISERY not in track_ids:
        fails.append("Misery missing from the DPS track")
    if not (track_ids & {wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE}):
        fails.append("no lily heals on the DPS track")

    print("\n" + "=" * 50)
    if fails:
        print(f"FAIL ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS — WHM wire response is fully populated and correctly tagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
