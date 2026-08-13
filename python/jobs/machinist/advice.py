"""Machinist deep-advice pack (`Job.advice_probes`).

The first per-job probe set — the registry pattern `sidecar/advice.py`'s
docstring promised. Two halves:

* `ProbeItem`s — in-place enrichment of existing cards: the wildfire /
  hypercharge window-shift probe, relocated here from the core (the shapes are
  MCH knowledge: a 10s Wildfire catches 6 weaponskills, an ~8s Overheat fires
  5 Blazing Shots).
* `RootCause`s — candidates for the cascade re-attribution, all deterministic
  ledger walks over the delivered cast stream: tool drift that cost an
  end-of-fight use, heat overcap that marks a delayed Hypercharge, and Queen
  battery stranded at the kill. Their `measured_p` stays 0 — the orchestrator
  prices each from its cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (combo_step,
queen_battery_spent, wf_cast_t…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.machinist import data as md

# Enabler-window shapes for the placement probe: kind -> (window duration s,
# weaponskill cap). `_WINDOW_WORDS`: the copy fragments per kind — the hit
# noun, the spelled-out cap, and the boundary-cast evidence key.
_WINDOW_SHAPES: dict[str, tuple[float, int]] = {
    "wildfire": (10.0, 6),
    "hypercharge": (8.0, 5),
}
_WINDOW_WORDS: dict[str, tuple[str, str, str]] = {
    "wildfire": ("weaponskills", "six", "Sixth WS"),
    "hypercharge": ("Blazing Shots", "five", "Fifth shot"),
}
_SHIFT_RANGE_S = 5.0
_SHIFT_STEP_S = 0.25

_DRILL, _AIR_ANCHOR, _CHAIN_SAW = 16498, 16500, 25788
_QUEEN, _HYPERCHARGE, _BARREL = 16501, 17209, 7414
_TOOLS = (_DRILL, _AIR_ANCHOR, _CHAIN_SAW)
_HEAT_OVERCAP_MIN = 25         # total overflowed heat before a card is worth it
_STRANDED_BATTERY_MIN = 50     # a summonable Queen died in the gauge


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "window_shift": {
        # Full catch after the shift vs a partial improvement.
        "rx_full": ("Cast {name} ~{shift:.1f}s {direction} so all {capword} "
                    "{noun} land inside the window."),
        "rx_part": ("Cast {name} ~{shift:.1f}s {direction} so {hits} of "
                    "{cap} {noun} land inside the window."),
        # Boundary-cast evidence row: KEY / "6:23 · missed by 0.3s" / note.
        "boundary_v": "{when} · missed by {late:.1f}s",
        "boundary_note": "a small shift recovers it",
    },
    "tool_drift": {
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
    "heat_overcap": {
        "summary": ("Hypercharge held past full heat, {total:.0f} heat "
                    "wasted"),
        "prescription": ("Use excess Heat right away here. First overcap at "
                         "{when}; each late Hypercharge slides the rest "
                         "until one stops fitting."),
        "worst_v": "{amount:.0f} heat",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} heat",
        "total_note": ("~{value:.0f}p of Overheat value across {count} "
                       "overcap{plural}"),
    },
    "queen_stranded": {
        "summary": ("Automaton Queen left with {battery:.0f} battery at the "
                    "kill"),
        "prescription": ("An extra Queen use fits by summoning at lower "
                         "battery late in the fight (~{value:.0f}p)."),
        "battery_v": "{battery:.0f} unspent",
        "battery_note": "last battery generator at {when} with no Queen after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "heat": GaugeText(
        label="Heat", short="HEAT",
        over_note="Hypercharge came later than the gauge allowed",
        under_note=None,     # running lean on heat is not a mistake by itself
        min_delta=20.0),
    "battery": GaugeText(
        label="Battery", short="BAT",
        over_note="a Queen was ready",
        under_note=None,
        min_delta=20.0),
    "free_hypercharges": GaugeText(
        label="Free HC", short="HC",
        over_note="a Barrel Stabilizer Hypercharge sat unused",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _probe_window_shift(card: dict, gcd_times: list[float]) -> ProbeItem | None:
    """Best small shift of an underfilled enabler window against the player's
    OWN GCD cadence. None when no shift within ±5s catches more casts (the
    static template already covers the generic advice)."""
    dur, cap = _WINDOW_SHAPES[card["kind"]]
    t0 = float(card.get("timeSec", 0.0) or 0.0)
    if t0 <= 0 or not gcd_times:
        return None

    def hits(delta: float) -> int:
        lo, hi = t0 + delta, t0 + delta + dur
        return min(cap, sum(1 for t in gcd_times if lo <= t <= hi))

    base = hits(0.0)
    if base >= cap:
        return None
    best: tuple[float, int] | None = None
    steps = int(round(_SHIFT_RANGE_S / _SHIFT_STEP_S))
    # Earlier (negative) first at each magnitude, so ties prefer "earlier".
    for k in range(1, steps + 1):
        for sign in (-1, 1):
            d = sign * k * _SHIFT_STEP_S
            h = hits(d)
            if h > base and (best is None or h > best[1]):
                best = (d, h)
        if best is not None and best[1] >= cap:
            break
    if best is None:
        return None
    d, h = best
    name = card.get("abilityName") or _name(int(card.get("abilityId", 0) or 0))
    noun, capword, boundary_k = _WINDOW_WORDS[str(card.get("kind"))]
    t = TEXT["window_shift"]
    direction = "earlier" if d < 0 else "later"
    if h >= cap:
        rx = t["rx_full"].format(name=name, shift=abs(d), direction=direction,
                                 capword=capword, noun=noun)
    else:
        rx = t["rx_part"].format(name=name, shift=abs(d), direction=direction,
                                 hits=h, cap=cap, noun=noun)
    evidence: list[EvidenceRow] = []
    # Name the boundary cast that just missed the unshifted window.
    after = [x for x in gcd_times if t0 + dur < x <= t0 + dur + 3.0]
    if after:
        evidence.append(EvidenceRow(
            k=boundary_k,
            v=t["boundary_v"].format(when=_mmss(after[0]),
                                     late=after[0] - (t0 + dur)),
            note=t["boundary_note"]))
    return ProbeItem(
        kind=str(card.get("kind")),
        ability_id=int(card.get("abilityId", 0) or 0),
        time_sec=float(card.get("timeSec", 0.0) or 0.0),
        prescription=rx,
        evidence=evidence)


def _tool_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A tool the sim fit more of than the player cast, with the drift ledger
    that shows where the use was lost. Drill's charge pool is shared with
    Bioblaster, so Bioblaster casts count as Drill consumptions (quirk #11 —
    otherwise AoE phases read as fake Drill drift)."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for tool in _TOOLS:
        recast, _ch = md.COOLDOWNS[tool]
        consume_ids = {tool, md.BIOBLASTER_ABILITY_ID} if tool == _DRILL \
            else {tool}
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume_ids and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(tool, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(tool)
        value = deficit * md.COOLDOWN_VALUE_P.get(tool, 0)
        t = TEXT["tool_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=tool, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=md.COOLDOWN_VALUE_P.get(tool, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal_counts.get(tool, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _heat_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the heat gauge over the delivered stream: overflow marks
    a Hypercharge fired later than the gauge allowed — the delay compounds
    into every later Overheat window. Barrel Stabilizer's free Hypercharge
    spends no heat (the Hypercharged buff), mirrored here."""
    heat = 0.0
    free_hc = False
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a == _BARREL and md.BARREL_STABILIZER_GRANTS_FREE_HYPERCHARGE:
            free_hc = True
        elif a == _HYPERCHARGE:
            if free_hc:
                free_hc = False
            else:
                heat = max(0.0, heat - md.HEAT_SPENDERS[_HYPERCHARGE])
        gain = md.HEAT_GENERATORS.get(a, 0)
        if gain:
            heat += gain
            if heat > md.HEAT_CAP:
                overflows.append((t, heat - md.HEAT_CAP))
                heat = float(md.HEAT_CAP)
    total = sum(v for _t, v in overflows)
    if total < _HEAT_OVERCAP_MIN or not overflows:
        return None
    first = next((t for t, v in overflows if v >= 5), overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["heat_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=_HYPERCHARGE,
        ability_name=_name(_HYPERCHARGE),
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
                    value=total * md.HEAT_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["heat"]])


def _queen_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Battery that died in the gauge: a summonable Queen (>= 50 battery) left
    unspent at fight end."""
    battery = 0.0
    last_gen_t = 0.0
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a == _QUEEN:
            battery = 0.0
        gain = md.BATTERY_GENERATORS.get(a, 0)
        if gain:
            battery = min(float(md.BATTERY_CAP), battery + gain)
            last_gen_t = t
    if battery < _STRANDED_BATTERY_MIN:
        return None
    t = TEXT["queen_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_QUEEN,
        ability_name=_name(_QUEEN),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(battery=battery),
        prescription=t["prescription"].format(
            battery=battery, value=battery * md.BATTERY_VALUE_P_PER_UNIT),
        evidence=[EvidenceRow(
            k="Battery",
            v=t["battery_v"].format(battery=battery),
            note=t["battery_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["battery"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """MCH probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost tool uses
    (highest per-use value first), then the heat-overcap Hypercharge delay,
    then the stranded Queen."""
    gcd_times = [t for t, a in sorted(ctx.norm_casts)
                 if t >= 0 and a in ctx.gcd_ids]
    items: list[ProbeItem] = []
    for card in cards:
        if card.get("kind") in _WINDOW_SHAPES:
            it = _probe_window_shift(card, gcd_times)
            if it is not None:
                items.append(it)
    causes: list[RootCause] = list(_tool_drift_causes(ctx))
    hc = _heat_overcap_cause(ctx)
    if hc is not None:
        causes.append(hc)
    queen = _queen_stranded_cause(ctx)
    if queen is not None:
        causes.append(queen)
    return items, causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
