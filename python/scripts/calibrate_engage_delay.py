"""Calibrate the melee engage delay (RolePolicy.engage_delay_s) from real Reaper
pulls (network — needs FFLogs creds in config.json).

A melee can't act at t=0: they cross distance to the boss first. The idealized
sim starts the in-fight loop at `engage_delay_s` so a t=0 ceiling isn't treated
as reachable. We measure it as the time of each pull's first MELEE-range GCD
(the main combo / Soul Slice / Gibbet-Gallows / Reaping — abilities that need 3y)
relative to the pull start, EXCLUDING pulls that pre-channel Harpe (Harpe rolls
the GCD, which pushes the first melee GCD later and inflates the "run-in").

Bake the FAST end (p25) into MELEE_DPS.engage_delay_s in jobs/_core/job.py so a
quick dasher rarely beats the ceiling (protecting the <=100% guard).

Run from python/:  python scripts/calibrate_engage_delay.py [n_per_encounter]
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import DIFFICULTY_SAVAGE                 # noqa: E402
from jobs._core.actors import find_fight, find_player_actor  # noqa: E402
from jobs.reaper import data as rd                        # noqa: E402
from sidecar.main import _client                          # noqa: E402

JOB = "Reaper"
FFLOGS_SUBTYPE = "Reaper"
# Encounters to sample: M10S (clean single-target) + M11S (boss never leaves).
ENCOUNTERS = [(102, "M10S"), (103, "M11S")]

# MELEE-range GCDs — the first of these in-fight marks "arrived + acting". Ranged
# openers (Harpe, Shadow of Death, Harvest Moon) are cast while running in, so
# they don't indicate melee arrival and are excluded from this set.
MELEE_GCDS: frozenset[int] = frozenset({
    rd.SLICE, rd.WAXING_SLICE, rd.INFERNAL_SLICE, rd.SOUL_SLICE,
    rd.GIBBET, rd.GALLOWS, rd.EXEC_GIBBET, rd.EXEC_GALLOWS,
    rd.VOID_REAPING, rd.CROSS_REAPING,
    rd.SPINNING_SCYTHE, rd.NIGHTMARE_SCYTHE, rd.GUILLOTINE, rd.GRIM_REAPING,
})


def _first_melee_gcd_delay(client, code: str, fight_id: int,
                           actor_id: int, start: int) -> float | None:
    """First melee-range GCD time relative to pull start, or None if the pull
    pre-channels Harpe before that GCD (excluded — Harpe shifts the start)."""
    casts = client.get_events(code, start - 3000, start + 30000, actor_id,
                              data_type="Casts")
    casts = [c for c in casts if c.get("type") == "cast" and c.get("abilityGameID")]
    casts.sort(key=lambda c: c["timestamp"])
    first_melee = next((c for c in casts
                        if c["abilityGameID"] in MELEE_GCDS
                        and c["timestamp"] >= start), None)
    if first_melee is None:
        return None
    harpe_before = any(c["abilityGameID"] == rd.HARPE
                       and c["timestamp"] < first_melee["timestamp"]
                       for c in casts)
    if harpe_before:
        return None
    return (first_melee["timestamp"] - start) / 1000.0


def main() -> int:
    n_per = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    client = _client()
    delays: list[float] = []
    n_harpe_excluded = 0

    for enc_id, enc_name in ENCOUNTERS:
        blob = client.get_rankings(encounter_id=enc_id, class_name=JOB,
                                   spec_name=JOB, difficulty=DIFFICULTY_SAVAGE)
        ranks = [r for r in ((blob or {}).get("rankings") or [])
                 if r.get("report") and r["report"].get("code")][:n_per]
        print(f"\n{enc_name} ({enc_id}): {len(ranks)} ranked Reaper parses")
        for r in ranks:
            code = r["report"]["code"]
            fid = r["report"].get("fightID")
            if fid is None:
                continue
            try:
                report = client.get_report_summary(code)
                fight = find_fight(report, fid)
                actor = find_player_actor(report, fight=fight, job_name=JOB,
                                          player_name=r.get("name"))
                if actor is None:
                    continue
                d = _first_melee_gcd_delay(client, code, fid, actor["id"],
                                           fight["startTime"])
                if d is None:
                    n_harpe_excluded += 1
                elif 0.0 <= d <= 6.0:           # sane band; drop obvious noise
                    delays.append(d)
            except Exception as ex:
                print(f"    skip {code}#{fid}: {ex}")

    print(f"\nSamples: {len(delays)}   (Harpe-opener pulls excluded: {n_harpe_excluded})")
    if not delays:
        print("No usable samples — leaving the provisional value in place.")
        return 1
    delays.sort()
    p = lambda q: delays[min(len(delays) - 1, int(q * len(delays)))]  # noqa: E731
    print(f"  min={delays[0]:.2f}  p10={p(0.10):.2f}  p25={p(0.25):.2f}  "
          f"median={statistics.median(delays):.2f}  mean={statistics.mean(delays):.2f}  "
          f"max={delays[-1]:.2f}")
    print(f"\n>>> Bake MELEE_DPS.engage_delay_s = {p(0.25):.1f}  (fast-end p25)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
