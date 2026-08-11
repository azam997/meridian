"""Pull a small quartile-stratified MCH sample for an arbitrary encounter
and save the fixtures under tests/fixtures/ with an encounter-specific
prefix.

Generalizes scripts/add_vamp_fatale_fixture.py to any encounter so we can
validate the downtime model against fights that actually have downtime
phases (M10S, M12S P1/P2), not just M11S The Tyrant (which has none).

Run from python/:
    python scripts/add_encounter_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Example:
    python scripts/add_encounter_fixtures.py 104 m12s_p1 1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                       # noqa: E402
from fflogs_api import FFLogsClient                  # noqa: E402
from tests.generate_fixtures import _build_fixture   # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _resolve_source_id(client: FFLogsClient, code: str, fight_id: int,
                       name: str) -> int | None:
    report = client.get_report_summary(code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = report["masterData"]["actors"]
    by_name = [a for a in actors
               if a["type"] == "Player" and a.get("subType") == "Machinist"
               and a["id"] in friendly and a["name"].lower() == name.lower()]
    any_mch = [a for a in actors
               if a["type"] == "Player" and a.get("subType") == "Machinist"
               and a["id"] in friendly]
    pick = by_name or any_mch
    return pick[0]["id"] if pick else None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    encounter_id = int(sys.argv[1])
    prefix = sys.argv[2]
    n_per_bucket = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        print("ERROR: API credentials missing from config.json")
        return 1
    client = FFLogsClient(cfg["client_id"], cfg["client_secret"])

    print(f"Fetching MCH rankings for encounter {encounter_id} ...")
    blob = client.get_rankings(
        encounter_id, class_name="Machinist", spec_name="Machinist",
        metric="rdps", page=1,
    )
    rankings = [r for r in ((blob or {}).get("rankings") or [])
                if r.get("report") and r["report"].get("code")]
    if not rankings:
        print("ERROR: no rankings returned")
        return 1
    rankings.sort(key=lambda r: r.get("rankPercent") or r.get("percentile") or 0,
                  reverse=True)
    n = len(rankings)
    qsize = max(1, n // 4)
    buckets = {
        "topq": rankings[:qsize],
        "q2":   rankings[qsize:2 * qsize],
        "q3":   rankings[2 * qsize:3 * qsize],
        "botq": rankings[3 * qsize:],
    }

    saved = 0
    for bucket_name, bucket in buckets.items():
        for j in range(min(n_per_bucket, len(bucket))):
            r = bucket[j]
            code = r["report"]["code"]
            fight_id = r["report"]["fightID"]
            name = r.get("name", "")
            parse_pct = r.get("rankPercent") or r.get("percentile")
            source_id = _resolve_source_id(client, code, fight_id, name)
            if source_id is None:
                print(f"  WARN: no MCH actor for {bucket_name}_{j+1} "
                      f"({code}/{fight_id})")
                continue
            label = f"{prefix}_{bucket_name}_{j+1}"
            fixture = _build_fixture(client, label, code, fight_id,
                                     source_id, parse_pct=parse_pct)
            if not fixture:
                continue
            path = FIXTURES_DIR / f"{label}.json"
            path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
            dt_evs = len(fixture["targetability_events"])
            print(f"  Saved {label}: dur={fixture['duration_s']:.0f}s "
                  f"casts={len(fixture['cast_events'])} tgt_events={dt_evs}")
            saved += 1

    print(f"Total saved: {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
