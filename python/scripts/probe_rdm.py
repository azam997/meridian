"""Live RDM calibration probe (network — needs FFLogs creds in config.json).

Pulls real Red Mage data to validate the best-effort tables in
`jobs/redmage/data.py`:

  1. Resolve a named RDM main, list their tier encounters + best pulls.
  2. Diff the report's `masterData.abilities` (the authoritative gameID -> name
     map) against our declared action ids, and confirm the Verfire/Verstone
     Ready proc status ids.
  3. Flag any ability the player CAST that we don't have potency for.
  4. Run `analyze_pull` on their best pull -> efficiency, proc budget.
  5. Fetch the top-10 RDM rankings (confirms the FFLogs className wiring) and
     report their per-second potency band to calibrate the ceiling against.

Run from python/:  python scripts/probe_rdm.py [Character Name] [server] [region]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ZONE_ID, DIFFICULTY_SAVAGE  # noqa: E402
from fflogs_api import fflogs_spec_slug  # noqa: E402
from jobs import analyze_pull  # noqa: E402
from jobs._core.actors import find_fight, find_player_actor  # noqa: E402
from jobs.redmage import data as rd  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Red Mage"
NAME = sys.argv[1] if len(sys.argv) > 1 else "Lazuli Lunaris"
SERVER = sys.argv[2] if len(sys.argv) > 2 else "coeurl"
REGION = sys.argv[3] if len(sys.argv) > 3 else "NA"

# id -> our constant label (e.g. 37004 -> 'JOLT_III')
_LABELS = {v: k for k, v in vars(rd).items()
           if isinstance(v, int) and k.isupper() and v > 1000}


def hr(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def main() -> int:
    client = _client()

    hr(f"1. Character: {NAME} / {SERVER} / {REGION}  (FFLogs spec={fflogs_spec_slug(JOB)})")
    char = client.find_character(name=NAME, server_slug=SERVER.lower(),
                                 server_region=REGION)
    if not char:
        print("  NOT FOUND")
        return 1
    lid = char["lodestoneID"]
    print(f"  lodestoneID={lid}  name={char['name']}")

    encs = client.get_character_zone_encounters(
        lid, AAC_HEAVYWEIGHT_ZONE_ID, spec_name=JOB, difficulty=DIFFICULTY_SAVAGE)
    print(f"  RDM encounters with kills: {len(encs)}")
    for e in encs:
        print(f"    [{e['id']}] {e['name']:32s} kills={e['total_kills']:3d} "
              f"bestParse={e.get('best_parse_pct')}")
    if not encs:
        print("  no RDM kills logged for this character on the current tier")
        return 1

    enc = max(encs, key=lambda e: (e.get("best_parse_pct") or 0))
    pulls = client.get_character_encounter_pulls(
        lid, enc["id"], spec_name=JOB, difficulty=DIFFICULTY_SAVAGE)
    pulls = [p for p in pulls if p.get("report_code")]
    if not pulls:
        print("  no pulls")
        return 1
    pull = max(pulls, key=lambda p: (p.get("parse_pct") or 0))
    code, fid = pull["report_code"], pull["fight_id"]
    print(f"\n  Best pull: {enc['name']}  parse={pull.get('parse_pct')}  "
          f"dps={pull.get('dps')}  dur={pull.get('duration_s')}s  "
          f"report={code}#{fid}")

    # --- 2. ID + proc-status validation -----------------------------------
    hr("2. Action-id + proc-status validation (masterData.abilities)")
    report = client.get_report_summary(code)
    abil = {a["gameID"]: a["name"]
            for a in ((report.get("masterData") or {}).get("abilities") or [])}
    name_to_id: dict[str, int] = {}
    for gid, nm in abil.items():
        name_to_id.setdefault(nm, gid)

    print("  Our declared ids vs the report's name for that id:")
    mismatches = 0
    for our_id, label in sorted(_LABELS.items(), key=lambda kv: kv[1]):
        real = abil.get(our_id)
        mark = "ok " if real else "?? "
        if real is None:
            # Not referenced in this report (maybe just unused this pull).
            mark = " · "
        print(f"    {mark} {label:26s} id={our_id:7d}  report={real!r}")

    print("\n  Proc statuses (expect Verfire Ready / Verstone Ready):")
    for status_label, our_sid in (("Verfire Ready", rd.VERFIRE_READY_STATUS_ID),
                                  ("Verstone Ready", rd.VERSTONE_READY_STATUS_ID)):
        real_id = name_to_id.get(status_label)
        ours_name = abil.get(our_sid)
        flag = "MATCH" if real_id == our_sid else "*** MISMATCH ***"
        if real_id is None:
            flag = "(status not in report abilities)"
        else:
            mismatches += (real_id != our_sid)
        print(f"    {status_label:16s} ours={our_sid}  report_id={real_id}  "
              f"(id {our_sid} is {ours_name!r})  {flag}")

    # --- 3. Cast abilities we don't price ---------------------------------
    hr("3. Player casts — anything we don't have potency for")
    fight = find_fight(report, fid)
    actor = find_player_actor(report, fight=fight, job_name=JOB,
                              player_name=char["name"])
    s, e = fight["startTime"], fight["endTime"]
    casts = client.get_events(code, s, e, actor["id"], data_type="Casts")
    cc = Counter(ev.get("abilityGameID") for ev in casts)
    print(f"  {len(casts)} casts, {len(cc)} distinct actions")
    for aid, n in cc.most_common():
        priced = aid in rd.POTENCIES
        nm = abil.get(aid, "?")
        flag = "" if priced else "  <-- NOT in POTENCIES"
        print(f"    {nm:26s} id={aid:7d}  x{n:3d}  "
              f"label={_LABELS.get(aid, '-'):20s}{flag}")

    # --- 4. analyze this pull --------------------------------------------
    hr("4. analyze_pull on the best pull")
    mr = analyze_pull(JOB, client, code, fid, ranking_name=char["name"], label="probe")
    sc = mr.aspects["Scoring"].state
    pr = mr.aspects["Procs"].state
    deliv, ideal = sc["delivered_potency"], sc["idealized_strict"]
    dur = sc["fight_duration_s"]
    print(f"  duration={dur:.0f}s  delivered={deliv:.0f}  idealized_strict={ideal:.0f}")
    print(f"  efficiency_strict={100*deliv/ideal:.1f}%   "
          f"delivered_pps={deliv/dur:.1f}")
    print(f"  proc_budget={sc.get('proc_budget')}  "
          f"procs used/granted/wasted="
          f"{pr['total_used']}/{pr['total_grants']}/{pr['total_wasted']}  "
          f"util={pr['utilization_pct']}%")

    # --- 5. top-10 RDM refs ----------------------------------------------
    hr("5. Top RDM rankings (near-perfect play) for calibration")
    rankings = client.get_rankings(
        encounter_id=enc["id"], class_name=JOB, spec_name=JOB,
        difficulty=DIFFICULTY_SAVAGE)
    ranks = ((rankings or {}).get("rankings") or [])[:10]
    print(f"  got {len(ranks)} ranked RDM parses for {enc['name']}")
    pps_band = []
    for i, r in enumerate(ranks[:5], 1):
        rep = r.get("report") or {}
        rc, rf = rep.get("code"), rep.get("fightID")
        if not rc or rf is None:
            continue
        try:
            rmr = analyze_pull(JOB, client, rc, rf,
                               ranking_name=r.get("name"), label=f"#{i}")
            rsc = rmr.aspects["Scoring"].state
            d, idl = rsc["delivered_potency"], rsc["idealized_strict"]
            du = rsc["fight_duration_s"]
            pps_band.append(d / du)
            print(f"    #{i} {r.get('name','?'):16s} parse={r.get('rankPercent')}  "
                  f"eff={100*d/idl:5.1f}%  pps={d/du:6.1f}  dur={du:.0f}s")
        except Exception as ex:
            print(f"    #{i} {r.get('name','?'):16s} analyze failed: {ex}")
    if pps_band:
        print(f"\n  top-RDM delivered pps band: "
              f"{min(pps_band):.1f}–{max(pps_band):.1f} "
              f"(sim ceiling pps ~ {ideal/dur:.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
