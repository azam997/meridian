"""B-probe for the multi-target cleave-geometry work: verify the FFLogs v2
position facts the design depends on but the app has never consumed (the
mitigation planner requests `includeResources` yet reads only HP — x/y/facing
arrive and are discarded; and they're only confirmed for FRIENDLY targets on
DamageTaken, per scripts/probe_damage_taken.py finding #4).

Questions (each gates a piece of jobs/_core/cleave_geometry.py):
  1. Does `includeResources: true` on the PLAYER's DamageDone stream attach
     `targetResources.x/y` when the target is an ENEMY — at what presence rate,
     and does `sourceResources` carry the player's own position (cone origins)?
  2. Units/scale sanity: do per-enemy coordinate spreads and pairwise
     enemy-to-enemy distances look like game yalms x100 (raw ~100 == 1y), i.e.
     consistent with a ~40y arena?
  3. Party-wide volume: one UNSOURCED DamageDone stream window-scoped to the
     candidate multi-target windows — how many events, how many position
     samples per enemy per second (sampling density for per-enemy position
     tracks), and does it stay a bounded fetch?
  4. (Feeds Phase D) Do PET-sourced DamageDone events carry packetID +
     targetResources, and what are the pet's distinct abilityGameIDs +
     packet-grouped hit counts (the MCH Queen id dump when run with
     --rank-spec Machinist)?

Run from python/:
    python scripts/probe_enemy_positions.py [--enc 101] [--rank-spec Machinist]

FINDINGS (run 2026-07-16 vs M9S top kill DFm9brj421X6T73k#1, all gates resolved):
  1. YES — `targetResources.x/y/facing` present on 100% (1257/1257) of the
     player's damage rows onto ENEMIES; `sourceResources.x/y` (the player's own
     position, cone origins) on ~51% (636/1257). Position-based geometry is
     fully derivable from the player's own DamageDone stream.
  2. YES — coordinates are CENTI-YALMS on the standard map grid (center =
     10000,10000 == 100.00y). Cross-checks: the boss walked y 9000->12178
     (~32y), pairwise enemy distances median 640 raw = 6.4y, max 2236 = 22.4y —
     all sane for a ~40y arena. Divide by 100 for yalms.
  3. YES, cheap — one UNSOURCED window-scoped DamageDone stream (per candidate
     MT window) returns every party member's hits on every enemy: 714 + 459
     events for M9S's two windows (single page each). Position sampling
     density: boss 7.5-9.5 samples/s, adds 0.6-4.3/s (sparsest: Charnel Cell
     0.59/s ~ one sample per 1.7s — enemies are near-stationary, so linear
     interpolation between samples is safe).
  4. NO QUEEN CLEAVE — decisive negative for the "MCH Queen pet AoE scaling"
     backlog lever. Pet DamageDone carries packetID (155/155) + targetResources
     (155/155), Queen ability ids resolve as 16503 (Roller Dash) / 16504 (Arm
     Punch) / 17206 (Pile Bunker) / 25787 (Crowned Collider) — but across the
     TOP SIX ranked MCH kills of this add fight (confirmed 3-4-target windows,
     ~460 packet-grouped Queen casts) every single cast hit EXACTLY 1 target.
     Whatever the tooltips imply, the Queen does not cleave in practice on this
     content; modeling it would inflate the ceiling with cleave nobody lands.
     Scan: scratchpad queen_cleave_scan.py pattern (top-6 loop over
     _group_packets of pet-sourced DamageDone).
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import encounter_difficulty  # noqa: E402
from fflogs_api import BundleStream  # noqa: E402
from jobs._core.downtime_sources import (  # noqa: E402
    fetch_multi_target_windows, resolve_enemy_actor_ids)
from jobs._core.multi_target import _group_packets  # noqa: E402
from sidecar.main import _client  # noqa: E402


def pick_fight(client, encounter_id: int, spec: str) -> tuple[str, dict, dict]:
    """A top-ranked kill (code, fight, summary) for the encounter."""
    diff = encounter_difficulty(encounter_id)
    blob = client.get_rankings(encounter_id, spec, spec, difficulty=diff)
    ranks = (blob or {}).get("rankings") or []
    if not ranks:
        raise SystemExit(f"no rankings for encounter {encounter_id} spec {spec}")
    for r in ranks:
        code = r["report"]["code"]
        fid = r["report"]["fightID"]
        summary = client.get_report_summary(code)
        for f in summary.get("fights") or []:
            if f.get("id") == fid and f.get("kill"):
                return code, f, summary
    raise SystemExit("no usable ranked kill found")


def res_xy(res: dict | None) -> tuple[float, float] | None:
    if not isinstance(res, dict):
        return None
    x, y = res.get("x"), res.get("y")
    if x is None or y is None:
        return None
    return float(x), float(y)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def pctiles(vals: list[float]) -> str:
    if not vals:
        return "n/a"
    vs = sorted(vals)

    def p(q: float) -> float:
        return vs[min(len(vs) - 1, int(q * len(vs)))]

    return (f"min={vs[0]:.0f} p25={p(0.25):.0f} p50={p(0.5):.0f} "
            f"p75={p(0.75):.0f} max={vs[-1]:.0f} (n={len(vs)})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=101)
    ap.add_argument("--rank-spec", default="Machinist")
    args = ap.parse_args()

    client = _client()
    code, fight, summary = pick_fight(client, args.enc, args.rank_spec)
    start, end = fight["startTime"], fight["endTime"]
    dur = (end - start) / 1000.0
    enemy_ids = resolve_enemy_actor_ids(fight)
    actors = (summary.get("masterData") or {}).get("actors") or []
    by_id = {a["id"]: a for a in actors}
    spec_slugless = args.rank_spec.replace(" ", "")
    friendly = fight.get("friendlyPlayers") or []
    player = next((a for a in actors
                   if a["id"] in friendly
                   and (a.get("subType") or "").replace(" ", "") == spec_slugless),
                  None)
    if player is None:
        raise SystemExit(f"no {args.rank_spec} player in fight")
    pets = [a for a in actors if a.get("petOwner") == player["id"]]

    print(f"report {code} fight {fight['id']} ({dur:.0f}s), enc {args.enc}")
    print(f"player: {player['name']} ({player['id']}), "
          f"pets: {[(p['id'], p['name']) for p in pets]}")
    print(f"enemies: {sorted(enemy_ids)} "
          f"({[(i, by_id.get(i, {}).get('name')) for i in sorted(enemy_ids)]})")

    mt_windows = fetch_multi_target_windows(client, code, summary, fight)
    print(f"\ncandidate MT windows: {[(round(s,1), round(e,1), n) for s, e, n in mt_windows]}")

    # --- Q1: player-sourced DamageDone with resources ------------------------
    streams = [BundleStream("DamageDone", start, end, source_id=player["id"],
                            include_resources=True)]
    # --- Q4 alongside: each pet's DamageDone with resources ------------------
    for p in pets:
        streams.append(BundleStream("DamageDone", start, end, source_id=p["id"],
                                    include_resources=True))
    bundles = client.get_event_bundle(code, streams)
    ev_player = bundles[0]

    dmg = [e for e in ev_player if e.get("type") in ("damage", "calculateddamage")
           and e.get("targetID") in enemy_ids]
    with_txy = [e for e in dmg if res_xy(e.get("targetResources"))]
    with_sxy = [e for e in dmg if res_xy(e.get("sourceResources"))]
    print(f"\nQ1 player DamageDone: {len(ev_player)} events, "
          f"{len(dmg)} damage rows onto enemies")
    print(f"   targetResources.x/y present: {len(with_txy)}/{len(dmg)}")
    print(f"   sourceResources.x/y present: {len(with_sxy)}/{len(dmg)}")
    if with_txy:
        s = with_txy[0]
        print(f"   sample targetResources: {s.get('targetResources')}")

    # --- Q2: units/scale ------------------------------------------------------
    # Per-enemy coordinate spread + pairwise enemy distances at close timestamps.
    track: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for e in with_txy:
        xy = res_xy(e.get("targetResources"))
        t = (e["timestamp"] - start) / 1000.0
        track[e["targetID"]].append((t, xy[0], xy[1]))
    print("\nQ2 per-enemy position samples (player stream):")
    for tid, pts in sorted(track.items()):
        xs = [p[1] for p in pts]
        ys = [p[2] for p in pts]
        name = by_id.get(tid, {}).get("name")
        print(f"   enemy {tid} ({name}): {len(pts)} samples over "
              f"{pts[-1][0]-pts[0][0]:.0f}s  x=[{min(xs):.0f},{max(xs):.0f}] "
              f"y=[{min(ys):.0f},{max(ys):.0f}]")
    pair_d: list[float] = []
    tids = sorted(track)
    for i, a in enumerate(tids):
        for b in tids[i + 1:]:
            for ta, xa, ya in track[a]:
                near = [(tb, xb, yb) for tb, xb, yb in track[b]
                        if abs(tb - ta) <= 2.0]
                if near:
                    tb, xb, yb = min(near, key=lambda p: abs(p[0] - ta))
                    pair_d.append(dist((xa, ya), (xb, yb)))
    print(f"   pairwise enemy distance (raw units, |dt|<=2s): {pctiles(pair_d)}")
    if pair_d:
        print(f"   /100 (assuming centi-yalms): "
              f"median {statistics.median(pair_d)/100:.1f}y, max {max(pair_d)/100:.1f}y")

    # --- Q3: party-wide unsourced DamageDone, window-scoped ------------------
    q3_windows = ([(s, e) for s, e, _n in mt_windows]
                  or [(max(0.0, dur - 60.0), dur)])
    vol_streams = [BundleStream("DamageDone", start + int(s * 1000),
                                start + int(e * 1000), include_resources=True)
                   for s, e in q3_windows]
    vol = client.get_event_bundle(code, vol_streams)
    print("\nQ3 party-wide DamageDone (window-scoped, includeResources):")
    for (s, e), evs in zip(q3_windows, vol):
        span = max(0.001, e - s)
        per_enemy: dict[int, int] = defaultdict(int)
        for ev in evs:
            if (ev.get("type") in ("damage", "calculateddamage")
                    and ev.get("targetID") in enemy_ids
                    and res_xy(ev.get("targetResources"))):
                per_enemy[ev["targetID"]] += 1
        rates = {tid: round(cnt / span, 2) for tid, cnt in sorted(per_enemy.items())}
        print(f"   window {s:.0f}-{e:.0f}s: {len(evs)} events; "
              f"position samples/enemy/s: {rates}")

    # --- Q4: pet streams ------------------------------------------------------
    for p, evs in zip(pets, bundles[1:]):
        pdmg = [e for e in evs if e.get("type") in ("damage", "calculateddamage")
                and e.get("targetID") in enemy_ids]
        with_pkt = [e for e in pdmg if e.get("packetID") is not None]
        with_pxy = [e for e in pdmg if res_xy(e.get("targetResources"))]
        groups = _group_packets(pdmg)
        by_ability: dict[int, list[int]] = defaultdict(list)
        for aid, _ts, targets in groups.values():
            by_ability[aid].append(len(targets))
        print(f"\nQ4 pet {p['id']} ({p['name']}): {len(pdmg)} damage rows, "
              f"packetID {len(with_pkt)}/{len(pdmg)}, "
              f"targetResources {len(with_pxy)}/{len(pdmg)}")
        for aid, ns in sorted(by_ability.items()):
            print(f"   ability {aid}: {len(ns)} casts, "
                  f"hit-count histo {dict(sorted(__import__('collections').Counter(ns).items()))}")


if __name__ == "__main__":
    main()
