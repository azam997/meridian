"""White Mage deep-advice pack (`Job.advice_probes`) — the first HEALER pack.

Same shape as the MCH reference (`jobs/machinist/advice.py`), with the healer
frame the rest of the pipeline already establishes: healing and raising are
PAID FOR by the ceiling (mit-plan locks + the reconciled heal budget + the
resurrection pardon in `jobs/_core/heal_locks.py`), so nothing here may read as
blaming a player for healing. Every cause below is a DAMAGE-side economy the
lily/Misery line owns:

* `RootCause`s only — no `ProbeItem`s. WHM's cards (missed Assize, missed
  Presence of Mind, Filler quality, the healing-GCD card) carry no window shape
  a probe could measure better than the static template already does.
* Four deterministic ledger walks over the delivered cast stream: damage-oGCD
  drift that cost a use, the lily gauge stalling the Blood Lily, a bloomed
  Afflatus Misery stranded at the kill, and Sacred Sight stacks that expired on
  top of Glare III filler. `measured_p` stays 0 — the orchestrator prices each
  from its cascade segment's unexplained loss.

Time the ceiling already pays for is subtracted before anything is called
drift: boss-untargetable windows, death windows, and the locked GCD slots of a
pardoned resurrection (`heal_lock_rez_casts` on the Scoring state). A raise or a
death is never re-billed here.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
wording is a data edit, never a logic change. `GAUGE_TEXT` is an allowlist: sim
state fields without an entry (next_lily_t, pom_until, dia_end…) never surface
in evidence lines.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.whitemage import data as wd

_ASSIZE = wd.ASSIZE
_POM = wd.PRESENCE_OF_MIND
_MISERY = wd.AFFLATUS_MISERY
_GLARE_III = wd.GLARE_III
_GLARE_IV = wd.GLARE_IV
_HOLY_III = wd.HOLY_III

# The two lily spends (0-damage instant heal GCDs that nourish the Blood Lily)
# and the filler slots a Sacred Sight stack could have upgraded.
_LILY_SPEND_IDS: frozenset[int] = frozenset(
    {wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE})
_FILLER_IDS: frozenset[int] = frozenset({_GLARE_III, _HOLY_III})

# The only genuinely recast-gated damage oGCDs (wd.COOLDOWNS): Presence of Mind
# then Assize. Both weave, so neither ever competes with a healing GCD.
_DRIFT_COOLDOWNS: tuple[int, ...] = (_POM, _ASSIZE)

# Derived per-use values (never new numbers — read straight off wd.POTENCIES).
# Afflatus Misery displaces one Glare III slot, so only the premium above the
# filler is at stake; the three lily spends that fund it are potency-neutral in
# uptime and completely free when the boss is away.
_MISERY_PREMIUM_P = wd.POTENCIES[_MISERY] - wd.POTENCIES[_GLARE_III]        # 1050
# One Sacred Sight stack, spent on Glare IV instead of the Glare III it replaces.
_GLARE_IV_PREMIUM_P = wd.POTENCIES[_GLARE_IV] - wd.POTENCIES[_GLARE_III]    # 290

# Thresholds — every producer is silent on a clean stream by construction.
_LILY_WASTE_MIN = 3          # one full Blood Lily's worth of ticks lost at cap
_SACRED_UNSPENT_MIN = 2      # a single edge-of-fight stack is not a card
_STRANDED_TAIL_S = 5.0       # a bloom this late could not have been cast
_DRIFT_FLOOR_FRAC = 0.25     # accumulated drift floor, as a share of the recast
_DRIFT_FLOOR_MIN_S = 10.0    # …never below this (Assize's 40s recast → 10s)
# Nominal GCD slot, used only to size the cast bar a pardoned rez occupied.
# Mirrors `jobs/_core/heal_locks._SLOT_S`, the length that pardon was priced in.
_SLOT_S = 2.5


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em dashes),
# no generic filler, and hold/spend advice scoped to the measured stretch
# ("here") so a legitimate hold elsewhere stays legitimate. Healer register:
# the healing plan comes first and the ceiling already pays for it, so no line
# here may read as a scolding for healing or raising.
TEXT: dict[str, dict] = {
    "cd_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Press {name} as soon as it comes back. Biggest "
                         "drift at {when}, {worst:.0f}s late; the drift adds "
                         "up until a use (~{value}p) is lost.{tail}"),
        # Per-ability tail: why this button never competes with the healing.
        "tail_pom": (" Presence of Mind weaves, so it costs you no healing "
                     "GCD."),
        "tail_assize": (" Assize heals as it damages, so the heal is not a "
                        "reason to hold it here."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "~{recasts:.1f} full recasts of waiting",
    },
    "lily_stall": {
        "summary": ("{wasted} lilies lost to a full gauge, {deficit} fewer "
                    "Afflatus Misery than the ideal line"),
        "prescription": ("Spend a lily heal here instead of letting the gauge "
                         "sit at three. Three spends bloom Afflatus Misery "
                         "(~{value:.0f}p over the Glare III it replaces), and "
                         "a spend made while the boss is away costs no damage "
                         "at all."),
        "waste_v": "{wasted} lost",
        "waste_note": "first at {when}; the lily timer keeps running at cap",
        "count_v": "{player} / {ideal}",
        "count_note": "Afflatus Misery casts vs the sim's line",
    },
    "misery_stranded": {
        "summary": "Afflatus Misery bloomed at {when} and was never cast",
        "prescription": ("Cast Afflatus Misery before the pull ends. Holding "
                         "it for a buff window is fine; letting it die in the "
                         "gauge is the whole cast (~{value:.0f}p over a "
                         "Glare III)."),
        "bloom_v": "bloomed {when}",
        "bloom_note": "the Blood Lily was full for the last {tail:.0f}s",
        "slots_v": "{filler} after the bloom",
        "slots_note": "filler casts, any one of which could have taken it",
    },
    "sacred_sight": {
        "summary": ("Presence of Mind window under-used, {unspent} Glare IV "
                    "left unspent"),
        "prescription": ("Spend the Sacred Sight stacks on Glare IV inside the "
                         "window. At {when} you cast {filler} {filler_name} "
                         "with {unspent} stack{plural} still up, ~{per:.0f}p "
                         "each."),
        "fired_v": "{fired} of {cap}",
        "fired_note": "stacks spent before the window closed",
        "slots_v": "{filler} {filler_name}",
        "slots_note": "filler casts in the window that could have taken one",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Lilies are a HEAL currency that is also damage-relevant (they bloom Misery),
# so they get an entry; the pure clocks (next_lily_t, pom_until, sacred_until,
# dia_end) stay silent. The delta is measured across ONE cascade segment (a few
# GCDs), so the caps here are small on purpose: Blood Lily only speaks at a full
# 3, the one reading that means "you are holding a bloom the ideal already
# cast", and the notes stay true at every delta that clears the floor.
GAUGE_TEXT: dict[str, GaugeText] = {
    "lilies": GaugeText(
        label="Lilies", short="LILY",
        over_note="a lily the ideal line had already spent",
        under_note=None,      # running lean on lilies is not a mistake
        min_delta=1.0),
    "blood": GaugeText(
        label="Blood Lily", short="BLD",
        over_note="a bloomed Afflatus Misery the ideal line had already cast",
        under_note=None,
        min_delta=3.0),
    "sacred_sight": GaugeText(
        label="Sacred Sight", short="G4",
        over_note="a Glare IV stack the ideal line had already fired",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _in_windows(t: float, windows) -> bool:
    return any(s <= t < e for s, e in windows)


def _overlap(a: float, b: float, windows) -> float:
    """Seconds of [a, b) covered by `windows` (assumed non-overlapping enough
    for a sum; overlaps only make the credit more generous, never less)."""
    total = 0.0
    for s, e in windows:
        lo, hi = max(a, float(s)), min(b, float(e))
        if hi > lo:
            total += hi - lo
    return total


def _filler_name(ids: list[int]) -> str:
    """The filler spell the player actually used in a stretch (Glare III on a
    single target, Holy III once the AoE-aware line swaps to it). Ties break on
    the lower id so the copy is deterministic."""
    counts: dict[int, int] = {}
    for a in ids:
        counts[a] = counts.get(a, 0) + 1
    if not counts:
        return _name(_GLARE_III)
    return _name(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0])


def _rez_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    """The cast bars a pardoned resurrection occupied, from the Scoring state's
    `heal_lock_rez_casts` rows (t, ability_id, locked GCD slots — written by
    jobs/_core/heal_locks). The ceiling already pays for these slots, so they
    can never count as drift here."""
    out: list[tuple[float, float]] = []
    for row in (ctx.scoring_state or {}).get("heal_lock_rez_casts") or []:
        try:
            t, slots = float(row[0]), int(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((t, t + max(1, slots) * _SLOT_S))
    return sorted(out)


def _paid_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    """Every stretch the ceiling already accounts for: boss-untargetable
    windows, death windows, and pardoned raises."""
    out = [(float(s), float(e)) for s, e in (ctx.downtime_windows or ())]
    out += [(float(s), float(e)) for s, e in (ctx.death_windows or ())]
    out += _rez_windows(ctx)
    return sorted(out)


# --- The lily ledger ---------------------------------------------------------

@dataclass(frozen=True)
class _LilyLedger:
    """One deterministic walk of the lily economy over the delivered stream."""
    wasted: int                       # lily ticks that arrived at a full gauge
    first_waste_t: float | None
    blood_end: int                    # Blood Lily nourishment at the kill
    bloom_t: float | None             # when it last reached full, if uncast
    misery_casts: int


def _lily_ledger(ctx: AdviceContext) -> _LilyLedger:
    """Mirror of the simulator's own lily rules (`_accrue_lilies` + the
    Solace/Rapture/Misery branches of `apply_cast`): one lily per 20s in
    combat, cap 3, the timer keeps running while capped, three spends bloom
    Afflatus Misery. Seeded with the measured phase-continuation entry state so
    a mid-combat log is read the same way the ceiling reads it. Ticks lost
    while the player was dead are not counted (the death card owns those)."""
    st = ctx.scoring_state or {}
    lilies = int(st.get("entryLilies") or 0)
    blood = int(st.get("entryBlood") or 0)
    deaths = [(float(s), float(e)) for s, e in (ctx.death_windows or ())]
    dur = float(ctx.fight_duration_s)

    # (time, kind) with ticks ordered before casts at the same instant, exactly
    # as `_accrue_lilies` runs at the head of `apply_cast`.
    events: list[tuple[float, int, int]] = []
    t = wd.LILY_INTERVAL_S
    while t <= dur:
        events.append((t, 0, 0))
        t += wd.LILY_INTERVAL_S
    for ct, aid in ctx.norm_casts:
        if ct is None or float(ct) < 0.0:
            continue
        if aid in _LILY_SPEND_IDS or aid == _MISERY:
            events.append((float(ct), 1, int(aid)))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    wasted = 0
    first_waste: float | None = None
    bloom_t: float | None = None
    misery = 0
    for et, kind, aid in events:
        if kind == 0:
            if lilies < wd.LILY_CAP:
                lilies += 1
            elif not _in_windows(et, deaths):
                wasted += 1
                if first_waste is None:
                    first_waste = et
        elif aid == _MISERY:
            misery += 1
            blood = 0
            bloom_t = None
        elif lilies > 0:
            lilies -= 1
            if blood < wd.BLOOD_LILY_CAP:
                blood += 1
                if blood >= wd.BLOOD_LILY_CAP:
                    bloom_t = et
    return _LilyLedger(wasted=wasted, first_waste_t=first_waste,
                       blood_end=blood, bloom_t=bloom_t, misery_casts=misery)


def _ideal_counts(ctx: AdviceContext) -> dict[int, int]:
    counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        counts[a] = counts.get(a, 0) + 1
    return counts


# --- Root causes -------------------------------------------------------------

def _cooldown_drift_causes(ctx: AdviceContext) -> list[tuple[float, RootCause]]:
    """A damage oGCD the sim fit more often than the player pressed it, with
    the drift ledger that shows where the use was lost. Time the ceiling
    already pays for (downtime, deaths, a pardoned raise's cast bar) is
    subtracted from every gap before it counts as drift."""
    ideal = _ideal_counts(ctx)
    paid = _paid_windows(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_COOLDOWNS:
        recast, _charges = wd.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        player_n = len(times)
        deficit = ideal.get(aid, 0) - player_n
        if deficit < 1 or player_n < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = (b - a) - recast - _overlap(a, b, paid)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        floor = max(recast * _DRIFT_FLOOR_FRAC, _DRIFT_FLOOR_MIN_S)
        if drift_total < floor or worst[0] <= 0:
            continue
        name = _name(aid)
        value = wd.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cd_drift"]
        tail = t["tail_pom"] if aid == _POM else t["tail_assize"]
        out.append((float(deficit * value), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0], value=value,
                tail=tail),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal.get(aid, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(recasts=drift_total / recast)),
            ])))
    return out


def _lily_stall_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Lilies lost to a full gauge while the Blood Lily stalled, gated on the
    only way that actually costs damage: fewer Afflatus Misery than the sim's
    line. A player who banks lilies and still lands every Misery is silent."""
    led = _lily_ledger(ctx)
    if led.wasted < _LILY_WASTE_MIN or led.first_waste_t is None:
        return None
    ideal_misery = _ideal_counts(ctx).get(_MISERY, 0)
    deficit = ideal_misery - led.misery_casts
    if deficit < 1:
        return None
    when = float(led.first_waste_t)
    t = TEXT["lily_stall"]
    return (float(deficit * _MISERY_PREMIUM_P), RootCause(
        kind="cascade_burst", ability_id=_MISERY, ability_name=_name(_MISERY),
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(wasted=led.wasted, deficit=deficit),
        prescription=t["prescription"].format(value=_MISERY_PREMIUM_P),
        evidence=[
            EvidenceRow(
                k="Lilies",
                v=t["waste_v"].format(wasted=led.wasted),
                note=t["waste_note"].format(when=_mmss(when))),
            EvidenceRow(
                k=_name(_MISERY),
                v=t["count_v"].format(player=led.misery_casts,
                                      ideal=ideal_misery),
                note=t["count_note"]),
        ],
        resources=[GAUGE_TEXT["lilies"]]))


def _misery_stranded_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """A bloomed Blood Lily that died in the gauge: three lily spends paid for
    an Afflatus Misery that was never cast. Silent when the bloom landed inside
    the last couple of GCDs (it could not have been cast), when a death window
    covers the tail (the death card owns that story), or when every GCD after
    the bloom was a heal (Misery would have had to displace one, and the
    healing is the ceiling's to pay for, not the player's to answer for)."""
    led = _lily_ledger(ctx)
    if led.blood_end < wd.BLOOD_LILY_CAP or led.bloom_t is None:
        return None
    dur = float(ctx.fight_duration_s)
    bloom = float(led.bloom_t)
    tail = dur - bloom
    if tail < _STRANDED_TAIL_S:
        return None
    if _overlap(bloom, dur, [(float(s), float(e))
                             for s, e in (ctx.death_windows or ())]) > 0:
        return None
    fills = [int(a) for ct, a in ctx.norm_casts
             if ct is not None and a in _FILLER_IDS and float(ct) > bloom]
    filler = len(fills)
    if filler < 1:
        return None
    t = TEXT["misery_stranded"]
    return (float(_MISERY_PREMIUM_P), RootCause(
        kind="cascade_lost_use", ability_id=_MISERY,
        ability_name=_name(_MISERY),
        time_sec=round(bloom, 1), measured_p=0.0,
        summary=t["summary"].format(when=_mmss(bloom)),
        prescription=t["prescription"].format(value=_MISERY_PREMIUM_P),
        evidence=[
            EvidenceRow(
                k="Blood Lily",
                v=t["bloom_v"].format(when=_mmss(bloom)),
                note=t["bloom_note"].format(tail=tail)),
            EvidenceRow(
                k=_filler_name(fills),
                v=t["slots_v"].format(filler=filler),
                note=t["slots_note"]),
        ],
        resources=[GAUGE_TEXT["blood"]]))


def _sacred_sight_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Sacred Sight stacks that expired while the filler spell took the slots
    they could have upgraded. Counted per Presence of Mind window against the
    player's OWN filler casts, so a window spent healing or waiting out
    downtime contributes nothing (only real filler slots are convertible). A
    window a death runs through is skipped outright: the stacks died with the
    player, and the death card already owns that stretch."""
    dur = float(ctx.fight_duration_s)
    deaths = [(float(s), float(e)) for s, e in (ctx.death_windows or ())]
    casts = sorted((float(t), int(a)) for t, a in ctx.norm_casts
                   if t is not None and float(t) >= 0.0)
    total = 0
    # (drop, t, fired, filler count, filler name)
    worst: tuple[int, float, int, int, str] | None = None
    for pt, aid in casts:
        if aid != _POM:
            continue
        end = pt + wd.SACRED_SIGHT_DURATION_S
        if end > dur:
            continue                     # the window outlived the pull
        if _overlap(pt, end, deaths) > 0:
            continue                     # the death card owns this stretch
        fired = sum(1 for t, a in casts if a == _GLARE_IV and pt <= t <= end)
        fills = [a for t, a in casts if a in _FILLER_IDS and pt <= t <= end]
        filler = len(fills)
        unspent = max(0, wd.SACRED_SIGHT_STACKS - fired)
        droppable = min(unspent, filler)
        if droppable <= 0:
            continue
        total += droppable
        if worst is None or droppable > worst[0]:
            worst = (droppable, pt, fired, filler, _filler_name(fills))
    if total < _SACRED_UNSPENT_MIN or worst is None:
        return None
    drop, pt, fired, filler, filler_name = worst
    t = TEXT["sacred_sight"]
    return (float(total * _GLARE_IV_PREMIUM_P), RootCause(
        kind="cascade_burst", ability_id=_GLARE_IV,
        ability_name=_name(_GLARE_IV),
        time_sec=round(pt, 1), measured_p=0.0,
        summary=t["summary"].format(unspent=drop),
        prescription=t["prescription"].format(
            when=_mmss(pt), filler=filler, filler_name=filler_name,
            unspent=drop, plural="s" if drop != 1 else "",
            per=_GLARE_IV_PREMIUM_P),
        evidence=[
            EvidenceRow(
                k=_name(_GLARE_IV),
                v=t["fired_v"].format(fired=fired,
                                      cap=wd.SACRED_SIGHT_STACKS),
                note=t["fired_note"]),
            EvidenceRow(
                k="Slots",
                v=t["slots_v"].format(filler=filler,
                                      filler_name=filler_name),
                note=t["slots_note"]),
        ],
        resources=[GAUGE_TEXT["sacred_sight"]]))


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """WHM probe set: root causes only. Deterministic, and ordered by the value
    at stake per cause — that order is the priority the orchestrator's
    first-in-segment-wins matching consumes."""
    scored: list[tuple[float, RootCause]] = list(_cooldown_drift_causes(ctx))
    for producer in (_lily_stall_cause, _misery_stranded_cause,
                     _sacred_sight_cause):
        hit = producer(ctx)
        if hit is not None:
            scored.append(hit)
    scored.sort(key=lambda r: (-r[0], r[1].ability_id, r[1].time_sec))
    return [], [c for _v, c in scored]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
