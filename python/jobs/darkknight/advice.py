"""Dark Knight deep-advice pack (`Job.advice_probes`).

The DRK probe set, following the Machinist registry pattern
(jobs/machinist/advice.py) and the Gunbreaker tank shape. One half only —
`RootCause`s, candidates for the cascade re-attribution, all deterministic
ledger walks over the delivered cast stream:

* **Cooldown drift that cost an end-of-fight use** — Living Shadow / Delirium /
  Salted Earth / Carve and Spit, the single-charge recast-gated actions
  (`data.COOLDOWNS`). Shadowbringer (2 charges) is deliberately NOT watched:
  consecutive-gap drift misreads charge banking. Edge of Shadow is MP-gated
  with only a local 1s recast, so it is not a drift subject either — its story
  is the MP ledger below. Carve and Spit shares its 60s recast with Abyssal
  Drain (`JobData.charge_sharing`), so Abyssal Drain casts count as Carve
  consumptions — otherwise an AoE phase reads as fake Carve drift.
* **Blood overcap marking a delayed Bloodspiller** — the Blood Gauge ledger,
  mirroring `simulator.apply_cast`: the combo finishers grant +20 (combo-gated,
  as in game), Delirium's Blood Weapon grants +10 per weaponskill for 3 stacks
  inside its 15s window, Bloodspiller / Quietus spend 50, everything above 100
  is gone. Only overflow in EXCESS of the ideal line's own ledger counts.
* **MP wasted at the cap marking a held Edge of Shadow** — the MP ledger. MP is
  TICK-FED state in this sim (200 per 3s from the pull, bar starting full), not
  a `GaugeModel`, so the walk mirrors `simulator._settle` exactly: lazy tick
  accrual against a watermark, plus the combo / Carve / Blood Weapon / chain
  grants, minus 3000 per Edge or Flood. The Blackest Night stays un-modeled on
  both sides (MP-net-neutral when popped — see the data.py header), which is
  why the floor here is two full Edge casts rather than one.
* **Blood stranded at the kill** — a spendable 50 Blood dead in the gauge at
  fight end, guarded against kill-timing slack (the sim's own end state and a
  last-generator spend-window check both have to agree it was avoidable).

Deaths never feed any of them: the drift ledger skips a slip stretch a death
(or a downtime window) forced, and the economy walk wipes gauge, stacks and
combo at every death the way the game does, so a rezzed player is never blamed
for a gauge the raise had already emptied. The death card owns those stretches.

DRK ships no `ProbeItem`s: it has no enabler-window placement shape like MCH's
Wildfire / Hypercharge probe (Delirium and Living Shadow timing is already
priced by the sim diff plus the alignment cards, and the Darkside amp has its
own located card from `improvement_contributors` — the MP cause below speaks
about lost Edge CASTS, never about the amp, so the two never tell the same
story). `measured_p` stays 0 on every cause — the orchestrator prices each
from its cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (the combo steps, the chain step
counter, the window end-times…) never surface in evidence lines.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.darkknight import data as dd

# Single-charge recast-gated actions the drift ledger watches, highest per-use
# value first (see the module docstring for the exclusions). Values from
# data.COOLDOWN_VALUE_P.
_DRIFT_WATCHED: tuple[int, ...] = (
    dd.LIVING_SHADOW, dd.DELIRIUM, dd.SALTED_EARTH, dd.CARVE_AND_SPIT,
)
# Actions that consume a watched recast besides the action itself
# (JobData.charge_sharing: an Abyssal Drain IS the Carve and Spit cooldown).
_SHARED_CONSUMERS: dict[int, frozenset[int]] = {
    dd.CARVE_AND_SPIT: frozenset({dd.CARVE_AND_SPIT, dd.ABYSSAL_DRAIN}),
}

# Weaponskills for the Blood Weapon ledger. Unmend is excluded on purpose: it
# is a spell, so it never consumes a Blood Weapon stack in game, and the sim
# never casts it (the ledger cannot diverge from the ideal line over it).
_WEAPONSKILLS: frozenset[int] = frozenset({
    dd.HARD_SLASH, dd.SYPHON_STRIKE, dd.SOULEATER, dd.BLOODSPILLER,
    dd.QUIETUS, dd.SCARLET_DELIRIUM, dd.COMEUPPANCE, dd.TORCLEAVER,
    dd.IMPALEMENT, dd.UNLEASH, dd.STALWART_SOUL, dd.DISESTEEM,
})
_CHAIN_IDS: frozenset[int] = frozenset({
    dd.SCARLET_DELIRIUM, dd.COMEUPPANCE, dd.TORCLEAVER, dd.IMPALEMENT,
})

_BLOOD_OVERCAP_MIN = 25       # half a Bloodspiller (~300p) before it is worth a card
_STRANDED_BLOOD_MIN = 50      # a full Bloodspiller died in the gauge
# Two full Edge casts. One would be a fair floor for a clean ledger, but a TBN
# that expires unpopped costs 3000 MP the model deliberately does not track
# (data.py header), so the floor absorbs one of those before speaking.
_MP_WASTE_MIN = 2 * dd.EDGE_MP_COST
# One GCD slot: a resource earned later than this before the kill had no
# weaponskill left to spend it (mirrors simulator.DRK_GCD_S without importing
# the sim module at register time).
_SPEND_WINDOW_S = 2.5
# Potency per wasted MP, derived from the tables (no new constants).
_MP_VALUE_P_PER_UNIT = dd.POTENCIES[dd.EDGE_OF_SHADOW] / float(dd.EDGE_MP_COST)


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and spend advice scoped to the measured stretch
# ("right away here") so banking Blood for a burst window elsewhere stays
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
    "blood_overcap": {
        "summary": ("Bloodspiller held past a full Blood Gauge, {total} "
                    "Blood wasted"),
        "prescription": ("Use excess Blood right away here. First overcap at "
                         "{when}."),
        "worst_v": "{amount} Blood",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total} Blood",
        "total_note": ("~{value:.0f}p of Bloodspiller value across {count} "
                       "overcap{plural}"),
    },
    "mp_waste": {
        "summary": ("Edge of Shadow held while MP sat at the cap, {total} "
                    "MP wasted"),
        "prescription": ("Spend Edge of Shadow as the bar fills here. First "
                         "waste at {when}."),
        "worst_v": "{amount} MP",
        "worst_note": "lost at {when}, the most consequential overcap",
        "total_v": "{total} MP",
        "total_note": "~{value:.0f}p, about {edges:.1f} Edge casts of income",
    },
    "blood_stranded": {
        "summary": "{blood} Blood left in the gauge at the kill",
        "prescription": ("Spend the gauge down as the kill approaches; "
                         "Bloodspiller converts it into ~{value:.0f}p."),
        "blood_v": "{blood} unspent",
        "blood_note": "last Blood earned at {when} with no spender after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Keys are exact SimState field names
# (jobs/darkknight/simulator.py). Rows read `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "blood": GaugeText(
        label="Blood", short="BLD",
        over_note="a Bloodspiller was ready",
        under_note=None,     # running lean on Blood is not a mistake by itself
        min_delta=50.0),     # one full spender
    "mp": GaugeText(
        label="MP", short="MP",
        over_note="an Edge of Shadow was ready",
        under_note=None,     # a dumped bar is the point of the button
        min_delta=float(dd.EDGE_MP_COST)),
    "delirium_stacks": GaugeText(
        label="Delirium", short="DEL",
        over_note="Delirium stacks were left unused",
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


def _clamp_t(ctx: AdviceContext, t: float) -> float:
    """A located time inside the fight, rounded to the emission grid."""
    return round(min(max(0.0, float(t)), float(ctx.fight_duration_s)), 1)


def _forced_stretch(ctx: AdviceContext, lo: float, hi: float) -> bool:
    """True when [lo, hi] overlaps a death window (the death card prices those)
    or a downtime window (a boss you cannot hit is not your drift)."""
    windows = list(ctx.death_windows or []) + list(ctx.downtime_windows or [])
    return any(s < hi and lo < e for s, e in windows)


# --- The Blood + MP ledger (shared by three causes) --------------------------

@dataclass(frozen=True)
class _Ledger:
    """One pass of the dual economy over a cast stream."""
    blood: int
    blood_overflows: tuple[tuple[float, int], ...]      # (t, wasted Blood)
    blood_last_gen_t: float | None
    mp: int
    mp_waste: tuple[tuple[float, int], ...]             # (t, wasted MP)


def _economy_walk(casts: list[tuple[float, int]],
                  deaths: list[tuple[float, float]] | None = None) -> _Ledger:
    """Walk the Blood Gauge and the MP bar over a cast stream, mirroring
    `simulator.apply_cast` / `simulator._settle`:

      * MP starts FULL and accrues 200 per 3s against a watermark (the sim's
        lazy `_settle`, ticking through downtime too); Edge / Flood spend 3000;
        the combo'd Syphon or Stalwart Soul grants 600, Carve 600, each
        Delirium-chain GCD 200, and each Blood Weapon stack 600.
      * Blood: +20 on a COMBO'd Souleater or Stalwart Soul (the in-game combo
        bonus — the sim always combos, so the ideal line is unchanged), +10 per
        weaponskill while Blood Weapon lives, minus 50 per Bloodspiller or
        Quietus, capped at 100.

    `deaths` (the DELIVERED side only — the ideal line never dies) wipes the
    gauge and the Delirium/Blood Weapon stacks the way the game does, drops the
    mid-step combo, and skips the tick income of the dead stretch (there is no
    combat regen while KO'd). Without it a player who banked Blood into a death
    reads as overcapping for the rest of the pull on a gauge the raise had
    already emptied. A trailing death still wipes the end-of-fight gauge (a
    death after the last cast). The raise's own MP is left alone: how full the
    bar comes back is not in the log, and reading it high only ever costs a
    detection.

    Prepull (t < 0) casts generate nothing and draw no tick income, but their
    SPENDS land, so both counts stay lower bounds and every detected waste is
    real. The Blackest Night is deliberately absent from the ledger
    (MP-net-neutral when popped, exactly as the sim and the delivered scorer
    treat it)."""
    blood = 0
    mp = dd.MP_MAX
    anchor = 0.0                 # MP tick watermark (preserves tick phase)
    bw_stacks = 0
    delirium_end = 0.0
    basic = 0                    # 0 boundary, 1 expect Syphon, 2 expect Souleater
    aoe = 0                      # 0 boundary, 1 expect Stalwart Soul
    last_gcd_t: float | None = None
    blood_overflows: list[tuple[float, int]] = []
    mp_waste: list[tuple[float, int]] = []
    blood_last_gen_t: float | None = None
    windows = sorted((float(s), float(e)) for s, e in (deaths or []))
    di = 0

    def gain_blood(t: float, amount: int) -> None:
        nonlocal blood, blood_last_gen_t
        blood_last_gen_t = t
        over = blood + amount - dd.BLOOD_CAP
        if over > 0:
            blood_overflows.append((t, over))
            blood = dd.BLOOD_CAP
        else:
            blood += amount

    def gain_mp(t: float, amount: int) -> None:
        nonlocal mp
        over = mp + amount - dd.MP_MAX
        if over > 0:
            mp_waste.append((t, over))
            mp = dd.MP_MAX
        else:
            mp += amount

    def die(resume: float) -> None:
        """The wipe a KO applies: gauge, stacks and combo gone, and the tick
        watermark walked forward past the dead stretch (whole ticks only, so
        the tick phase survives)."""
        nonlocal blood, bw_stacks, delirium_end, basic, aoe, anchor
        nonlocal last_gcd_t, blood_last_gen_t
        blood = 0
        bw_stacks = 0
        delirium_end = 0.0
        basic = aoe = 0
        last_gcd_t = None
        blood_last_gen_t = None
        if resume > anchor:
            anchor += int((resume - anchor) / dd.MP_TICK_S) * dd.MP_TICK_S

    # Stable time-only sort: same-timestamp cast order is state-bearing.
    for t, a in sorted(casts, key=lambda c: c[0]):
        if t < 0:
            # Prepull: no tick income and nothing to generate, but a spend that
            # already happened has to land. Skipping it would leave the bar
            # 3000 MP high for the rest of the pull, and the ledger would read
            # the difference as cap waste the player never had.
            if a in (dd.EDGE_OF_SHADOW, dd.FLOOD_OF_SHADOW):
                mp = max(0, mp - dd.EDGE_MP_COST)
            elif a in (dd.BLOODSPILLER, dd.QUIETUS):
                blood = max(0, blood - 50)
            continue
        while di < len(windows) and windows[di][0] <= t:
            die(windows[di][1])
            di += 1
        ticks = int((t - anchor) / dd.MP_TICK_S)
        if ticks > 0:
            anchor += ticks * dd.MP_TICK_S
            gain_mp(t, ticks * dd.MP_PER_TICK)
        if t >= delirium_end:
            bw_stacks = 0                       # the 15s buff took them
        is_ws = a in _WEAPONSKILLS
        if is_ws and last_gcd_t is not None \
                and t - last_gcd_t > dd.COMBO_TIMEOUT_S:
            basic = aoe = 0                     # the in-game combo timer
        if is_ws and bw_stacks > 0:
            bw_stacks -= 1
            gain_blood(t, dd.BLOOD_WEAPON_BLOOD)
            gain_mp(t, dd.BLOOD_WEAPON_MP)
        # Blood: combo'd finishers generate, the spenders consume.
        if a == dd.SOULEATER and basic == 2:
            gain_blood(t, 20)
        elif a == dd.STALWART_SOUL and aoe == 1:
            gain_blood(t, 20)
        elif a in (dd.BLOODSPILLER, dd.QUIETUS):
            blood = max(0, blood - 50)
        # MP grants and costs.
        if a == dd.SYPHON_STRIKE and basic == 1:
            gain_mp(t, dd.COMBO_MP_GRANT)
        elif a == dd.STALWART_SOUL and aoe == 1:
            gain_mp(t, dd.COMBO_MP_GRANT)
        elif a == dd.CARVE_AND_SPIT:
            gain_mp(t, dd.CARVE_MP_GRANT)
        elif a in (dd.EDGE_OF_SHADOW, dd.FLOOD_OF_SHADOW):
            mp = max(0, mp - dd.EDGE_MP_COST)
        if a in _CHAIN_IDS:
            gain_mp(t, dd.CHAIN_RESTORE_MP)
        if a == dd.DELIRIUM:
            bw_stacks = dd.BLOOD_WEAPON_STACKS
            delirium_end = t + dd.DELIRIUM_DURATION_S
        # Combo transitions (the Delirium chain does not touch them — probed).
        if a == dd.HARD_SLASH:
            basic, aoe = 1, 0
        elif a == dd.SYPHON_STRIKE:
            basic = 2 if basic == 1 else 0
        elif a == dd.SOULEATER:
            basic = 0
        elif a == dd.UNLEASH:
            aoe, basic = 1, 0
        elif a == dd.STALWART_SOUL:
            aoe = 0
        if is_ws:
            last_gcd_t = t
    # A death after the last cast still wipes the gauge before the kill.
    while di < len(windows):
        die(windows[di][1])
        di += 1
    return _Ledger(blood=blood, blood_overflows=tuple(blood_overflows),
                   blood_last_gen_t=blood_last_gen_t, mp=mp,
                   mp_waste=tuple(mp_waste))


# --- RootCause producers ----------------------------------------------------

def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A watched cooldown the sim fit more of than the player cast, with the
    drift ledger that shows where the use was lost. Counts include prepull
    casts (dropping one would misstate the deficit); the drift ledger itself
    walks in-fight times only and skips any stretch a death or a downtime
    window forced."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_WATCHED:
        recast, _ch = dd.COOLDOWNS[aid]
        consumers = _SHARED_CONSUMERS.get(aid, frozenset({aid}))
        player_n = sum(1 for _t, a in ctx.norm_casts if a in consumers)
        ideal_n = sum(ideal_counts.get(c, 0) for c in sorted(consumers))
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consumers and t >= 0)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a_t, b_t in zip(times, times[1:]):
            over = (b_t - a_t) - recast
            if over <= 0:
                continue
            if _forced_stretch(ctx, a_t + recast, b_t):
                continue                         # not the player's drift
            drift_total += over
            if over > worst[0]:
                worst = (over, a_t)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = deficit * dd.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=_clamp_t(ctx, worst[1]), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural=_plural(deficit)),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=dd.COOLDOWN_VALUE_P.get(aid, 0)),
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


def _blood_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Blood Gauge overflow marks a Bloodspiller fired later than the gauge
    allowed: a combo finisher into a full gauge builds nothing, and under Blood
    Weapon the gauge refills every weaponskill. Only overflow IN EXCESS of the
    ideal line's own ledger counts — waste the sim itself wears (a Blood Weapon
    window that ends on a full gauge) is not part of the player's gap."""
    player = _economy_walk(ctx.norm_casts, ctx.death_windows)
    ideal = _economy_walk(ctx.idealized)
    ideal_total = sum(v for _t, v in ideal.blood_overflows)
    total = sum(v for _t, v in player.blood_overflows) - ideal_total
    if total < _BLOOD_OVERCAP_MIN or not player.blood_overflows:
        return None
    first = next((t for t, v in player.blood_overflows if v >= 10),
                 player.blood_overflows[0][0])
    worst_t, worst_v = max(player.blood_overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["blood_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=dd.BLOODSPILLER,
        ability_name=_name(dd.BLOODSPILLER),
        time_sec=_clamp_t(ctx, first), measured_p=0.0,
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
                    value=total * dd.BLOOD_VALUE_P_PER_UNIT,
                    count=len(player.blood_overflows),
                    plural=_plural(len(player.blood_overflows)))),
        ],
        resources=[GAUGE_TEXT["blood"]])


def _mp_waste_cause(ctx: AdviceContext) -> RootCause | None:
    """MP income lost at the cap marks an Edge of Shadow held too long. The bar
    starts full and the passive tick never stops, so a bar left untouched burns
    200 MP every 3s. Only waste IN EXCESS of the ideal line's own ledger counts,
    and the floor is two full Edge casts (the un-modeled Blackest Night is worth
    one, see the module docstring). This card is about lost Edge CASTS; the
    Darkside amp has its own card."""
    player = _economy_walk(ctx.norm_casts, ctx.death_windows)
    ideal = _economy_walk(ctx.idealized)
    ideal_total = sum(v for _t, v in ideal.mp_waste)
    total = sum(v for _t, v in player.mp_waste) - ideal_total
    if total < _MP_WASTE_MIN or not player.mp_waste:
        return None
    first = next((t for t, v in player.mp_waste if v >= dd.MP_PER_TICK),
                 player.mp_waste[0][0])
    worst_t, worst_v = max(player.mp_waste, key=lambda o: (o[1], -o[0]))
    t = TEXT["mp_waste"]
    return RootCause(
        kind="cascade_burst", ability_id=dd.EDGE_OF_SHADOW,
        ability_name=_name(dd.EDGE_OF_SHADOW),
        time_sec=_clamp_t(ctx, first), measured_p=0.0,
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
                    value=total * _MP_VALUE_P_PER_UNIT,
                    edges=total / float(dd.EDGE_MP_COST))),
        ],
        resources=[GAUGE_TEXT["mp"]])


def _blood_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Blood that died in the gauge at the kill. Only the excess over the sim's
    own end state counts (a kill mid-build strands Blood on the ideal line
    too), and the last Blood earned must have landed with at least a GCD left
    to spend it — silence beats blaming kill timing."""
    player = _economy_walk(ctx.norm_casts, ctx.death_windows)
    ideal = _economy_walk(ctx.idealized)
    stranded = player.blood - max(0, ideal.blood)
    last_gen_t = player.blood_last_gen_t
    if stranded < _STRANDED_BLOOD_MIN or last_gen_t is None:
        return None
    if last_gen_t > ctx.fight_duration_s - _SPEND_WINDOW_S:
        return None
    t = TEXT["blood_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=dd.BLOODSPILLER,
        ability_name=_name(dd.BLOODSPILLER),
        time_sec=_clamp_t(ctx, last_gen_t), measured_p=0.0,
        summary=t["summary"].format(blood=stranded),
        prescription=t["prescription"].format(
            value=stranded * dd.BLOOD_VALUE_P_PER_UNIT),
        evidence=[EvidenceRow(
            k="Blood",
            v=t["blood_v"].format(blood=stranded),
            note=t["blood_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["blood"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """DRK probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest per-use value first), then the Blood-overcap delayed Bloodspiller,
    then the MP wasted at the cap, then the Blood stranded at the kill. No
    ProbeItems (see the module docstring)."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    blood_over = _blood_overcap_cause(ctx)
    if blood_over is not None:
        causes.append(blood_over)
    mp_over = _mp_waste_cause(ctx)
    if mp_over is not None:
        causes.append(mp_over)
    stranded = _blood_stranded_cause(ctx)
    if stranded is not None:
        causes.append(stranded)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
