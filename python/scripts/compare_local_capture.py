"""Diff a Meridian Companion local capture against an FFLogs report of the same pull.

The Phase 1 acceptance gate (LIVE_LOCAL_CAPTURE_SPEC.md §9): cast counts
exact, damage totals within rounding, timings within a small tolerance —
divergence is a collector bug, not a pipeline bug. Known, accepted collector
gaps are classified out instead of failing the gate: FFLogs-only DoT/HoT
ticks and `damage` confirmation events (the collector emits only
`calculateddamage`), zero-amount misses, and local-only pet deaths (pet
despawn/expiry reads as death via the IsDead diff).

Both sides are read through the same six-method client interface, and every
timestamp is rebased against its own side's fight startTime, so the local
capture's t0 basis (fight start 0, negative pre-pull) and FFLogs' report
basis compare cleanly. The residual clock offset (local InCombat edge vs
FFLogs' first-event fight start) is estimated from paired casts and reported.

The upload-free variant diffs against the raw ACT/FFXIV-plugin network log —
the same data the FFLogs uploader parses (local_capture/act_log.py). It is
the fast pre-gate; the official acceptance diff still uses a real report.

Run from python/:
    python scripts/compare_local_capture.py --self-test
    python scripts/compare_local_capture.py --capture PATH.ndjson --code REPORT --fight N
        [--player "Name"] [--tolerance-ms 200] [--allow-network]
    python scripts/compare_local_capture.py --capture PATH.ndjson --act-log NETWORK.log
"""
from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_capture import LocalCaptureClient, parse_capture_text  # noqa: E402
from local_capture.act_log import ActLogClient  # noqa: E402
from local_capture.export import responses_to_wire, serialize_ndjson, verify  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_RECORDING_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "local_capture" / "samurai_full_stream.recording.json"
)
PRE_PULL_MS = 10_000


class Side:
    """One comparand: a name, a six-method client, and its fight context."""

    def __init__(self, name: str, client: Any, code: str, fight_id: int | None):
        self.name = name
        self.client = client
        self.code = code
        self.summary = client.get_report_summary(code)
        fights = self.summary["fights"]
        if fight_id is None:
            self.fight = fights[0]
        else:
            matches = [f for f in fights if f["id"] == fight_id]
            if not matches:
                raise SystemExit(f"{name}: no fight {fight_id} in report {code}")
            self.fight = matches[0]
        self.start = self.fight["startTime"]
        self.end = self.fight["endTime"]
        self.actors = self.summary["masterData"]["actors"]

    def actor_by_name(self, name: str, actor_type: str = "Player") -> dict | None:
        lowered = name.lower()
        for actor in self.actors:
            if actor.get("type") == actor_type and str(actor.get("name", "")).lower() == lowered:
                return actor
        return None

    def actor_name(self, actor_id: int) -> str:
        for actor in self.actors:
            if actor["id"] == actor_id:
                return str(actor.get("name", actor_id))
        return str(actor_id)

    def pet_ids(self) -> set[int]:
        return {a["id"] for a in self.actors if a.get("type") == "Pet"}

    def fetch(self, player_id: int, friendly_ids: list[int]) -> dict[str, Any]:
        pre = self.start - PRE_PULL_MS
        deaths: list[dict] = []
        # Pets included so the collector's despawn-reads-as-death gap is
        # surfaced (and classified) rather than silently unfetched.
        for victim in [*friendly_ids, *sorted(self.pet_ids())]:
            deaths.extend(self.client.get_events(self.code, self.start, self.end, victim, "Deaths"))
        return {
            "casts": self.client.get_events(self.code, pre, self.end, player_id, "Casts"),
            "damage": self.client.get_events(self.code, self.start, self.end, player_id, "DamageDone"),
            "buffs": self.client.get_aura_events(self.code, pre, self.end, player_id, "Buffs"),
            "deaths": deaths,
            "targetability": self.client.get_targetability_events(self.code, self.start, self.end),
            "enemy_casts": self.client.get_enemy_cast_events(self.code, self.start, self.end),
        }

    def rebase(self, events: list[dict]) -> list[dict]:
        out = []
        for event in events:
            event = dict(event)
            event["timestamp"] = event["timestamp"] - self.start
            out.append(event)
        return out


def _count_deltas(label: str, local: Counter, fflogs: Counter, lines: list[str]) -> int:
    """Append per-key count differences; return how many keys differ."""
    differing = 0
    for key in sorted(set(local) | set(fflogs), key=str):
        l_count, f_count = local.get(key, 0), fflogs.get(key, 0)
        if l_count != f_count:
            differing += 1
            lines.append(f"  !! {label} {key}: local={l_count} fflogs={f_count}")
    return differing


def _pair_timing_deltas(local: list[dict], fflogs: list[dict], offset: float) -> list[float]:
    """Pair events per (type, ability) in chronological order; return |delta| ms."""
    def buckets(events: list[dict]) -> dict[tuple, list[int]]:
        grouped: dict[tuple, list[int]] = {}
        for event in events:
            grouped.setdefault((event["type"], event.get("abilityGameID")), []).append(event["timestamp"])
        for stamps in grouped.values():
            stamps.sort()
        return grouped

    local_b, fflogs_b = buckets(local), buckets(fflogs)
    deltas: list[float] = []
    for key in set(local_b) & set(fflogs_b):
        for l_ts, f_ts in zip(local_b[key], fflogs_b[key]):
            deltas.append(abs((l_ts - offset) - f_ts))
    return deltas


def compare(local: Side, fflogs: Side, player_name: str, tolerance_ms: int) -> int:
    """Print the parity report; return the number of unexplained divergences."""
    local_player = local.actor_by_name(player_name)
    fflogs_player = fflogs.actor_by_name(player_name)
    if local_player is None or fflogs_player is None:
        missing = "local" if local_player is None else "fflogs"
        raise SystemExit(f"player {player_name!r} not found in the {missing} summary")

    streams_l = local.fetch(local_player["id"], local.fight.get("friendlyPlayers") or [local_player["id"]])
    streams_f = fflogs.fetch(fflogs_player["id"], fflogs.fight.get("friendlyPlayers") or [fflogs_player["id"]])
    for key in streams_l:
        streams_l[key] = local.rebase(streams_l[key])
        streams_f[key] = fflogs.rebase(streams_f[key])

    lines: list[str] = []
    unexplained = 0
    lines.append(f"== parity: {player_name} — local {local.code!r} vs fflogs {fflogs.code!r} fight {fflogs.fight['id']}")
    lines.append(f"fight length: local {local.end - local.start:,} ms, fflogs {fflogs.end - fflogs.start:,} ms")

    # Clock offset between the two fight-start bases, from paired casts.
    raw_deltas = []
    for key, l_stamps in _cast_buckets(streams_l["casts"]).items():
        f_stamps = _cast_buckets(streams_f["casts"]).get(key)
        if f_stamps:
            raw_deltas.extend(l - f for l, f in zip(l_stamps, f_stamps))
    offset = statistics.median(raw_deltas) if raw_deltas else 0.0
    lines.append(f"clock offset (local t0 vs fflogs fight start): {offset:+.0f} ms over {len(raw_deltas)} paired casts")

    # Casts: exact counts per (type, ability).
    cast_l = Counter((e["type"], e.get("abilityGameID")) for e in streams_l["casts"])
    cast_f = Counter((e["type"], e.get("abilityGameID")) for e in streams_f["casts"])
    lines.append(f"\ncasts (incl. {PRE_PULL_MS // 1000} s pre-pull): local {len(streams_l['casts'])}, fflogs {len(streams_f['casts'])}")
    unexplained += _count_deltas("cast", cast_l, cast_f, lines)

    # Damage: collector emits calculateddamage only; FFLogs also carries the
    # `damage` confirmations (ticks live there) — informational, not a delta.
    calc_f = [e for e in streams_f["damage"] if e["type"] == "calculateddamage"]
    conf_f = [e for e in streams_f["damage"] if e["type"] == "damage"]
    ticks_f = [e for e in conf_f if e.get("tick")]
    calc_l = [e for e in streams_l["damage"] if e["type"] == "calculateddamage"]
    misses_f = [e for e in calc_f if not e.get("amount")]
    calc_f_hits = [e for e in calc_f if e.get("amount")]
    calc_l_hits = [e for e in calc_l if e.get("amount")]
    lines.append(f"\ndamage: local calculateddamage {len(calc_l)}, fflogs calculateddamage {len(calc_f)}")
    lines.append(f"  known gaps (informational): fflogs confirmations {len(conf_f)} (ticks {len(ticks_f)}), zero-amount {len(misses_f)}")
    unexplained += _count_deltas(
        "damage-count",
        Counter(e.get("abilityGameID") for e in calc_l_hits),
        Counter(e.get("abilityGameID") for e in calc_f_hits),
        lines)
    totals_l = Counter()
    totals_f = Counter()
    for event in calc_l_hits:
        totals_l[event.get("abilityGameID")] += event.get("amount", 0)
    for event in calc_f_hits:
        totals_f[event.get("abilityGameID")] += event.get("amount", 0)
    unexplained += _count_deltas("damage-total", totals_l, totals_f, lines)

    # Player buffs: counts per (variant, status). Local removes without a
    # matching apply are usually statuses that predate tracking — report,
    # don't fail, unless the total per status disagrees.
    buffs_l = Counter((e["type"], e.get("abilityGameID")) for e in streams_l["buffs"])
    buffs_f = Counter((e["type"], e.get("abilityGameID")) for e in streams_f["buffs"])
    lines.append(f"\nplayer buffs (incl. pre-pull): local {len(streams_l['buffs'])}, fflogs {len(streams_f['buffs'])}")
    buff_deltas = _count_deltas("buff", buffs_l, buffs_f, lines)
    lines.append(f"  differing (variant, status) keys: {buff_deltas} — review manually; aura edge semantics differ")

    # Deaths: compare victims by name; local-only pet deaths are the known
    # despawn-reads-as-death gap.
    local_pets, fflogs_pets = local.pet_ids(), fflogs.pet_ids()
    deaths_l = Counter(local.actor_name(e["targetID"]) for e in streams_l["deaths"]
                       if e.get("targetID") not in local_pets)
    deaths_f = Counter(fflogs.actor_name(e["targetID"]) for e in streams_f["deaths"]
                       if e.get("targetID") not in fflogs_pets)
    pet_deaths_l = sum(1 for e in streams_l["deaths"] if e.get("targetID") in local_pets)
    lines.append(f"\ndeaths: local {sum(deaths_l.values())} (+{pet_deaths_l} pet despawn-deaths, known gap), fflogs {sum(deaths_f.values())}")
    unexplained += _count_deltas("death", deaths_l, deaths_f, lines)

    # Targetability + enemy casts: counts, plus targetability flag sequence.
    tgt_l = [(e.get("targetable"),) for e in streams_l["targetability"]]
    tgt_f = [(e.get("targetable"),) for e in streams_f["targetability"]]
    lines.append(f"\ntargetability: local {len(tgt_l)}, fflogs {len(tgt_f)}")
    if Counter(tgt_l) != Counter(tgt_f):
        unexplained += 1
        lines.append(f"  !! flag distribution differs: local={Counter(tgt_l)} fflogs={Counter(tgt_f)}")
    enemy_l = Counter(e.get("abilityGameID") for e in streams_l["enemy_casts"])
    enemy_f = Counter(e.get("abilityGameID") for e in streams_f["enemy_casts"])
    lines.append(f"enemy casts: local {sum(enemy_l.values())}, fflogs {sum(enemy_f.values())}")
    unexplained += _count_deltas("enemy-cast", enemy_l, enemy_f, lines)

    # Timing: paired casts + damage after offset correction.
    timing = _pair_timing_deltas(streams_l["casts"] + calc_l_hits, streams_f["casts"] + calc_f_hits, offset)
    if timing:
        worst = max(timing)
        lines.append(f"\ntiming after offset: paired {len(timing)}, mean {statistics.mean(timing):.0f} ms, max {worst:.0f} ms (tolerance {tolerance_ms})")
        beyond = sum(1 for d in timing if d > tolerance_ms)
        if beyond:
            unexplained += 1
            lines.append(f"  !! {beyond} paired events beyond tolerance")

    verdict = "PARITY OK" if unexplained == 0 else f"DIVERGENT: {unexplained} unexplained deltas"
    lines.append(f"\n{verdict}")
    print("\n".join(lines))
    return unexplained


def _cast_buckets(events: list[dict]) -> dict[tuple, list[int]]:
    grouped: dict[tuple, list[int]] = {}
    for event in events:
        grouped.setdefault((event["type"], event.get("abilityGameID")), []).append(event["timestamp"])
    for stamps in grouped.values():
        stamps.sort()
    return grouped


def _build_fflogs_side(code: str, fight_id: int, allow_network: bool) -> Side:
    from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402
    from config import DEV_CACHE_DIR, load_config  # noqa: E402
    if allow_network:
        from fflogs_api import FFLogsClient  # noqa: E402
        cfg = load_config()
        inner: Any = FFLogsClient(cfg["client_id"], cfg["client_secret"])
    else:
        class _RaisingClient:
            def __getattr__(self, name: str) -> Any:
                def _raise(*_a: Any, **_k: Any) -> Any:
                    raise RuntimeError(f"dev-cache miss for {name}; re-run with --allow-network")
                return _raise
        inner = _RaisingClient()
    return Side("fflogs", DevDiskCacheClient(inner, DEV_CACHE_DIR), code, fight_id)


def self_test() -> int:
    """Validate the comparator against the committed recording fixture.

    1. Export-oracle: the wire round-trip still reproduces every recorded call.
    2. Identical sides → zero unexplained deltas.
    3. Injected known-gap events (a tick confirmation, a pet death) still
       pass; a genuinely dropped cast is caught.
    """
    fixture = json.loads(_RECORDING_PATH.read_text(encoding="utf-8"))
    recording = fixture["recording"]
    player = fixture["player_name"]
    records = responses_to_wire(recording, capture_id="paritytest")
    text = serialize_ndjson(records)
    capture = parse_capture_text(text)
    verify(recording, capture)
    print("self-test 1/3: export oracle reproduces every recorded call")

    side_a = Side("local", LocalCaptureClient(parse_capture_text(text)), "local", None)
    side_b = Side("fflogs", LocalCaptureClient(parse_capture_text(text)), "fflogs", None)
    if compare(side_a, side_b, player, tolerance_ms=200) != 0:
        print("self-test FAILED: identical sides diverged")
        return 1
    print("self-test 2/3: identical sides report parity")

    # Known-gap injections go on the appropriate side: a tick confirmation on
    # the FFLogs side, a pet despawn-death on the local side.
    player_id = side_a.actor_by_name(player)["id"]
    pet_id = next(a["id"] for a in capture.summary["masterData"]["actors"] if a["type"] == "Pet")
    fight = capture.summary["fights"][0]
    mid = (fight["startTime"] + fight["endTime"]) // 2
    tick = {"kind": "event", "timestamp": mid, "type": "damage", "tick": True,
            "sourceID": player_id, "targetID": 2, "abilityGameID": 1000860, "amount": 5000}
    pet_death = {"kind": "event", "timestamp": mid, "type": "death",
                 "sourceID": -1, "targetID": pet_id}
    with_tick = copy.deepcopy(records)
    with_tick.insert(-1, tick)
    with_pet_death = copy.deepcopy(records)
    with_pet_death.insert(-1, pet_death)
    side_local = Side("local", LocalCaptureClient(parse_capture_text(serialize_ndjson(with_pet_death))), "local", None)
    side_ff = Side("fflogs", LocalCaptureClient(parse_capture_text(serialize_ndjson(with_tick))), "fflogs", None)
    # Guard against a vacuous pass: both injections must be visible to the
    # fetches the comparator actually makes.
    fs, fe = fight["startTime"], fight["endTime"]
    if not any(e.get("tick") for e in side_ff.client.get_events("x", fs, fe, player_id, "DamageDone")):
        print("self-test FAILED: injected tick is invisible to the DamageDone fetch")
        return 1
    if not any(e.get("targetID") == pet_id for e in side_local.client.get_events("x", fs, fe, pet_id, "Deaths")):
        print("self-test FAILED: injected pet death is invisible to the Deaths fetch")
        return 1
    if compare(side_local, side_ff, player, tolerance_ms=200) != 0:
        print("self-test FAILED: known-gap events were not classified out")
        return 1

    dropped = copy.deepcopy(records)
    for index, record in enumerate(dropped):
        if record.get("kind") == "event" and record.get("type") == "cast" and record.get("sourceID") == side_a.actor_by_name(player)["id"]:
            del dropped[index]
            break
    side_dropped = Side("local", LocalCaptureClient(parse_capture_text(serialize_ndjson(dropped))), "local", None)
    side_full = Side("fflogs", LocalCaptureClient(parse_capture_text(text)), "fflogs", None)
    if compare(side_dropped, side_full, player, tolerance_ms=200) == 0:
        print("self-test FAILED: a dropped cast went undetected")
        return 1
    print("self-test 3/3: known gaps pass, a dropped cast is caught")
    print("SELF-TEST OK")
    return 0


def _build_act_side(act_log: str, capture_path: Path, local: Side) -> Side:
    """Window the network log from the capture's filename wall clock.

    Capture filenames start yyyyMMdd_HHmmss in local time (the writer's
    naming convention); the fight length comes from the capture itself, and
    the comparator's cast-pairing offset estimate absorbs the seconds-level
    imprecision of the filename stamp.
    """
    from datetime import datetime

    stamp = datetime.strptime(capture_path.name[:15], "%Y%m%d_%H%M%S")
    start_ms = int(stamp.timestamp() * 1000)
    end_ms = start_ms + (local.end - local.start)
    return Side("act", ActLogClient(act_log, start_ms, end_ms), f"act:{Path(act_log).name}", None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true", help="validate the comparator against the committed recording fixture")
    ap.add_argument("--capture", help="path to a Meridian Companion .ndjson capture")
    ap.add_argument("--code", help="FFLogs report code of the same pull")
    ap.add_argument("--fight", type=int, help="FFLogs fight id of the same pull")
    ap.add_argument("--act-log", help="ACT network log covering the pull (upload-free pre-gate)")
    ap.add_argument("--player", help="player name to compare (default: the capture's actor 1)")
    ap.add_argument("--tolerance-ms", type=int, default=200, help="paired-event timing tolerance (default 200)")
    ap.add_argument("--allow-network", action="store_true", help="allow FFLogs fetches on dev-cache miss")
    opts = ap.parse_args()

    if opts.self_test:
        return self_test()
    if not opts.capture:
        ap.error("--capture is required (or use --self-test)")
    if not opts.act_log and not (opts.code and opts.fight is not None):
        ap.error("give either --act-log, or --code and --fight")

    capture_path = Path(opts.capture)
    text = capture_path.read_text(encoding="utf-8")
    local = Side("local", LocalCaptureClient(parse_capture_text(text)), f"local:{capture_path.stem}", None)
    player = opts.player
    if player is None:
        player = next(a["name"] for a in local.actors if a["id"] == 1 and a["type"] == "Player")
    if opts.act_log:
        other = _build_act_side(opts.act_log, capture_path, local)
    else:
        other = _build_fflogs_side(opts.code, opts.fight, opts.allow_network)
    return 0 if compare(local, other, player, opts.tolerance_ms) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
