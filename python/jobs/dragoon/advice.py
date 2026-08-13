"""Dragoon deep-advice pack (`Job.advice_probes`).

The DRG probe set for the cascade re-attribution (`sidecar/advice.py`), on the
MCH registry pattern. DRG ships RootCauses only (no ProbeItems — there is no
window-shape card to enrich the way MCH's Wildfire/Hypercharge probe does),
all deterministic ledger walks over the delivered cast stream:

* **Cooldown drift** that cost an end-of-fight use — Geirskogul (the Life of
  the Dragon burst enabler and DRG's biggest button), Lance Charge, Dragonfire
  Dive, and High Jump. All four are single-charge recast-gated cooldowns in
  `data.COOLDOWNS` with no CDR and no shared pools, so the consecutive-use
  gap-over-recast ledger is exact. Gap time inside downtime or a death window
  is forgiven (the player could not press anything there). Life Surge is
  EXCLUDED (2 charges, and its misuse is already carded by LifeSurgeAspect);
  Battle Litany is excluded (crit-only party buff, zero own potency).
* **Firstminds' Focus overcap** marking a delayed Wyrmwind Thrust — a Focus
  generator (Raiden Thrust / Draconian Fury) landing on the full 2-stack gauge
  wastes the stack; each is ~half a Wyrmwind Thrust (`FOCUS_VALUE_P_PER_UNIT`).
* **Focus stranded at the kill** — a castable Wyrmwind Thrust (2 stacks) dead
  in the gauge at fight end.

`measured_p` stays 0 on every cause — the orchestrator prices each from its
cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (combo_step, dot_end,
nastrond_ready, dive_ready…) never surface in evidence lines. The transient
chain flags and buff-end clocks stay silent on purpose: their deltas at a cut
boundary read as noise, and the automatic cooldown-drift evidence rows already
name Geirskogul / Lance Charge by their real cooldowns.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.dragoon import data as dd

# Drift-watched cooldowns: single-charge, recast-gated, own damage value (see
# module docstring for the Life Surge / Battle Litany exclusions).
_DRIFT_WATCHED: tuple[int, ...] = (
    dd.GEIRSKOGUL, dd.LANCE_CHARGE, dd.DRAGONFIRE_DIVE, dd.HIGH_JUMP,
)
# A cause is worth speaking about from one full Wyrmwind Thrust of waste
# (2 Focus stacks x 220p each = the 440p spender).
_FOCUS_OVERCAP_MIN = 2
_STRANDED_FOCUS_MIN = dd.FOCUS_CAP


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cd_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Drifting {name} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s late; the drift adds up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of idle time",
    },
    "focus_overcap": {
        "summary": ("Wyrmwind Thrust held past a full gauge, {total:.0f} "
                    "Firstminds' Focus wasted"),
        "prescription": ("Spend full Firstminds' Focus on Wyrmwind Thrust "
                         "right away here. First overcap at {when}; every "
                         "stack lost at the cap is ~{per:.0f}p of Wyrmwind "
                         "Thrust value."),
        "total_v": "{total:.0f} Focus",
        "total_note": "~{value:.0f}p across {count} overcap{plural}",
        "first_v": "{when}",
        "first_note": "{name} landed on a full gauge",
    },
    "focus_stranded": {
        "summary": ("Wyrmwind Thrust left with {focus:.0f} Firstminds' Focus "
                    "at the kill"),
        "prescription": ("A last Wyrmwind Thrust fits before the end "
                         "(~{value:.0f}p)."),
        "focus_v": "{focus:.0f} unspent",
        "focus_note": ("last Focus generator at {when} with no Wyrmwind "
                       "Thrust after"),
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Only `focus` speaks: under-running the gauge means Wyrmwind Thrust was
# spent (not a mistake), and only a full 2-stack hold is a real signal.
GAUGE_TEXT: dict[str, GaugeText] = {
    "focus": GaugeText(
        label="Firstminds' Focus", short="FOC",
        over_note="a full Wyrmwind Thrust was ready",
        under_note=None,     # a lean gauge means the spender went out
        min_delta=2.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _blocked_overlap(a: float, b: float,
                     windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] covered by `windows` (downtime / death stretches the
    player could not act in — that time is never counted as drift)."""
    total = 0.0
    for s, e in windows or []:
        total += max(0.0, min(b, e) - max(a, s))
    return total


def _cd_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A watched cooldown the sim fit more of than the player cast, with the
    drift ledger that shows where the use was lost. Every watched id is
    single-charge with no CDR and no shared pool (data.COOLDOWNS), so
    gap-over-recast between consecutive uses is the exact idle time; gap time
    inside downtime or a death window is forgiven."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_WATCHED:
        recast, _ch = dd.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast
            over -= _blocked_overlap(a, b, ctx.downtime_windows)
            over -= _blocked_overlap(a, b, ctx.death_windows)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        per_use = dd.COOLDOWN_VALUE_P.get(aid, 0)
        value = deficit * per_use
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=per_use),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal_counts.get(aid, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _focus_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the Firstminds' Focus gauge over the delivered stream: a
    generator (Raiden Thrust / Draconian Fury) landing on the full 2-stack
    gauge wastes the stack — Wyrmwind Thrust came later than the gauge
    allowed. Generators and spenders come from data.py's gauge tables."""
    focus = 0
    overflows: list[tuple[float, int, int]] = []   # (t, wasted, generator id)
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        spend = dd.FOCUS_SPENDERS.get(a, 0)
        if spend:
            focus = max(0, focus - spend)
        gain = dd.FOCUS_GENERATORS.get(a, 0)
        if gain:
            focus += gain
            if focus > dd.FOCUS_CAP:
                overflows.append((t, focus - dd.FOCUS_CAP, a))
                focus = dd.FOCUS_CAP
    total = sum(w for _t, w, _a in overflows)
    if total < _FOCUS_OVERCAP_MIN or not overflows:
        return None
    first_t, _w, first_gen = overflows[0]
    t = TEXT["focus_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=dd.WYRMWIND_THRUST,
        ability_name=_name(dd.WYRMWIND_THRUST),
        time_sec=round(first_t, 1), measured_p=0.0,
        summary=t["summary"].format(total=total),
        prescription=t["prescription"].format(
            when=_mmss(first_t), per=dd.FOCUS_VALUE_P_PER_UNIT),
        evidence=[
            EvidenceRow(
                k="Wasted",
                v=t["total_v"].format(total=total),
                note=t["total_note"].format(
                    value=total * dd.FOCUS_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
            EvidenceRow(
                k="First",
                v=t["first_v"].format(when=_mmss(first_t)),
                note=t["first_note"].format(name=_name(first_gen))),
        ],
        resources=[GAUGE_TEXT["focus"]])


def _focus_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Focus that died in the gauge: a castable Wyrmwind Thrust (2 stacks)
    left unspent at fight end, located at the last generator cast."""
    focus = 0
    last_gen_t = 0.0
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        spend = dd.FOCUS_SPENDERS.get(a, 0)
        if spend:
            focus = max(0, focus - spend)
        gain = dd.FOCUS_GENERATORS.get(a, 0)
        if gain:
            focus = min(dd.FOCUS_CAP, focus + gain)
            last_gen_t = t
    if focus < _STRANDED_FOCUS_MIN:
        return None
    t = TEXT["focus_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=dd.WYRMWIND_THRUST,
        ability_name=_name(dd.WYRMWIND_THRUST),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(focus=float(focus)),
        prescription=t["prescription"].format(
            value=focus * dd.FOCUS_VALUE_P_PER_UNIT),
        evidence=[EvidenceRow(
            k="Focus",
            v=t["focus_v"].format(focus=float(focus)),
            note=t["focus_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["focus"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """DRG probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest lost value first), then the Focus-overcap Wyrmwind delay, then
    the stranded Focus."""
    items: list[ProbeItem] = []          # DRG ships causes only (no ProbeItems)
    causes: list[RootCause] = list(_cd_drift_causes(ctx))
    oc = _focus_overcap_cause(ctx)
    if oc is not None:
        causes.append(oc)
    stranded = _focus_stranded_cause(ctx)
    if stranded is not None:
        causes.append(stranded)
    return items, causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
