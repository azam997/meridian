"""Gunbreaker deep-advice pack (`Job.advice_probes`).

The GNB probe set, following the Machinist registry pattern
(jobs/machinist/advice.py). One half only — `RootCause`s, candidates for the
cascade re-attribution, all deterministic ledger walks over the delivered cast
stream:

* **Cooldown drift that cost an end-of-fight use** — No Mercy / Bloodfest /
  Blasting Zone / Bow Shock, the single-charge on-cooldown actions. Gnashing
  Fang (2 charges — consecutive-gap drift misreads charge banking) and Double
  Down (the sim's own No Mercy lattice discipline holds it up to 25s, so
  gap-over-recast is often correct play) are deliberately NOT watched.
* **Cartridge overcap marking a delayed spender** — the Powder Gauge ledger
  (generators / spenders from data.py), mirroring the simulator's dynamic cap:
  Bloodfest raises 3 -> 6 for 30s and bonus cartridges above 3 expire when the
  window ends (the lazy clamp in `apply_cast`). Only overflow in excess of
  the ideal timeline's own ledger counts, so waste the sim itself wears (an
  unavoidable window-edge expiry) never reads as a player mistake.
* **Cartridges stranded at the kill** — spendable cartridges dead in the gauge
  at fight end, guarded against kill-timing slack (the sim's own end state and
  a last-generator spend-window check both have to agree it was avoidable).

GNB ships no `ProbeItem`s: it has no enabler-window shape like MCH's
Wildfire/Hypercharge placement probe (No Mercy timing is already priced by the
sim diff + alignment cards). `measured_p` stays 0 on every cause — the
orchestrator prices each from its cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (combo steps, the continuation
proc flags, the window end-times…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.gunbreaker import data as gd

# Single-charge on-cooldown actions the drift ledger watches, highest per-use
# value first (see module docstring for why Gnashing Fang and Double Down are
# excluded). Values from data.COOLDOWN_VALUE_P.
_DRIFT_WATCHED: tuple[int, ...] = (
    gd.BLOODFEST, gd.NO_MERCY, gd.BLASTING_ZONE, gd.BOW_SHOCK,
)
_CART_OVERCAP_MIN = 1     # one full cartridge is a whole Burst Strike (~420p)
_STRANDED_CART_MIN = 1    # a spendable cartridge died in the gauge
# One GCD slot: a cartridge earned later than this before the kill had no
# weaponskill left to spend it (mirrors simulator.GNB_GCD_S without importing
# the sim module at register time).
_SPEND_WINDOW_S = 2.5


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and spend advice scoped to the measured stretch
# ("right away here") so banking cartridges for No Mercy elsewhere stays
# legitimate. Sentence shapes follow the approved MCH register.
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
    "cart_overcap": {
        "summary": ("Burst Strike held past a full Powder Gauge, {total} "
                    "cartridge{plural} wasted"),
        "prescription": ("Use excess cartridges right away here. First "
                         "overcap at {when}."),
        "worst_v": "{amount} cartridge{plural}",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total} cartridge{plural}",
        "total_note": ("~{value:.0f}p of spender value across {count} "
                       "overcap{plural}"),
    },
    "cart_stranded": {
        "summary": ("{count} cartridge{plural} left in the Powder Gauge at "
                    "the kill"),
        "prescription": ("Spend the gauge down as the kill approaches; "
                         "Burst Strike converts it into ~{value:.0f}p."),
        "cart_v": "{count} unspent",
        "cart_note": "last cartridge earned at {when} with no spender after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Keys are exact SimState field names
# (jobs/gunbreaker/simulator.py). Rows read `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "cartridges": GaugeText(
        label="Cartridges", short="CART",
        over_note="a cartridge spender was ready",
        under_note=None,     # running lean on cartridges is not a mistake
        min_delta=1.0),
    "ready_to_break": GaugeText(
        label="Ready to Break", short="BRK",
        over_note="Sonic Break sat uncast while its window ran down",
        under_note=None,
        min_delta=1.0),
    "ready_to_reign": GaugeText(
        label="Ready to Reign", short="RGN",
        over_note="the Reign of Beasts combo sat unused after Bloodfest",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _in_death_window(ctx: AdviceContext, lo: float, hi: float) -> bool:
    """True when [lo, hi] overlaps any death window — the death card already
    prices those stretches, so the ledgers stay silent there."""
    return any(s < hi and lo < e for s, e in (ctx.death_windows or []))


# --- Cartridge ledger (shared by the overcap + stranded causes) -------------

def _cartridge_walk(casts: list[tuple[float, int]]
                    ) -> tuple[int, list[tuple[float, int]], float | None]:
    """One pass of the Powder Gauge over a cast stream, mirroring
    `simulator.apply_cast` exactly: Bloodfest raises the cap 3 -> 6 for 30s
    BEFORE its +3 lands, bonus cartridges above 3 expire lazily when the
    window ends, generation past the live cap overflows. Prepull (t < 0)
    casts are ignored, so the count is a lower bound and every detected
    overflow is real. Returns (end_cartridges, overflows [(t, wasted)],
    last_generator_t)."""
    carts = 0
    cap_end = 0.0
    overflows: list[tuple[float, int]] = []
    last_gen_t: float | None = None
    # Stable time-only sort: same-timestamp cast order is state-bearing.
    for t, a in sorted(casts, key=lambda c: c[0]):
        if t < 0:
            continue
        # Lazy expiry clamp: bonus cartridges die when the Bloodfest cap
        # window ends (the player could not hold them either).
        if carts > gd.CARTRIDGE_CAP and t >= cap_end:
            overflows.append((t, carts - gd.CARTRIDGE_CAP))
            carts = gd.CARTRIDGE_CAP
        if a == gd.BLOODFEST:
            cap_end = t + gd.BLOODFEST_CAP_DURATION_S
        cap = (gd.CARTRIDGE_CAP_BLOODFEST if t < cap_end
               else gd.CARTRIDGE_CAP)
        spend = gd.CARTRIDGE_SPENDERS.get(a, 0)
        if spend:
            carts = max(0, carts - spend)
        gain = gd.CARTRIDGE_GENERATORS.get(a, 0)
        if gain:
            carts += gain
            last_gen_t = t
            if carts > cap:
                overflows.append((t, carts - cap))
                carts = cap
    return carts, overflows, last_gen_t


# --- RootCause producers ----------------------------------------------------

def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A watched cooldown the sim fit more of than the player cast, with the
    drift ledger that shows where the use was lost. Counts include prepull
    casts (the canonical opener presses Bloodfest during the countdown — the
    reconstruction reinstates it at t < 0, and dropping it from the count
    would misstate the deficit); the drift ledger itself walks in-fight
    times only. Slip stretches inside a death window attribute nothing."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_WATCHED:
        recast, _ch = gd.COOLDOWNS[aid]
        player_n = sum(1 for _t, a in ctx.norm_casts if a == aid)
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a_t, b_t in zip(times, times[1:]):
            over = (b_t - a_t) - recast
            if over <= 0:
                continue
            if _in_death_window(ctx, a_t + recast, b_t):
                continue                         # the death card owns it
            drift_total += over
            if over > worst[0]:
                worst = (over, a_t)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = deficit * gd.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural=_plural(deficit)),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=gd.COOLDOWN_VALUE_P.get(aid, 0)),
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


def _cartridge_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Powder Gauge overflow marks a cartridge spender fired later than the
    gauge allowed — a combo finisher into a full gauge builds nothing, and the
    delay compounds into the next No Mercy's spender budget. Bloodfest's
    3 -> 6 cap window (and its expiry) is mirrored from the simulator. Only
    overflow IN EXCESS of the ideal line's own ledger counts: the sim's
    timeline can wear an unavoidable expiry loss at a Bloodfest window edge,
    and waste the ideal also commits is not part of the player's gap."""
    _end, overflows, _gen = _cartridge_walk(ctx.norm_casts)
    _i_end, ideal_overflows, _i_gen = _cartridge_walk(ctx.idealized)
    ideal_total = sum(v for _t, v in ideal_overflows)
    total = sum(v for _t, v in overflows) - ideal_total
    if total < _CART_OVERCAP_MIN or not overflows:
        return None
    first = overflows[0][0]
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["cart_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=gd.BURST_STRIKE,
        ability_name=_name(gd.BURST_STRIKE),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(total=total, plural=_plural(total)),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(amount=worst_v,
                                      plural=_plural(worst_v)),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(total=total, plural=_plural(total)),
                note=t["total_note"].format(
                    value=total * gd.CARTRIDGE_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural=_plural(len(overflows)))),
        ],
        resources=[GAUGE_TEXT["cartridges"]])


def _cartridge_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Cartridges that died in the gauge at the kill. Only the excess over
    the sim's own end state counts (a kill mid-build strands a cartridge on
    the ideal line too), and the last one earned must have landed with at
    least a GCD left to spend it — silence beats blaming kill timing."""
    player_carts, _ovf, last_gen_t = _cartridge_walk(ctx.norm_casts)
    ideal_carts, _i_ovf, _i_gen = _cartridge_walk(ctx.idealized)
    stranded = player_carts - max(0, ideal_carts)
    if stranded < _STRANDED_CART_MIN or last_gen_t is None:
        return None
    if last_gen_t > ctx.fight_duration_s - _SPEND_WINDOW_S:
        return None
    value = stranded * gd.CARTRIDGE_VALUE_P_PER_UNIT
    t = TEXT["cart_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=gd.BURST_STRIKE,
        ability_name=_name(gd.BURST_STRIKE),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(count=stranded,
                                    plural=_plural(stranded)),
        prescription=t["prescription"].format(value=value),
        evidence=[EvidenceRow(
            k="Cartridges",
            v=t["cart_v"].format(count=stranded),
            note=t["cart_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["cartridges"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """GNB probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest per-use value first), then the cartridge-overcap delayed spender,
    then the cartridges stranded at the kill. No ProbeItems (see module
    docstring)."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    over = _cartridge_overcap_cause(ctx)
    if over is not None:
        causes.append(over)
    stranded = _cartridge_stranded_cause(ctx)
    if stranded is not None:
        causes.append(stranded)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
