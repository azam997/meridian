"""One-shot helper: fetch a top-ranked Vamp Fatale (M9S, encounter 101)
MCH pull and save it as tests/fixtures/vamp_fatale_topq_1.json.

Vamp Fatale has a confirmed-untargetable phase (boss jumps away mid-fight
for ~51s), so this fixture provides Tier-A regression coverage. All the
other fixtures are Tyrant (M11S), where the boss stays targetable.

Run from python/:
    python scripts/add_vamp_fatale_fixture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config             # noqa: E402
from fflogs_api import FFLogsClient        # noqa: E402
from tests.generate_fixtures import _build_fixture  # noqa: E402


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ENCOUNTER_ID = 101    # Vamp Fatale (M9S) per encounters.py
LABEL = "vamp_fatale_topq_1"


def main() -> int:
    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        print("ERROR: API credentials missing from config.json")
        return 1
    client = FFLogsClient(cfg["client_id"], cfg["client_secret"])

    print(f"Fetching top MCH ranking for encounter {ENCOUNTER_ID} (Vamp Fatale)...")
    blob = client.get_rankings(
        ENCOUNTER_ID, class_name="Machinist", spec_name="Machinist",
        metric="rdps", page=1,
    )
    rankings = (blob or {}).get("rankings") or []
    if not rankings:
        print("ERROR: no rankings returned")
        return 1

    r = rankings[0]
    code = r["report"]["code"]
    fight_id = r["report"]["fightID"]
    name = r.get("name", "")
    parse_pct = r.get("rankPercent") or r.get("percentile")
    print(f"  Top rank: report={code} fight={fight_id} parse={parse_pct} name={name}")

    # Resolve the source ID via the report.
    report = client.get_report_summary(code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        print(f"ERROR: fight {fight_id} not in report {code}")
        return 1
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = report["masterData"]["actors"]
    candidates = [
        a for a in actors
        if a["type"] == "Player" and a.get("subType") == "Machinist"
        and a["id"] in friendly
        and a["name"].lower() == name.lower()
    ]
    if not candidates:
        candidates = [
            a for a in actors
            if a["type"] == "Player" and a.get("subType") == "Machinist"
            and a["id"] in friendly
        ]
    if not candidates:
        print(f"ERROR: no MCH actor in fight {fight_id} of {code}")
        return 1
    source_id = candidates[0]["id"]
    print(f"  source_id={source_id}")

    fixture = _build_fixture(client, LABEL, code, fight_id, source_id,
                              parse_pct=parse_pct)
    if not fixture:
        print("ERROR: fixture build failed")
        return 1

    path = FIXTURES_DIR / f"{LABEL}.json"
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    print(f"Saved -> {path}")
    print(f"  duration: {fixture['duration_s']:.1f}s")
    print(f"  cast_events: {len(fixture['cast_events'])}")
    print(f"  targetability_events: {len(fixture['targetability_events'])}")
    print(f"  master_npc_actors: {len(fixture['master_npc_actors'])}")
    print(f"  fight enemy_npcs: {len(fixture['enemy_npcs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
