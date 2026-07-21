"""Live probe: how do top-10 Samurai actually use Tengentsu for Kenki?

Tengentsu (Third Eye upgrade, lvl 82) grants +10 Kenki when it blocks a hit,
applying the `Tengentsu's Foresight` buff. The +10 Kenki is the only *offensive*
payoff of an otherwise-defensive button — and whether it's available depends on
the boss damage timeline (you must choose to eat a survivable hit), which is NOT
in a player's own log generically. This probe quantifies real usage so we can
decide whether the idealized ceiling should model it at all.

For each top-ranked SAM pull it counts, from the player's own buff stream:
  - Tengentsu casts            (button presses)
  - Tengentsu's Foresight apps (SUCCESSFUL procs -> +10 Kenki each)
  - Meditate casts             (the *other* off-rotation Kenki source: downtime)
and prints the Kenki gained, an upper-bound potency value, and the % of a
representative fight potency that represents.

Run from python/:  python scripts/probe_samurai_tengentsu.py [--top N] [--enc 101,103]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from sidecar.main import _client  # noqa: E402

JOB = "Samurai"
SUBTYPE = "Samurai"

# 25 Kenki = one Hissatsu: Shinten = 250 potency. So 10 Kenki ~ 100p RAW, but in
# practice Kenki overcaps, so treat this as a generous upper bound.
KENKI_PER_PROC = 10
POTENCY_PER_KENKI = 250 / 25  # = 10p / Kenki (Shinten-equivalent, upper bound)


def hr(t: str) -> None:
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def _sam_actor(report: dict, fight_id: int, name: str | None):
    fight = next((f for f in report["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None, None
    friendly = set(fight.get("friendlyPlayers") or [])
    actors = [a for a in report["masterData"]["actors"]
              if a["type"] == "Player" and a.get("subType") == SUBTYPE
              and a["id"] in friendly]
    by_name = [a for a in actors if a["name"].lower() == (name or "").lower()]
    pick = (by_name or actors)
    if not pick:
        return None, fight
    return pick[0]["id"], fight


def probe_encounter(client, enc_id: int, enc_name: str, top: int) -> list[dict]:
    blob = client.get_rankings(enc_id, class_name=JOB, spec_name=JOB,
                               difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
    ranks = [r for r in ((blob or {}).get("rankings") or [])
             if r.get("report", {}).get("code")][:top]
    hr(f"[{enc_id}] {enc_name}  —  top {len(ranks)} SAM parses")
    print(f"  {'name':16s} {'dur':>5s} {'cast':>4s} {'proc':>4s} {'rate':>5s} "
          f"{'Kenki':>5s} {'~pot':>5s} {'med':>3s}  parse")
    rows = []
    for r in ranks:
        rep = r.get("report") or {}
        code, fid = rep.get("code"), rep.get("fightID")
        if not code or fid is None:
            continue
        try:
            report = client.get_report_summary(code)
            aid, fight = _sam_actor(report, fid, r.get("name"))
            if aid is None:
                continue
            s, e = fight["startTime"], fight["endTime"]
            dur = (e - s) / 1000.0
            abil = {a["gameID"]: a["name"]
                    for a in (report["masterData"].get("abilities") or [])}

            buffs = client.get_aura_events(code, s, e, aid, data_type="Buffs")
            casts = client.get_events(code, s, e, aid, data_type="Casts")
            cast_names = Counter(abil.get(ev.get("abilityGameID"), "?")
                                 for ev in casts)

            foresight = sum(
                1 for ev in buffs
                if ev.get("type") == "applybuff"
                and "foresight" in abil.get(ev.get("abilityGameID"), "").lower())
            teng_casts = sum(n for nm, n in cast_names.items()
                             if "tengentsu" in nm.lower() or "third eye" in nm.lower())
            med_casts = sum(n for nm, n in cast_names.items()
                            if "meditate" in nm.lower())

            kenki = foresight * KENKI_PER_PROC
            pot = kenki * POTENCY_PER_KENKI
            rate = (foresight / teng_casts) if teng_casts else 0.0
            rows.append({"kenki": kenki, "pot": pot, "dur": dur,
                         "foresight": foresight, "teng": teng_casts,
                         "med": med_casts})
            print(f"  {(r.get('name') or '?')[:16]:16s} {dur:5.0f} {teng_casts:4d} "
                  f"{foresight:4d} {rate:5.0%} {kenki:5d} {pot:5.0f} {med_casts:3d}"
                  f"  {r.get('rankPercent')}")
        except Exception as ex:  # noqa: BLE001
            print(f"  {(r.get('name') or '?')[:16]:16s}  FAILED: {ex}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--enc", default="101,103,105",
                    help="comma list of encounter ids")
    args = ap.parse_args()

    client = _client()
    enc_names = dict(AAC_HEAVYWEIGHT_ENCOUNTERS)
    want = [int(x) for x in args.enc.split(",") if x.strip()]

    all_rows: list[dict] = []
    for enc_id in want:
        all_rows += probe_encounter(client, enc_id, enc_names.get(enc_id, "?"),
                                    args.top)

    hr("AGGREGATE — Tengentsu Kenki across all sampled top parses")
    if not all_rows:
        print("  no data")
        return 1
    n = len(all_rows)
    avg_proc = sum(r["foresight"] for r in all_rows) / n
    avg_kenki = sum(r["kenki"] for r in all_rows) / n
    avg_pot = sum(r["pot"] for r in all_rows) / n
    avg_med = sum(r["med"] for r in all_rows) / n
    max_proc = max(r["foresight"] for r in all_rows)
    procless = sum(1 for r in all_rows if r["foresight"] == 0)
    print(f"  pulls sampled        : {n}")
    print(f"  avg Tengentsu procs  : {avg_proc:.1f}  (max {max_proc})")
    print(f"  pulls with 0 procs   : {procless}/{n}")
    print(f"  avg Kenki from procs : {avg_kenki:.0f}  -> ~{avg_pot:.0f}p upper bound")
    print(f"  avg Meditate casts   : {avg_med:.1f}  (downtime Kenki source)")
    # A representative M-tier fight delivers ~150,000 potency; show the share.
    print(f"  ~potency share       : {avg_pot/150000*100:.2f}% of a ~150k-potency fight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
