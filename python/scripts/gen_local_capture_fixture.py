"""Mint a Tier-2 local-capture recording fixture from the warm dev disk cache.

Records a full `analyze_pull` run (every FFLogs response it consumed) for one
cached pull, scrubs identifying data, proves the wire round-trip with
`local_capture.verify`, and writes:

  * tests/fixtures/local_capture/<name>.recording.json — the committed
    Tier-2 fixture `test_local_capture_replay.py` replays, and
  * (--wire-out) the same pull as wire NDJSON — the golden sample the
    Meridian Companion repo's C# tests parse.

Cache-only by default: the inner client raises on any miss, so the mint can
never hit the network silently (--allow-network opts in, using config.json
credentials, and the responses land in the dev cache like any other run).

Pull discovery: the dev cache has no reverse index, but cached `get_rankings`
entries can be FOUND by recomputing their keys over (job x encounter), and a
ranked pull that served as a warm reference has its whole stream set cached.

Usage (from python/):
  python scripts/gen_local_capture_fixture.py --list
  python scripts/gen_local_capture_fixture.py --job Samurai --encounter 101
  python scripts/gen_local_capture_fixture.py --code XXXX --fight 3 \
      --job Samurai --name "Some Player" --wire-out ..\\..\\meridian-plogon\\...
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEV_CACHE_DIR, load_config  # noqa: E402
from encounters import ALL_ENCOUNTERS, encounter_difficulty  # noqa: E402
from local_capture import (  # noqa: E402
    RecordingClient,
    parse_capture_text,
    responses_to_wire,
    serialize_ndjson,
    verify,
)
from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "local_capture"
_FAKE_CODE = "LOCALCAPFIXTURE1"

_JOBS = [
    "Machinist", "Reaper", "Red Mage", "Paladin", "Warrior", "Samurai",
    "White Mage", "Dancer", "Black Mage", "Viper", "Dragoon", "Gunbreaker",
    "Ninja", "Monk", "Bard", "Pictomancer", "Summoner", "Dark Knight",
    "Astrologian", "Scholar", "Sage",
]
# Jobs whose coverage passes read damage-event `buffs` strings — the streams
# Tier 2 exists to exercise (WIRE_CONTRACT.md §4.1).
_PREFERRED_JOBS = ["Samurai", "Warrior", "Reaper", "Viper"]


class _RaisingClient:
    """Inner client that turns any dev-cache miss into a hard error."""

    def __getattr__(self, name: str):
        def _fail(*args, **kwargs):
            raise RuntimeError(
                f"dev-cache miss for {name}{args!r} — the chosen pull is not "
                f"fully cached; pick another (--list) or pass --allow-network")
        return _fail


def _build_disk_client(allow_network: bool) -> DevDiskCacheClient:
    if allow_network:
        from fflogs_api import FFLogsClient
        cfg = load_config()
        inner = FFLogsClient(cfg["client_id"], cfg["client_secret"])
    else:
        inner = _RaisingClient()
    return DevDiskCacheClient(inner, DEV_CACHE_DIR)


def _warm_ranked_pulls(disk: DevDiskCacheClient) -> list[dict]:
    """Enumerate ranked pulls whose rankings page is in the dev cache, by
    recomputing the cache keys (there is no reverse index)."""
    out: list[dict] = []
    for job in _JOBS:
        for eid, ename in ALL_ENCOUNTERS:
            key = ("get_rankings", eid, job, job, encounter_difficulty(eid),
                   "rdps", 1)
            path = disk._path_for(key)
            if not path.exists():
                continue
            blob = disk._read(path)
            if not isinstance(blob, dict):
                continue
            for rank, e in enumerate((blob.get("rankings") or [])[:10], 1):
                rep = e.get("report") or {}
                if rep.get("code") and rep.get("fightID") is not None:
                    out.append({
                        "job": job, "encounter": eid, "encounter_name": ename,
                        "rank": rank, "code": rep["code"],
                        "fight": rep["fightID"], "name": e.get("name") or "",
                    })
    return out


def _scrub(recording: list[dict], real_code: str, label: str,
           fight_id: int) -> tuple[list[dict], dict[str, str]]:
    """Scrub identifying data from a recording (WIRE_CONTRACT fixtures ship
    in the public source snapshot): report code -> synthetic, player
    names/servers -> deterministic fakes, report title -> the fixture label,
    report-level epoch times -> relative. NPC/pet names, ids, subTypes stay.

    Also trims the summary to the analyzed fight: an FFLogs report carries
    every pull of the session, but a local capture is one pull = one fight
    (WIRE_CONTRACT.md §1) — the fixture should look like what a collector
    emits. The oracle stays sound because BOTH replay runs consume the same
    trimmed summary."""
    recording = copy.deepcopy(recording)

    name_map: dict[str, str] = {}
    for rec in recording:
        if rec["method"] != "get_report_summary":
            continue
        for a in ((rec["response"].get("masterData") or {}).get("actors")) or []:
            if a.get("type") == "Player" and a.get("name"):
                name_map.setdefault(a["name"], f"Player {len(name_map) + 1:02d}")

    for rec in recording:
        args = rec["args"]
        if args.get("code") == real_code:
            args["code"] = _FAKE_CODE
        if rec["method"] != "get_report_summary":
            continue
        summary = rec["response"]
        summary["title"] = label
        fights = [f for f in summary.get("fights") or []
                  if f.get("id") == fight_id]
        if not fights:
            raise SystemExit(f"fight {fight_id} not in the report summary")
        summary["fights"] = fights
        summary["startTime"] = 0
        summary["endTime"] = max((f.get("endTime", 0) for f in fights), default=0)
        for a in ((summary.get("masterData") or {}).get("actors")) or []:
            if a.get("type") == "Player":
                if a.get("name") in name_map:
                    a["name"] = name_map[a["name"]]
                a["server"] = "TestServer"
    return recording, name_map


def _load_forbidden() -> list[bytes]:
    spec = importlib.util.spec_from_file_location(
        "export_public", _REPO_ROOT / "scripts" / "export_public.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.FORBIDDEN)


def _assert_clean(blob: bytes, real_code: str, real_names: list[str]) -> None:
    lowered = blob.lower()
    needles = _load_forbidden() + [real_code.lower().encode()] + [
        n.lower().encode() for n in real_names if n]
    hits = [n for n in needles if n in lowered]
    if hits:
        raise SystemExit(
            f"scrub failed — output still contains: "
            f"{[h.decode(errors='replace') for h in hits]}")


def _stream_stats(events: list[dict]) -> dict[str, int]:
    counts = {"cast": 0, "damage": 0, "aura": 0, "death": 0,
              "targetability": 0, "with_buffs": 0}
    for e in events:
        t = e.get("type")
        if t in ("cast", "begincast"):
            counts["cast"] += 1
        elif t in ("damage", "calculateddamage"):
            counts["damage"] += 1
            if e.get("buffs"):
                counts["with_buffs"] += 1
        elif isinstance(t, str) and ("buff" in t):
            counts["aura"] += 1
        elif t == "death":
            counts["death"] += 1
        elif t == "targetabilityupdate":
            counts["targetability"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true",
                    help="list warm ranked pulls discoverable in the dev cache")
    ap.add_argument("--job", help="job to analyze (internal name, e.g. Samurai)")
    ap.add_argument("--encounter", type=int, help="FFLogs encounter id filter")
    ap.add_argument("--rank", type=int, default=1,
                    help="which ranked pull to take (default 1)")
    ap.add_argument("--code", help="explicit report code (with --fight/--job)")
    ap.add_argument("--fight", type=int, help="explicit fight id")
    ap.add_argument("--name", help="player name disambiguator (ranking name)")
    ap.add_argument("--out", help="output recording path "
                    "(default tests/fixtures/local_capture/<auto>.recording.json)")
    ap.add_argument("--wire-out", help="also write the wire NDJSON capture here")
    ap.add_argument("--allow-network", action="store_true",
                    help="fall back to FFLogs on cache misses (uses config.json)")
    opts = ap.parse_args()

    disk = _build_disk_client(opts.allow_network)

    if opts.list:
        pulls = _warm_ranked_pulls(disk)
        if not pulls:
            print("no cached rankings pages found — warm refs in the app first")
            return 1
        for p in pulls:
            star = " *" if p["job"] in _PREFERRED_JOBS else ""
            print(f"  {p['job']:<12} enc {p['encounter']} #{p['rank']:<2} "
                  f"{p['code']} fight {p['fight']:<3} {p['name']}{star}")
        print("\n  * = job whose sims read damage-event `buffs` (preferred for Tier 2)")
        return 0

    if opts.code:
        if not (opts.fight is not None and opts.job):
            ap.error("--code needs --fight and --job")
        job, code, fight, name = opts.job, opts.code, opts.fight, opts.name
    elif opts.job and opts.encounter:
        # Direct rankings lookup through the cache client — a disk hit when
        # warm, a (cached-on-write) fetch under --allow-network, an error
        # otherwise.
        rankings = disk.get_rankings(
            encounter_id=opts.encounter, class_name=opts.job,
            spec_name=opts.job,
            difficulty=encounter_difficulty(opts.encounter))
        entries = ((rankings or {}).get("rankings") or [])
        if len(entries) < opts.rank:
            print(f"rankings page has only {len(entries)} entries")
            return 1
        e = entries[opts.rank - 1]
        rep = e.get("report") or {}
        job, code, fight = opts.job, rep.get("code"), rep.get("fightID")
        name = e.get("name") or None
        if not code or fight is None:
            print("ranked entry carries no report code/fight")
            return 1
        print(f"picked: {job} enc {opts.encounter} #{opts.rank} "
              f"{code} fight {fight} ({name})")
    else:
        pulls = _warm_ranked_pulls(disk)
        job_order = ([opts.job] if opts.job else
                     _PREFERRED_JOBS + [j for j in _JOBS
                                        if j not in _PREFERRED_JOBS])
        pick = None
        for j in job_order:
            candidates = [p for p in pulls if p["job"] == j
                          and (opts.encounter is None
                               or p["encounter"] == opts.encounter)
                          and p["rank"] == opts.rank]
            if candidates:
                pick = candidates[0]
                break
        if pick is None:
            print("no warm ranked pull matches — try --list")
            return 1
        job, code, fight, name = (pick["job"], pick["code"], pick["fight"],
                                  pick["name"])
        print(f"picked: {job} enc {pick['encounter']} #{pick['rank']} "
              f"{code} fight {fight} ({name})")

    recorder = RecordingClient(disk)
    from jobs import analyze_pull
    print("running analyze_pull over the recorded client…")
    analyze_pull(job, recorder, code, fight, ranking_name=name, label="You")
    print(f"recorded {len(recorder.recording)} client calls")

    label = f"local-capture fixture ({job})"
    scrubbed, name_map = _scrub(recorder.recording, code, label, fight)
    player_name = name_map.get(name) if name else None

    out_stem = (opts.out and Path(opts.out).stem.replace(".recording", "")) or \
        f"{job.lower().replace(' ', '')}_full_stream"
    records = responses_to_wire(scrubbed, capture_id=out_stem)
    text = serialize_ndjson(records)
    capture = parse_capture_text(text)

    stats = _stream_stats(capture.events)
    print(f"wire events: {stats}")
    if not (stats["cast"] and stats["damage"] and stats["aura"]):
        raise SystemExit("fixture too thin — needs casts, damage AND auras "
                         "(pick a different pull)")
    if stats["death"] == 0:
        print("  note: no deaths in this pull (death replay stays untested)")
    if job in _PREFERRED_JOBS and stats["with_buffs"] == 0:
        raise SystemExit(f"{job} fixture has no damage-event `buffs` strings "
                         f"— pick a different pull")

    checked = verify(scrubbed, capture)
    print(f"verify: {checked} recorded calls reproduced exactly")

    payload = {"job": job, "code": _FAKE_CODE, "fight_id": fight,
               "player_name": player_name, "recording": scrubbed}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    real_names = list(name_map)
    _assert_clean(payload_bytes, code, real_names)
    _assert_clean(text.encode("utf-8"), code, real_names)

    out_path = Path(opts.out) if opts.out else \
        _FIXTURES_DIR / f"{out_stem}.recording.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(payload_bytes + b"\n")
    print(f"wrote {out_path} ({len(payload_bytes) / 1024:.0f} KB)")

    if opts.wire_out:
        wire_path = Path(opts.wire_out)
        wire_path.parent.mkdir(parents=True, exist_ok=True)
        wire_path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {wire_path} ({len(text) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
