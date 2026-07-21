"""One-shot live probe for the FFLogs downtime-detection API surface.

Verifies that the schema additions we plan to make for Tier-A (boss
untargetability) downtime actually work against the live API before we
wire them into the analyzer. Reads credentials from
~/.fflogs_efficiency_analyzer/config.json and queries one fixture report.

Target selection:
  --encounter ID      auto-pick a top-ranked MCH pull via rankings (best
                      way to grab a known-shape fight for verification)
                      Vamp Fatale=101, Red Hot=102, The Tyrant=103,
                      Lindwurm P1=104, Lindwurm P2=105
  --report CODE --fight ID    direct report + fight
  (no flag)           default: topq_1 fixture (M11S Tyrant — confirmed
                      to have NO targetability flips, so good baseline)

Run from python/:
    python scripts/probe_downtime.py --encounter 101    # Vamp Fatale (has flips)
    python scripts/probe_downtime.py user_tyrant_recent --verbose
    python scripts/probe_downtime.py --report rXyz --fight 5

What we're verifying:
  1. ReportFight exposes phaseTransitions / lastPhase / enemyNPCs.
  2. masterData.actors(type:"NPC") returns NPC actors with subType tags
     we can filter to bosses.
  3. events(filterExpression:'type="targetabilityupdate"') returns
     parseable events on a fight known to have untargetable phases.
  4. We can pair those events into downtime windows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `from config import ...` and `from fflogs_api import ...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config            # noqa: E402
from fflogs_api import FFLogsClient       # noqa: E402


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))
        raise SystemExit(
            f"Fixture not found: {path}\nAvailable fixtures: {available}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _client_from_config() -> FFLogsClient:
    cfg = load_config()
    cid = cfg.get("client_id")
    csec = cfg.get("client_secret")
    if not cid or not csec:
        raise SystemExit(
            "FFLogs credentials missing from "
            "~/.fflogs_efficiency_analyzer/config.json\n"
            "Expected keys: client_id, client_secret."
        )
    return FFLogsClient(cid, csec)


# --- Probe 1: ReportFight schema additions ---------------------------------

def probe_report_fight(client: FFLogsClient, code: str,
                       fight_id: int) -> dict:
    """Read fields the analyzer doesn't pull today: phaseTransitions,
    lastPhase, lastPhaseIsIntermission, enemyNPCs.

    The phaseTransitions sub-fields (id, startTime) and enemyNPCs sub-
    fields (id, gameID, groupID, petOwner) are guesses based on the
    Warcraft Logs schema heritage. If the GraphQL query 400s with
    "Cannot query field X", the names need adjustment.
    """
    print()
    print("[Probe 1] ReportFight: phaseTransitions / lastPhase / enemyNPCs")
    q = """
    query($code: String!, $ids: [Int]) {
      reportData {
        report(code: $code) {
          fights(fightIDs: $ids) {
            id name startTime endTime kill
            lastPhase
            lastPhaseIsIntermission
            phaseTransitions { id startTime }
            enemyNPCs { id gameID petOwner }
          }
        }
      }
    }
    """
    try:
        data = client.query(q, {"code": code, "ids": [fight_id]})
    except Exception as e:
        print(f"  [FAIL] query raised: {e}")
        return {}
    fights = ((data.get("reportData") or {})
              .get("report", {}).get("fights") or [])
    if not fights:
        print(f"  [FAIL] no fight returned for id {fight_id}")
        return {}
    fight = fights[0]
    print(f"  [OK  ] fight {fight_id}: {fight.get('name')!r}  "
          f"kill={fight.get('kill')}")
    print(f"          lastPhase = {fight.get('lastPhase')}  "
          f"intermission = {fight.get('lastPhaseIsIntermission')}")
    pts = fight.get("phaseTransitions") or []
    print(f"          phaseTransitions: {len(pts)} entries")
    for pt in pts:
        rel = (pt.get("startTime", 0) - fight.get("startTime", 0)) / 1000.0
        print(f"            phase {pt.get('id')}  @ t+{rel:.1f}s")
    enemies = fight.get("enemyNPCs") or []
    print(f"          enemyNPCs: {len(enemies)} entries")
    for e in enemies:
        print(f"            id={e.get('id')}  gameID={e.get('gameID')}  "
              f"petOwner={e.get('petOwner')}")
    return fight


# --- Probe 2: NPC actor resolution -----------------------------------------

def probe_master_actors(client: FFLogsClient, code: str) -> list[dict]:
    """Fetch NPC actors so we can resolve boss IDs by subType."""
    print()
    print("[Probe 2] masterData.actors(type:\"NPC\") with subType tagging")
    q = """
    query($code: String!) {
      reportData {
        report(code: $code) {
          masterData {
            actors(type: "NPC") {
              id gameID name subType type petOwner
            }
          }
        }
      }
    }
    """
    try:
        data = client.query(q, {"code": code})
    except Exception as e:
        print(f"  [FAIL] query raised: {e}")
        return []
    actors = (((data.get("reportData") or {}).get("report", {}) or {})
              .get("masterData", {}).get("actors") or [])
    print(f"  [OK  ] {len(actors)} NPC actors")
    by_sub: dict[str, list[dict]] = {}
    for a in actors:
        by_sub.setdefault(a.get("subType") or "<none>", []).append(a)
    for sub, group in sorted(by_sub.items(), key=lambda kv: -len(kv[1])):
        print(f"          subType={sub!r}: {len(group)} actors")
        # Print up to 5 representative actors per subType so we can eyeball
        # which subType FFLogs uses for the main boss.
        for a in group[:5]:
            print(f"            id={a['id']}  gameID={a.get('gameID')}  "
                  f"name={a.get('name')!r}")
    return actors


# --- Probe 3: targetabilityupdate events -----------------------------------

_EVENTS_Q = """
query($code: String!, $start: Float!, $end: Float!,
      $type: EventDataType, $expr: String) {
  reportData {
    report(code: $code) {
      events(
        startTime: $start,
        endTime:   $end,
        dataType:  $type,
        filterExpression: $expr,
        limit: 10000
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""


def _fetch_events(client: FFLogsClient, code: str, start_ms: int, end_ms: int,
                  *, data_type: str | None = None,
                  filter_expr: str | None = None,
                  max_pages: int = 20) -> tuple[list[dict], str | None]:
    """One-call helper for arbitrary events queries. Returns
    (events, error_message_or_None)."""
    out: list[dict] = []
    cur = float(start_ms)
    for _ in range(max_pages):
        try:
            data = client.query(_EVENTS_Q, {
                "code":  code,
                "start": cur,
                "end":   float(end_ms),
                "type":  data_type,
                "expr":  filter_expr,
            })
        except Exception as e:
            return out, str(e)
        block = (((data.get("reportData") or {})
                  .get("report", {}) or {}).get("events") or {})
        evs = block.get("data") or []
        out.extend(evs)
        nxt = block.get("nextPageTimestamp")
        if nxt is None or nxt <= cur:
            break
        cur = nxt
    return out, None


def probe_targetability_events(client: FFLogsClient, code: str,
                                fight_start_ms: int, fight_end_ms: int,
                                verbose: bool) -> list[dict]:
    """Search the events endpoint for targetability flips.

    We don't know the exact filter-expression syntax FFLogs uses for the
    targetabilityupdate event type (their schema page is auth-gated, and
    xivanalysis abstracts past the wire format). Try several common
    variants; report which one(s) return data.
    """
    print()
    print("[Probe 3] events: targetabilityupdate search")

    # Variant table: (label, dataType, filterExpression). Try multiple
    # candidate event-type names because the FFLogs schema page is auth-
    # gated and we couldn't confirm the exact string.
    variants = [
        ("type=\"targetabilityupdate\"   (dataType:All)",
         "All",  'type="targetabilityupdate"'),
        ("type=\"targetable\"            (alt name)",
         "All",  'type="targetable"'),
        ("type=\"targetabilitychange\"   (alt name)",
         "All",  'type="targetabilitychange"'),
        ("type IN (\"targetabilityupdate\",\"targetable\")",
         "All",  'type IN ("targetabilityupdate","targetable")'),
        ("target.id = <boss>  any-type      (sanity: bound to boss)",
         "All",  None),  # filled per-fight below
    ]

    successful_events: list[dict] = []
    for label, dt, expr in variants:
        evs, err = _fetch_events(
            client, code, fight_start_ms, fight_end_ms,
            data_type=dt, filter_expr=expr, max_pages=3,
        )
        if err:
            print(f"  [FAIL] {label}: {err.splitlines()[0]}")
            continue
        status = "OK  " if evs else "WARN"
        print(f"  [{status}] {label}: {len(evs)} events")
        if evs and not successful_events:
            successful_events = evs

    # --- Full-fight histogram ----------------------------------------------
    # Sample three windows (early, mid, late) to catch event types that only
    # appear after a phase transition. Capped at 2 pages each so we don't
    # drown in fat fights.
    print("  [INFO] sampling type histogram across full fight (3 windows)")
    dur_ms = fight_end_ms - fight_start_ms
    sample_windows = [
        (fight_start_ms,                     fight_start_ms + 30_000),
        (fight_start_ms + dur_ms // 2,       fight_start_ms + dur_ms // 2 + 30_000),
        (max(fight_end_ms - 30_000, fight_start_ms), fight_end_ms),
    ]
    all_types: dict[str, int] = {}
    for s_ms, e_ms in sample_windows:
        evs, err = _fetch_events(
            client, code, s_ms, e_ms,
            data_type="All", filter_expr=None, max_pages=2,
        )
        if err:
            print(f"          window {s_ms}->{e_ms}: error {err.splitlines()[0]}")
            continue
        for ev in evs:
            t = ev.get("type", "<missing>")
            all_types[t] = all_types.get(t, 0) + 1
    print(f"          aggregate type histogram across {len(sample_windows)} "
          f"sample windows:")
    for t, n in sorted(all_types.items(), key=lambda kv: -kv[1]):
        marker = "  <-- TARGET!" if "target" in t.lower() else ""
        print(f"            {t:35s} {n:6d}{marker}")

    if not successful_events:
        print("          (no variant returned targetability events)")
        return []

    # Show one sample so we can inspect the field shape.
    print("          sample event:")
    sample = json.dumps(successful_events[0], indent=2)
    for line in sample.splitlines():
        print(f"            {line}")
    if verbose:
        print("          all events:")
        for ev in successful_events:
            t_rel = (ev.get("timestamp", 0) - fight_start_ms) / 1000.0
            print(f"            t+{t_rel:6.1f}s  {ev}")
    return successful_events


# --- Probe 4: pair events into windows -------------------------------------

def derive_windows(events: list[dict], fight_start_ms: int,
                   fight_end_ms: int) -> list[tuple[int, float, float]]:
    """Pair targetabilityupdate events per actor into (start_s, end_s)
    windows relative to fight start. Returns (target_id, start, end).

    Heuristic: targetable=0/false opens a window, targetable=1/true closes
    it. If the fight ends with an open window, we close at fight_end.
    """
    print()
    print("[Probe 4] pair events into per-actor downtime windows")
    by_actor: dict[int, list[dict]] = {}
    for ev in events:
        # targetID is the standard field for the actor whose targetability
        # changed; sourceID may be the same depending on how the parser
        # emits the event. Prefer targetID, fall back to sourceID.
        tid = ev.get("targetID") if ev.get("targetID") is not None \
            else ev.get("sourceID")
        if tid is None:
            continue
        by_actor.setdefault(tid, []).append(ev)
    windows: list[tuple[int, float, float]] = []
    for tid, evs in by_actor.items():
        evs.sort(key=lambda e: e["timestamp"])
        open_start: float | None = None
        for ev in evs:
            tgt = ev.get("targetable")
            t_rel = (ev["timestamp"] - fight_start_ms) / 1000.0
            if tgt in (0, False):
                open_start = t_rel
            elif tgt in (1, True) and open_start is not None:
                windows.append((tid, open_start, t_rel))
                open_start = None
        if open_start is not None:
            end_rel = (fight_end_ms - fight_start_ms) / 1000.0
            windows.append((tid, open_start, end_rel))
    if not windows:
        print("  [WARN] no paired windows derived (no events, or the "
              "`targetable` field isn't present on the events)")
        return []
    print(f"  [OK  ] {len(windows)} window(s) across "
          f"{len(by_actor)} actor(s):")
    for tid, s, e in sorted(windows, key=lambda w: w[1]):
        print(f"          actor {tid}: {s:6.1f}s -> {e:6.1f}s  "
              f"({e - s:5.1f}s)")
    return windows


# --- Driver ----------------------------------------------------------------

def _parse_flag(name: str) -> str | None:
    """Cheap --name value parser. Returns the value following --name, or
    None if the flag isn't present."""
    try:
        i = sys.argv.index(f"--{name}")
    except ValueError:
        return None
    if i + 1 >= len(sys.argv):
        return None
    return sys.argv[i + 1]


def _fetch_top_pull(client: FFLogsClient, encounter_id: int) -> dict:
    """Pull a top-ranked MCH kill for `encounter_id` via the rankings API,
    return enough metadata to drive the probe (report code, fight ID,
    start/end ms, duration). Picks rank 1 to keep the test deterministic.

    Returns a dict matching the fixture schema we already use.
    """
    print(f"Fetching top MCH ranking for encounter {encounter_id}...")
    rankings = client.get_rankings(
        encounter_id=encounter_id,
        class_name="Machinist", spec_name="Machinist",
        difficulty=101, metric="rdps", page=1,
    )
    ranks = rankings.get("rankings") or []
    if not ranks:
        raise SystemExit(
            f"No rankings returned for encounter {encounter_id}.")
    r = ranks[0]
    rep = r.get("report") or {}
    code = rep.get("code")
    fid = rep.get("fightID")
    if not code or fid is None:
        raise SystemExit(
            f"Top ranking missing report code/fight ID: {r}")
    # The rankings response doesn't give us startTime/endTime directly;
    # fetch the report summary to get them.
    summary = client.get_report_summary(code)
    fight_obj = next((f for f in summary.get("fights", [])
                      if f.get("id") == fid), None)
    if fight_obj is None:
        raise SystemExit(
            f"Could not find fight {fid} in report {code}.")
    return {
        "label": f"enc{encounter_id}_rank1",
        "report_code": code,
        "fight_id": fid,
        "source_id": -1,  # not used by the probe
        "fight_start_ms": fight_obj["startTime"],
        "fight_end_ms":   fight_obj["endTime"],
        "duration_s": (fight_obj["endTime"] - fight_obj["startTime"]) / 1000.0,
    }


def main() -> int:
    verbose = "--verbose" in sys.argv
    enc_arg = _parse_flag("encounter")
    report_arg = _parse_flag("report")
    fight_arg = _parse_flag("fight")

    client = _client_from_config()

    if enc_arg:
        fix = _fetch_top_pull(client, int(enc_arg))
        print(f"Top ranking for encounter {enc_arg}:")
    elif report_arg and fight_arg:
        summary = client.get_report_summary(report_arg)
        fight_obj = next((f for f in summary.get("fights", [])
                          if f.get("id") == int(fight_arg)), None)
        if fight_obj is None:
            raise SystemExit(f"Fight {fight_arg} not in report {report_arg}.")
        fix = {
            "label": f"{report_arg}_{fight_arg}",
            "report_code": report_arg,
            "fight_id": int(fight_arg),
            "source_id": -1,
            "fight_start_ms": fight_obj["startTime"],
            "fight_end_ms":   fight_obj["endTime"],
            "duration_s": (fight_obj["endTime"] - fight_obj["startTime"]) / 1000.0,
        }
        print(f"Direct report probe:")
    else:
        positional = [a for a in sys.argv[1:]
                      if not a.startswith("--")
                      and sys.argv[sys.argv.index(a) - 1] not in
                      ("--encounter", "--report", "--fight")]
        fixture_name = positional[0] if positional else "topq_1"
        fix = _load_fixture(fixture_name)
        print(f"Probing fixture: {fixture_name}")

    print(f"  report_code={fix['report_code']}  fight_id={fix['fight_id']}")
    print(f"  duration_s={fix['duration_s']:.1f}")

    fight = probe_report_fight(client, fix["report_code"], fix["fight_id"])
    actors = probe_master_actors(client, fix["report_code"])
    events = probe_targetability_events(
        client, fix["report_code"],
        fix["fight_start_ms"], fix["fight_end_ms"], verbose,
    )
    windows = derive_windows(
        events, fix["fight_start_ms"], fix["fight_end_ms"],
    )

    # Cross-reference: do the derived window actor IDs match a Boss-tagged
    # actor in masterData? That's the end-to-end smoke test for our plan.
    print()
    print("[Cross-ref] window actor IDs vs Boss-tagged masterData actors")
    boss_ids = {a["id"] for a in actors if a.get("subType") == "Boss"}
    window_actor_ids = {tid for tid, _, _ in windows}
    matched = window_actor_ids & boss_ids
    unmatched = window_actor_ids - boss_ids
    print(f"  Boss-tagged actor IDs: {sorted(boss_ids)}")
    print(f"  Window actor IDs:      {sorted(window_actor_ids)}")
    print(f"  Matched (boss-driven): {sorted(matched)}")
    if unmatched:
        print(f"  Unmatched (non-boss):  {sorted(unmatched)}  "
              f"<- check actor subTypes in Probe 2 output")

    print()
    print("=" * 60)
    print("Summary")
    print(f"  ReportFight schema fields:    "
          f"{'OK' if fight else 'FAIL'}")
    print(f"  NPC actor resolution:         "
          f"{'OK' if actors else 'FAIL'}")
    print(f"  targetabilityupdate events:   "
          f"{'OK' if events else 'EMPTY/FAIL'}")
    print(f"  Paired windows:               {len(windows)}")
    print(f"  Boss-driven windows:          {len(matched)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
