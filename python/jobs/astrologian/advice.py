"""Astrologian deep-advice pack (`Job.advice_probes`).

The healer register first: AST's ceiling already PAYS for healing and raising
(jobs/_core/heal_locks.py locks the player's own costed heal GCDs into the
idealized line, and the resurrection pardon buys each uptime raise its GCD
slots plus a recovery window). Nothing here prices a heal, a raise, or the
damage lost while casting one — every stretch a cast bar, a raise, a death or
downtime explains is subtracted from the ledger *before* it is spoken about,
and the copy says plainly that the damage oGCDs weave around the healing plan
rather than competing with it.

Causes only, no `ProbeItem`s: AST's two located cards (missed Earthly Star,
missed Divination) already carry everything a placement probe could measure —
there is no enabler window to shift, because Divination is a party buff modeled
job-agnostically in `raid_buffs.py`. The three `RootCause` producers are
deterministic ledger walks over the delivered cast stream:

* **Oracle left unfired after Divination** — the state-flag economy
  (`SimState.divining_ready`) the ceiling spends every window. Oracle is not
  cooldown-gated, so no missed-cast card can see it.
* **Divination / Earthly Star cooldown drift** — the MCH tool-drift archetype.
  Lord of Crowns is absent by design: its `COOLDOWNS` entry is a statistical
  ~120s card cadence, not a real recast (`JobData.drift_exclusions` /
  `rng_proc_ids`), so reading it as drift would blame draw luck.
* **Combust III lapsing between refreshes** — uncarded anywhere: the DoT's
  table potency is 0 (the ticks are scored in `scoring._combust_dot_potency`),
  so neither the missed-cast diff nor `split_residual` can see a dropped DoT.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below. `GAUGE_TEXT` is an
allowlist over AST's two sim-state gauge fields (`SimState.combust_end`,
`SimState.divining_ready`); only the Divining flag earns a line, because the
Combust timer is a rolling 30s phase offset whose player-vs-ideal delta is
noise at least as often as it is a mistake.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.astrologian import data as ad

_COMBUST = ad.COMBUST_III
_DIVINATION = ad.DIVINATION
_ORACLE = ad.ORACLE
_EARTHLY_STAR = ad.EARTHLY_STAR

# The genuinely recast-gated damage oGCDs (see the module docstring for why
# Lord of Crowns is not one of them).
_DRIFT_OGCDS: tuple[int, ...] = (_DIVINATION, _EARTHLY_STAR)

# A Divining granted closer than this to the kill had no window left to spend
# it in, so it is never read as an unfired Oracle.
_ORACLE_GRACE_S = 12.0
# Ledger floors — a clean pull must stay silent.
_COMBUST_LAPSE_MIN_S = 9.0     # three Combust III ticks
_LAPSE_NOISE_S = 0.5           # a sub-tick refresh slip is not a lapse
# Mirrors jobs/_core/heal_locks._REZ_RECOVERY_WINDOW_S: the costed heals that
# follow a raise are part of the raise's own cost, so a GCD ledger skips them.
_REZ_RECOVERY_S = 15.0

# Every cast that spent a damage GCD on healing. The honest budget's costed set
# is the currency the heal-lock accounting runs on, but a defensive GCD outside
# it (Macrocosmos) takes the slot just the same, so the GCD ledger must pardon
# it too or a lapse spent healing reads as a dropped DoT. GCD-ness comes from
# the shared ability-metadata table, never a hand-written list; the BUNDLED
# lookup keeps the inline deep pass off the network (every AST defensive id
# ships bundled).
_HEAL_GCD_IDS: frozenset[int] = ad.COSTED_HEAL_GCD_IDS | frozenset(
    aid for aid in ad.DEFENSIVE_IDS
    if (m := ability_metadata.BUNDLED.get(aid)) is not None and not m.is_ogcd)


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, hold/spend advice scoped to the measured stretch.
# Healer rule: healing and raising are paid for by the ceiling, so no line here
# may read as blaming the player for either.
TEXT: dict[str, dict[str, str]] = {
    "oracle_unfired": {
        "summary": ("{count} Oracle{plural} left unfired after Divination, "
                    "~{value:.0f}p"),
        "prescription": ("Weave Oracle in the seconds right after Divination "
                         "here. It is an oGCD, so it never takes a healing "
                         "GCD. Latest one lost at {when}."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "unspent_v": "{count} unspent",
        "unspent_note": "Divining granted at {when} with no Oracle after it",
    },
    "ogcd_drift": {
        "summary": ("{name} sat {drift:.0f}s past its recast, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Press {name} as it comes back, in any weave slot "
                         "the healing plan leaves open. Biggest drift at "
                         "{when}, {worst:.1f}s late; the holds add up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "held_v": "{drift:.0f}s",
        "held_note": "about {recasts:.1f} full recasts of extra hold",
    },
    "combust_lapse": {
        "summary": ("Combust III off the target {total:.0f}s between "
                    "refreshes, about {ticks:.0f} ticks lost"),
        "prescription": ("Refresh Combust as it drops, on the first GCD the "
                         "healing leaves free. Longest gap at {when}, "
                         "{worst:.0f}s with no DoT running."),
        "total_v": "{total:.0f}s down",
        "total_note": "about {value:.0f}p of ticks across {count} lapse{plural}",
        "worst_v": "{worst:.0f}s",
        "worst_note": "the longest single one, opening at {when}",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: AST sim-state
# fields without an entry — `combust_end` — never render). Rows read
# `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "divining_ready": GaugeText(
        label="Oracle", short="ORCL",
        over_note="a Divination Oracle was still unfired",
        under_note=None,      # spending it earlier than the sim is not a miss
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


# --- Forced-stretch bookkeeping (the healer guardrail) ----------------------

def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Non-overlapping ascending windows, so an overlap sum never double
    counts a stretch two sources both claim."""
    out: list[tuple[float, float]] = []
    for s, e in sorted((float(a), float(b)) for a, b in windows if b > a):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap(a: float, b: float, windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b) covered by (already merged) `windows`."""
    if b <= a:
        return 0.0
    return sum(max(0.0, min(b, e) - max(a, s)) for s, e in windows)


def _rez_bars(ctx: AdviceContext) -> list[tuple[float, float]]:
    """Cast bars of the uptime resurrections the ceiling already pays for
    (`heal_lock_rez_casts` rows are `[t, ability_id, locked_slots]`). No oGCD
    weaves during a raise, so this time is never the player's to spend."""
    out: list[tuple[float, float]] = []
    for row in (ctx.scoring_state or {}).get("heal_lock_rez_casts") or []:
        try:
            t, slots = float(row[0]), int(row[2])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        out.append((t, t + max(1, slots) * ad.AST_GCD_S))
    return out


def _forced_ogcd(ctx: AdviceContext) -> list[tuple[float, float]]:
    """Stretches where an oGCD weave was not available: downtime, deaths (their
    own card prices those) and raise cast bars."""
    return _merge(
        [(float(s), float(e)) for s, e in (ctx.downtime_windows or ())]
        + [(float(s), float(e)) for s, e in (ctx.death_windows or ())]
        + _rez_bars(ctx))


def _forced_gcd(ctx: AdviceContext) -> list[tuple[float, float]]:
    """Stretches where a damage GCD was not the player's to spend: everything
    an oGCD is blocked by, plus each heal GCD the player actually cast
    (`_HEAL_GCD_IDS`) and the recovery window after a raise."""
    heals = [(float(t), float(t) + ad.AST_GCD_S)
             for t, a in ctx.norm_casts
             if a in _HEAL_GCD_IDS and t is not None and t >= 0]
    recovery = [(s, e + _REZ_RECOVERY_S) for s, e in _rez_bars(ctx)]
    return _merge([(float(s), float(e)) for s, e in (ctx.downtime_windows or ())]
                  + [(float(s), float(e)) for s, e in (ctx.death_windows or ())]
                  + recovery + heals)


def _counts(timeline: list[tuple[float, int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for t, a in timeline:
        if t >= 0:
            out[a] = out.get(a, 0) + 1
    return out


# --- Root causes ------------------------------------------------------------

def _oracle_unfired_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Divination grants one Divining stack; Oracle spends it. A second
    Divination cast on top of a live stack destroys it, and a stack still live
    at the kill dies with the pull. Both are a whole Oracle (860p) the ceiling
    fires and the player did not, and no card can see them (Oracle is
    flag-gated, not cooldown-gated, so it sits outside `data.cooldowns`).

    Located at the LAST moment a stack was destroyed, not at the Divination
    that granted it: Oracle has no recast, so the stack is recoverable right up
    until the overwrite (or the kill) takes it. That is both the honest moment
    of loss and the moment the cascade measures it."""
    forced = _forced_ogcd(ctx)
    dur = float(ctx.fight_duration_s)

    def spendable(granted: float, deadline: float) -> bool:
        # Too little window to fire it in, or a window the player never
        # controlled (a death, a raise cast bar, downtime).
        return (deadline - granted >= _ORACLE_GRACE_S
                and _overlap(granted, min(deadline, granted + _ORACLE_GRACE_S),
                             forced) <= 0.0)

    live_t: float | None = None
    unfired: list[tuple[float, float]] = []      # (granted, destroyed)
    for t, a in sorted(ctx.norm_casts, key=lambda c: c[0]):
        if t < 0:
            continue
        if a == _DIVINATION:
            if live_t is not None and spendable(live_t, float(t)):
                unfired.append((live_t, float(t)))
            live_t = float(t)
        elif a == _ORACLE:
            live_t = None
    if live_t is not None and spendable(live_t, dur):
        unfired.append((live_t, live_t))

    player_n = _counts(ctx.norm_casts).get(_ORACLE, 0)
    ideal_n = _counts(ctx.idealized).get(_ORACLE, 0)
    if not unfired or ideal_n <= player_n:
        return None
    # Never claim more Oracles than the sim's own line fits: on a short or
    # downtime-heavy pull the ceiling spends fewer stacks than the player was
    # granted, and a stack it could not spend either is no lost use. Keep the
    # LATEST destructions (the ones still recoverable closest to the kill), so
    # the card and its "player / sim" evidence row can never disagree.
    unfired = sorted(unfired, key=lambda u: (u[1], u[0]))[-(ideal_n - player_n):]
    count = len(unfired)
    value = float(count * ad.POTENCIES[_ORACLE])
    lost_at = max(d for _g, d in unfired)
    first_granted = min(g for g, _d in unfired)
    t = TEXT["oracle_unfired"]
    return value, RootCause(
        kind="cascade_lost_use", ability_id=_ORACLE,
        ability_name=_name(_ORACLE),
        time_sec=round(lost_at, 1), measured_p=0.0,
        summary=t["summary"].format(
            count=count, plural="s" if count != 1 else "", value=value),
        prescription=t["prescription"].format(when=_mmss(lost_at)),
        evidence=[
            EvidenceRow(k=_name(_ORACLE),
                        v=t["count_v"].format(player=player_n, ideal=ideal_n),
                        note=t["count_note"]),
            EvidenceRow(k="Divining",
                        v=t["unspent_v"].format(count=count),
                        note=t["unspent_note"].format(
                            when=_mmss(first_granted))),
        ],
        resources=[GAUGE_TEXT["divining_ready"]])


def _ogcd_drift_causes(ctx: AdviceContext) -> list[tuple[float, RootCause]]:
    """A damage oGCD the sim fit more of than the player cast, with the ledger
    that shows where the use was lost. Time inside downtime, a death or a raise
    cast bar is removed from every gap first, so a hold the player never chose
    can neither reach the noise floor nor become the worst slip."""
    ideal_counts = _counts(ctx.idealized)
    forced = _forced_ogcd(ctx)
    excluded = frozenset(getattr(ctx.data, "drift_exclusions", ()) or ())
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_OGCDS:
        if aid in excluded:
            continue                      # mirrors the sim's own RNG rules
        recast, _charges = ad.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or player_n < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])           # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast - _overlap(a, b, forced)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = float(deficit * ad.COOLDOWN_VALUE_P.get(aid, 0))
        t = TEXT["ogcd_drift"]
        out.append((value, RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=ad.COOLDOWN_VALUE_P.get(aid, 0)),
            evidence=[
                EvidenceRow(k=name,
                            v=t["count_v"].format(
                                player=player_n, ideal=ideal_counts.get(aid, 0)),
                            note=t["count_note"]),
                EvidenceRow(k="Held", v=t["held_v"].format(drift=drift_total),
                            note=t["held_note"].format(
                                recasts=drift_total / recast)),
            ])))
    return out


def _combust_lapse_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Time the Combust III DoT was not running between two refreshes. The DoT
    is scored per application by time-to-next (scoring.py), so a lapse is real
    lost potency that no card carries: Combust's table potency is 0, which
    keeps it out of the missed-cast diff and out of `split_residual`.

    Only the stretches the player could have spent on a GCD count: downtime,
    deaths, raise bars, post-raise recovery and every costed heal GCD the
    honest budget credits are subtracted first. The opener (before the first
    Combust) and the tail after the last one are never counted."""
    casts = sorted(t for t, a in ctx.norm_casts if a == _COMBUST and t >= 0)
    if len(casts) < 2:
        return None
    forced = _forced_gcd(ctx)
    lapses: list[tuple[float, float]] = []     # (start, seconds down)
    for a, b in zip(casts, casts[1:]):
        end = a + ad.COMBUST_DOT_DURATION_S
        if b <= end:
            continue
        down = (b - end) - _overlap(end, b, forced)
        if down > _LAPSE_NOISE_S:
            lapses.append((end, down))
    total = sum(d for _s, d in lapses)
    if not lapses or total < _COMBUST_LAPSE_MIN_S:
        return None
    ticks = total / ad.COMBUST_DOT_TICK_S
    value = ticks * ad.COMBUST_DOT_TICK_P
    worst_t, worst_d = max(lapses, key=lambda l: (l[1], -l[0]))
    t = TEXT["combust_lapse"]
    return value, RootCause(
        kind="cascade_pacing", ability_id=_COMBUST, ability_name=_name(_COMBUST),
        time_sec=round(worst_t, 1), measured_p=0.0,
        summary=t["summary"].format(total=total, ticks=ticks),
        prescription=t["prescription"].format(
            when=_mmss(worst_t), worst=worst_d),
        evidence=[
            EvidenceRow(k=_name(_COMBUST), v=t["total_v"].format(total=total),
                        note=t["total_note"].format(
                            value=value, count=len(lapses),
                            plural="s" if len(lapses) != 1 else "")),
            EvidenceRow(k="Longest", v=t["worst_v"].format(worst=worst_d),
                        note=t["worst_note"].format(when=_mmss(worst_t))),
        ])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """AST probe set: no card enrichment (`cards` is unused), three ledger
    causes. Deterministic — the returned order is descending measured value
    with the ability id as the tie-break, which is the priority order the
    orchestrator's first-cause-in-segment matching consumes."""
    weighted: list[tuple[float, RootCause]] = list(_ogcd_drift_causes(ctx))
    oracle = _oracle_unfired_cause(ctx)
    if oracle is not None:
        weighted.append(oracle)
    combust = _combust_lapse_cause(ctx)
    if combust is not None:
        weighted.append(combust)
    weighted.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [], [c for _v, c in weighted]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
