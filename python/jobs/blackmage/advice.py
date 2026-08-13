"""Black Mage deep-advice pack (`Job.advice_probes`).

The BLM probe set, following the registry pattern `sidecar/advice.py`'s
docstring promised (Machinist is the reference implementation). One half only:

* `RootCause`s — candidates for the cascade re-attribution, all deterministic
  ledger walks over the delivered cast stream: cooldown drift that cost an
  end-of-fight use (Manafont / Ley Lines / Amplifier — the three genuinely
  recast-gated DPS buttons in `data.COOLDOWNS`), Polyglot overcap that marks a
  delayed Xenoglossy (the 30s Enochian accrual is deterministic, so the gauge
  ledger is exact up to anchor fuzz — a short grace window forgives it), and
  Polyglot stacks stranded at the kill (measured as the EXCESS over the ideal
  timeline's own ending gauge, so a fight that ends mid-accrual never blames
  the player). Their `measured_p` stays 0 — the orchestrator prices each from
  its cascade segment's unexplained loss.
* No `ProbeItem`s — BLM has no fixed-shape enabler window like MCH's Wildfire
  (Ley Lines is haste, not a cast-count box), so there is no card enrichment
  that would measure a concrete better placement. Causes only.

Dropped hypotheses (checked against data.py / simulator.py, 2026-08-13):
Triplecast is NOT modeled (movement/MP utility, excluded from COOLDOWNS and
never cast by the sim), and Umbral Hearts are tracked but economically
non-binding in the flat-MP abstraction — neither can be priced honestly, so
neither gets a cause or a gauge entry.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (mp, umbral_hearts, firestarter,
thunderhead_until, ley_end…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.blackmage import data as bd

# The recast-gated DPS cooldowns the drift ledger watches (data.COOLDOWNS is
# the authority; this tuple just fixes iteration order — output is re-sorted
# by value anyway).
_CDS: tuple[int, ...] = (bd.MANAFONT, bd.LEY_LINES, bd.AMPLIFIER)

# A Polyglot tick that lands on a full gauge is forgiven when a spend follows
# within this window (the ledger's 30s anchor sits at t=0; a player whose real
# Enochian clock is a hair offset spends "just after" our modeled tick).
_POLY_TICK_GRACE_S = 3.0
# Wasted stacks before the overcap card is worth speaking about. One stack is
# a whole Xenoglossy, so a single confirmed waste already clears the bar.
_POLY_WASTED_MIN = 1
# A stack that lands with less than this left in the fight is not blamable as
# stranded (no GCD slot left to fire it).
_STRANDED_GRACE_S = 5.0


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
        "idle_note": "about {recasts:.1f} full recasts of idle time",
    },
    "poly_overcap": {
        "summary": ("Xenoglossy held at full Polyglot, {n} stack{plural} "
                    "wasted"),
        "prescription": ("Spend a Polyglot stack right away here. First "
                         "wasted stack at {when}."),
        "wasted_v": "{n} stack{plural}",
        "wasted_note": "~{value:.0f}p of Xenoglossy value",
        "gauge_v": "3 / 3",
        "gauge_note": "the gauge was already full when a new stack landed",
    },
    "poly_stranded": {
        "summary": "Polyglot left with {n} stack{plural} at the kill",
        "prescription": ("Fire the spare Xenoglossy before the fight ends "
                         "(~{value:.0f}p)."),
        "stranded_v": "{n} unspent",
        "stranded_note": ("last stack landed at {when} with no Xenoglossy "
                          "after"),
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of simulator.SimState.
GAUGE_TEXT: dict[str, GaugeText] = {
    "polyglot": GaugeText(
        label="Polyglot", short="POLY",
        over_note="a Xenoglossy was ready",
        under_note=None,     # spending stacks early is not a mistake by itself
        min_delta=1.0),
    "astral_soul": GaugeText(
        label="Astral Soul", short="SOUL",
        over_note="the Flare Star build ran behind the ideal line",
        under_note=None,     # being ahead on the spend is not a mistake
        min_delta=4.0),      # cap 6; small deltas are phase-offset noise
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlaps_death(a: float, b: float,
                    death_windows: list[tuple[float, float]]) -> bool:
    return any(a < e and b > s for s, e in death_windows)


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A recast-gated cooldown the sim fit more of than the player cast, with
    the drift ledger that shows where the use was lost. No charge sharing and
    no CDR rules in BLM's data, so the walk is the plain gap-over-recast one;
    gaps that overlap a death window are excluded (deaths are priced by their
    own card)."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for cd in _CDS:
        recast, _ch = bd.COOLDOWNS[cd]
        times = sorted(t for t, a in ctx.norm_casts if a == cd and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(cd, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast
            if over <= 0:
                continue
            if _overlaps_death(a, b, ctx.death_windows):
                continue
            drift_total += over
            if over > worst[0]:
                worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(cd)
        value = deficit * bd.COOLDOWN_VALUE_P.get(cd, 0)
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=cd, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=bd.COOLDOWN_VALUE_P.get(cd, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal_counts.get(cd, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _poly_ledger(casts: list[tuple[float, int]], dur: float,
                 death_windows: list[tuple[float, float]],
                 ) -> tuple[list[float], int, float]:
    """Walk the Polyglot gauge over one cast stream. Gains: the deterministic
    30s Enochian ticks (anchored at t=0, matching the sim's schedule; ticks
    inside a death window are skipped — Enochian is down) and Amplifier casts.
    Spends: Xenoglossy / Foul. A gain landing on a full gauge is an overflow
    candidate; a spend within the grace window right after forgives it (anchor
    fuzz, not waste). Returns (counted overflow times, final stacks, last
    successful grant time)."""
    events: list[tuple[float, int, str]] = []
    tick = bd.POLYGLOT_INTERVAL_S
    while tick <= dur:
        if not _overlaps_death(tick, tick, death_windows):
            events.append((tick, 1, "gain"))
        tick += bd.POLYGLOT_INTERVAL_S
    spends: list[float] = []
    for t, a in casts:
        if t < 0:
            continue
        if a == bd.AMPLIFIER:
            events.append((t, 2, "gain"))
        elif a in (bd.XENOGLOSSY, bd.FOUL):
            events.append((t, 0, "spend"))
            spends.append(t)
    # Same-instant order: spend before gain (generous to the player).
    events.sort(key=lambda e: (e[0], e[1]))
    spends.sort()
    poly = 0
    overflow: list[float] = []
    last_grant = 0.0
    for t, _o, kind in events:
        if kind == "spend":
            poly = max(0, poly - 1)
        elif poly >= bd.POLYGLOT_CAP:
            overflow.append(t)
        else:
            poly += 1
            last_grant = t
    counted = [t for t in overflow
               if not any(t < s <= t + _POLY_TICK_GRACE_S for s in spends)]
    return counted, poly, last_grant


def _polyglot_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the Polyglot gauge over the delivered stream: a stack
    landing on a full gauge marks a Xenoglossy held later than the gauge
    allowed. BLM's accrual is deterministic (1 per 30s of Enochian + Amplifier),
    so the ledger is exact up to the anchor-fuzz grace."""
    wasted, _final, _lg = _poly_ledger(
        sorted(ctx.norm_casts, key=lambda c: c[0]),
        float(ctx.fight_duration_s), ctx.death_windows)
    n = len(wasted)
    if n < _POLY_WASTED_MIN:
        return None
    first = wasted[0]
    value = n * bd.POTENCIES[bd.XENOGLOSSY]
    plural = "s" if n != 1 else ""
    t = TEXT["poly_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=bd.XENOGLOSSY,
        ability_name=_name(bd.XENOGLOSSY),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(n=n, plural=plural),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Wasted",
                v=t["wasted_v"].format(n=n, plural=plural),
                note=t["wasted_note"].format(value=value)),
            EvidenceRow(
                k="Gauge",
                v=t["gauge_v"],
                note=t["gauge_note"]),
        ],
        resources=[GAUGE_TEXT["polyglot"]])


def _polyglot_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Polyglot that died in the gauge: stacks left unspent at fight end, in
    EXCESS of what the ideal timeline itself strands (a fight that ends
    mid-accrual leaves the sim a stack too — that boundary stack is never the
    player's fault). Located at the last successful grant."""
    dur = float(ctx.fight_duration_s)
    _ov, final_p, last_grant = _poly_ledger(
        sorted(ctx.norm_casts, key=lambda c: c[0]), dur, ctx.death_windows)
    _ov_i, final_i, _lg_i = _poly_ledger(
        sorted(ctx.idealized, key=lambda c: c[0]), dur, ctx.death_windows)
    excess = final_p - final_i
    if excess < 1:
        return None
    if last_grant > dur - _STRANDED_GRACE_S:
        return None          # the surviving stack landed too late to blame
    value = excess * bd.POTENCIES[bd.XENOGLOSSY]
    plural = "s" if excess != 1 else ""
    t = TEXT["poly_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=bd.XENOGLOSSY,
        ability_name=_name(bd.XENOGLOSSY),
        time_sec=round(last_grant, 1), measured_p=0.0,
        summary=t["summary"].format(n=excess, plural=plural),
        prescription=t["prescription"].format(value=value),
        evidence=[EvidenceRow(
            k="Polyglot",
            v=t["stranded_v"].format(n=excess),
            note=t["stranded_note"].format(when=_mmss(last_grant)))],
        resources=[GAUGE_TEXT["polyglot"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """BLM probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest total value first), then the Polyglot-overcap Xenoglossy delay,
    then the stranded Polyglot. No ProbeItems (see module docstring)."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    oc = _polyglot_overcap_cause(ctx)
    if oc is not None:
        causes.append(oc)
    stranded = _polyglot_stranded_cause(ctx)
    if stranded is not None:
        causes.append(stranded)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
