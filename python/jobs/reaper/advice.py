"""Reaper deep-advice pack (`Job.advice_probes`).

RPR's probe set for the cascade re-attribution, modeled on the Machinist
reference pack (`jobs/machinist/advice.py`). RootCauses only — RPR has no
window-shape card the shared analytic probes don't already cover, so no
`ProbeItem`s are produced. Every cause is a deterministic ledger walk over
the delivered cast stream, grounded in `jobs/reaper/data.py`:

* Recast drift that cost an end-of-fight use of Gluttony or Soul Slice (the
  only recast-gated damage buttons in `data.COOLDOWNS`; Soul Scythe consumes
  Soul Slice's shared charge pool — `charge_sharing` — so AoE phases never
  read as fake Soul Slice drift). Both are held on purpose by the optimal
  line (Gluttony waits on 50 soul and never fires inside Enshroud), so the
  ledger runs over the idealized line too and blames only the excess.
* Soul overcap marking a delayed spender: builders pushed the gauge past 100
  while Blood Stalk (or Gluttony) sat in hand.
* Shroud overcap marking a delayed Enshroud. Plentiful Harvest's Ideal Host
  makes the next Enshroud free (no shroud spent) — mirrored from the
  simulator's `apply_cast` rule, the Barrel-Stabilizer analog.
* Soul / shroud stranded at the kill: a full spender's worth (50) dead in
  the gauge at fight end, located at the last builder cast, and only when
  the gauge became spendable early enough for the spend to have paid off.

Death handling: job gauges reset on death, so the gauge ledgers reset at
each death-window start, and the drift ledger skips gaps that overlap a
death window — that time is priced by the death card, not blamed here.
Boss-untargetable seconds come off every recast gap for the same reason:
an invulnerability phase is not a late press.

`measured_p` stays 0 on every cause — the orchestrator prices each from its
cascade segment's unexplained loss. Death's Design uptime and positional
misses are already carded by RPR's bespoke aspects and are never duplicated
here.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (combo_step, soul_reaver,
lemure, void_shroud, death_design_end…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.reaper import data as rd

# Recast-gated buttons the drift ledger watches. Only `data.COOLDOWNS`
# members qualify — everything else on RPR (Enshroud, Blood Stalk, the
# Reaver/Executioner GCDs…) is gauge-gated and would read as false drift.
# Arcane Circle is deliberately absent: holding it is buff alignment, and the
# alignment/buff-window cards already own that story.
_DRIFT_TOOLS: tuple[int, ...] = (rd.GLUTTONY, rd.SOUL_SLICE)
_SOUL_OVERCAP_MIN = 25       # half a 50-soul spend (mirrors MCH's 25-heat floor)
_SHROUD_OVERCAP_MIN = 20     # two wasted 10-shroud builder GCDs
_STRANDED_MIN = 50           # one full spender (Blood Stalk / Enshroud) dead
# Room a stranded gauge needed to actually be spendable before the kill: an
# Enshroud only pays off across its whole window (`data.ENSHROUD_WINDOW_S`),
# while a Blood Stalk weave cashes in on the next GCD slot. Without these the
# ledger blames a gauge that filled in the fight's last seconds.
_SOUL_SPEND_SLOT_S = 2.5     # one GCD for the Soul Reaver the weave grants


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cd_drift": {
        # The number is idle time BEYOND the sim's own holds (RPR banks these
        # buttons on purpose), so the copy says so rather than implying the
        # button should always go the second it comes off cooldown.
        "summary": ("{name} sat idle {drift:.0f}s longer than the sim's "
                    "line, {deficit} use{plural} lost"),
        "prescription": ("Drifting {name} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s later than the sim held it; "
                         "the drift adds up until a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of extra idle time",
    },
    "soul_overcap": {
        "summary": ("Blood Stalk held past a full soul gauge, {total:.0f} "
                    "soul wasted"),
        "prescription": ("Use excess Soul right away here. First overcap at "
                         "{when}; a Blood Stalk weave banks it as a Soul "
                         "Reaver instead of losing it."),
        "worst_v": "{amount:.0f} soul",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} soul",
        "total_note": ("~{value:.0f}p of spender value across {count} "
                       "overcap{plural}"),
    },
    "shroud_overcap": {
        "summary": ("Enshroud held past a full shroud gauge, {total:.0f} "
                    "shroud wasted"),
        "prescription": ("Use excess Shroud right away here. First overcap "
                         "at {when}; each delayed Enshroud pushes the next "
                         "window later until one stops fitting."),
        "worst_v": "{amount:.0f} shroud",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} shroud",
        "total_note": ("~{value:.0f}p of Enshroud value across {count} "
                       "overcap{plural}"),
    },
    "soul_stranded": {
        "summary": "Blood Stalk left with {soul:.0f} soul at the kill",
        "prescription": ("Spend soul the moment it clears 50 late in the "
                         "fight; {uses} more Blood Stalk weave{plural} fit "
                         "before the kill (~{value:.0f}p)."),
        "gauge_v": "{soul:.0f} unspent",
        "gauge_note": "last soul builder at {when} with no spender after",
    },
    "shroud_stranded": {
        "summary": "Enshroud left with {shroud:.0f} shroud at the kill",
        "prescription": ("Enter Enshroud as soon as the gauge hits 50 late "
                         "in the fight; the window still fits before the "
                         "kill (~{value:.0f}p)."),
        "gauge_v": "{shroud:.0f} unspent",
        "gauge_note": "last shroud builder at {when} with no Enshroud after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of `simulator.SimState`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "soul": GaugeText(
        label="Soul", short="SOUL",
        over_note="a soul spender was ready",
        under_note=None,     # running lean on soul is not a mistake by itself
        min_delta=20.0),
    "shroud": GaugeText(
        label="Shroud", short="SHRD",
        over_note="an Enshroud sat ready in the gauge",
        under_note=None,
        min_delta=20.0),
    "ideal_host": GaugeText(
        label="Free Enshroud", short="HOST",
        over_note="the free Enshroud from Plentiful Harvest sat unused",
        under_note=None,
        min_delta=1.0),
    "plentiful_ready": GaugeText(
        label="Plentiful Harvest", short="PH",
        over_note="Plentiful Harvest was ready",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlaps_death(a: float, b: float,
                    windows: list[tuple[float, float]]) -> bool:
    return any(a < e and s < b for s, e in windows or [])


def _covered_s(a: float, b: float,
               windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] the boss spent untargetable. Subtracted from a recast
    gap so an invulnerability phase never reads as a late press (deaths are
    handled differently: that whole stretch is the death card's, not ours)."""
    return sum(max(0.0, min(b, e) - max(a, s)) for s, e in windows or [])


def _walk_gauges(ctx: AdviceContext) -> dict:
    """ONE deterministic pass over the delivered stream (t >= 0, stable
    time-only sort): both gauge ledgers with the simulator's own spend rules
    — an Ideal Host Enshroud (the one right after Plentiful Harvest) spends
    no shroud, mirroring `apply_cast`. Gauges reset at each death-window
    start (job gauges zero on death), so post-death stretches never read as
    phantom overcap. Returns overflow events + end state + the last builder
    cast per gauge + when each gauge last became spendable (>= 50 with no
    later drop), which is when the player's chance to use it opened."""
    soul = 0
    shroud = 0
    ideal_host = False
    soul_over: list[tuple[float, float]] = []     # (t, overflowed amount)
    shroud_over: list[tuple[float, float]] = []
    last_soul_t = 0.0
    last_shroud_t = 0.0
    soul_ready_t: float | None = None             # gauge >= 50 since this cast
    shroud_ready_t: float | None = None
    death_starts = sorted(s for s, _e in (ctx.death_windows or []))
    di = 0
    for t, a in sorted(ctx.norm_casts, key=lambda c: c[0]):
        if t < 0:
            continue                              # prepull never enters the ledger
        while di < len(death_starts) and t >= death_starts[di]:
            soul = 0
            shroud = 0
            ideal_host = False
            di += 1
        if a == rd.PLENTIFUL_HARVEST:
            ideal_host = True
        elif a == rd.ENSHROUD:
            if ideal_host:
                ideal_host = False                # free Enshroud: no shroud spent
            else:
                shroud = max(0, shroud - rd.SHROUD_SPENDERS[rd.ENSHROUD])
        if a in rd.SOUL_SPENDERS:
            soul = max(0, soul - rd.SOUL_SPENDERS[a])
        gain = rd.SOUL_GENERATORS.get(a, 0)
        if gain:
            soul += gain
            if soul > rd.SOUL_CAP:
                soul_over.append((t, float(soul - rd.SOUL_CAP)))
                soul = rd.SOUL_CAP
            last_soul_t = t
        sgain = rd.SHROUD_GENERATORS.get(a, 0)
        if sgain:
            shroud += sgain
            if shroud > rd.SHROUD_CAP:
                shroud_over.append((t, float(shroud - rd.SHROUD_CAP)))
                shroud = rd.SHROUD_CAP
            last_shroud_t = t
        soul_ready_t = (soul_ready_t if soul_ready_t is not None else t) \
            if soul >= _STRANDED_MIN else None
        shroud_ready_t = (shroud_ready_t if shroud_ready_t is not None else t) \
            if shroud >= _STRANDED_MIN else None
    return {
        "soul": soul, "shroud": shroud,
        "soul_over": soul_over, "shroud_over": shroud_over,
        "last_soul_t": last_soul_t, "last_shroud_t": last_shroud_t,
        "soul_ready_t": soul_ready_t, "shroud_ready_t": shroud_ready_t,
    }


def _drift_ledger(times: list[float], recast: float,
                  downtime: list[tuple[float, float]],
                  deaths: list[tuple[float, float]] | None = None
                  ) -> tuple[float, tuple[float, float]]:
    """Seconds one button sat idle past its recast across a press list, plus
    the worst single gap as (over_s, gap start). Boss-untargetable seconds
    come off each gap (nobody presses through an invulnerability phase), and
    gaps overlapping a death window are skipped whole — that stretch is the
    death card's, not this ledger's."""
    total = 0.0
    worst = (0.0, times[0] if times else 0.0)
    for a, b in zip(times, times[1:]):
        if _overlaps_death(a, b, deaths or []):
            continue
        over = (b - a) - _covered_s(a, b, downtime) - recast
        if over > 0:
            total += over
            if over > worst[0]:
                worst = (over, a)
    return total, worst


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A recast-gated button the sim fit more of than the player cast, with
    the drift ledger that shows where the use was lost. Soul Scythe spends
    Soul Slice's shared charge pool (`data.charge_sharing`), so its casts
    count as Soul Slice consumptions on both sides — otherwise AoE phases
    read as fake Soul Slice drift.

    Both RPR buttons are held ON PURPOSE by the optimal line: Gluttony waits
    for 50 soul and never fires inside Enshroud, so the sim's own line sits
    on it ~30-60s per pull, and Soul Slice banks charges rather than overcap
    soul. Raw gap-over-recast is therefore NOT drift here — the ledger runs
    over the idealized line too and blames only the EXCESS, so a stream that
    paces these buttons like the sim stays silent no matter how long the
    holds are."""
    out: list[tuple[float, RootCause]] = []
    for tool in _DRIFT_TOOLS:
        recast, _ch = rd.COOLDOWNS[tool]
        consume_ids = ({rd.SOUL_SLICE, rd.SOUL_SCYTHE}
                       if tool == rd.SOUL_SLICE else {tool})
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume_ids and t >= 0)
        ideal_times = sorted(t for t, a in ctx.idealized
                             if a in consume_ids and t >= 0)
        player_n = len(times)
        ideal_n = len(ideal_times)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        played, worst = _drift_ledger(times, recast, ctx.downtime_windows,
                                      ctx.death_windows)
        planned, hold = _drift_ledger(ideal_times, recast,
                                      ctx.downtime_windows)
        drift_total = played - planned      # idle beyond the sim's own holds
        late = worst[0] - hold[0]           # vs the sim's longest single hold
        if drift_total < recast * 0.5 or late <= 0:
            continue
        name = _name(tool)
        value = deficit * rd.COOLDOWN_VALUE_P.get(tool, 0)
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=tool, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=late,
                value=rd.COOLDOWN_VALUE_P.get(tool, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n, ideal=ideal_n),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _shroud_overcap_cause(walk: dict) -> RootCause | None:
    """Shroud built past the 100 cap: an Enshroud was available (>= 50 in
    the gauge) and held while Reaver/Executioner GCDs kept feeding it — the
    delay compounds into every later Enshroud window."""
    overflows = walk["shroud_over"]
    total = sum(v for _t, v in overflows)
    if not overflows or total < _SHROUD_OVERCAP_MIN:
        return None
    first = next((t for t, v in overflows if v >= 10), overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["shroud_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=rd.ENSHROUD,
        ability_name=_name(rd.ENSHROUD),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(total=total),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(amount=worst_v),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(total=total),
                note=t["total_note"].format(
                    value=total * rd.SHROUD_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["shroud"]])


def _soul_overcap_cause(walk: dict) -> RootCause | None:
    """Soul built past the 100 cap: a spender (Blood Stalk, or Gluttony when
    it was off cooldown) sat in hand while builders overflowed the gauge."""
    overflows = walk["soul_over"]
    total = sum(v for _t, v in overflows)
    if not overflows or total < _SOUL_OVERCAP_MIN:
        return None
    first = next((t for t, v in overflows if v >= 10), overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["soul_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=rd.BLOOD_STALK,
        ability_name=_name(rd.BLOOD_STALK),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(total=total),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(amount=worst_v),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(total=total),
                note=t["total_note"].format(
                    value=total * rd.SOUL_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["soul"]])


def _stranded_causes(ctx: AdviceContext, walk: dict) -> list[RootCause]:
    """Gauge that died at the kill: a full spender's worth (>= 50 soul or
    >= 50 shroud) left unspent at fight end, located at the last builder
    cast. Silent unless the gauge was spendable long enough for the spend to
    have paid off (a whole Enshroud window; one GCD slot per Blood Stalk), so
    a gauge that only filled in the closing seconds is never blamed. Ordered
    by descending value (shroud's per-unit value dwarfs soul's)."""
    out: list[tuple[float, RootCause]] = []
    dur = float(ctx.fight_duration_s)
    shroud = walk["shroud"]
    sh_ready = walk["shroud_ready_t"]
    if (shroud >= _STRANDED_MIN and walk["last_shroud_t"] > 0
            and sh_ready is not None
            and dur - sh_ready >= rd.ENSHROUD_WINDOW_S):
        when = round(min(walk["last_shroud_t"], ctx.fight_duration_s), 1)
        value = shroud * rd.SHROUD_VALUE_P_PER_UNIT
        t = TEXT["shroud_stranded"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=rd.ENSHROUD,
            ability_name=_name(rd.ENSHROUD),
            time_sec=when, measured_p=0.0,
            summary=t["summary"].format(shroud=shroud),
            prescription=t["prescription"].format(value=value),
            evidence=[EvidenceRow(
                k="Shroud",
                v=t["gauge_v"].format(shroud=shroud),
                note=t["gauge_note"].format(when=_mmss(when)))],
            resources=[GAUGE_TEXT["shroud"]])))
    soul = walk["soul"]
    s_ready = walk["soul_ready_t"]
    fits = (int((dur - s_ready) // _SOUL_SPEND_SLOT_S)
            if s_ready is not None else 0)
    uses = min(int(soul // _STRANDED_MIN), fits)
    if soul >= _STRANDED_MIN and walk["last_soul_t"] > 0 and uses >= 1:
        when = round(min(walk["last_soul_t"], ctx.fight_duration_s), 1)
        value = uses * _STRANDED_MIN * rd.SOUL_VALUE_P_PER_UNIT
        t = TEXT["soul_stranded"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=rd.BLOOD_STALK,
            ability_name=_name(rd.BLOOD_STALK),
            time_sec=when, measured_p=0.0,
            summary=t["summary"].format(soul=soul),
            prescription=t["prescription"].format(
                uses=uses, plural="s" if uses != 1 else "", value=value),
            evidence=[EvidenceRow(
                k="Soul",
                v=t["gauge_v"].format(soul=soul),
                note=t["gauge_note"].format(when=_mmss(when)))],
            resources=[GAUGE_TEXT["soul"]])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """RPR probe set. Deterministic; RootCause order is the priority order
    the orchestrator's first-in-segment-wins matching consumes: lost recast
    uses (highest total value first), then the shroud overcap (a delayed
    Enshroud outweighs a delayed Blood Stalk), then the soul overcap, then
    the gauges stranded at the kill. No ProbeItems — RPR's existing cards
    need no bespoke enrichment."""
    walk = _walk_gauges(ctx)
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    sh = _shroud_overcap_cause(walk)
    if sh is not None:
        causes.append(sh)
    so = _soul_overcap_cause(walk)
    if so is not None:
        causes.append(so)
    causes.extend(_stranded_causes(ctx, walk))
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
