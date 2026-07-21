"""Pull a quartile-stratified sample of REAL Samurai pulls for an encounter and
save them under tests/fixtures/sam/ for the real-data simulator tests.

The SAM analogue of add_reaper_fixtures.py. Validating the sim against its own
synthetic output is circular; these fixtures are real top-rankings pulls across
quartiles so test_samurai_pulls.py checks the sim against actual human play (and
that efficiency correlates with skill).

Run from python/:
    python scripts/add_samurai_fixtures.py <encounter_id> <prefix> [n_per_bucket]

Example:
    python scripts/add_samurai_fixtures.py 103 tyrant 1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE              # noqa: E402
from sidecar.main import _client                       # noqa: E402
from tests.generate_fixtures import _build_fixture     # noqa: E402

JOB = "Samurai"
FFLOGS_SUBTYPE = "Samurai"                            # FFLogs reports it spaceless
SAM_FIXTURES_DIR = (Path(__file__).resolve().parent.parent
                    / "tests" / "fixtures" / "sam")


def _resolve_source_id(client, code: str, fight_id: int, name: str) -> int | None:
    report = client.get_report_summary(code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = report["masterData"]["actors"]
    sam = [a for a in actors
           if a["type"] == "Player" and a.get("subType") == FFLOGS_SUBTYPE
           and a["id"] in friendly]
    by_name = [a for a in sam if a["name"].lower() == (name or "").lower()]
    pick = by_name or sam
    return pick[0]["id"] if pick else None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    encounter_id = int(sys.argv[1])
    prefix = sys.argv[2]
    n_per_bucket = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    client = _client()
    print(f"Fetching Samurai rankings for encounter {encounter_id} ...")
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

    SAM_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for bucket_name, bucket in buckets.items():
        for j in range(min(n_per_bucket, len(bucket))):
            r = bucket[j]
            code = r["report"]["code"]
            fight_id = r["report"]["fightID"]
            parse_pct = r.get("rankPercent") or r.get("percentile")
            source_id = _resolve_source_id(client, code, fight_id, r.get("name", ""))
            if source_id is None:
                print(f"  WARN: no SAM actor for {bucket_name}_{j+1} ({code}/{fight_id})")
                continue
            label = f"{prefix}_{bucket_name}_{j+1}"
            fixture = _build_fixture(client, label, code, fight_id, source_id,
                                     parse_pct=parse_pct)
            if not fixture:
                continue
            # Capture the measured Tengentsu Kenki so the offline test's ceiling
            # spends the same bonus Kenki the player got (the symmetric sim_context
            # — else the 0-bonus ceiling would sit below the player's real
            # Tengentsu-funded Shinten and false-flag >100%). _build_fixture
            # doesn't capture buffs, so we attach it here from the player's stream.
            from jobs.samurai.buffs import measured_tengentsu_kenki
            rep = client.get_report_summary(code)
            fght = next(f for f in rep["fights"] if f["id"] == fight_id)
            fixture["tengentsu_kenki"] = measured_tengentsu_kenki(
                client, code, fght, {"id": source_id})
            (SAM_FIXTURES_DIR / f"{label}.json").write_text(
                json.dumps(fixture, indent=2), encoding="utf-8")
            print(f"  Saved {label}: dur={fixture['duration_s']:.0f}s "
                  f"casts={len(fixture['cast_events'])} parse={parse_pct}")
            saved += 1

    print(f"Total saved: {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
