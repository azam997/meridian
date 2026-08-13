"""Red Mage deep-advice pack (`Job.advice_probes`).

RootCause candidates for the cascade re-attribution, all deterministic ledger
walks over the delivered cast stream:

* **Cooldown drift** — Manafication / Embolden / Fleche / Contre Sixte counted
  against the strict sim's line; when a use fell off the end AND the delivered
  stream shows real accumulated gap-over-recast, the cause lands at the worst
  slip. Death and no-target downtime overlap is subtracted from every gap so a
  raid mechanic never reads as drift. The gap-closers stay out (they're
  `DRIFT_EXCLUSIONS` — movement tools), and Swiftcast is deliberately not a
  tracked cooldown (held for mechanics by design).
* **Mana overcap** — a joint White/Black gauge walk (generators/spenders from
  data.py; the Magicked Swordplay combo ids spend nothing, mirroring the free
  casts Manafication grants). Overflow past 100 marks an enchanted combo that
  started later than the gauges allowed; one cause at the first meaningful
  overflow.
* **Acceleration banked** — a charge-regen walk over the 2-charge pool: time
  spent at the cap is recharge time thrown away, and when the sim fit more
  uses the cause lands at the start of the longest fully-capped stretch.

RDM ships causes only — no ProbeItem card enrichment. The Verfire/Verstone
proc story is already carded by the bespoke ProcsAspect
(`jobs/redmage/procs.py`), so this pack never speaks about procs.

`measured_p` stays 0 on every cause — the orchestrator prices each from its
cascade segment's unexplained loss. ALL user-facing copy lives in `TEXT` /
`GAUGE_TEXT` below — improving the feedback wording is a data edit here, never
a logic change. `GAUGE_TEXT` is an allowlist: sim-state fields without an
entry (combo_step, mana_stacks, finisher_step, dualcast…) never surface in
evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.redmage import data as rd

_MANAFICATION = rd.MANAFICATION
_EMBOLDEN = rd.EMBOLDEN
_FLECHE = rd.FLECHE
_CONTRE_SIXTE = rd.CONTRE_SIXTE
_ACCELERATION = rd.ACCELERATION
_RIPOSTE = rd.ENCHANTED_RIPOSTE

# Single-charge (or effectively cadence-gated) damage cooldowns the drift
# ledger tracks. Acceleration's 2-charge pool gets its own banked-charge walk;
# the gap-closers are movement tools (DRIFT_EXCLUSIONS) and never appear here.
_DRIFT_CDS: tuple[int, ...] = (_MANAFICATION, _EMBOLDEN, _FLECHE,
                               _CONTRE_SIXTE)

_MANA_OVERCAP_MIN = 50    # total overflowed mana (both colors) before a card
                          # is worth it — half an enchanted combo's 50/50 spend
_ACCEL_CAPPED_FLOOR_FRAC = 0.5   # capped time >= recast * this before speaking


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and spend advice scoped to the measured stretch
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
    "mana_overcap": {
        "summary": ("Melee combo held past full mana, {total:.0f} mana "
                    "wasted"),
        "prescription": ("Spend mana on the enchanted melee combo right away "
                         "here. First overcap at {when}."),
        "worst_v": "{amount:.0f} mana",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} mana",
        "total_note": ("~{value:.0f}p of combo value across {count} "
                       "overcap{plural}"),
    },
    "accel_banked": {
        "summary": ("{name} sat at {cap} charges {capped:.0f}s in total, "
                    "{deficit} use{plural} lost"),
        "prescription": ("Spend an {name} charge before the second one is "
                         "ready. Longest full stretch starts at {when}; "
                         "recharge stands still while both charges are up, "
                         "and a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "capped_v": "{capped:.0f}s",
        "capped_note": "time spent sitting at the charge cap",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of simulator.SimState.
GAUGE_TEXT: dict[str, GaugeText] = {
    "white_mana": GaugeText(
        label="White Mana", short="WHT",
        over_note="the enchanted combo came later than the gauge allowed",
        under_note=None,     # running lean on mana is not a mistake by itself
        min_delta=20.0),
    "black_mana": GaugeText(
        label="Black Mana", short="BLK",
        over_note="the enchanted combo came later than the gauge allowed",
        under_note=None,
        min_delta=20.0),
    "magicked_swordplay": GaugeText(
        label="Magicked Swordplay", short="MSW",
        over_note="free combo casts from Manafication sat unspent",
        under_note=None,
        min_delta=1.0),
    "free_instant": GaugeText(
        label="Free instant", short="INST",
        over_note=("a banked instant from Acceleration or Swiftcast sat "
                   "unused"),
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlap_s(a: float, b: float,
               windows: list[tuple[float, float]]) -> float:
    """Total overlap of [a, b] with the given windows (death / downtime), so
    ledgers never blame a stretch the player could not act in."""
    total = 0.0
    for s, e in windows or []:
        lo, hi = max(a, float(s)), min(b, float(e))
        if hi > lo:
            total += hi - lo
    return total


def _quiet_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    return list(ctx.death_windows or []) + list(ctx.downtime_windows or [])


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A tracked cooldown the sim fit more of than the player cast, with the
    drift ledger that shows where the use was lost. The recast/2 noise floor
    keeps the legitimate 2-minute Manafication hold (110s recast pressed at
    the 120s buff cadence) silent — that alignment is play, not drift."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    quiet = _quiet_windows(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_CDS:
        recast, _ch = rd.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast - _overlap_s(a, b, quiet)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = rd.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cd_drift"]
        out.append((float(deficit * value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=value),
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


def _mana_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Joint ledger walk of the White/Black gauges over the delivered stream:
    overflow past the 100 cap marks an enchanted combo started later than the
    gauges allowed. The Magicked Swordplay combo ids (Manafication's free
    casts, distinct action ids in real logs) are absent from the spender
    tables, so the ledger charges mana only for paid combos — mirroring the
    sim's own rule."""
    white = 0.0
    black = 0.0
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    for t, a in sorted(ctx.norm_casts, key=lambda c: c[0]):
        if t < 0:
            continue
        if a in rd.WHITE_MANA_SPENDERS:
            white = max(0.0, white - rd.WHITE_MANA_SPENDERS[a])
            black = max(0.0, black - rd.BLACK_MANA_SPENDERS[a])
        waste = 0.0
        w_gain = rd.WHITE_MANA_GENERATORS.get(a, 0)
        if w_gain:
            white += w_gain
            if white > rd.MANA_CAP:
                waste += white - rd.MANA_CAP
                white = float(rd.MANA_CAP)
        b_gain = rd.BLACK_MANA_GENERATORS.get(a, 0)
        if b_gain:
            black += b_gain
            if black > rd.MANA_CAP:
                waste += black - rd.MANA_CAP
                black = float(rd.MANA_CAP)
        if waste > 0:
            overflows.append((t, waste))
    total = sum(v for _t, v in overflows)
    if total < _MANA_OVERCAP_MIN or not overflows:
        return None
    first = next((t for t, v in overflows if v >= 5), overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["mana_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=_RIPOSTE,
        ability_name=_name(_RIPOSTE),
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
                    value=total * rd.MANA_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["white_mana"], GAUGE_TEXT["black_mana"]])


def _accel_banked_cause(ctx: AdviceContext) -> RootCause | None:
    """Charge-regen walk over Acceleration's 2-charge pool (55s per charge,
    starting full like the sim's own opener state). Time sitting at the cap is
    recharge time thrown away; when the sim fit more uses AND enough capped
    time accumulated, the cause lands at the start of the longest fully-capped
    stretch. Death / downtime overlap is subtracted so a mechanic never reads
    as banking."""
    recast, max_ch = rd.COOLDOWNS[_ACCELERATION]
    ideal_n = sum(1 for _t, a in ctx.idealized if a == _ACCELERATION)
    times = sorted(t for t, a in ctx.norm_casts
                   if a == _ACCELERATION and 0 <= t)
    deficit = ideal_n - len(times)
    if deficit < 1:
        return None
    quiet = _quiet_windows(ctx)
    dur = float(ctx.fight_duration_s)
    state = {"ch": float(max_ch), "t_prev": 0.0,
             "total": 0.0, "worst": (0.0, 0.0)}

    def _advance(t: float) -> None:
        if t <= state["t_prev"]:
            return
        ch, t_prev = state["ch"], state["t_prev"]
        t_full = t_prev if ch >= max_ch else t_prev + (max_ch - ch) * recast
        if t > t_full:
            net = (t - t_full) - _overlap_s(t_full, t, quiet)
            if net > 0:
                state["total"] += net
                if net > state["worst"][0]:
                    state["worst"] = (net, t_full)
        state["ch"] = min(float(max_ch), ch + (t - t_prev) / recast)
        state["t_prev"] = t

    for t in times:
        _advance(min(t, dur))
        state["ch"] = max(0.0, state["ch"] - 1.0)
    _advance(dur)
    capped = state["total"]
    if capped < recast * _ACCEL_CAPPED_FLOOR_FRAC:
        return None
    when = min(max(state["worst"][1], 0.0), dur)
    name = _name(_ACCELERATION)
    value = rd.COOLDOWN_VALUE_P.get(_ACCELERATION, 0)
    t = TEXT["accel_banked"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_ACCELERATION,
        ability_name=name,
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(
            name=name, cap=max_ch, capped=capped, deficit=deficit,
            plural="s" if deficit != 1 else ""),
        prescription=t["prescription"].format(
            name=name, when=_mmss(when), value=value),
        evidence=[
            EvidenceRow(
                k=name,
                v=t["count_v"].format(player=len(times), ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Full",
                v=t["capped_v"].format(capped=capped),
                note=t["capped_note"]),
        ])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """RDM probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest per-use value first), then the mana-overcap combo delay, then the
    banked Acceleration charges. No ProbeItems — RDM's existing cards carry
    their own stories (procs live in the bespoke ProcsAspect)."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    oc = _mana_overcap_cause(ctx)
    if oc is not None:
        causes.append(oc)
    ab = _accel_banked_cause(ctx)
    if ab is not None:
        causes.append(ab)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
