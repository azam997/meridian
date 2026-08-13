"""Diagnostic: GNB Double Down / No Mercy lattice alignment (the Fuseir over-100).

The Fuseir Warblade M12S-P1 lesson: DD's 60s cooldown IS the No Mercy lattice —
the player lands DD inside all 7 NM windows, the pre-fix beam lands 1-2 of 7
(+200p per missed window at NM's x0.2 premium). This prints, for the player
line and the production strict ceiling: every NM window, every DD cast, and
the DD-in-NM count — the direct witness metric for the lattice fix.

A/B knobs:
  --hold H [H2...]   force forbidden_windows=((NO_MERCY, first_nm, first_nm+H),)
                     on the production model and print the greedy + beam lines
                     under the hold (the audit's NM-phasing experiment, now
                     reproducible; one hold on the first NM phases the whole
                     lattice because NM fires ASAP on cooldown)
  --width W [W2...]  beam widths for the hold A/B (default: the shipped 256)
  --ab-cart-credit   prototype variant: PLD-style NM-scaled cartridge credit
                     (carts x 420 x 1.2 when a window approaches) — measured
                     via beam_perfect, NOT shipped (SAM over-optimism class)

Run from python/:
    python scripts/diag_gnb_dd_lattice.py                     # enc 104, Fuseir
    python scripts/diag_gnb_dd_lattice.py --hold 2.5 5.0
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import ALL_ENCOUNTERS, encounter_difficulty   # noqa: E402
from jobs import analyze_pull                                 # noqa: E402
from jobs._core.sim import engine                             # noqa: E402
from jobs.gunbreaker import data as gd                        # noqa: E402
from jobs.gunbreaker import simulator as gnb_sim              # noqa: E402
from sidecar.main import _client                              # noqa: E402

JOB = "Gunbreaker"


def lattice(tl):
    """(nm_windows, dd_casts, dd_in_window_count) from a timeline."""
    casts = sorted((t, a) for t, a in tl if t >= 0)
    wins = [(t, t + gd.NO_MERCY_DURATION_S) for t, a in casts if a == gd.NO_MERCY]
    dds = [t for t, a in casts if a == gd.DOUBLE_DOWN]
    in_win = sum(1 for d in dds if any(s < d < e for s, e in wins))
    return wins, dds, in_win


def dump_lattice(label, tl, score=None):
    wins, dds, in_win = lattice(tl)
    head = f"{label}: NM x{len(wins)}  DD x{len(dds)}  DD-in-NM {in_win}/{len(wins)}"
    if score is not None:
        head += f"  score={score:.1f}"
    print(head)
    for i, (s, e) in enumerate(wins):
        inside = [d for d in dds if s < d < e]
        if inside:
            print(f"    NM[{i}] {s:7.1f}-{e:6.1f}  DD @ "
                  + ", ".join(f"{d:.1f}" for d in inside))
        else:
            nearest = min(dds, key=lambda d: min(abs(d - s), abs(d - e)),
                          default=None)
            miss = f"nearest DD {nearest:.1f}" if nearest is not None else "no DD"
            print(f"    NM[{i}] {s:7.1f}-{e:6.1f}  MISS ({miss})")


class _CartNmCreditModel(gnb_sim.GunbreakerRotationModel):
    """A/B ONLY (not shipped): PLD-style NM-scaled cartridge credit — +20% on
    every banked cart whenever a window approaches. +84/cart of optimism on
    carts that may be the overcap valve (the SAM over-optimism class)."""

    def beam_prune(self, state, score_fn, buff_intervals):
        base = super().beam_prune(state, score_fn, buff_intervals)
        t = state.t
        nm_open = t if state.no_mercy_end > t else max(
            state.cd_ready.get(gd.NO_MERCY, 0.0), t)
        if nm_open - t <= gnb_sim._DD_BANK_LEAD_S and nm_open < state.fight_duration_s:
            base += state.cartridges * gnb_sim._CARTRIDGE_PRUNE_VALUE * (
                gd.NO_MERCY_MULT - 1.0)
        return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=104)
    ap.add_argument("--name", default="Fuseir")
    ap.add_argument("--hold", type=float, nargs="*", default=[])
    ap.add_argument("--width", type=int, nargs="*", default=[gnb_sim._BEAM_WIDTH])
    ap.add_argument("--ab-cart-credit", action="store_true")
    args = ap.parse_args()

    client = _client()
    blob = client.get_rankings(args.enc, class_name=JOB, spec_name=JOB,
                               difficulty=encounter_difficulty(args.enc),
                               metric="rdps", page=1)
    r = next(x for x in (blob or {}).get("rankings", [])
             if args.name.lower() in (x.get("name") or "").lower()
             and x.get("report", {}).get("code"))
    code, fid, nm = r["report"]["code"], r["report"]["fightID"], r["name"]
    mr = analyze_pull(JOB, client, code, fid, ranking_name=nm, label=nm)
    st = mr.aspects["Scoring"].state
    dur, dt = st["fight_duration_s"], st["downtime_windows"]
    delivered = st["delivered_potency"]
    wgap = float(st.get("ceiling_witness_gap") or 0.0)
    ctx = st.get("sim_context")

    print(f"{nm}  ({dict(ALL_ENCOUNTERS).get(args.enc, args.enc)})  dur={dur:.1f}s  "
          f"delivered={delivered:.1f}  idealized_raw={st['idealized_strict'] - wgap:.1f}  "
          f"witness_gap={wgap:.1f}")
    print()
    dump_lattice("player ", mr.norm_casts, delivered)

    model = gnb_sim._model_for(dur, ctx)
    score = gnb_sim._make_score(model.mt_schedule)
    t0 = time.perf_counter()
    ctl, caux = gnb_sim.simulate_idealized_perfect(dur, dt, None, sim_context=ctx)
    el = time.perf_counter() - t0
    print()
    dump_lattice(f"ceiling ({el:.1f}s)", ctl, score(ctl, caux, None))

    if args.hold:
        # The audit's phasing experiment: one hold on the FIRST NM phases the
        # whole lattice (NM fires ASAP on cooldown afterwards).
        wins, _dds, _n = lattice(ctl)
        first_nm = wins[0][0] if wins else 0.0
        _tl0, _aux0, base_params, _s0 = engine.sweep_best(model, score, dur, dt)
        gtl0, gaux0 = engine.run_rotation(model, dur, dt, base_params)
        print()
        dump_lattice("greedy unheld", gtl0, score(gtl0, gaux0, None))
        for h in args.hold:
            fw = ((gd.NO_MERCY, first_nm, first_nm + h),)
            params = replace(base_params, forbidden_windows=fw)
            gtl, gaux = engine.run_rotation(model, dur, dt, params)
            print()
            dump_lattice(f"greedy hold={h}", gtl, score(gtl, gaux, None))
            for width in args.width:
                t0 = time.perf_counter()
                btl, baux = engine.beam_search(model, score, dur, dt, params, width)
                el = time.perf_counter() - t0
                dump_lattice(f"beam w={width} hold={h} ({el:.1f}s)",
                             btl, score(btl, baux, None))

    if args.ab_cart_credit:
        from jobs._core.gcd_speed import unwrap_ceiling_context
        gcd, _payload = unwrap_ceiling_context(ctx)
        am = _CartNmCreditModel(entry=None, gcd_base_s=gcd,
                                mt_schedule=model.mt_schedule)
        t0 = time.perf_counter()
        atl, aaux = engine.beam_perfect(am, score, dur, dt, None,
                                        width=gnb_sim._BEAM_WIDTH)
        el = time.perf_counter() - t0
        print()
        dump_lattice(f"[A/B cart-NM-credit] ({el:.1f}s)", atl, score(atl, aaux, None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
