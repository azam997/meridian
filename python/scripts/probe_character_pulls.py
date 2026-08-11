"""Live probe for the `list_character_pulls` fan-out.

Verifies the two things the merged Pulls list depends on but the app had
never sent before shipping it:

  1. The chunked multi-spec `zoneRankings` probe (`get_character_kill_probe`)
     survives FFLogs' per-request complexity ceiling at the default chunk of
     5 specs x 2 zone groups (10 aliases). Prints per-chunk timing and
     whether the chunk-halving fallback ever engaged.
  2. The full handler path end-to-end: row counts by job/kill, the merged
     order, and total wall time — cold vs the handler memo.

Run from python/:
    python scripts/probe_character_pulls.py --lodestone 12345678 \
        --name "First Last" --server Hyperion [--chunk 5] [--recent 10]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import ALL_JOBS  # noqa: E402
from encounters import ZONE_GROUPS  # noqa: E402
import sidecar.main as main_mod  # noqa: E402
from sidecar.main import _client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lodestone", type=int, required=True)
    ap.add_argument("--name", default="", help="character name (wipe attribution)")
    ap.add_argument("--server", default="", help="server name, e.g. Hyperion")
    ap.add_argument("--chunk", type=int, default=5)
    ap.add_argument("--recent", type=int, default=10)
    args = ap.parse_args()

    client = _client()

    # -- 1. the probe query alone, with chunk telemetry ---------------------
    chunks: list[tuple[int, float]] = []
    t0 = time.perf_counter()
    last = [t0]

    def on_chunk(done: int, total: int) -> None:
        now = time.perf_counter()
        chunks.append((done, now - last[0]))
        last[0] = now
        print(f"  chunk done={done}/{total}  {chunks[-1][1]:.2f}s")

    print(f"kill probe: {len(ALL_JOBS)} specs x {len(ZONE_GROUPS)} zone "
          f"groups, chunk={args.chunk}")
    probe = client.get_character_kill_probe(
        args.lodestone, ZONE_GROUPS, ALL_JOBS,
        chunk=args.chunk, on_chunk=on_chunk)
    probe_s = time.perf_counter() - t0
    hits = {s: [e["id"] for e in encs] for s, encs in probe.items() if encs}
    expected_chunks = (len(ALL_JOBS) + args.chunk - 1) // args.chunk
    print(f"probe total {probe_s:.2f}s in {len(chunks)} chunks "
          f"(expected {expected_chunks}; more => complexity fallback engaged)")
    print(f"specs with kills: {hits or 'none'}")

    # -- 2. the full handler, cold then memoized ----------------------------
    req = {"lodestoneId": args.lodestone, "characterName": args.name,
           "server": args.server, "recentLimit": args.recent,
           "forceRefresh": True}
    t1 = time.perf_counter()
    out = main_mod.list_character_pulls(req, "probe")
    cold_s = time.perf_counter() - t1
    by_job = Counter(r["job"] for r in out["pulls"])
    kills = sum(1 for r in out["pulls"] if r["kill"])
    print(f"\nhandler cold: {cold_s:.2f}s  rows={len(out['pulls'])} "
          f"(kills={kills}, wipes={len(out['pulls']) - kills})")
    print(f"  by job: {dict(by_job)}")
    print(f"  jobs field: {out['jobs']}")
    print(f"  encounters: {[(e['id'], e['totalKills']) for e in out['encounters']]}")
    order_ok = all(a["startTimeMs"] >= b["startTimeMs"]
                   for a, b in zip(out["pulls"], out["pulls"][1:]))
    print(f"  newest-first: {order_ok}")

    req2 = dict(req, forceRefresh=False)
    t2 = time.perf_counter()
    main_mod.list_character_pulls(req2, "probe")
    print(f"handler memo:  {time.perf_counter() - t2:.3f}s")


if __name__ == "__main__":
    main()
