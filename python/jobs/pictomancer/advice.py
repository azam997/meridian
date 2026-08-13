"""Pictomancer deep-advice pack (`Job.advice_probes`).

The PCT probe set, mirroring the Machinist reference pack: no `ProbeItem`s (PCT
has no bespoke enabler-window card kinds — the shared missed-cast / residual
probes already cover its cards), and four `RootCause` producers, all
deterministic ledger walks over the delivered cast stream:

* **Cooldown drift** over the four recast-gated pools in `data.COOLDOWNS` —
  the Living Muse charge pool (Pom/Winged/Clawed/Fanged fold onto POM via
  `CHARGE_SHARING`), Striking Muse, Starry Muse, and the shared portrait
  recast (Mog/Retribution fold onto MOG) — that cost an end-of-fight use.
  Downtime and death windows are subtracted from every gap, so the sim's own
  downtime-painting deferral (motifs held for the gap, muses firing after it)
  never reads as drift; the portrait pool is measured from its LOAD (the
  Winged / Fanged muse that granted it), never from the 30s recast the muse
  ladder outruns.
* **Palette overcap** marking a delayed Subtractive Palette — the gauge ledger
  from `data.PALETTE_GAUGE`, with Starry Muse's Subtractive Spectrum spending
  no palette (the free-spend rule, mirroring the simulator exactly).
* **Comet in Black stranded at the kill** — a Monochrome Tones conversion the
  player banked and never fired; the sim explicitly never strands it
  (`COMET_TAIL_S`). White-paint overcap itself is deliberately NOT a cause:
  data.py models paint banking/waste as optimal play, so the ledger stays
  silent there by design.
* **Hammer Time swings dropped** — Striking Muse windows (30s) that expired or
  were overwritten with guaranteed-crit swings unused. Windows a downtime gap
  or a death cut into are left alone (hammers need a target).

Their `measured_p` stays 0 — the orchestrator prices each from its cascade
segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (hue_step, creature_stage,
white_paint, portrait, rainbow_bright…) never surface in evidence lines —
white_paint in particular stays silent on purpose (banking paint is optimal).
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.pictomancer import data as pd

_POM = pd.POM_MUSE
_STRIKING = pd.STRIKING_MUSE
_STARRY = pd.STARRY_MUSE
_MOG = pd.MOG_OF_THE_AGES
_SUBTRACTIVE = pd.SUBTRACTIVE_PALETTE
_COMET = pd.COMET_IN_BLACK
_HOLY = pd.HOLY_IN_WHITE

# Pool -> consumer ids (the shared-charge fold, from data.CHARGE_SHARING: the
# creature muses spend the POM pool, Retribution shares the MOG recast).
_POOL_CONSUMERS: dict[int, frozenset[int]] = {
    pool: frozenset({pool} | {c for c, p in pd.CHARGE_SHARING.items()
                              if p == pool})
    for pool in pd.COOLDOWNS
}

# Pools whose real cadence is a LOAD, not the recast. A portrait exists only
# once a creature muse grants it (Winged -> Moogle, Fanged -> Madeen) and the
# slot empties on use, so the 30s shared recast is never the binding gate: in
# the sim's own line portraits land ~80s apart (the muse ladder reaches Winged
# or Fanged every other use). Measuring those gaps against 30s would read as
# 50s of "drift" per portrait on flawless play, so the ledger blames only the
# stretch between the grant and the press.
_POOL_GATES: dict[int, frozenset[int]] = {
    _MOG: frozenset({pd.WINGED_MUSE, pd.FANGED_MUSE}),
}

# White-paint generators, mirroring the simulator's apply_cast (+1 each);
# Holy spends 1, the Subtractive Monochrome conversion turns 1 white black.
_WHITE_GEN_IDS: frozenset[int] = frozenset({
    pd.WATER_IN_BLUE, pd.WATER_II_IN_BLUE,
    pd.THUNDER_IN_MAGENTA, pd.THUNDER_II_IN_MAGENTA,
    pd.RAINBOW_DRIP,
})

_PALETTE_OVERCAP_MIN = 25          # one full Water in Blue wasted
_COMET_TAIL_GUARD_S = 5.0          # a grant this close to the end can't fire
_HAMMER_VALUE_P = pd.COOLDOWN_VALUE_P[_STRIKING] / 3.0   # per swing, net of filler
_COMET_VALUE_P = pd.POTENCIES[_COMET]


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
TEXT: dict[str, dict] = {
    "cooldown_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Drifting {press} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s late; the drift adds up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of idle time",
        # Pool display names: (card title noun, "press ..." noun).
        "names": {
            _POM: ("Living Muse", "Living Muse"),
            _STRIKING: ("Striking Muse", "Striking Muse"),
            _STARRY: ("Starry Muse", "Starry Muse"),
            _MOG: ("Portraits", "your portrait"),
        },
    },
    "palette_overcap": {
        "summary": ("Subtractive Palette held past a full gauge, "
                    "{total:.0f} palette wasted"),
        "prescription": ("Use Subtractive Palette right away here. First "
                         "overcap at {when}."),
        "worst_v": "{amount:.0f} palette",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} palette",
        "total_note": ("~{value:.0f}p of Subtractive value across {count} "
                       "overcap{plural}"),
    },
    "comet_stranded": {
        "summary": "Comet in Black left at the kill, ~{value:.0f}p unspent",
        "prescription": ("Cast the banked Comet before the fight ends "
                         "(~{value:.0f}p). While it sits it also blocks the "
                         "next Monochrome Tones conversion."),
        "comet_v": "1 banked",
        "comet_note": "granted at {when} with no Comet in Black after",
    },
    "hammer_dropped": {
        "summary": "Hammer Time ended with {lost} swing{plural} unused",
        "prescription": ("Fit all three hammer swings in before Hammer Time "
                         "runs out. Worst stretch starts at {when}; each "
                         "swing lands a guaranteed critical direct hit worth "
                         "~{per:.0f}p over the filler it replaces."),
        "worst_v": "{used} of 3 swings",
        "worst_note": "in the worst of the armed windows",
        "total_v": "{lost} swing{plural}",
        "total_note": ("~{value:.0f}p of guaranteed crit damage across "
                       "{count} window{plural}"),
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# white_paint has NO entry on purpose: banking or wasting paint is optimal
# play (see data.py's scoring-basis note), so neither direction is a mistake.
GAUGE_TEXT: dict[str, GaugeText] = {
    "palette": GaugeText(
        label="Palette", short="PAL",
        over_note="Subtractive Palette came later than the gauge allowed",
        under_note=None,     # running the gauge lean is not a mistake
        min_delta=25.0),
    "subtractive": GaugeText(
        label="Subtractive", short="CMY",
        over_note="the boosted CMY spells waited while cheaper casts went out",
        under_note=None,
        min_delta=1.0),
    "black_paint": GaugeText(
        label="Black Paint", short="BLK",
        over_note="a full power Comet sat unused",
        under_note=None,
        min_delta=1.0),
    "hammer_stacks": GaugeText(
        label="Hammers", short="HMR",
        over_note="hammer swings from Striking Muse went unused",
        under_note=None,
        min_delta=1.0),
    "spectrum_free": GaugeText(
        label="Free Subtractive", short="SPEC",
        over_note="the free Subtractive Palette from Starry Muse sat unused",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _blocked_overlap(a: float, b: float,
                     windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] covered by any window (downtime / deaths). Windows may
    overlap each other; double-counting only forgives more, never blames."""
    tot = 0.0
    for s, e in windows:
        lo, hi = max(a, float(s)), min(b, float(e))
        if hi > lo:
            tot += hi - lo
    return tot


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A recast-gated pool the sim fit more uses of than the player cast, with
    the drift ledger that shows where the use was lost. Consumers fold onto
    their pool (creature muses -> POM, Retribution -> MOG, per
    data.CHARGE_SHARING). Gap-over-recast is the exact wasted-regen measure for
    the charge pools (they start full, so banking never reads as drift), and
    `_POOL_GATES` holds the portrait pool to its load instead of its recast.
    Downtime and death windows are subtracted from every gap — a muse that
    fired late because its motif was (correctly) deferred into a downtime
    window, or a Starry held across a gap, must not read as drift; deaths are
    priced by their own card."""
    ideal_counts: dict[int, int] = {}
    for t, a in ctx.idealized:
        if t >= 0:
            ideal_counts[a] = ideal_counts.get(a, 0) + 1
    blocked = (list(ctx.downtime_windows or [])
               + list(ctx.death_windows or []))
    out: list[tuple[float, RootCause]] = []
    for pool in sorted(_POOL_CONSUMERS):
        recast, _ch = pd.COOLDOWNS[pool]
        consumers = _POOL_CONSUMERS[pool]
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consumers and t >= 0)
        player_n = len(times)
        ideal_n = sum(ideal_counts.get(a, 0) for a in consumers)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        gate_ids = _POOL_GATES.get(pool)
        grants = sorted(t for t, a in ctx.norm_casts
                        if gate_ids and a in gate_ids and t >= 0)
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, when it was ready)
        for a, b in zip(times, times[1:]):
            ready = a + recast
            if gate_ids is not None:
                # The load that made THIS use pressable: the slot empties on
                # use, so it always sits inside the gap. No load in the gap
                # means nothing was held, so nothing is blamed.
                load = next((g for g in grants if a < g <= b), None)
                if load is None:
                    continue
                ready = max(ready, load)
            over = (b - ready) - _blocked_overlap(ready, b, blocked)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, ready if gate_ids is not None else a)
        if drift_total < recast * 0.5:
            continue
        title, press = TEXT["cooldown_drift"]["names"][pool]
        value_per = pd.COOLDOWN_VALUE_P.get(pool, 0)
        t = TEXT["cooldown_drift"]
        out.append((float(deficit * value_per), RootCause(
            kind="cascade_lost_use", ability_id=pool, ability_name=title,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=title, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                press=press, when=_mmss(worst[1]), worst=worst[0],
                value=value_per),
            evidence=[
                EvidenceRow(
                    k=title,
                    v=t["count_v"].format(player=player_n, ideal=ideal_n),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _palette_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the palette gauge over the delivered stream: overflow
    marks a Subtractive Palette used later than the gauge allowed. Starry
    Muse's Subtractive Spectrum makes the next Subtractive free (no palette
    spent), mirrored here exactly as the simulator plays it."""
    gens = pd.PALETTE_GAUGE.generators
    spends = pd.PALETTE_GAUGE.spenders
    cap = float(pd.PALETTE_GAUGE.cap)
    palette = 0.0
    spectrum_free = False
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a == _STARRY:
            spectrum_free = True
        elif a in spends:
            if spectrum_free:
                spectrum_free = False
            else:
                palette = max(0.0, palette - spends[a])
        gain = gens.get(a, 0)
        if gain:
            palette += gain
            if palette > cap:
                overflows.append((t, palette - cap))
                palette = cap
    total = sum(v for _t, v in overflows)
    if total < _PALETTE_OVERCAP_MIN or not overflows:
        return None
    first = overflows[0][0]
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["palette_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=_SUBTRACTIVE,
        ability_name=_name(_SUBTRACTIVE),
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
                    value=total * pd.PALETTE_GAUGE.value_p_per_unit,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["palette"]])


def _comet_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """A Comet in Black that died in the gauge: the Monochrome Tones
    conversion the player banked and never fired. The white-paint ledger
    mirrors apply_cast (Water/Thunder/Drip +1, Holy -1, the Subtractive
    conversion 1 white -> black when a white is held and the black slot is
    free); starting the count at zero only under-counts, so a false stranding
    can never be claimed. A grant inside the last few seconds stays silent
    (the Comet genuinely could not fit)."""
    white = 0
    black = False
    grant_t: float | None = None
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a in _WHITE_GEN_IDS:
            white = min(pd.WHITE_PAINT_CAP, white + 1)
        elif a == _HOLY:
            white = max(0, white - 1)
        elif a == _COMET:
            black = False
            grant_t = None
        elif a == _SUBTRACTIVE and white >= 1 and not black:
            white -= 1
            black = True
            grant_t = t
    if not black or grant_t is None:
        return None
    if ctx.fight_duration_s - grant_t < _COMET_TAIL_GUARD_S:
        return None
    # Monochrome Tones rides a status (data.MONOCHROME_TONES_STATUS_ID), so a
    # death after the grant takes the banked Comet with it. That loss belongs
    # to the death card.
    if any(float(e) >= grant_t for _s, e in (ctx.death_windows or [])):
        return None
    t = TEXT["comet_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_COMET,
        ability_name=_name(_COMET),
        time_sec=round(grant_t, 1), measured_p=0.0,
        summary=t["summary"].format(value=_COMET_VALUE_P),
        prescription=t["prescription"].format(value=_COMET_VALUE_P),
        evidence=[EvidenceRow(
            k="Comet",
            v=t["comet_v"],
            note=t["comet_note"].format(when=_mmss(grant_t)))],
        resources=[GAUGE_TEXT["black_paint"]])


def _hammer_dropped_cause(ctx: AdviceContext) -> RootCause | None:
    """Hammer Time swings the player armed and never threw: each Striking Muse
    grants 3 stacks for 30s; stacks still pending when the window lapses (or
    when the next Striking overwrites them) are lost guaranteed-crit hits.
    A window still open when the fight ends stays silent (the tail may simply
    not fit three GCDs), and a window a death or a downtime gap interrupted is
    left alone — hammers need a target, so a gap inside the 30s eats swings on
    flawless play too (the sim's own line drops one that way), and deaths are
    priced by their own card."""
    hammer_ids = frozenset(pd.HAMMER_IDS)
    blocked = (list(ctx.downtime_windows or [])
               + list(ctx.death_windows or []))
    dur = float(ctx.fight_duration_s)
    windows: list[tuple[float, int]] = []        # (striking_t, lost stacks)
    pending = 0
    win_t = 0.0
    win_end = float("-inf")

    def settle() -> None:
        nonlocal pending
        if pending <= 0:
            return
        w_lo, w_hi = win_t, min(win_end, dur)
        if _blocked_overlap(w_lo, w_hi, blocked) <= 0.0:
            windows.append((win_t, pending))
        pending = 0

    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a == _STRIKING:
            settle()                             # expiry or overwrite alike
            pending = 3
            win_t = t
            win_end = t + pd.HAMMER_TIME_DURATION_S
        elif a in hammer_ids and pending > 0 and t <= win_end:
            pending -= 1
    if win_end < dur:
        settle()                                 # lapsed in-fight
    if not windows:
        return None
    lost = sum(n for _t, n in windows)
    worst_t, worst_n = max(windows, key=lambda w: (w[1], -w[0]))
    t = TEXT["hammer_dropped"]
    return RootCause(
        kind="cascade_lost_use", ability_id=_STRIKING,
        ability_name=_name(_STRIKING),
        time_sec=round(worst_t, 1), measured_p=0.0,
        summary=t["summary"].format(
            lost=lost, plural="s" if lost != 1 else ""),
        prescription=t["prescription"].format(
            when=_mmss(worst_t), per=_HAMMER_VALUE_P),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(used=3 - worst_n),
                note=t["worst_note"]),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(
                    lost=lost, plural="s" if lost != 1 else ""),
                note=t["total_note"].format(
                    value=lost * _HAMMER_VALUE_P,
                    count=len(windows),
                    plural="s" if len(windows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["hammer_stacks"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """PCT probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost pool uses
    (highest total value first), then the palette-overcap Subtractive delay,
    then the stranded Comet, then the dropped hammer swings. No ProbeItems —
    the shared missed-cast / residual probes cover PCT's cards."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    pal = _palette_overcap_cause(ctx)
    if pal is not None:
        causes.append(pal)
    comet = _comet_stranded_cause(ctx)
    if comet is not None:
        causes.append(comet)
    hammers = _hammer_dropped_cause(ctx)
    if hammers is not None:
        causes.append(hammers)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
