"""Throwaway diagnostic: localize the PLD ceiling residual at TRUE gear GCD.

The PLD analog of diag_mch_ceiling.py — applies the same clean-measurement gate
to Paladin (the other over-100% job at true gear): at 2.50 (not the 2.45
headroom), is the perfect-sim ceiling genuinely below a top player, and what is
the shape of the miss (FoF burst-packing vs base GCD-count vs magic-combo
sequencing)? FoF is a self-buff derived from the timeline, so buff-agnostic
scoring still includes it.

Run from python/:
    python scripts/diag_pld_ceiling.py Rudeus
    python scripts/diag_pld_ceiling.py Astral --gcd 2.50 2.45
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import AAC_HEAVYWEIGHT_ENCOUNTERS, DIFFICULTY_SAVAGE   # noqa: E402
from jobs import analyze_pull                                          # noqa: E402
from jobs._core.ability_metadata import get_metadata                  # noqa: E402
from jobs._core.gcd_speed import CeilingContext                       # noqa: E402
from jobs._core.sim import engine                                     # noqa: E402
from jobs.paladin import data as pd                                   # noqa: E402
from jobs.paladin import scoring as pld_sc                            # noqa: E402
from jobs.paladin import simulator as pld_sim                         # noqa: E402
from sidecar.main import _client                                      # noqa: E402

JOB = "Paladin"
_FOF = pd.FIGHT_OR_FLIGHT


def _is_gcd(aid):
    return aid in pd.POTENCIES and aid not in pd.OGCD_IDS


def find_pull(client, substr, only_enc=None):
    encs = [(only_enc, "")] if only_enc else AAC_HEAVYWEIGHT_ENCOUNTERS
    for enc, _name in encs:
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


class PldFixedModel(pld_sim.PaladinRotationModel):
    """Prototype fix: when Fight or Flight is imminent, HOLD the Atonement chain +
    Divine Might Holy Spirit (do main-combo filler, which re-grants a fresh chain)
    so the high-potency procs land UNDER the FoF window instead of being spent
    before it — the buff-window burst-packing the greedy misses."""
    HOLD_S = 8.0

    def pick_gcd(self, state, params):
        t = state.t
        fof_soon = state.cd_ready.get(pld_sim.FIGHT_OR_FLIGHT, 0.0) - t
        holding = (0.0 < fof_soon <= self.HOLD_S
                   and state.magic_combo == 0 and not state.goring_ready)
        if holding:
            # Main-combo filler only (push the chain/DM into the FoF window).
            if state.combo_step == 1:
                return pld_sim.RIOT_BLADE
            if state.combo_step == 2:
                return pld_sim.ROYAL_AUTHORITY
            return pld_sim.FAST_BLADE
        return super().pick_gcd(state, params)


class PldBeamModel(pld_sim.PaladinRotationModel):
    """Prototype beam: fork the 'spend Divine Might Holy Spirit now vs HOLD it'
    decision (holding is lossless — Royal Authority re-grants Divine Might), so the
    GCD-tree search can pack the 500p HS under a FoF window instead of bleeding it
    pre-buff. Prune = exact (FoF-aware) partial score + an admissible held-HS credit
    so the holding line survives until it pays off under FoF; signature dedups."""

    def gcd_candidates(self, state, params):
        greedy = self.pick_gcd(state, params)
        # Fork only the filler decision: spend the highest-priority proc (chain
        # step / Divine Might HS) NOW, vs advance the main combo (hold the procs to
        # pack them under a coming FoF). Never fork when forced (magic combo /
        # Goring / a Kaeshi follow-up) — those stay greedy.
        forced = (state.magic_combo != 0 or state.goring_ready)
        is_proc = greedy in (pld_sim.SEPULCHRE, pld_sim.SUPPLICATION,
                             pld_sim.ATONEMENT, pld_sim.HOLY_SPIRIT)
        if not forced and is_proc:
            if state.combo_step == 1:
                alt = pld_sim.RIOT_BLADE
            elif state.combo_step == 2:
                alt = pld_sim.ROYAL_AUTHORITY
            else:
                alt = pld_sim.FAST_BLADE
            return [greedy, alt]
        return [greedy]

    def beam_prune(self, state, score_fn, buff_intervals):
        base = score_fn(state.timeline, self.final_aux(state), buff_intervals)
        # Admissible credit for held procs (each will be spent, ideally under FoF).
        held = 0.0
        if state.sepulchre_ready:
            held += pd.POTENCIES[pld_sim.SEPULCHRE]
        elif state.supplication_ready:
            held += pd.POTENCIES[pld_sim.SUPPLICATION]
        elif state.atonement_ready:
            held += pd.POTENCIES[pld_sim.ATONEMENT]
        if state.divine_might:
            held += pd.POTENCIES[pld_sim.HOLY_SPIRIT]
        return base + held * pd.FIGHT_OR_FLIGHT_MULT

    def beam_signature(self, state):
        return (
            round(state.t, 2), state.combo_step, state.atonement_ready,
            state.supplication_ready, state.sepulchre_ready, state.divine_might,
            state.magic_combo, state.goring_ready, state.blade_of_honor_ready,
            round(max(0.0, state.cd_ready.get(pld_sim.FIGHT_OR_FLIGHT, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(pld_sim.IMPERATOR, 0.0) - state.t), 2),
        )


def fof_windows(tl):
    return [(t, t + pd.FIGHT_OR_FLIGHT_DURATION_S) for t, a in tl if a == _FOF]


def components(tl):
    """(raw_table, fof_bonus, total) buff-agnostic. FoF is derived from the tl."""
    raw = sum(pd.POTENCIES.get(a, 0) for t, a in tl)
    total = pld_sc.score_delivered_potency(tl, buff_intervals=None)
    return raw, total - raw, total


def fof_packing(tl):
    """Per-FoF-window: covered GCD count + raw potency under the window."""
    wins = fof_windows(tl)
    out = []
    st = sorted(tl, key=lambda x: x[0])
    for s, e in wins:
        gcds = [(t, a) for t, a in st if s <= t < e and _is_gcd(a)]
        pot = sum(pd.POTENCIES.get(a, 0) for t, a in st if s <= t < e)
        out.append((round(s, 1), len(gcds), pot))
    return out


def per_ability_diff(player_tl, ceiling_tl):
    pc = Counter(a for t, a in player_tl if t >= 0)
    cc = Counter(a for t, a in ceiling_tl if t >= 0)
    rows = []
    for a in set(pc) | set(cc):
        pot = pd.POTENCIES.get(a, 0)
        d = cc.get(a, 0) - pc.get(a, 0)
        if d == 0:
            continue
        m = get_metadata(a)
        rows.append((d * pot, m.name if m else str(a), pc.get(a, 0), cc.get(a, 0), pot, d))
    return sorted(rows, key=lambda x: -abs(x[0]))


def run(name, gcds, only_enc):
    client = _client()
    found = find_pull(client, name, only_enc)
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
    player_ctx = pld_sim.measure_pld_context(mr.norm_casts)

    # Reconcile with the analyze_pull production path (inference-driven GCD).
    from jobs._core.ability_metadata import get_metadata as _gm
    from jobs._core.gcd_speed import infer_effective_gcd
    inf = infer_effective_gcd(mr.norm_casts,
                              lambda a: (_gm(a) is not None and not _gm(a).is_ogcd),
                              pld_sim.GCD_BASE_S, dt)
    print(f"  analyze idealized_strict(state)={state['idealized_strict']:.0f}  "
          f"EFF(state)={100*delivered_strict/state['idealized_strict']:.2f}%  "
          f"inferred_gcd={inf:.3f} (constant={pld_sim.GCD_BASE_S})")

    pc = [(t, a) for t, a in mr.norm_casts if t >= 0]
    praw, pfof, ptot = components(pc)
    pgcd = sum(1 for t, a in pc if _is_gcd(a))

    def dump_fof(tl, idx=2):
        wins = fof_windows(tl)
        if idx >= len(wins):
            return "(no window)"
        s, e = wins[idx]
        st = sorted(tl, key=lambda x: x[0])
        parts = []
        for t, a in st:
            if s - 0.1 <= t < e:
                m = get_metadata(a)
                nm2 = (m.name if m else str(a))[:4]
                parts.append(f"{nm2}{pd.POTENCIES.get(a,0)}")
        return " ".join(parts)
    print(f"  player FoF[2] content: {dump_fof(pc)}")

    print(f"\n=== {nm}  ({label})  dur={dur:.0f}s  downtime={len(dt)}  "
          f"entry_ctx={'yes' if player_ctx else 'cold'} ===")
    print(f"player: {len(pc)} casts, {pgcd} GCDs  raw_table={praw} FoF_bonus={pfof:.0f} "
          f"TOTAL={ptot:.0f}  delivered_strict(state)={delivered_strict:.0f}")
    print(f"  player FoF packing (start, #GCD, potency): {fof_packing(pc)}")

    for g in gcds:
        m = pld_sim.PaladinRotationModel(ctx=player_ctx, gcd_base_s=(
            g if g != pld_sim.GCD_BASE_S else None))
        tl, _aux = engine.perfect(m, pld_sim._score, dur, dt, None)
        craw, cfof, ctot = components(tl)
        cgcd = sum(1 for t, a in tl if _is_gcd(a) and t >= 0)
        print(f"\n  --- ceiling @ GCD {g:.2f} ---")
        print(f"  ceiling: {sum(1 for t,a in tl if t>=0)} casts, {cgcd} GCDs  "
              f"raw_table={craw} FoF_bonus={cfof:.0f}  TOTAL={ctot:.0f}")
        print(f"    vs player: raw_table {craw-praw:+d}  FoF_bonus {cfof-pfof:+.0f}  "
              f"TOTAL {ctot-ptot:+.0f}  (>0 ceiling above player)")
        print(f"  ceiling FoF packing: {fof_packing(tl)}")
        print(f"  ceiling FoF[2] content: {dump_fof(tl)}")
        print("  per-ability (ceiling - player), by raw potency swing:")
        for swing, anm, pn, cn, pot, d in per_ability_diff(pc, tl)[:10]:
            print(f"    {anm:<20} pot={pot:<5} player={pn:<3} ceiling={cn:<3} "
                  f"d={d:+d} p*d={swing:+d}")
        idl = pld_sc._FNS.idealized_at_duration(
            dur, dt, sim_context=CeilingContext(gcd_base_s=g, payload=player_ctx))
        print(f"  PRODUCTION idealized_strict={idl:.0f}  "
              f"EFF=delivered/idl = {100*delivered_strict/idl:.2f}%")

        # --- A/B: expand refine anchors (score-guided hold via existing hill-climb) ---
        m2 = pld_sim.PaladinRotationModel(ctx=player_ctx, gcd_base_s=(
            g if g != pld_sim.GCD_BASE_S else None))
        m2.agnostic_anchors = (
            pld_sim.FIGHT_OR_FLIGHT, pld_sim.IMPERATOR, pld_sim.ROYAL_AUTHORITY,
            pld_sim.ATONEMENT, pld_sim.SUPPLICATION, pld_sim.SEPULCHRE,
            pld_sim.HOLY_SPIRIT)
        atl, _ = engine.perfect(m2, pld_sim._score, dur, dt, None)
        araw, afof, atot = components(atl)
        print(f"  [ANCHORS+chain] raw={araw} FoF_bonus={afof:.0f} TOTAL={atot:.0f} "
              f"(vs player {atot-ptot:+.0f})")

        # --- A/B: prototype beam (HS-hold fork) ---
        import time as _time
        for width in ARGS_WIDTH:
            bm = PldBeamModel(ctx=player_ctx, gcd_base_s=(
                g if g != pld_sim.GCD_BASE_S else None))
            _t0 = _time.perf_counter()
            btl, _ = engine.beam_perfect(bm, pld_sim._score, dur, dt, None, width=width)
            _el = _time.perf_counter() - _t0
            print(f"  [BEAM w={width}] {_el:.1f}s", end="")
            braw, bfof, btot = components(btl)
            bpack = [p for _s, _n, p in fof_packing(btl)]
            print(f"  raw={braw} FoF_bonus={bfof:.0f} TOTAL={btot:.0f} "
                  f"(vs player {btot-ptot:+.0f})  FoF pot/window~{sum(bpack)//len(bpack) if bpack else 0}")

        # --- A/B: prototype FoF chain-hold packing ---
        for hold in ARGS_HOLD:
            fm = PldFixedModel(ctx=player_ctx, gcd_base_s=(
                g if g != pld_sim.GCD_BASE_S else None))
            fm.HOLD_S = hold
            ftl, _ = engine.perfect(fm, pld_sim._score, dur, dt, None)
            fraw, ffof, ftot = components(ftl)
            fpack = [p for _s, _n, p in fof_packing(ftl)]
            print(f"  [FIX hold={hold}] raw={fraw} FoF_bonus={ffof:.0f} TOTAL={ftot:.0f} "
                  f"(vs player {ftot-ptot:+.0f})  FoF pot/window~{sum(fpack)//len(fpack)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="ranking name substring (e.g. Rudeus)")
    ap.add_argument("--gcd", type=float, nargs="*", default=[2.50, 2.45])
    ap.add_argument("--enc", type=int, default=105)
    ap.add_argument("--hold", type=float, nargs="*", default=[])
    ap.add_argument("--width", type=int, nargs="*", default=[32, 128])
    args = ap.parse_args()
    global ARGS_HOLD, ARGS_WIDTH
    ARGS_HOLD = args.hold
    ARGS_WIDTH = args.width
    run(args.name, args.gcd, args.enc)


if __name__ == "__main__":
    main()
