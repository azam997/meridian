"""Pull a quartile-stratified sample of REAL Black Mage pulls for an encounter and
save them under tests/fixtures/blm/ for the real-data simulator tests.

The BLM analogue of add_samurai_fixtures.py, but simpler: BLM is RNG-free and has
no measured per-pull buff inputs (no Tengentsu Kenki / Fugetsu), so the fixture is
just the captured cast + targetability + comp streams.

Run from python/:
    python scripts/add_blackmage_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Example:
    python scripts/add_blackmage_fixtures.py 103 tyrant 1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE              # noqa: E402
from sidecar.main import _client                       # noqa: E402
from tests.generate_fixtures import _build_fixture     # noqa: E402

JOB = "Black Mage"
FFLOGS_SUBTYPE = "BlackMage"                          # FFLogs reports it spaceless
BLM_FIXTURES_DIR = (Path(__file__).resolve().parent.parent
                    / "tests" / "fixtures" / "blm")


def _resolve_source_id(client, code: str, fight_id: int, name: str) -> int | None:
    report = client.get_report_summary(code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = report["masterData"]["actors"]
    blm = [a for a in actors
           if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
           and a["id"] in friendly]
    by_name = [a for a in blm if a["name"].lower() == (name or "").lower()]
    pick = by_name or blm
    return pick[0]["id"] if pick else None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    encounter_id = int(sys.argv[1])
    prefix = sys.argv[2]
    n_per_bucket = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    client = _client()
    print(f"Fetching Black Mage rankings for encounter {encounter_id} ...")
    blob = client.get_rankings(
        encounter_id, class_name=JOB, spec_name=JOB,
        difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
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

    BLM_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for bucket_name, bucket in buckets.items():
        for j in range(min(n_per_bucket, len(bucket))):
            r = bucket[j]
            code = r["report"]["code"]
            fight_id = r["report"]["fightID"]
            parse_pct = r.get("rankPercent") or r.get("percentile")
            source_id = _resolve_source_id(client, code, fight_id, r.get("name", ""))
            if source_id is None:
                print(f"  WARN: no BLM actor for {bucket_name}_{j+1} ({code}/{fight_id})")
                continue
            label = f"{prefix}_{bucket_name}_{j+1}"
            fixture = _build_fixture(client, label, code, fight_id, source_id,
                                     parse_pct=parse_pct)
            if not fixture:
                continue
            (BLM_FIXTURES_DIR / f"{label}.json").write_text(
                json.dumps(fixture, indent=2), encoding="utf-8")
            print(f"  Saved {label}: dur={fixture['duration_s']:.0f}s "
                  f"casts={len(fixture['cast_events'])} parse={parse_pct}")
            saved += 1

    print(f"Total saved: {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
