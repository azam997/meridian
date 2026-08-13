"""Monk deep-advice pack (`Job.advice_probes`).

The MNK entry in the per-job probe registry `sidecar/advice.py`'s docstring
promises (the MCH pack is the reference implementation). One half only:

* `RootCause`s — candidates for the cascade re-attribution, all deterministic
  ledger walks over the delivered cast stream: burst-cooldown drift (Riddle of
  Fire / Brotherhood / Riddle of Wind held past ready until a use fell off the
  end), a Masterful Blitz resolved as the 2+1 Celestial Revolution (the mixed
  Beast Chakra set top logs never press), and a fully charged blitz stranded
  at the kill. Their `measured_p` stays 0 — the orchestrator prices each from
  its cascade segment's unexplained loss.

No `ProbeItem`s: MNK has no enabler-window card whose better placement a probe
can compute against the player's own cadence (the MCH Wildfire bar); the
static card templates stand.

What is deliberately NOT a cause here, because the model does not track it:

* Chakra overcap — chakra generation is crit-RNG + party-fed (invisible to
  the cast stream; `data.py` models it as a measured The Forbidden Chakra
  BUDGET, not a gauge), so an overcap ledger would only invent waste.
* Fury overcap — the three Fury stacks are real cast-derived gauges, but the
  shared OvercapAspect already prices and locates those cards; a cause here
  would double-tell the story.
* Perfect Balance drift — PB is 2-charge and the sim itself HOLDS it so the
  blitz lands inside Riddle of Fire, so gap-over-recast on the delivered
  stream flags perfect play; the ledger stays silent rather than lie.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (form, pb_goal, rof_end,
opo_fury…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.monk import data as md

# The single-charge burst cooldowns the sim fires the moment they light up
# (`pick_ogcd` casts each unconditionally when ready), so gap-over-recast on
# the delivered stream is genuine drift. Perfect Balance is excluded (see the
# module docstring).
_DRIFT_CDS: tuple[int, ...] = (
    md.RIDDLE_OF_FIRE, md.BROTHERHOOD, md.RIDDLE_OF_WIND,
)

# Beast-Chakra family bits for the blitz ledger (mirrors the simulator's
# `_BIT`/`_FORM_OF`, rebuilt from data so this module stays data-grounded).
_FAMILY_BIT: dict[int, int] = {
    **{a: 1 for a in md.OPO_GCD_IDS},
    **{a: 2 for a in md.RAPTOR_GCD_IDS},
    **{a: 4 for a in md.COEURL_GCD_IDS},
}

# Fallback GCD set for the "was there a slot left to press it" check, used
# only when `ctx.gcd_ids` is empty (synthetic contexts); production fills it
# from the pull's own casts. Derived from data, never hand-listed.
_GCD_IDS: frozenset[int] = frozenset(md.POTENCIES) - md.OGCD_IDS

# What a Celestial Revolution left on the table next to a clean 3-same or
# 3-distinct blitz (900 vs 600 — derived, never restated).
_CR_SHORTFALL_P: int = md.POTENCIES[md.ELIXIR_BURST] - \
    md.POTENCIES[md.CELESTIAL_REVOLUTION]


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# so holding for buffs elsewhere stays legitimate. Run new dialogue copy by
# the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cooldown_drift": {
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
    "botched_blitz": {
        "summary": ("Masterful Blitz resolved as Celestial Revolution "
                    "{n} time{plural}"),
        "prescription": ("Commit each Perfect Balance to one plan: three "
                         "Opo-opo GCDs for the Lunar Nadi, or one GCD of "
                         "each form for the Solar. The mixed set at {when} "
                         "came out {short}p under a clean blitz and pushed "
                         "Phantom Rush back."),
        "mix_v": "{n} mixed set{plural}",
        "mix_note": "two Beast Chakra of one form plus one other",
        "cost_v": "~{total:.0f}p",
        "cost_note": "{short}p per set next to Elixir Burst or Rising Phoenix",
    },
    "stranded_blitz": {
        "summary": "{name} left uncast at the kill, ~{value:.0f}p",
        "prescription": ("Press Masterful Blitz as soon as the third Beast "
                         "Chakra lands. The set was complete at {when} and "
                         "the blitz never fired (~{value:.0f}p)."),
        "beast_v": "3 banked at {when}",
        "beast_note": "no blitz followed before the kill",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Keys are exact public scalar fields of `simulator.SimState`. Deliberately
# muted: `opo_fury`/`raptor_fury` (cap 1 — a 1-stack delta at an arbitrary
# cut is cycle-phase noise, not a mistake) and the window clocks/form state.
GAUGE_TEXT: dict[str, GaugeText] = {
    "coeurl_fury": GaugeText(
        label="Coeurl's Fury", short="CRL",
        over_note="two boosted Pouncing Coeurls sat unused",
        under_note=None,     # spending early is not a mistake by itself
        min_delta=2.0),
    "beast_n": GaugeText(
        label="Beast Chakra", short="BC",
        over_note="a blitz set sat unused unresolved",
        under_note=None,
        min_delta=2.0),
    "tfc_left": GaugeText(
        label="Forbidden Chakra", short="TFC",
        over_note="the spends fell behind the fight's pace",
        under_note=None,     # spending ahead of pace lands inside buff windows
        min_delta=2.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _sorted_casts(ctx: AdviceContext) -> list[tuple[float, int]]:
    """Stable time-only sort (same-timestamp order is state-bearing for the
    PB machine, mirroring `replay._ordered_casts`)."""
    return sorted(((float(t), int(a)) for t, a in ctx.norm_casts),
                  key=lambda c: c[0])


def _overlap_s(a: float, b: float,
               windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] covered by `windows`."""
    total = 0.0
    for s, e in windows or ():
        lo, hi = max(a, float(s)), min(b, float(e))
        if hi > lo:
            total += hi - lo
    return total


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A burst cooldown the sim fit more of than the player cast, with the
    drift ledger that shows where the use was lost. Downtime and death
    windows are subtracted from each gap before it counts as drift (a hold
    across a forced disconnect or a death is owned by those cards, not this
    one)."""
    ideal_counts: dict[int, int] = {}
    for t, a in ctx.idealized:
        if t >= 0:
            ideal_counts[a] = ideal_counts.get(a, 0) + 1
    casts = _sorted_casts(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_CDS:
        recast, _ch = md.COOLDOWNS[aid]
        times = [t for t, a in casts if a == aid and t >= 0]
        player_n = len(times)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = ((b - a)
                    - _overlap_s(a, b, ctx.downtime_windows)
                    - _overlap_s(a, b, ctx.death_windows)
                    - recast)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = deficit * md.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cooldown_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=md.COOLDOWN_VALUE_P.get(aid, 0)),
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


def _botched_blitz_cause(ctx: AdviceContext) -> RootCause | None:
    """Any Masterful Blitz that resolved as Celestial Revolution (the 2+1
    Beast Chakra mix): 300p short of a clean blitz each time, and the mixed
    set scrambles the Nadi plan toward Phantom Rush. Top logs press it zero
    times, so a single cast is worth speaking about."""
    crs = [t for t, a in _sorted_casts(ctx)
           if a == md.CELESTIAL_REVOLUTION and t >= 0]
    if not crs:
        return None
    n = len(crs)
    t = TEXT["botched_blitz"]
    return RootCause(
        kind="cascade_lost_use", ability_id=md.CELESTIAL_REVOLUTION,
        ability_name=_name(md.CELESTIAL_REVOLUTION),
        time_sec=round(crs[0], 1), measured_p=0.0,
        summary=t["summary"].format(n=n, plural="s" if n != 1 else ""),
        prescription=t["prescription"].format(
            when=_mmss(crs[0]), short=_CR_SHORTFALL_P),
        evidence=[
            EvidenceRow(
                k="Blitz",
                v=t["mix_v"].format(n=n, plural="s" if n != 1 else ""),
                note=t["mix_note"]),
            EvidenceRow(
                k="Cost",
                v=t["cost_v"].format(total=float(n * _CR_SHORTFALL_P)),
                note=t["cost_note"].format(short=_CR_SHORTFALL_P)),
        ],
        resources=[GAUGE_TEXT["beast_n"]])


def _stranded_blitz_cause(ctx: AdviceContext) -> RootCause | None:
    """A fully charged Masterful Blitz dead at the kill: three Beast Chakra
    banked and the blitz button never pressed before the fight ended. The
    ledger walks the PB machine as `data.py` models it (PB grants 3 form-free
    GCDs for 20s, each banking its action's form; the blitz resolves and
    clears the set); partial sets (1 or 2 banked) stay silent — the sim's own
    endgame lines can strand those too.

    Three ways it stays quiet on clean play:
    * a death anywhere after the set completed (the death card owns that
      stretch);
    * a death BEFORE the set completed drops the open Perfect Balance stacks,
      since those are a status and die with the player — without this, the
      normal form GCDs a rezzed Monk resumes with bank against a press that
      no longer exists and the ledger invents a set nobody held;
    * no GCD left after the third Beast Chakra landed, which is the fight
      ending on the set rather than the player sitting on it."""
    gcds = ctx.gcd_ids or _GCD_IDS
    death_starts = sorted(float(s) for s, _e in (ctx.death_windows or ()))
    di = 0
    pb_left = 0
    pb_until = float("-inf")
    beast_n = 0
    types = 0
    lunar = solar = False
    last_bank_t: float | None = None
    for t, a in _sorted_casts(ctx):
        if t < 0:
            continue
        while di < len(death_starts) and death_starts[di] <= t:
            pb_left = 0
            pb_until = float("-inf")
            di += 1
        if a == md.PERFECT_BALANCE:
            pb_left = md.PB_STACKS
            pb_until = t + md.PB_STACK_DURATION_S
        elif a in md.BLITZ_IDS:
            if a == md.PHANTOM_RUSH:
                lunar = solar = False
            elif a == md.ELIXIR_BURST:
                lunar = True
            elif a == md.RISING_PHOENIX:
                solar = True
            elif a == md.CELESTIAL_REVOLUTION:
                if lunar:
                    solar = True
                else:
                    lunar = True
            pb_left = 0
            beast_n = 0
            types = 0
        elif a in md.FORM_GCD_IDS and pb_left > 0 and t <= pb_until:
            pb_left -= 1
            beast_n += 1
            types |= _FAMILY_BIT[a]
            last_bank_t = t
    if beast_n < md.PB_STACKS or last_bank_t is None:
        return None
    if _overlap_s(last_bank_t, ctx.fight_duration_s,
                  ctx.death_windows) > 0:
        return None
    if not any(t > last_bank_t and a in gcds for t, a in _sorted_casts(ctx)):
        return None
    # The blitz the completed set would have resolved (the sim's rule).
    if lunar and solar:
        resolved = md.PHANTOM_RUSH
    else:
        distinct = bin(types).count("1")
        resolved = (md.ELIXIR_BURST if distinct == 1
                    else md.RISING_PHOENIX if distinct == 3
                    else md.CELESTIAL_REVOLUTION)
    value = float(md.POTENCIES[resolved])
    name = _name(resolved)
    t = TEXT["stranded_blitz"]
    return RootCause(
        kind="cascade_lost_use", ability_id=resolved, ability_name=name,
        time_sec=round(last_bank_t, 1), measured_p=0.0,
        summary=t["summary"].format(name=name, value=value),
        prescription=t["prescription"].format(
            when=_mmss(last_bank_t), value=value),
        evidence=[EvidenceRow(
            k="Beast Chakra",
            v=t["beast_v"].format(when=_mmss(last_bank_t)),
            note=t["beast_note"])],
        resources=[GAUGE_TEXT["beast_n"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """MNK probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost burst
    cooldown uses (highest total value first), then the Celestial Revolution
    mix, then the blitz stranded at the kill."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    cr = _botched_blitz_cause(ctx)
    if cr is not None:
        causes.append(cr)
    stranded = _stranded_blitz_cause(ctx)
    if stranded is not None:
        causes.append(stranded)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
