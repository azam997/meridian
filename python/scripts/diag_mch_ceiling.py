"""Throwaway diagnostic: localize the MCH ceiling residual at TRUE gear GCD.

The NEXT_STEPS "ceiling burst-alignment" gate demands a clean, single-code-path
re-measurement before touching production scoring: at the player's real 2.50 GCD
(not the 2.43 headroom), is our "optimal" rotation genuinely below the player —
and does frac-correct Queen + the separable Queen DP clear it?

Run from python/:
    python scripts/diag_mch_ceiling.py Flandre
    python scripts/diag_mch_ceiling.py Flandre --gcd 2.50 2.43
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE   # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from jobs._core.ability_metadata import get_metadata                  # noqa: E402
from jobs._core.buff_windows import multiplier_at, multiplier_intervals  # noqa: E402
from jobs._core.gcd_speed import CeilingContext                       # noqa: E402
from jobs._core.sim import engine                                     # noqa: E402
from jobs.machinist import data as md                                 # noqa: E402
from jobs.machinist import scoring as mch_sc                          # noqa: E402
from jobs.machinist import simulator as mch_sim                       # noqa: E402
from sidecar.main import _client                                      # noqa: E402

JOB = "Machinist"
_WF_ID = 2878
_REASSEMBLE_ID = 2876
_QUEEN_ID = 16501
BV = md.BATTERY_VALUE_P_PER_UNIT


def _is_gcd(aid):
    m = get_metadata(aid)
    return m is not None and not m.is_ogcd


_HC_ID = 17209
_BS_ID = 36978
_BARREL_ID = 7414


def counts(tl):
    from collections import Counter
    c = Counter(a for t, a in tl if t >= 0)
    return c


def per_ability_diff(player_tl, ceiling_tl):
    from collections import Counter
    pc = Counter(a for t, a in player_tl if t >= 0)
    cc = Counter(a for t, a in ceiling_tl if t >= 0)
    rows = []
    for a in set(pc) | set(cc):
        pot = md.POTENCIES.get(a, 0)
        d = (cc.get(a, 0) - pc.get(a, 0))
        if d == 0 and a not in (_HC_ID, _BARREL_ID, _QUEEN_ID):
            continue
        m = get_metadata(a)
        rows.append((d * pot, m.name if m else str(a), pc.get(a, 0),
                     cc.get(a, 0), pot, d))
    return sorted(rows, key=lambda x: -abs(x[0]))


_AIR_ANCHOR = 16500
_CHAIN_SAW = 25788
_DRILL = 16498


class FixedModel(mch_sim.MachinistRotationModel):
    """Prototype fix: clip a ready 1-charge cooldown tool into an Overheated
    window when the window still has slack for it (so no Blazing Shot is lost).
    Keeps the 1-charge tools (Air Anchor / Chain Saw) from drifting past their
    cooldown while the Blazing Shot chain runs."""

    def pick_gcd(self, state, params=None):
        t = state.t
        # Live Overheated window: defer to the base picker (Blazing Shot chain).
        if state.overheated_stacks > 0 and t < state.overheated_window_end:
            return super().pick_gcd(state, params)
        # Not overheated. A ready 1-charge cooldown tool starts drifting the
        # instant it's off cooldown, while a proc keeps a 30s window — so a
        # ready tool beats a NON-expiring proc (canonical "never sit on a
        # tool"). Only when the proc itself is about to expire does it win.
        soon = self.gcd_base_s * 1.5
        proc_expiring = (
            (mch_sim.FMF in state.procs and state.procs[mch_sim.FMF] - t < soon) or
            (mch_sim.EXCAVATOR in state.procs
             and state.procs[mch_sim.EXCAVATOR] - t < soon))
        if not proc_expiring:
            if state.cd_ready.get(_AIR_ANCHOR, 1e9) <= t:
                return _AIR_ANCHOR
            if state.cd_ready.get(_CHAIN_SAW, 1e9) <= t:
                return _CHAIN_SAW
        return super().pick_gcd(state, params)


def find_pull(client, substr):
    for enc, _name in AAC_HEAVYWEIGHT_ENCOUNTERS:
        blob = client.get_rankings(enc, class_name=JOB, spec_name=JOB,
                                   difficulty=DIFFICULTY_SAVAGE, metric="rdps", page=1)
        for r in ((blob or {}).get("rankings") or []):
            if substr.lower() not in (r.get("name") or "").lower():
                continue
            rep = r.get("report") or {}
            if not rep.get("code"):
                continue
            code, fid = rep["code"], rep["fightID"]
            full = client.get_report_summary(code)
            fight = next((f for f in full["fights"] if f["id"] == fid), None)
            if fight is None:
                continue
            friendly = set(fight.get("friendlyPlayers") or [])
            actors = [a for a in full["masterData"]["actors"]
                      if a["type"] == "Player" and a.get("subType") == JOB
                      and a["id"] in friendly]
            by_name = [a for a in actors if a["name"].lower() == r["name"].lower()]
            pick = by_name or actors
            if pick:
                return code, fid, r["name"], enc
    return None


# --- Queen scoring: frac-correct prototype + per-summon term ----------------

def queen_summons(tl):
    """[(summon_t, battery_at_summon)] from a timeline (raw battery, no frac)."""
    return mch_sc._queen_summons(sorted(tl, key=lambda x: x[0]))


def queen_value(tl, bi, dur, dt, frac_on):
    """Total Queen pet potency for a timeline under buff overlay `bi`. `frac_on`
    applies the deliverable fraction (the fix); off = the current buggy raw."""
    total = 0.0
    for st, bat in queen_summons(tl):
        f = mch_sim.queen_deliverable_fraction(st, dur, dt) if frac_on else 1.0
        bm = multiplier_at(st, bi) if bi else 1.0
        total += bat * f * BV * bm
    return total


def score_fracfix(tl, aux, bi, dur, dt):
    """`score_delivered_potency` with the buff-aware Queen path frac-corrected.
    Buff-agnostic (bi falsy) is already correct (uses aux = deliverable battery)."""
    base = mch_sc.score_delivered_potency(tl, aux, bi)
    if not bi:
        return base
    return base - queen_value(tl, bi, dur, dt, False) + queen_value(tl, bi, dur, dt, True)


# --- Separable Queen DP (maximize Sum battery x frac(t) x buff(t)) ----------

def queen_dp(tl, dur, dt, bi):
    """Optimal Queen schedule value on a fixed GCD timeline. Queen is
    fire-and-forget, so her summon times are a separable sub-problem: an O(n^2)
    DP over the battery-gen events maximizing delivered `battery x frac x buff`,
    cap 100, >=50 to summon, >= QUEEN_RECAST_S apart. Returns (value, n_summons,
    [(summon_t, battery)])."""
    gens = [(t, md.BATTERY_GENERATORS[a]) for t, a in sorted(tl, key=lambda x: x[0])
            if a in md.BATTERY_GENERATORS and t >= 0.0]
    n = len(gens)
    if n == 0:
        return 0.0, 0, []
    recast = mch_sim.QUEEN_RECAST_S

    def seg_battery(j, i):
        # battery available at summon right after gen i, having last summoned
        # right after gen j (exclusive); j = -1 means from fight start.
        s = sum(gens[k][1] for k in range(j + 1, i + 1))
        return min(md.BATTERY_CAP, s)

    def val(i, j):
        t = gens[i][0] + 0.01
        bat = seg_battery(j, i)
        if bat < md.QUEEN_MIN_BATTERY:
            return None
        f = mch_sim.queen_deliverable_fraction(t, dur, dt)
        bm = multiplier_at(t, bi) if bi else 1.0
        return bat * f * BV * bm

    NEG = float("-inf")
    best = [NEG] * n          # best total with last summon right after gen i
    prev = [-2] * n           # back-pointer (gen index of previous summon, -1=start)
    for i in range(n):
        v0 = val(i, -1)
        if v0 is not None:
            best[i] = v0
            prev[i] = -1
        for j in range(i):
            if best[j] == NEG:
                continue
            if (gens[i][0] + 0.01) - (gens[j][0] + 0.01) < recast:
                continue
            v = val(i, j)
            if v is None:
                continue
            if best[j] + v > best[i]:
                best[i] = best[j] + v
                prev[i] = j
    # Best end point (trailing battery left unspent is lost).
    ei = max(range(n), key=lambda i: best[i])
    if best[ei] == NEG:
        return 0.0, 0, []
    chosen = []
    i = ei
    while i >= 0:
        t = gens[i][0] + 0.01
        chosen.append((t, seg_battery(prev[i], i)))
        i = prev[i]
    chosen.reverse()
    return best[ei], len(chosen), chosen


# --- Decomposition ----------------------------------------------------------

def components(tl, queen_battery_deliverable, bi, dur, dt):
    """(base_table, crit_bonus, wf_payload, queen_pet) under overlay bi,
    frac-correct Queen. base+crit comes from differencing the scorer."""
    # Total scored (frac-correct), buff-agnostic if bi falsy.
    if bi:
        total = score_fracfix(tl, queen_battery_deliverable, bi, dur, dt)
        qp = queen_value(tl, bi, dur, dt, True)
    else:
        total = mch_sc.score_delivered_potency(tl, queen_battery_deliverable, None)
        qp = queen_battery_deliverable * BV
    # WF payload: count covered weaponskills per WF cast (cap 6) x 240 x bmult@cast.
    st = sorted(tl, key=lambda x: x[0])
    wf = 0.0
    for wt, a in st:
        if a != _WF_ID:
            continue
        hits = 0
        for t2, a2 in st:
            if t2 <= wt or t2 > wt + 10.0:
                continue
            m = get_metadata(a2)
            if m and not m.is_ogcd and a2 not in (_WF_ID, 7418):
                hits += 1
        wf += min(hits, 6) * 240.0 * (multiplier_at(wt, bi) if bi else 1.0)
    base_crit = total - qp - wf
    return base_crit, wf, qp, total


def run(name):
    client = _client()
    found = find_pull(client, name)
    if not found:
        print(f"no pull matching {name!r}")
        return
    code, fid, nm, enc = found
    label = dict(AAC_HEAVYWEIGHT_ENCOUNTERS).get(enc, enc)
    mr = analyze_pull(JOB, client, code, fid, ranking_name=nm, label=nm)
    state = mr.aspects["Scoring"].state
    dur = state["fight_duration_s"]
    dt = state["downtime_windows"]
    delivered_strict = state["delivered_potency"]

    # Player buff overlays (observed raid + own tincture).
    obs = multiplier_intervals([
        # observed_buff_windows is [(s,e,m,label)]
    ])
    tinc_mult = state.get("tincture_multiplier") or 1.0
    tinc_wins = state.get("observed_tincture_windows") or []
    from jobs._core.buff_windows import BuffWindow
    tinc_overlay = multiplier_intervals(
        [BuffWindow(s, e, tinc_mult, "Tincture") for s, e in tinc_wins]) or None

    pc = [(t, a) for t, a in mr.norm_casts if t >= 0]
    player_q_raw = mch_sc.compute_queen_battery_spent(mr.norm_casts)
    player_q_deliv = mch_sc.compute_queen_battery_spent(mr.norm_casts, dur, dt)
    player_summons = queen_summons(pc)

    print(f"\n=== {nm}  ({label})  dur={dur:.0f}s  downtime={len(dt)} windows ===")
    print(f"player: {len(pc)} casts, {sum(1 for c in pc if _is_gcd(c[1]))} GCDs, "
          f"Queen raw={player_q_raw:.0f} deliverable={player_q_deliv:.0f} "
          f"({len(player_summons)} summons)")
    print(f"  tincture mult={tinc_mult:.4f} windows={len(tinc_wins)}  "
          f"delivered_strict(state)={delivered_strict:.0f}")

    pbc, pwf, pqp, ptot = components(pc, player_q_deliv, None, dur, dt)
    print(f"  player AGNOSTIC scored: base+crit={pbc:.0f} WF={pwf:.0f} "
          f"Queen={pqp:.0f}  TOTAL={ptot:.0f}")

    def battery_gen(tl):
        return sum(md.BATTERY_GENERATORS[a] for t, a in tl
                   if a in md.BATTERY_GENERATORS and t >= 0)
    pdp_v, pdp_n, _ = queen_dp(pc, dur, dt, None)
    print(f"  player battery GENERATED={battery_gen(pc)}  DP-on-player="
          f"{pdp_v/BV:.0f} ({pdp_n} summons)  actual deliverable={player_q_deliv:.0f}")

    for g in ARGS.gcd:
        m = mch_sim.MachinistRotationModel(gcd_base_s=g)

        # Buff-agnostic perfect ceiling at this GCD.
        tl, aux = engine.perfect(
            m, lambda t, a, b: mch_sc.score_delivered_potency(t, a, b),
            dur, dt, None)
        n_gcd = sum(1 for t, a in tl if _is_gcd(a) and t >= 0)
        cbc, cwf, cqp, ctot = components(tl, aux, None, dur, dt)
        # Queen: greedy (aux) vs DP, frac-correct, agnostic.
        dp_v, dp_n, _ = queen_dp(tl, dur, dt, None)
        greedy_q = aux  # deliverable battery
        ctot_dp = ctot - cqp + dp_v

        print(f"\n  --- ceiling @ GCD {g:.2f} ---")
        print(f"  ceiling: {sum(1 for t,a in tl if t>=0)} casts, {n_gcd} GCDs")
        print(f"  AGNOSTIC scored: base+crit={cbc:.0f} WF={cwf:.0f} "
              f"Queen={cqp:.0f}  TOTAL={ctot:.0f}")
        print(f"    vs player AGNOSTIC: base+crit {cbc-pbc:+.0f}  WF {cwf-pwf:+.0f}  "
              f"Queen {cqp-pqp:+.0f}  TOTAL {ctot-ptot:+.0f}  "
              f"(>0 ceiling above player)")
        # Ceiling Queen raw (the frac-phantom) vs deliverable.
        ceil_q_raw = sum(b for _t, b in queen_summons(tl))
        ceil_q_deliv = sum(b * mch_sim.queen_deliverable_fraction(t2, dur, dt)
                           for t2, b in queen_summons(tl))
        print(f"  Queen battery: greedy raw={ceil_q_raw:.0f} deliv={ceil_q_deliv:.0f} "
              f"(phantom {ceil_q_raw-ceil_q_deliv:+.0f})  "
              f"DP-optimal={dp_v/BV:.0f} ({dp_n} summons)  player={player_q_deliv:.0f}")
        print(f"  AGNOSTIC TOTAL with Queen-DP: {ctot_dp:.0f}  "
              f"vs player {ctot_dp-ptot:+.0f}")
        cc = counts(tl)
        pcnt = counts(pc)
        print(f"  counts (ceiling/player): HC {cc.get(_HC_ID,0)}/{pcnt.get(_HC_ID,0)}  "
              f"BlazingShot {cc.get(_BS_ID,0)}/{pcnt.get(_BS_ID,0)}  "
              f"WF {cc.get(_WF_ID,0)}/{pcnt.get(_WF_ID,0)}  "
              f"Barrel {cc.get(_BARREL_ID,0)}/{pcnt.get(_BARREL_ID,0)}  "
              f"Queen {cc.get(_QUEEN_ID,0)}/{pcnt.get(_QUEEN_ID,0)}")
        print("  per-ability (ceiling - player), by raw potency swing:")
        for swing, name, pn, cn, pot, d in per_ability_diff(pc, tl)[:12]:
            print(f"    {name:<20} pot={pot:<5} player={pn:<3} ceiling={cn:<3} "
                  f"d={d:+d} p*d={swing:+d}")
        # Tool drift probe: cast times + inter-cast gaps for the 1-charge tools.
        for tid, tname, cd in ((16500, "AirAnchor", 40.0), (25788, "ChainSaw", 60.0)):
            ct = [round(t, 1) for t, a in sorted(tl) if a == tid and t >= 0]
            pt = [round(t, 1) for t, a in sorted(pc) if a == tid]
            cgaps = [round(ct[i+1]-ct[i], 1) for i in range(len(ct)-1)]
            print(f"  {tname} (cd {cd:.0f}s): ceiling n={len(ct)} gaps={cgaps}")
            print(f"  {tname}: player  n={len(pt)} first={pt[0] if pt else '-'} "
                  f"last={pt[-1] if pt else '-'}  ceiling last={ct[-1] if ct else '-'}")

        # Production-style strict EFF at this GCD (frac-buggy tincture-swept ceiling).
        idl = mch_sc._FNS.idealized_at_duration(
            dur, dt, sim_context=CeilingContext(gcd_base_s=g))
        print(f"  PRODUCTION idealized_strict(frac-buggy)={idl:.0f}  "
              f"EFF=delivered/idl = {100*delivered_strict/idl:.2f}%")

        # Dump the timeline around the worst Air Anchor gap to see the blocker.
        aat = [t for t, a in sorted(tl) if a == _AIR_ANCHOR and t >= 0]
        if len(aat) >= 2:
            gaps = [(aat[i+1]-aat[i], aat[i], aat[i+1]) for i in range(len(aat)-1)]
            worst = max(gaps, key=lambda x: x[0])
            lo, hi = worst[1] - 1, worst[2] + 1
            print(f"  worst AA gap {worst[0]:.1f}s between {worst[1]:.1f} and {worst[2]:.1f}:")
            seg = [(round(t, 1), get_metadata(a).name if get_metadata(a) else a)
                   for t, a in sorted(tl) if lo <= t <= hi]
            print("   ", "  ".join(f"{t}:{n}" for t, n in seg))

        # --- A/B: prototype tool-clip fix ---
        fm = FixedModel(gcd_base_s=g)
        ftl, faux = engine.perfect(
            fm, lambda t, a, b: mch_sc.score_delivered_potency(t, a, b),
            dur, dt, None)
        fbc, fwf, fqp, ftot = components(ftl, faux, None, dur, dt)
        fc = counts(ftl)
        fdp_v, fdp_n, _ = queen_dp(ftl, dur, dt, None)
        ftot_dp = ftot - fqp + fdp_v
        print(f"  [FIX] AA {fc.get(_AIR_ANCHOR,0)}  GCDs {sum(1 for t,a in ftl if _is_gcd(a) and t>=0)}  "
              f"battery_gen={battery_gen(ftl)}  CleanShot {fc.get(7413,0)}  Excavator {fc.get(36981,0)}  "
              f"Queen greedy={faux:.0f} DP={fdp_v/BV:.0f}  "
              f"AGNOSTIC TOTAL={ftot:.0f} (vs player {ftot-ptot:+.0f}); "
              f"+DP={ftot_dp:.0f} (vs player {ftot_dp-ptot:+.0f})")
        print(f"        player CleanShot {pcnt.get(7413,0)}  Excavator {pcnt.get(36981,0)}")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="ranking name substring (e.g. Flandre)")
    ap.add_argument("--gcd", type=float, nargs="*", default=[2.50, 2.43])
    ARGS = ap.parse_args()
    run(ARGS.name)


if __name__ == "__main__":
    main()
