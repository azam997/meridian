"""Derive (and validate) the tincture damage multiplier `M` for the current tier.

A tincture is a flat +main-stat buff; main stat enters damage only through the
attack-power term `f(MAIN) = floor(coeff*(MAIN-440)/440) + 100`, so a tincture is
a pure multiplicative buff `M = f(base+Δ)/f(base)` (jobs/_core/tincture.py).

Two modes, both printed:

  * **Formula** (no network): `M` for the per-job effective BiS main stat in
    JobData + the tier's Δ. This is the value the analyzer uses.

  * **Empirical** (top parses, validates the formula): measure `M` from real
    damage. For fixed-potency tools, take the *floor* hits (non-crit, non-DH —
    deterministic base × f(MAIN) × buffs) and compare INSIDE vs OUTSIDE the
    player's Medicated windows. Two estimates:
      - `M_norm`  — floor hits buff-normalized by each event's `multiplier`
        (removes raid buffs). Valid iff FFLogs excludes Medicated from
        `multiplier`.
      - `M_raw1`  — floor hits with NO raid buff (`multiplier ≈ 1`), raw. This is
        immune to whether Medicated is folded into `multiplier` (there are no
        other buffs to divide out), so it's the trusted cross-check.
    If `M_norm ≈ M_raw1` the normalized path is clean; if `M_norm ≈ 1` while
    `M_raw1 ≈ formula`, FFLogs folds Medicated into `multiplier` — trust `M_raw1`.
    The back-solved effective main stat is printed too (refines JobData).

Re-run when a new tier unlocks (Δ and BiS main stat drift). Run from python/:
    python scripts/calibrate_tincture.py [n_reports] [encounter_id] [base_main_stat]
"""
from __future__ import annotations

import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                       # noqa: E402
from fflogs_api import FFLogsClient                  # noqa: E402
from jobs._core.tincture import (                    # noqa: E402
    MAIN_STAT_LV,
    MEDICATED_STATUS_ID,
    TINCTURE_DELTA,
    _pair_medicated_intervals,
    tincture_multiplier,
)

# Drill, Air Anchor, Chain Saw, Excavator — all 660p, identical crit mechanics
# (same fixed-potency basis the crit/DH calibration uses).
TOOLS = [16498, 16500, 25788, 36981]
DEFAULT_ENCOUNTER = 103   # The Tyrant
DEFAULT_N_REPORTS = 20
DEFAULT_BASE_MAIN = 6838  # MCH effective BiS Dexterity, xivgear (incl. party bonus)
_MULT1_EPS = 0.005        # |multiplier - 1| under this == "no raid buff"


def _back_solve_main(m: float, delta: int, coeff: int,
                     main_lv: int = MAIN_STAT_LV) -> float:
    """Invert M = f(base+Δ)/f(base) for `base` (continuous approximation of the
    floored f). Reports the effective main stat the sampled players had."""
    if m <= 1.0:
        return float("nan")
    a = coeff / main_lv
    return main_lv + delta / (m - 1.0) - 100.0 / a


def _mch_source(client: FFLogsClient, code: str, fight_id: int) -> int | None:
    rep = client.get_report_summary(code)
    fight = next((f for f in rep["fights"] if f["id"] == fight_id), None)
    if fight is None:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    mch = [a for a in rep["masterData"]["actors"]
           if a["type"] == "Player" and a.get("subType") == "Machinist"
           and a["id"] in friendly]
    return mch[0]["id"] if mch else None


def _medicated_windows(client: FFLogsClient, code: str, s: int, e: int,
                       sid: int) -> list[tuple[float, float]]:
    evs = client.get_aura_events(code, s, e, sid, "Buffs")
    med = [ev for ev in evs if ev.get("abilityGameID") == MEDICATED_STATUS_ID]
    return _pair_medicated_intervals(med, s, e)


def _print_formula() -> None:
    base = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BASE_MAIN
    print(f"\n=== Formula (base={base}, delta={TINCTURE_DELTA}, "
          f"MAIN_LV={MAIN_STAT_LV}) ===")
    for coeff in (237, 190):
        m = tincture_multiplier(base, TINCTURE_DELTA, coeff)
        role = "non-tank" if coeff == 237 else "tank"
        print(f"  coeff={coeff:>3} ({role:>8}):  M = {m:.5f}   (+{(m - 1) * 100:.2f}%)")
    print("  -> store the effective BiS main stat per job in "
          "JobData.tincture_main_stat")


def main() -> int:
    n_reports = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_N_REPORTS
    encounter = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ENCOUNTER

    _print_formula()

    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret") or n_reports <= 0:
        print("\n(no credentials / n_reports=0 — skipping empirical cross-check)")
        return 0
    client = FFLogsClient(cfg["client_id"], cfg["client_secret"])

    blob = client.get_rankings(encounter, class_name="Machinist",
                               spec_name="Machinist", metric="rdps", page=1)
    rankings = [r for r in ((blob or {}).get("rankings") or [])
                if r.get("report", {}).get("code")][:n_reports]

    in_norm: list[float] = []
    out_norm: list[float] = []
    in_raw1: list[float] = []
    out_raw1: list[float] = []
    n_pots = 0

    for r in rankings:
        code, fid = r["report"]["code"], r["report"]["fightID"]
        src = _mch_source(client, code, fid)
        if src is None:
            continue
        rep = client.get_report_summary(code)
        fight = next((f for f in rep["fights"] if f["id"] == fid), None)
        if fight is None:
            continue
        s, e = fight["startTime"], fight["endTime"]
        windows = _medicated_windows(client, code, s, e, src)
        n_pots += len(windows)

        def inside(ts: int) -> bool:
            t = (ts - s) / 1000.0
            return any(a <= t < b for a, b in windows)

        for aid in TOOLS:
            try:
                evs = client.get_events(code, s, e, src,
                                        data_type="DamageDone", ability_id=aid)
            except Exception:
                continue
            for ev in evs:
                if ev.get("type") != "calculateddamage":
                    continue
                if ev.get("hitType") == 2 or ev.get("directHit") is True:
                    continue   # floor only (non-crit, non-DH)
                amt = ev.get("unmitigatedAmount") or ev.get("amount")
                mult = ev.get("multiplier") or 1.0
                if not amt:
                    continue
                here = inside(ev.get("timestamp", s))
                (in_norm if here else out_norm).append(amt / mult)
                if abs(mult - 1.0) <= _MULT1_EPS:
                    (in_raw1 if here else out_raw1).append(float(amt))

    base = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BASE_MAIN
    formula_m = tincture_multiplier(base, TINCTURE_DELTA, 237)
    print(f"\n=== Empirical ({len(rankings)} reports, {n_pots} pots seen) ===")
    print(f"  floor hits: inside={len(in_norm)} outside={len(out_norm)}  | "
          f"no-raid-buff: inside={len(in_raw1)} outside={len(out_raw1)}")
    m_norm = mean(in_norm) / mean(out_norm) if (in_norm and out_norm) else None
    m_raw1 = mean(in_raw1) / mean(out_raw1) if (in_raw1 and out_raw1) else None
    if m_norm is not None:
        print(f"  M_norm (buff-normalized)       = {m_norm:.5f}   "
              f"(+{(m_norm - 1) * 100:.2f}%)   "
              f"-> implied main {_back_solve_main(m_norm, TINCTURE_DELTA, 237):.0f}")
    if m_raw1 is not None:
        print(f"  M_raw1 (no-raid-buff, trusted) = {m_raw1:.5f}   "
              f"(+{(m_raw1 - 1) * 100:.2f}%)   "
              f"-> implied main {_back_solve_main(m_raw1, TINCTURE_DELTA, 237):.0f}")
    else:
        print("  M_raw1 = n/a (pots overlap raid buffs -> no clean no-buff floor)")

    print(f"\n=== Verdict (formula M = {formula_m:.5f}) ===")
    if m_raw1 is not None and abs(m_raw1 - formula_m) < 0.01:
        print("  M_raw1 ~= formula -> formula confirmed empirically.")
    elif m_norm is not None and m_norm < formula_m - 0.02 and m_raw1 is None:
        print("  M_norm sits well below the formula and there's no clean no-buff")
        print("  in-pot sample (pots always overlap raid buffs): FFLogs folds")
        print("  Medicated into its `multiplier` field, so logs can't recover M.")
        print("  -> The FORMULA is authoritative (the 'diluted buff' problem; this")
        print("     is exactly why we model M from f(MAIN), not from logs).")
    else:
        print("  inconclusive -- gather more reports / a longer-fight encounter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
