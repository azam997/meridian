"""Warrior deep-advice pack (`Job.advice_probes`).

The WAR probe set, following the Machinist registry pattern
(`jobs/machinist/advice.py`). WAR ships RootCauses only — no ProbeItem card
enrichment (there is no MCH-style enabler-window shape to re-place; Surging
Tempest uptime is already carded by `surging_tempest.py` and is deliberately
NOT duplicated here).

All causes are deterministic ledger walks over the delivered cast stream,
grounded in `jobs/warrior/data.py`:

* Cooldown drift that cost an end-of-fight use — Inner Release and Upheaval
  (Orogeny consumes Upheaval's shared recast, mirroring `charge_sharing`).
  Infuriate and Onslaught are charge pools, so the gap-over-recast ledger
  would misread them; Infuriate gets its own CDR-aware producer below and
  Onslaught (150p) stays silent.
* Infuriate charges banked at 2 — the pool ledger mirrors the simulator's
  fractional-charge model EXACTLY, including the 5s cooldown reduction every
  Beast-gauge weaponskill (Fell Cleave / Inner Chaos / Decimate / Chaotic
  Cyclone) grants: while both charges sit ready, both the natural recharge
  and the weaponskill refunds are wasted.
* Beast Gauge overcap marking a delayed Fell Cleave — the gauge ledger
  mirrors the simulator's Inner Release rule (a Fell Cleave / Decimate cast
  on an IR stack inside the 15s buff spends NO gauge; Inner Chaos always
  spends 50).
* Beast Gauge stranded at the kill — a spendable 50+ dead in the gauge at
  fight end, located at the last gauge builder.

Their `measured_p` stays 0 — the orchestrator prices each from its cascade
segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (combo_step, aoe_combo_step,
surging_end, primal_rend_ready…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.warrior import data as wd

# Recast-gated cooldowns the drift ledger watches: cd id -> the ids that
# consume its recast. Orogeny (the AoE Upheaval) shares Upheaval's 30s timer
# (`JobData.charge_sharing`), so it counts as an Upheaval consumption —
# otherwise multi-target pulls read as fake Upheaval drift.
_DRIFT_CDS: tuple[tuple[int, frozenset[int]], ...] = (
    (wd.INNER_RELEASE, frozenset({wd.INNER_RELEASE})),
    (wd.UPHEAVAL, frozenset({wd.UPHEAVAL, wd.OROGENY})),
)

# Every Beast-gauge weaponskill cuts Infuriate's recast by INFURIATE_CDR_S —
# the simulator applies the refund for all four forms (apply_cast), so the
# ledger mirrors all four, not just the two in CDR_RULES.
_INFURIATE_CDR_IDS: frozenset[int] = frozenset({
    wd.FELL_CLEAVE, wd.INNER_CHAOS, wd.DECIMATE, wd.CHAOTIC_CYCLONE,
})

_BEAST_OVERCAP_MIN = 25       # total overflowed gauge before a card is worth it
_BEAST_OVERCAP_FIRST_MIN = 5  # "first overcap" skips trivial 1-2 gauge slops
_STRANDED_BEAST_MIN = 50      # a full Fell Cleave died in the gauge
_FELL_CLEAVE_COST = wd.BEAST_SPENDERS[wd.FELL_CLEAVE]   # 50


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
    "infuriate_banked": {
        "summary": ("Infuriate sat at {cap} charges for {frozen:.0f}s, "
                    "{deficit} use{plural} lost"),
        "prescription": ("Use Infuriate before it fills to {cap} charges. "
                         "Longest full stretch starts at {when}; while both "
                         "charges sit ready, Fell Cleave and Inner Chaos "
                         "stop shortening the recast and an Inner Chaos "
                         "(~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "full_v": "{frozen:.0f}s",
        "full_note": "time at {cap} charges with the recharge frozen",
    },
    "beast_overcap": {
        "summary": ("Fell Cleave held past a full Beast Gauge, {total:.0f} "
                    "gauge wasted"),
        "prescription": ("Use excess Beast Gauge right away here. First "
                         "overcap at {when}."),
        "worst_v": "{amount:.0f} gauge",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} gauge",
        "total_note": ("~{value:.0f}p of Fell Cleave value across {count} "
                       "overcap{plural}"),
    },
    "beast_stranded": {
        "summary": "Beast Gauge left with {gauge:.0f} at the kill",
        "prescription": ("An extra Fell Cleave fits by spending the gauge "
                         "in the last GCDs of the fight (~{value:.0f}p)."),
        "gauge_v": "{gauge:.0f} unspent",
        "gauge_note": "last gauge builder at {when} with no spender after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of simulator.SimState.
GAUGE_TEXT: dict[str, GaugeText] = {
    "beast": GaugeText(
        label="Beast Gauge", short="BST",
        over_note="a Fell Cleave was ready",
        under_note=None,     # running lean on gauge is not a mistake by itself
        min_delta=20.0),
    "inner_release": GaugeText(
        label="Inner Release stacks", short="IR",
        over_note="free guaranteed crit weaponskills sat unspent",
        under_note=None,
        min_delta=1.0),
    "nascent_chaos": GaugeText(
        label="Nascent Chaos", short="NC",
        over_note="an Inner Chaos sat ready but uncast",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _inside(t: float, windows: list[tuple[float, float]]) -> bool:
    return any(s <= t <= e for s, e in windows or [])


def _overlaps(a: float, b: float,
              windows: list[tuple[float, float]]) -> bool:
    return any(s < b and a < e for s, e in windows or [])


# --- RootCause producers ----------------------------------------------------

def _cooldown_drift_causes(ctx: AdviceContext
                           ) -> list[tuple[float, RootCause]]:
    """A recast-gated cooldown the sim fit more of than the player cast, with
    the drift ledger that shows where the use was lost. Gaps overlapping a
    death window are excluded (deaths are priced by their own card)."""
    out: list[tuple[float, RootCause]] = []
    for cd_id, consume_ids in _DRIFT_CDS:
        recast, _ch = wd.COOLDOWNS[cd_id]
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume_ids and t >= 0)
        player_n = len(times)
        ideal_n = sum(1 for _t, a in ctx.idealized if a in consume_ids)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            if _overlaps(a, b, ctx.death_windows):
                continue
            over = (b - a) - recast
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(cd_id)
        per_use = wd.COOLDOWN_VALUE_P.get(cd_id, 0)
        value = float(deficit * per_use)
        t = TEXT["cd_drift"]
        out.append((value, RootCause(
            kind="cascade_lost_use", ability_id=cd_id, ability_name=name,
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
                    v=t["count_v"].format(player=player_n, ideal=ideal_n),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(recasts=drift_total / recast)),
            ])))
    return out


def _infuriate_banked_cause(ctx: AdviceContext
                            ) -> tuple[float, RootCause] | None:
    """Infuriate charges banked at the cap: the pool ledger mirrors the
    simulator's fractional-charge model exactly — natural regen at 1/recast
    per second PLUS the 5s refund every Beast-gauge weaponskill grants
    (`INFURIATE_CDR_S`, all four forms, matching apply_cast). Time spent at
    the full 2 charges is regen thrown away; the cause fires only when the
    player also finished behind the sim's Infuriate count (deficit gate), so
    pre-burst pooling for Inner Release stays legitimate."""
    recast, max_ch = wd.COOLDOWNS[wd.INFURIATE]
    casts = [(t, a) for t, a in sorted(ctx.norm_casts) if t >= 0]
    player_n = sum(1 for _t, a in casts if a == wd.INFURIATE)
    ideal_n = sum(1 for _t, a in ctx.idealized if a == wd.INFURIATE)
    deficit = ideal_n - player_n
    if deficit < 1:
        return None
    charges = float(max_ch)                      # sim init: 2.0 charges
    full_since: float | None = 0.0
    prev_t = 0.0
    stretches: list[tuple[float, float]] = []
    for t, a in casts:
        if full_since is None:
            gained = (t - prev_t) / recast
            if charges + gained >= max_ch:
                full_since = prev_t + (max_ch - charges) * recast
                charges = float(max_ch)
            else:
                charges += gained
        prev_t = t
        if a == wd.INFURIATE:
            if full_since is not None:
                stretches.append((full_since, t))
                full_since = None
            charges = max(0.0, charges - 1.0)
        elif a in _INFURIATE_CDR_IDS and full_since is None:
            # The weaponskill refund; wasted entirely while the pool is full.
            charges += wd.INFURIATE_CDR_S / recast
            if charges >= max_ch:
                charges = float(max_ch)
                full_since = t
    end = float(ctx.fight_duration_s)
    if full_since is None:
        gained = (end - prev_t) / recast
        if charges + gained >= max_ch:
            full_since = prev_t + (max_ch - charges) * recast
    if full_since is not None and full_since < end:
        stretches.append((full_since, end))
    keep = [(s, e) for s, e in stretches
            if e > s and not _overlaps(s, e, ctx.death_windows)]
    frozen = sum(e - s for s, e in keep)
    if frozen < recast * 0.5 or not keep:
        return None
    worst = max(keep, key=lambda w: (w[1] - w[0], -w[0]))
    per_use = wd.COOLDOWN_VALUE_P.get(wd.INFURIATE, 0)
    value = float(deficit * per_use)
    name = _name(wd.INFURIATE)
    t = TEXT["infuriate_banked"]
    when = min(max(worst[0], 0.0), end)
    return (value, RootCause(
        kind="cascade_lost_use", ability_id=wd.INFURIATE, ability_name=name,
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(
            cap=max_ch, frozen=frozen, deficit=deficit,
            plural="s" if deficit != 1 else ""),
        prescription=t["prescription"].format(
            cap=max_ch, when=_mmss(when), value=per_use),
        evidence=[
            EvidenceRow(
                k=name,
                v=t["count_v"].format(player=player_n, ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Full",
                v=t["full_v"].format(frozen=frozen),
                note=t["full_note"].format(cap=max_ch)),
        ]))


def _beast_walk(ctx: AdviceContext
                ) -> tuple[list[tuple[float, float]], float, float | None,
                           float | None]:
    """One Beast Gauge ledger walk over the delivered stream. Returns
    (overflow events outside death windows, end-of-fight gauge, last builder
    time, last PAID spend time). Mirrors the simulator's rules: an Inner
    Release stack (3 per cast, 15s buff) frees the Beast cost of Fell Cleave /
    Decimate ONLY; Inner Chaos / Chaotic Cyclone always spend 50. Free IR
    casts do not count as paid spends (they touch stacks, not gauge)."""
    beast = 0.0
    ir_stacks = 0
    ir_until = float("-inf")
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    last_gen_t: float | None = None
    last_spend_t: float | None = None
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a == wd.INNER_RELEASE:
            ir_stacks = wd.INNER_RELEASE_STACKS
            ir_until = t + wd.INNER_RELEASE_BUFF_S
        free_ir = (a in (wd.FELL_CLEAVE, wd.DECIMATE)
                   and ir_stacks > 0 and t < ir_until)
        if free_ir:
            ir_stacks -= 1
        elif a in wd.BEAST_SPENDERS:
            beast = max(0.0, beast - wd.BEAST_SPENDERS[a])
            last_spend_t = t
        gain = wd.BEAST_GENERATORS.get(a, 0)
        if gain:
            beast += gain
            last_gen_t = t
            if beast > wd.BEAST_CAP:
                if not _inside(t, ctx.death_windows):
                    overflows.append((t, beast - wd.BEAST_CAP))
                beast = float(wd.BEAST_CAP)
    return overflows, beast, last_gen_t, last_spend_t


def _beast_overcap_cause(ctx: AdviceContext
                         ) -> tuple[float, RootCause] | None:
    """Gauge overflow marks a Fell Cleave fired later than the gauge allowed
    — the wasted gauge is combo-finisher (and Infuriate) value thrown away."""
    overflows, _end_gauge, _last, _spend = _beast_walk(ctx)
    total = sum(v for _t, v in overflows)
    if total < _BEAST_OVERCAP_MIN or not overflows:
        return None
    first = next((t for t, v in overflows if v >= _BEAST_OVERCAP_FIRST_MIN),
                 overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    value = total * wd.BEAST_VALUE_P_PER_UNIT
    t = TEXT["beast_overcap"]
    return (value, RootCause(
        kind="cascade_burst", ability_id=wd.FELL_CLEAVE,
        ability_name=_name(wd.FELL_CLEAVE),
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
                    value=value, count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["beast"]]))


def _beast_stranded_cause(ctx: AdviceContext
                          ) -> tuple[float, RootCause] | None:
    """Gauge that died at the kill: a spendable Fell Cleave (>= 50 Beast)
    left unspent at fight end, located at the last gauge builder. Priced by
    the spendable part only (Fell Cleave spends exactly 50). Silent when the
    fight's tail overlaps a death window (the death card owns that story)."""
    _overflows, end_gauge, last_gen_t, last_spend_t = _beast_walk(ctx)
    if end_gauge < _STRANDED_BEAST_MIN or last_gen_t is None:
        return None
    if last_spend_t is not None and last_spend_t > last_gen_t:
        # A paid spend landed after the last builder (a Fell Cleave only
        # subtracts 50, so 50+ can survive it). The "no spender after"
        # evidence line would be false here, so stay silent.
        return None
    if _overlaps(last_gen_t, float(ctx.fight_duration_s), ctx.death_windows):
        return None
    spendable = (int(end_gauge) // _FELL_CLEAVE_COST) * _FELL_CLEAVE_COST
    value = spendable * wd.BEAST_VALUE_P_PER_UNIT
    t = TEXT["beast_stranded"]
    return (value, RootCause(
        kind="cascade_lost_use", ability_id=wd.FELL_CLEAVE,
        ability_name=_name(wd.FELL_CLEAVE),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(gauge=end_gauge),
        prescription=t["prescription"].format(value=value),
        evidence=[EvidenceRow(
            k="Gauge",
            v=t["gauge_v"].format(gauge=end_gauge),
            note=t["gauge_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["beast"]]))


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """WAR probe set. No ProbeItems; RootCauses ordered by descending lost
    value (stable ability-id tie-break) — that order is the priority the
    orchestrator's first-in-segment-wins matching consumes."""
    weighted: list[tuple[float, RootCause]] = []
    weighted.extend(_cooldown_drift_causes(ctx))
    for producer in (_infuriate_banked_cause, _beast_overcap_cause,
                     _beast_stranded_cause):
        got = producer(ctx)
        if got is not None:
            weighted.append(got)
    weighted.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [], [c for _v, c in weighted]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
