"""Throwaway probe: what does engine.refine actually try/accept for GNB on the
Fuseir Warblade pull, and how do the beams under refined vs base params compare?

Replicates refine's hill-climb inline with per-trial prints (cast_t, delay,
trial score, accept/reject), then runs beam_search under the final refined
params AND base params, printing each line's score + DD/NM lattice.

Run from python/:
    python scripts/probe_gnb_refine.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import encounter_difficulty                   # noqa: E402
from jobs import analyze_pull                                 # noqa: E402
from jobs._core.sim import engine                             # noqa: E402
from jobs.gunbreaker import data as gd                        # noqa: E402
from jobs.gunbreaker import simulator as gnb_sim              # noqa: E402
from sidecar.main import _client                              # noqa: E402

from diag_gnb_dd_lattice import dump_lattice                  # noqa: E402


def main() -> int:
    client = _client()
    blob = client.get_rankings(104, class_name="Gunbreaker", spec_name="Gunbreaker",
                               difficulty=encounter_difficulty(104),
                               metric="rdps", page=1)
    r = next(x for x in (blob or {}).get("rankings", [])
             if "fuseir" in (x.get("name") or "").lower()
             and x.get("report", {}).get("code"))
    mr = analyze_pull("Gunbreaker", client, r["report"]["code"],
                      r["report"]["fightID"], ranking_name=r["name"],
                      label=r["name"])
    st = mr.aspects["Scoring"].state
    dur, dt = st["fight_duration_s"], st["downtime_windows"]
    ctx = st.get("sim_context")

    model = gnb_sim._model_for(dur, ctx)
    score = gnb_sim._make_score(model.mt_schedule)

    _tl, _aux, base_params, _s = engine.sweep_best(model, score, dur, dt)
    print(f"sweep_best params: max_weaves={base_params.max_weaves_per_gcd}")

    # --- refine's hill-climb, inline with prints (mirror of engine.refine) ---
    anchors = model.agnostic_anchors
    forbidden: list = []
    timeline, aux = engine.run_rotation(model, dur, dt, base_params)
    best_score = score(timeline, aux, None)
    print(f"greedy unheld: {best_score:.1f}")
    for iteration in range(engine._PERFECT_MAX_ITERATIONS):
        improved = False
        cast_anchors = sorted(
            [(t, aid) for t, aid in timeline if aid in anchors],
            key=lambda x: -x[0])
        for cast_t, cast_id in cast_anchors:
            delays = list(engine._PERFECT_DELAY_OPTIONS) + engine.alignment_delays(
                cast_t, None)
            for delay in delays:
                trial_forbidden = (*forbidden, (cast_id, cast_t, cast_t + delay))
                trial_tl, trial_aux = engine.run_rotation(
                    model, dur, dt,
                    replace(base_params, forbidden_windows=trial_forbidden))
                trial_score = score(trial_tl, trial_aux, None)
                name = "NM" if cast_id == gd.NO_MERCY else "BF"
                verdict = "ACCEPT" if trial_score > best_score + 1e-3 else "reject"
                print(f"  it{iteration} {name}@{cast_t:6.1f} +{delay:3.1f}: "
                      f"{trial_score:9.1f} ({trial_score - best_score:+7.1f}) {verdict}")
                if (name == "NM" and delay == 2.5
                        and trial_score - best_score < -1000.0):
                    dump_lattice(f"    TRIAL NM@{cast_t:.1f}+2.5", trial_tl,
                                 trial_score)
                    from collections import Counter
                    cc = Counter(a for _t, a in trial_tl)
                    uc = Counter(a for _t, a in timeline)
                    diff = {aid: cc.get(aid, 0) - uc.get(aid, 0)
                            for aid in set(cc) | set(uc)
                            if cc.get(aid, 0) != uc.get(aid, 0)}
                    print(f"    count diff vs unheld: {diff}")
                if trial_score > best_score + 1e-3:
                    timeline, aux, best_score = trial_tl, trial_aux, trial_score
                    forbidden = list(trial_forbidden)
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    print(f"\nrefined greedy: {best_score:.1f}  holds={forbidden}")
    dump_lattice("refined greedy", timeline, best_score)

    refined_params = replace(base_params, forbidden_windows=tuple(forbidden))
    for label, params in (("beam(refined)", refined_params),
                          ("beam(base)   ", base_params)):
        btl, baux = engine.beam_search(model, score, dur, dt, params,
                                       gnb_sim._BEAM_WIDTH)
        print()
        dump_lattice(label, btl, score(btl, baux, None))

    # --- A/B: in-window ready-DD outranks the Reign/Sonic procs (the player's
    # geometry: DD at the FIRST in-window boundary keeps DD's 60s cooldown on
    # the NM lattice forever; procs shift 2-3 GCDs later in the window). ---
    class DdFirstModel(gnb_sim.GunbreakerRotationModel):
        def _st_boundary_pick(self, state, params):
            if (state.no_mercy_end > state.t and state.cartridges >= 2
                    and self._dd_ready(state)):
                return gnb_sim.DOUBLE_DOWN
            return super()._st_boundary_pick(state, params)

    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, _payload = unwrap_ceiling_context(ctx)
    dm = DdFirstModel(entry=None, gcd_base_s=gcd, mt_schedule=model.mt_schedule)
    dtl, daux = engine.run_rotation(dm, dur, dt, base_params)
    print()
    dump_lattice("greedy DD-first", dtl, score(dtl, daux, None))

    # --- tincture lens: pot markers in the ceiling timeline vs the player ---
    from jobs._core.tincture import TINCTURE_ACTION_ID
    ctl2, caux2 = gnb_sim.simulate_idealized_perfect(dur, dt, None, sim_context=ctx)
    sim_pots = [round(p, 1) for p, a in ctl2 if a == TINCTURE_ACTION_ID]
    player_pots = [round(p, 1) for p, a in mr.norm_casts if a == TINCTURE_ACTION_ID]
    raw_minus_marks = gnb_sim._make_score(model.mt_schedule)(
        [(p, a) for p, a in ctl2 if a != TINCTURE_ACTION_ID], caux2, None)
    print(f"\nceiling pot markers: {sim_pots}   player pot casts in norm_casts: "
          f"{player_pots}")
    print(f"ceiling potted={score(ctl2, caux2, None):.1f}  "
          f"pot-stripped={raw_minus_marks:.1f}  "
          f"(pot uplift {score(ctl2, caux2, None) - raw_minus_marks:+.1f})")
    for k in ("delivered_potency", "idealized_strict", "tincture_loss",
              "tincture_gain", "tincture_used", "tincture_windows"):
        if k in st:
            v = st[k]
            print(f"  state[{k!r}] = {v if not isinstance(v, list) else v[:4]}")

    # --- REPLAY the player's own casts through the model (the SAM diagnostic:
    # legal replay at ~delivered => pure search gap; legality violations =>
    # fidelity gap). Track cartridges going negative at spends. ---
    print("\n--- player-cast replay through the model ---")
    pc = sorted((t, a) for t, a in mr.norm_casts if t >= 0)
    rm = gnb_sim._model_for(dur, ctx)
    rst = rm.init_state()
    rst.fight_duration_s = dur
    rm.seed_run_state(rst)      # mirror the engine's root construction
    violations = []
    spends = {gnb_sim.DOUBLE_DOWN: 2, gnb_sim.BURST_STRIKE: 1,
              gnb_sim.GNASHING_FANG: 1, gnb_sim.FATED_CIRCLE: 1}
    for t, aid in pc:
        engine.advance_time(rm, rst, t)
        need = spends.get(aid, 0)
        if need and rst.cartridges < need:
            violations.append((t, aid, rst.cartridges, need))
        if aid in (gnb_sim.DOUBLE_DOWN,) and \
                rst.cd_ready.get(aid, 0.0) > t + 0.05:
            violations.append((t, aid, "cd", round(rst.cd_ready[aid] - t, 2)))
        rm.apply_cast(rst, aid)
    replay_score = score(rst.timeline, rm.final_aux(rst), None)
    print(f"replay: {len(rst.timeline)} casts  score={replay_score:.1f}  "
          f"(delivered {st['delivered_potency']:.1f})")
    if violations:
        print(f"VIOLATIONS ({len(violations)}):")
        for v in violations[:15]:
            print(f"    {v}")
    else:
        print("no legality violations — the player's line is model-executable")
    dump_lattice("player replay", rst.timeline, replay_score)

    # --- lattice-LOCKED beam: force DD at in-window boundaries (skeleton),
    # let the beam optimize the filler around it; max-guard candidate. ---
    class LatticeLockedModel(gnb_sim.GunbreakerRotationModel):
        def gcd_candidates(self, state, params):
            forced = self._forced_step(state)
            if forced is None and state.no_mercy_end > state.t \
                    and state.cartridges >= 2 and self._dd_ready(state) \
                    and self._n(state.t) < gnb_sim._AOE_MIN_TARGETS:
                return [gnb_sim.DOUBLE_DOWN]
            return super().gcd_candidates(state, params)

    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, _payload = unwrap_ceiling_context(ctx)
    import time
    lm = LatticeLockedModel(entry=None, gcd_base_s=gcd,
                            mt_schedule=model.mt_schedule)
    t0 = time.perf_counter()
    ltl, laux = engine.beam_search(lm, score, dur, dt, base_params,
                                   gnb_sim._BEAM_WIDTH)
    el = time.perf_counter() - t0
    print()
    dump_lattice(f"LOCKED beam ({el:.1f}s)", ltl, score(ltl, laux, None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
