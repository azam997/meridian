"""One-time fixture generator for ExecutionAspect unit tests.

Pulls a stratified sample of MCH Tyrant kills from FFLogs:
- The saved character's most recent Tyrant pull (`user`)
- 2 random kills from each parse-pct quartile of the top rankings page

Saves each as JSON in tests/fixtures/ with the minimal data the analyzer needs:
report_code, fight_id, source_id, fight_start_ms, fight_end_ms, duration_s,
parse_pct, label, and the cast event list.

Re-running with the same seed re-picks the same samples. Add/remove fixtures
by deleting / restoring files in tests/fixtures/ — the test runner picks up
whatever it finds.

NOTE: committed fixtures are anonymized by hand after generation (player
names and the user pull's report code are scrubbed — the deny-list in
scripts/export_public.py enforces it). Re-scrub after regenerating.

Usage:  python tests/generate_fixtures.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# Allow `python tests/generate_fixtures.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from fflogs_api import FFLogsClient


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEED = 42
ENCOUNTER_ID = 103   # The Tyrant per encounters.py
RANKINGS_PAGE = 1    # first page (top 50 or 100)
SAMPLES_PER_QUARTILE = 2


def _fetch_casts(client: FFLogsClient, report_code: str,
                  fight_start_ms: int, fight_end_ms: int,
                  source_id: int) -> list[dict]:
    """Pull casts including a 10s pre-pull lookback."""
    fetch_start = fight_start_ms - 10000
    return client.get_events(report_code, fetch_start, fight_end_ms,
                              source_id, data_type="Casts")


def _save_fixture(name: str, fixture: dict) -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return path


def _build_fixture(client: FFLogsClient, label: str, report_code: str,
                    fight_id: int, source_id: int,
                    parse_pct: float | None = None) -> dict | None:
    """Fetch the fight metadata + casts for a single (report, fight, player).

    Also captures Tier-A inputs (targetability events, enemyNPCs,
    masterData NPC actors) so the MockClient in tests can serve the
    downtime path realistically.
    """
    report = client.get_report_summary(report_code)
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        print(f"  WARN: fight {fight_id} not found in {report_code}")
        return None
    fight_start = fight["startTime"]
    fight_end = fight["endTime"]
    duration_s = (fight_end - fight_start) / 1000.0
    casts = _fetch_casts(client, report_code, fight_start, fight_end, source_id)
    try:
        tgt_events = client.get_targetability_events(
            report_code, fight_start, fight_end,
        )
    except Exception as e:
        print(f"  WARN: targetability fetch failed for {report_code}/{fight_id}: {e}")
        tgt_events = []
    enemy_npcs = fight.get("enemyNPCs") or []
    all_actors = report.get("masterData", {}).get("actors") or []
    npc_actors = [a for a in all_actors if a.get("type") == "NPC"]
    # Party composition — the friendly players in this fight, with their
    # job (subType). Drives composition-aware raid-buff alignment.
    friendly = set(fight.get("friendlyPlayers") or [])
    friendly_actors = [
        {"id": a["id"], "name": a.get("name"), "subType": a.get("subType")}
        for a in all_actors
        if a.get("type") == "Player" and a["id"] in friendly
    ]
    return {
        "label": label,
        "report_code": report_code,
        "fight_id": fight_id,
        "source_id": source_id,
        "fight_start_ms": fight_start,
        "fight_end_ms": fight_end,
        "duration_s": duration_s,
        "parse_pct": parse_pct,
        "cast_events": casts,
        "targetability_events": tgt_events,
        "enemy_npcs": enemy_npcs,
        "master_npc_actors": npc_actors,
        "friendly_actors": friendly_actors,
    }


def _fetch_user_pull(client: FFLogsClient, cfg: dict) -> dict | None:
    """Most recent Tyrant pull for the saved character."""
    lodestone_id = cfg.get("lodestone_id")
    if not lodestone_id:
        print("  No saved character — skipping user fixture")
        return None
    pulls = client.get_character_encounter_pulls(
        lodestone_id, encounter_id=ENCOUNTER_ID, spec_name="Machinist")
    if not pulls:
        print("  No Tyrant pulls for saved character — skipping user fixture")
        return None
    pull = pulls[0]
    print(f"  User pull: report={pull['report_code']} fight={pull['fight_id']} "
          f"duration={pull['duration_s']:.0f}s parse={pull['parse_pct']:.1f}%")

    # We need source_id (the player actor in this report). Look it up from the report.
    report = client.get_report_summary(pull["report_code"])
    fight = next((f for f in report["fights"] if f["id"] == pull["fight_id"]), None)
    if fight is None:
        return None
    actors = report["masterData"]["actors"]
    friendly = set(fight.get("friendlyPlayers") or [])
    candidates = [a for a in actors
                  if a["type"] == "Player" and a.get("subType") == "Machinist"
                  and a["id"] in friendly]
    if not candidates:
        print("  WARN: no MCH actor in user's pull")
        return None
    source_id = candidates[0]["id"]
    return _build_fixture(client, "user_tyrant_recent", pull["report_code"],
                           pull["fight_id"], source_id,
                           parse_pct=pull["parse_pct"])


def _fetch_quartile_samples(client: FFLogsClient, rng: random.Random) -> list[dict]:
    """Pull top rankings, sort by parse_pct, sample SAMPLES_PER_QUARTILE per quartile."""
    print("  Fetching MCH Tyrant rankings (page 1)...")
    rankings_blob = client.get_rankings(
        ENCOUNTER_ID, class_name="Machinist", spec_name="Machinist",
        metric="rdps", page=RANKINGS_PAGE,
    )
    rankings = rankings_blob.get("rankings") or []
    print(f"  Got {len(rankings)} rankings")
    # Each ranking has: name, amount, report.code, report.fightID, percentile, ...
    rankings = [r for r in rankings if r.get("report") and r["report"].get("code")]
    if not rankings:
        return []

    # Sort by percentile descending so quartiles partition top-to-bottom.
    # NOTE: page 1 of rankings is already the top performers (1%-ish). The
    # "quartile" here is across the top rankings page, not the entire player
    # population. That's intentional — we want a stratified sample of
    # *competitive* players, not low-effort runs.
    rankings.sort(key=lambda r: r.get("rankPercent") or r.get("percentile") or 0,
                   reverse=True)
    n = len(rankings)
    quartile_size = max(1, n // 4)
    quartiles = [
        rankings[0:quartile_size],                              # top
        rankings[quartile_size:quartile_size * 2],              # 2nd
        rankings[quartile_size * 2:quartile_size * 3],          # 3rd
        rankings[quartile_size * 3:],                            # bottom
    ]

    picked: list[dict] = []
    for i, q in enumerate(quartiles):
        label_prefix = ["topq", "q2", "q3", "botq"][i]
        if not q:
            continue
        k = min(SAMPLES_PER_QUARTILE, len(q))
        sample = rng.sample(q, k)
        for j, r in enumerate(sample, 1):
            label = f"{label_prefix}_{j}"
            report_code = r["report"]["code"]
            fight_id = r["report"]["fightID"]
            # ranking entries have a per-player "sourceID" via the player's
            # spec name + duration; for FFLogs we treat the ranking's own
            # ID field if present, else look up via report.
            source_id = r.get("report", {}).get("playerID")
            if source_id is None:
                # Look up source from report summary by character name match
                report = client.get_report_summary(report_code)
                fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
                if fight is None:
                    continue
                actors = report["masterData"]["actors"]
                friendly = set(fight.get("friendlyPlayers") or [])
                name = r.get("name", "")
                candidates = [a for a in actors
                              if a["type"] == "Player"
                              and a.get("subType") == "Machinist"
                              and a["id"] in friendly
                              and a["name"].lower() == name.lower()]
                if not candidates:
                    # Fallback: any MCH in the fight
                    candidates = [a for a in actors
                                  if a["type"] == "Player"
                                  and a.get("subType") == "Machinist"
                                  and a["id"] in friendly]
                if not candidates:
                    print(f"  WARN: couldn't find MCH actor for {label} in {report_code}")
                    continue
                source_id = candidates[0]["id"]
            parse_pct = r.get("rankPercent") or r.get("percentile")
            print(f"  {label}: report={report_code} fight={fight_id} "
                  f"parse={parse_pct}")
            fixture = _build_fixture(client, label, report_code, fight_id,
                                      source_id, parse_pct=parse_pct)
            if fixture:
                picked.append(fixture)
    return picked


def main() -> None:
    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        print("ERROR: API credentials not in config.")
        sys.exit(1)
    client = FFLogsClient(cfg["client_id"], cfg["client_secret"])
    rng = random.Random(SEED)

    print("=== User fixture ===")
    user_fix = _fetch_user_pull(client, cfg)
    if user_fix:
        path = _save_fixture(user_fix["label"], user_fix)
        print(f"  Saved -> {path}")

    print()
    print("=== Quartile-stratified fixtures ===")
    quartile_fixtures = _fetch_quartile_samples(client, rng)
    for fix in quartile_fixtures:
        path = _save_fixture(fix["label"], fix)
        print(f"  Saved -> {path}")

    print()
    print(f"Total fixtures saved: {1 + len(quartile_fixtures) if user_fix else len(quartile_fixtures)}")


if __name__ == "__main__":
    main()
