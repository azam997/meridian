"""Empirically derive each raid-buff provider's canonical OPENER timing for the
current tier, from real top-parse data.

Why: the MCH "master" idealized ceiling models party buffs on the perfect
2-minute cadence (`jobs/_core/buff_windows.expected_windows`). The opener burst
is NOT at t=0 — most jobs hold their 2-min buff to ~the 3rd GCD while they ramp
into burst, and the exact timing is job-specific (Dokumori lands ~4.6s, Embolden
~8.4s). Assuming t=0 inflates the ceiling and front-loads MCH burst into a window
that isn't really there yet. We bake a per-provider opener offset into
`raid_buffs.PROVIDER_BUFFS[*].opener_offset_s`; this script regenerates it.

Method: for each current-tier encounter, take the top-N Machinist rankings,
resolve the MCH actor, and reuse the analyzer's own `fetch_observed_buff_windows`
to recover each present provider's buff windows. The *first* window start per
provider per fight is that provider's opener application time relative to fight
start. The per-provider MEDIAN across all sampled fights is the canonical offset
(median is robust to the occasional very-late re-pull / phased opener).

Sibling of calibrate_crit_dh.py — same shape: pull top parses, print paste-ready
values, re-run + re-paste when a new tier unlocks (or comp meta shifts which
providers show up). Reuses the dev disk cache when `is_dev`, so re-runs are cheap;
delete ~/.fflogs_efficiency_analyzer/dev_cache to force a fresh pull.

Run from python/:
    python scripts/calibrate_buff_timing.py [top_n]
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEV_CACHE_DIR, load_config  # noqa: E402
from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE  # noqa: E402
from fflogs_api import FFLogsClient  # noqa: E402
from jobs._core.actors import find_fight, find_player_actor  # noqa: E402
from jobs._core.buff_windows import fetch_observed_buff_windows  # noqa: E402
from jobs._core.cached_client import SessionCachedClient  # noqa: E402
from jobs._core.raid_buffs import PROVIDER_BUFFS  # noqa: E402
from sidecar.dev_cache import DevDiskCacheClient  # noqa: E402

JOB = "Machinist"
DEFAULT_TOP_N = 10
GCD = 2.5  # MCH base GCD, for the "~which GCD" annotation
# Below this many samples, the median is too noisy to trust — flag it so the
# maintainer leaves the registry default in place rather than pasting it.
MIN_CONFIDENT_N = 6

# provider-name (BuffProvider.name) -> job, for the paste hint.
_NAME_TO_JOB = {p.name: job for job, p in PROVIDER_BUFFS.items()}


def build_client():
    cfg = load_config()
    base = FFLogsClient(cfg["client_id"], cfg["client_secret"])
    if cfg.get("is_dev"):
        base = DevDiskCacheClient(base, DEV_CACHE_DIR)
    return SessionCachedClient(base)


def gcd_label(t: float) -> str:
    return f"G{int(t // GCD) + 1}"


def main() -> int:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TOP_N
    client = build_client()

    opener_starts: dict[str, list[float]] = defaultdict(list)
    n_fights = n_skipped = 0

    for enc_id, enc_name in AAC_HEAVYWEIGHT_ENCOUNTERS:
        try:
            rankings = client.get_rankings(
                encounter_id=enc_id, class_name=JOB, spec_name=JOB,
                difficulty=DIFFICULTY_SAVAGE)
        except Exception as e:  # noqa: BLE001
            print(f"  [{enc_name}] rankings failed: {e}")
            continue
        entries = ((rankings or {}).get("rankings") or [])[:top_n]
        print(f"[{enc_name}] {len(entries)} top {JOB} parses")
        for e in entries:
            rep = e.get("report") or {}
            code, fid = rep.get("code"), rep.get("fightID")
            if not code or fid is None:
                continue
            try:
                report = client.get_report_summary(code)
                fight = find_fight(report, fid)
                actor = find_player_actor(
                    report, fight=fight, job_name=JOB,
                    player_name=e.get("name")) if fight else None
                if not fight or not actor:
                    n_skipped += 1
                    continue
                windows = fetch_observed_buff_windows(
                    client, code, report, fight, actor["id"])
            except Exception as ex:  # noqa: BLE001
                print(f"    {code}#{fid} failed: {ex}")
                n_skipped += 1
                continue
            n_fights += 1
            by_prov: dict[str, list[float]] = defaultdict(list)
            for w in windows:
                by_prov[w.label].append(w.start_s)
            for label, starts in by_prov.items():
                opener_starts[label].append(min(starts))

    print(f"\nAnalyzed {n_fights} fights ({n_skipped} skipped).\n")
    if not opener_starts:
        print("No buff windows captured — cannot calibrate.")
        return 1

    # Detail table.
    print(f"{'provider':<16}{'n':>4}{'median':>9}{'mean':>8}"
          f"{'p25':>7}{'p75':>7}{'min':>7}{'max':>7}{'~GCD':>7}")
    print("-" * 71)
    for label in sorted(opener_starts, key=lambda lbl: statistics.median(opener_starts[lbl])):
        xs = sorted(opener_starts[label])
        med = statistics.median(xs)
        flag = "" if len(xs) >= MIN_CONFIDENT_N else "  (low)"
        print(f"{label:<16}{len(xs):>4}{med:>9.2f}{statistics.fmean(xs):>8.2f}"
              f"{xs[len(xs)//4]:>7.2f}{xs[(3*len(xs))//4]:>7.2f}"
              f"{xs[0]:>7.2f}{xs[-1]:>7.2f}{gcd_label(med):>7}{flag}")

    # Paste-ready block: opener_offset_s per PROVIDER_BUFFS entry.
    print("\n>>> Paste opener_offset_s into jobs/_core/raid_buffs.py::PROVIDER_BUFFS")
    print("    (providers with n < %d are too noisy — leave the registry default):"
          % MIN_CONFIDENT_N)
    for label in sorted(opener_starts, key=lambda lbl: statistics.median(opener_starts[lbl])):
        xs = opener_starts[label]
        med = round(statistics.median(xs), 1)
        job = _NAME_TO_JOB.get(label, "?")
        note = f"# n={len(xs)}" + ("" if len(xs) >= MIN_CONFIDENT_N else " (low — keep default)")
        print(f'    "{job}": opener_offset_s={med:<5}  {note}')
    # Providers in the registry that never showed up in the sample.
    missing = [job for job, p in PROVIDER_BUFFS.items()
               if p.name not in opener_starts]
    if missing:
        print(f"    (no sample, keep default): {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
