"""Phase-0 probe for prog-log (wipe) analysis: verify the two FFLogs v2 facts
the feature depends on but the app has never fetched.

Questions (each maps to a design toggle in the prog-logs plan):
  1. Does `characterData.character(lodestoneID:).recentReports(limit:)` exist,
     and what does its `ReportPagination` carry — code / startTime / endTime /
     zone? (Drives `get_character_recent_reports` + whether we can pre-filter
     reports to the tier zone before fetching summaries.)
  2. What do `fightPercentage` / `bossPercentage` / `lastPhase` mean on wipe
     fights — is `fightPercentage` the phase-weighted remaining % of the WHOLE
     fight (the projector input) and `bossPercentage` the current boss's HP
     (display-only)? Checked on a phased ultimate (Dancing Mad) where the two
     must diverge on post-P1 wipes.

Run from python/:
    python scripts/probe_recent_reports.py [--lodestone 12345678]
        [--enc 101] [--spec Machinist] [--report CODE] [--ult-enc 1085]

Without --lodestone the probe resolves one from the encounter's top character
rankings (name + server -> find_character). Without --report it probes fights
on the recent reports it just listed, plus one top-ranked ultimate report
(ranked ultimate kill reports nearly always contain same-session wipes).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import encounter_difficulty  # noqa: E402
from sidecar.main import _client  # noqa: E402

_REGION_SLUGS = {"na": "NA", "eu": "EU", "jp": "JP", "oc": "OC",
                 "north america": "NA", "europe": "EU", "japan": "JP",
                 "oceania": "OC"}


def _resolve_lodestone_from_rankings(client, encounter_id: int,
                                     spec: str) -> int | None:
    """Best-effort: top character rankings -> name+server -> lodestoneID."""
    diff = encounter_difficulty(encounter_id)
    blob = client.get_rankings(encounter_id, spec, spec, difficulty=diff)
    ranks = (blob or {}).get("rankings") or []
    if ranks:
        print("rankings entry keys:", sorted(ranks[0].keys()))
    for r in ranks[:8]:
        name = r.get("name")
        server = r.get("server") or {}
        if isinstance(server, dict):
            s_name = server.get("name") or ""
            s_region = server.get("region") or ""
        else:
            s_name, s_region = str(server), ""
        region = _REGION_SLUGS.get(str(s_region).strip().lower(),
                                   str(s_region).strip().upper()[:2] or "NA")
        slug = s_name.strip().lower().replace(" ", "-").replace("'", "")
        if not (name and slug):
            continue
        try:
            char = client.find_character(name, slug, region)
        except Exception as e:  # noqa: BLE001
            print(f"  find_character({name}, {slug}, {region}) failed: {e}")
            continue
        lid = (char or {}).get("lodestoneID")
        if lid:
            print(f"resolved lodestone {lid} from rank char {name} @ {slug}/{region}")
            return int(lid)
    return None


def probe_recent_reports(client, lodestone_id: int) -> list[str]:
    """[1] recentReports shape. Returns the report codes it found."""
    print(f"\n--- [1] recentReports(lodestoneID={lodestone_id}) ---")
    rich = """
    query($lid: Int!) {
      characterData {
        character(lodestoneID: $lid) {
          id
          name
          recentReports(limit: 10, page: 1) {
            total
            per_page
            current_page
            has_more_pages
            data { code title startTime endTime zone { id name } }
          }
        }
      }
    }
    """
    minimal = """
    query($lid: Int!) {
      characterData {
        character(lodestoneID: $lid) {
          recentReports(limit: 10) { data { code startTime endTime } }
        }
      }
    }
    """
    for label, q in (("rich", rich), ("minimal", minimal)):
        try:
            data = client.query(q, {"lid": lodestone_id})
            char = (data.get("characterData") or {}).get("character") or {}
            rr = char.get("recentReports") or {}
            print(f"{label} selection OK; pagination keys:",
                  sorted(k for k in rr.keys() if k != "data"))
            reports = rr.get("data") or []
            print(f"{len(reports)} reports:")
            for rep in reports:
                print(" ", json.dumps(rep)[:220])
            return [r["code"] for r in reports if r.get("code")]
        except Exception as e:  # noqa: BLE001
            print(f"{label} selection FAILED: {e}")
    return []


def probe_fight_percentages(client, code: str, note: str) -> None:
    """[2] kill/fightPercentage/bossPercentage/lastPhase on every fight."""
    q = """
    query($code: String!) {
      reportData {
        report(code: $code) {
          title
          fights(killType: Encounters) {
            id name encounterID difficulty kill startTime endTime
            fightPercentage bossPercentage lastPhase lastPhaseIsIntermission
          }
        }
      }
    }
    """
    try:
        rep = (client.query(q, {"code": code}) or {}).get(
            "reportData", {}).get("report") or {}
    except Exception as e:  # noqa: BLE001
        print(f"report {code} fights query FAILED: {e}")
        return
    fights = rep.get("fights") or []
    kills = sum(1 for f in fights if f.get("kill"))
    print(f"\nreport {code} ({note}; {rep.get('title')!r}): "
          f"{len(fights)} encounter fights, {kills} kills")
    print(f"  {'id':>3} {'kill':>5} {'dur':>7} {'fight%':>7} {'boss%':>7} "
          f"{'phase':>5} {'inter':>5}  name")
    for f in fights:
        dur = (f["endTime"] - f["startTime"]) / 1000.0
        print(f"  {f['id']:>3} {str(f.get('kill')):>5} {dur:>6.0f}s "
              f"{str(f.get('fightPercentage')):>7} "
              f"{str(f.get('bossPercentage')):>7} "
              f"{str(f.get('lastPhase')):>5} "
              f"{str(f.get('lastPhaseIsIntermission')):>5}  {f.get('name')}")


def _top_report_for(client, encounter_id: int, spec: str) -> str | None:
    diff = encounter_difficulty(encounter_id)
    try:
        blob = client.get_rankings(encounter_id, spec, spec, difficulty=diff)
    except Exception as e:  # noqa: BLE001
        print(f"rankings for enc {encounter_id} failed: {e}")
        return None
    for r in (blob or {}).get("rankings") or []:
        code = (r.get("report") or {}).get("code")
        if code:
            return code
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lodestone", type=int, default=None)
    ap.add_argument("--enc", type=int, default=101)
    ap.add_argument("--spec", default="Machinist")
    ap.add_argument("--report", default=None,
                    help="explicit report code for the fight-percentage probe")
    ap.add_argument("--ult-enc", type=int, default=1085,
                    help="ultimate encounter for the phased-fight probe")
    args = ap.parse_args()

    client = _client()

    lid = args.lodestone
    if lid is None:
        lid = _resolve_lodestone_from_rankings(client, args.enc, args.spec)
    if lid is None:
        raise SystemExit("could not resolve a lodestone id — pass --lodestone")

    codes = probe_recent_reports(client, lid)

    print("\n--- [2] fight percentages (kills vs wipes) ---")
    if args.report:
        probe_fight_percentages(client, args.report, "explicit --report")
    else:
        for code in codes[:2]:
            probe_fight_percentages(client, code, "from recentReports")
        # A phased ultimate is the decisive case: fight% vs boss% must diverge
        # on post-P1 wipes if fight% is phase-weighted whole-fight progress.
        ult = _top_report_for(client, args.ult_enc, "Samurai")
        if ult:
            probe_fight_percentages(client, ult, f"top enc-{args.ult_enc} report")
        else:
            print("no ultimate report found for the phased-fight check")

    print("\n=== probe complete ===")


if __name__ == "__main__":
    main()
