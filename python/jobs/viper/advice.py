"""Viper deep-advice pack (`Job.advice_probes`).

RootCause candidates for the cascade re-attribution (the MCH registry
pattern), all deterministic ledger walks over the delivered cast stream:

* Serpent's Ire drift that cost an end-of-fight use — the 120s burst engine
  (+1 Rattling Coil and the free Reawaken it grants).
* Serpent Offering overcap that marks a delayed Reawaken. The ledger mirrors
  the sim's free-Reawaken rule: a Reawaken cast under Ready to Reawaken
  (from Serpent's Ire) spends no offering.
* Rattling Coil overcap that marks a delayed Uncoiled Fury (a full gauge
  turns the next Vicewinder / Serpent's Ire coil into nothing).
* Rattling Coils stranded at the kill — a banked Uncoiled Fury that never
  happened, measured as the excess over the ideal line's own end balance
  (a final Vicewinder coil pair the sim itself banks is correct play).

No ProbeItems: Viper has no underfilled-window card (the MCH wildfire shape)
to enrich, so the pack ships causes only. `measured_p` stays 0 — the
orchestrator prices each cause from its cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (anguine, reawaken_step,
combo_step, vice_step, the cosmetic alternation flags…) never surface in
evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.viper import data as vd

_IRE = vd.SERPENTS_IRE
_REAWAKEN = vd.REAWAKEN
_UNCOILED = vd.UNCOILED_FURY

# Emission floors (silent-when-clean thresholds).
_IRE_DRIFT_FLOOR_S = 10.0      # confirmed slip before a lost Ire is spoken about
                               # (the MCH recast*0.5 rule would demand 60s on a
                               # 120s cooldown; a use falls off the end well
                               # before that, so an absolute floor is honest)
_OFFERING_OVERCAP_MIN = 20.0   # two full finishers' worth of wasted offering
_COIL_OVERCAP_MIN = 1.0        # one whole coil (an Uncoiled Fury) wasted
_STRANDED_COILS_MIN = 1.0      # a spendable Uncoiled Fury dead in the gauge
_STRANDED_TAIL_S = 5.0         # a coil gained this close to the kill was never
                               # spendable (Uncoiled Fury is a 3.5s GCD slot)


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "ire_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Drifting {name} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s late; the drift adds up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "total lateness between uses",
    },
    "offering_overcap": {
        "summary": ("Reawaken held past full Serpent Offering, {total:.0f} "
                    "offering wasted"),
        "prescription": ("Spend the full gauge on Reawaken right away here. "
                         "First overcap at {when}."),
        "worst_v": "{amount:.0f} offering",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} offering",
        "total_note": ("~{value:.0f}p of Reawaken value across {count} "
                       "overcap{plural}"),
    },
    "coil_overcap": {
        "summary": ("Rattling Coils overcapped, {total:.0f} "
                    "coil{plural} wasted"),
        "prescription": ("Spend a banked coil on Uncoiled Fury right away "
                         "here. First wasted coil at {when}."),
        "coils_v": "{total:.0f} wasted",
        "coils_note": ("each one is an Uncoiled Fury (~{value:.0f}p) that "
                       "never happened"),
        "first_v": "{when}",
        "first_note": "the first coil lost over the {cap}-coil cap",
    },
    "coils_stranded": {
        "summary": ("{total:.0f} Rattling Coil{plural} left at the kill"),
        "prescription": ("An extra Uncoiled Fury fits by spending banked "
                         "coils late in the fight (~{value:.0f}p)."),
        "coils_v": "{total:.0f} unspent",
        "coils_note": "last coil gained at {when} with no Uncoiled Fury after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of simulator.SimState.
GAUGE_TEXT: dict[str, GaugeText] = {
    "offering": GaugeText(
        label="Serpent Offering", short="OFR",
        over_note="Reawaken came later than the gauge allowed",
        under_note=None,   # running lean on offering is not a mistake by itself
        min_delta=20.0),
    "rattling": GaugeText(
        label="Rattling Coils", short="COIL",
        over_note="an Uncoiled Fury was ready",
        under_note=None,
        min_delta=1.0),
    "ready_to_reawaken": GaugeText(
        label="Free Reawaken", short="RWK",
        over_note="the free Reawaken from Serpent's Ire sat unused",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlaps(a: float, b: float,
              windows: list[tuple[float, float]]) -> bool:
    return any(s < b and a < e for s, e in windows or [])


def _ire_drift_cause(ctx: AdviceContext) -> RootCause | None:
    """Serpent's Ire fit fewer times than the sim's line, with the drift
    ledger that shows where the use was lost. Gaps that overlap a death
    window are excluded from both the total and the worst slip — a death
    already owns that delay (priced by its own card)."""
    recast, _ch = vd.COOLDOWNS[_IRE]
    ideal_n = sum(1 for _t, a in ctx.idealized if a == _IRE)
    times = sorted(t for t, a in ctx.norm_casts if a == _IRE and t >= 0)
    deficit = ideal_n - len(times)
    if deficit < 1 or len(times) < 2:
        return None
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
    if drift_total < _IRE_DRIFT_FLOOR_S:
        return None
    name = _name(_IRE)
    value = vd.COOLDOWN_VALUE_P.get(_IRE, 0)
    t = TEXT["ire_drift"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_IRE, ability_name=name,
        time_sec=round(worst[1], 1), measured_p=0.0,
        summary=t["summary"].format(
            name=name, drift=drift_total, deficit=deficit,
            plural="s" if deficit != 1 else ""),
        prescription=t["prescription"].format(
            name=name, when=_mmss(worst[1]), worst=worst[0], value=value),
        evidence=[
            EvidenceRow(
                k=name,
                v=t["count_v"].format(player=len(times), ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Idle",
                v=t["idle_v"].format(drift=drift_total),
                note=t["idle_note"]),
        ],
        resources=[GAUGE_TEXT["ready_to_reawaken"]])


def _offering_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the Serpent Offering gauge over the delivered stream:
    overflow marks a Reawaken fired later than the gauge allowed. Serpent's
    Ire's Ready to Reawaken makes the next Reawaken free (it spends no
    offering) — mirrored here exactly as the sim plays it. A death zeroes the
    real gauge and drops the Ready flag, so the ledger resets at each death
    window's start; without that, a rebuilt gauge reads as phantom overcap
    (the death card already owns the loss)."""
    offering = 0.0
    ready = False
    deaths = sorted(s for s, _e in ctx.death_windows or [])
    di = 0
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        while di < len(deaths) and deaths[di] <= t:
            offering, ready = 0.0, False
            di += 1
        if a == _IRE:
            ready = True
        elif a == _REAWAKEN:
            if ready:
                ready = False
            else:
                offering = max(0.0, offering - vd.OFFERING_SPENDERS[_REAWAKEN])
        gain = vd.OFFERING_GENERATORS.get(a, 0)
        if gain:
            offering += gain
            if offering > vd.OFFERING_CAP:
                overflows.append((t, offering - vd.OFFERING_CAP))
                offering = float(vd.OFFERING_CAP)
    total = sum(v for _t, v in overflows)
    if total < _OFFERING_OVERCAP_MIN or not overflows:
        return None
    first = overflows[0][0]
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["offering_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=_REAWAKEN,
        ability_name=_name(_REAWAKEN),
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
                    value=total * vd.OFFERING_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["offering"]])


def _coil_walk(casts: list[tuple[float, int]], death_starts: list[float]
               ) -> tuple[list[tuple[float, float]], float, float]:
    """One walk of the Rattling Coil gauge over a cast stream:
    (overflow events, end-of-fight balance, last generator time). A death
    zeroes the real gauge, so the ledger resets at each death window's start
    (including one after the last cast); without that, coils banked before a
    death read as phantom overcap or phantom stranding later."""
    coils = 0.0
    last_gen_t = 0.0
    di = 0
    overflows: list[tuple[float, float]] = []
    for t, a in sorted(casts):
        if t < 0:
            continue
        while di < len(death_starts) and death_starts[di] <= t:
            coils = 0.0
            di += 1
        spend = vd.RATTLING_SPENDERS.get(a, 0)
        if spend:
            coils = max(0.0, coils - spend)
        gain = vd.RATTLING_GENERATORS.get(a, 0)
        if gain:
            coils += gain
            last_gen_t = t
            if coils > vd.RATTLING_CAP:
                overflows.append((t, coils - vd.RATTLING_CAP))
                coils = float(vd.RATTLING_CAP)
    if di < len(death_starts):
        coils = 0.0        # died after the last cast: the bank died too
    return overflows, coils, last_gen_t


def _coil_ledger(ctx: AdviceContext
                 ) -> tuple[list[tuple[float, float]], float, float]:
    """The delivered-stream coil walk (death resets from the pull's windows)."""
    return _coil_walk(ctx.norm_casts,
                      sorted(s for s, _e in ctx.death_windows or []))


def _coil_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """A Vicewinder or Serpent's Ire coil that landed on a full gauge — an
    Uncoiled Fury held until its coil vanished."""
    overflows, _end, _last = _coil_ledger(ctx)
    total = sum(v for _t, v in overflows)
    if total < _COIL_OVERCAP_MIN or not overflows:
        return None
    first = overflows[0][0]
    t = TEXT["coil_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=_UNCOILED,
        ability_name=_name(_UNCOILED),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(
            total=total, plural="s" if total != 1 else ""),
        prescription=t["prescription"].format(
            when=_mmss(first), cap=vd.RATTLING_CAP),
        evidence=[
            EvidenceRow(
                k="Coils",
                v=t["coils_v"].format(total=total),
                note=t["coils_note"].format(
                    value=vd.RATTLING_VALUE_P_PER_UNIT)),
            EvidenceRow(
                k="First",
                v=t["first_v"].format(when=_mmss(first)),
                note=t["first_note"].format(cap=vd.RATTLING_CAP)),
        ],
        resources=[GAUGE_TEXT["rattling"]])


def _coils_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Coils that died in the gauge: a spendable Uncoiled Fury (>= 1 coil)
    left unspent at fight end. Measured as the EXCESS over the sim's own
    end-of-fight balance — the ideal line itself banks a coil when a final
    Vicewinder's coil pair outvalues swapping to Uncoiled Fury, and that is
    correct play, not stranding. Also silent when the player's last coil
    arrived inside the final seconds (it was never spendable)."""
    _overflows, coils, last_gen_t = _coil_ledger(ctx)
    _io, ideal_end, _it = _coil_walk(ctx.idealized, [])
    stranded = coils - ideal_end
    if stranded < _STRANDED_COILS_MIN:
        return None
    if last_gen_t > float(ctx.fight_duration_s) - _STRANDED_TAIL_S:
        return None
    t = TEXT["coils_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_UNCOILED,
        ability_name=_name(_UNCOILED),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(
            total=stranded, plural="s" if stranded != 1 else ""),
        prescription=t["prescription"].format(
            value=stranded * vd.RATTLING_VALUE_P_PER_UNIT),
        evidence=[EvidenceRow(
            k="Coils",
            v=t["coils_v"].format(total=stranded),
            note=t["coils_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["rattling"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """VPR probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes, descending per-use
    value: a lost Serpent's Ire (~2000p), the delayed Reawaken behind an
    offering overcap, the delayed Uncoiled Fury behind a coil overcap, then
    coils stranded at the kill. No ProbeItems (no VPR window card to enrich)."""
    causes: list[RootCause] = []
    for producer in (_ire_drift_cause, _offering_overcap_cause,
                     _coil_overcap_cause, _coils_stranded_cause):
        c = producer(ctx)
        if c is not None:
            causes.append(c)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
