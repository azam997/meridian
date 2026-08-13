"""Sage deep-advice pack (`Job.advice_probes`).

The SGE probe set, mirroring the Machinist reference (`jobs/machinist/advice.py`)
and the healer frame the analyzer already ships: the ceiling PAYS for this
player's healing (jobs/_core/heal_locks.py reconciles the honest budget, and the
rez pardon locks an uptime Egeiro's own GCD slots into it), so nothing here may
read as blaming a heal or a raise. Every cause below is a DAMAGE-side economy
that stays lost even after the healing is paid for.

No `ProbeItem`s: SGE has no enabler window and no bespoke located card to
enrich (`data.ENABLER_IDS` is empty, the only job card is the shared
beyond-budget heal-GCD one), so nothing meets the MCH window-shift bar. Three
`RootCause` families, all deterministic ledger walks over the delivered cast
stream using `data.py`'s own tables:

* **Eukrasian Dosis III lapse** — gaps between consecutive DoT applications
  longer than `EUKRASIAN_DOSIS_DOT_DURATION_S`, i.e. stretches where the DoT was
  provably off the target. Nothing else cards this: the DoT id is not in
  `data.COOLDOWNS` (so the missed-cast diff never sees it) and it carries 0 table
  potency (so `split_residual` never tails it), yet it is 90 potency a tick.
  An Eukrasian Dyskrasia application counts as coverage: the AoE DoT is
  unmodeled here, so an add phase played on it is a stretch this ledger cannot
  speak about honestly, and silence beats a false story.
* **Phlegma III banked** — the 2-charge / 40s pool walked forward: seconds spent
  at full charges are regen that stopped, and (gated on a real deficit vs the
  sim's count) they are casts that never happened.
* **Psyche drift** — the lone damage oGCD (60s), the MCH/BRD gap-over-recast
  ledger, located at the worst slip.

Excused stretches (subtracted from every ledger, never blamed): boss-untargetable
downtime, death windows, and each uptime resurrection's own cast bar, read from
the Scoring state's rez block (`heal_lock_rez_casts` — the ceiling already pays
those slots).

Dropped hypotheses, on purpose:

* **Addersting overcap / stranded Toxikon II.** The model tracks no Addersting
  int at all (data.py: Toxikon II's 380 EQUALS Dosis III's 380, so spending or
  banking a sting is potency-neutral and the ceiling never casts it). There is no
  loss to name, so silence is correct.
* **Eukrasia over-refresh.** An early refresh is already priced by the scorer
  (`scoring._eukrasian_dosis_dot_potency` credits each application by
  time-to-next), so carding it would double-count a cost the headline gap already
  carries.

`measured_p` stays 0 on every cause — the orchestrator prices causes from the
cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / the resource tiles below; improving the
wording is a data edit here, never a logic change.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.sage import data as gd

_EK_DOSIS = gd.EUKRASIAN_DOSIS_III
_PHLEGMA = gd.PHLEGMA_III
_PSYCHE = gd.PSYCHE

# The Eukrasia DoT applications the lapse ledger counts as "a DoT went on".
# Eukrasian Dyskrasia is the AoE DoT an SGE applies instead of Eukrasian Dosis
# III in an add phase; data.py leaves it unmodeled and unscored (deferred AoE
# lever), so a stretch it covers is a stretch this ledger cannot speak about
# honestly. Counting it as an application keeps the card off a correctly played
# AoE phase rather than telling that player their DoT was never on the target.
_DOT_APPLY_IDS: frozenset[int] = frozenset(
    {gd.EUKRASIAN_DOSIS_III, gd.EUKRASIAN_DYSKRASIA})

# The recast-gated damage cooldowns the generic drift ledger watches. Phlegma III
# is deliberately absent: it is a 2-charge pool, where consecutive-gap-over-recast
# reads a legitimate double dump as drift, so it gets the charge walk below.
_DRIFT_COOLDOWNS: tuple[int, ...] = (_PSYCHE,)

# Per-lost-use values, derived from data.POTENCIES so they can never drift from
# the tables. Same convention as the shipped missed-cast card
# (jobs/_core/improvements.py): a GCD only loses the potency ABOVE the filler it
# would have displaced, a damaging oGCD displaces nothing and loses all of it.
_PHLEGMA_NET_P: int = gd.POTENCIES[_PHLEGMA] - gd.POTENCIES[gd.DOSIS_III]
_PSYCHE_NET_P: int = gd.POTENCIES[_PSYCHE]
# Per-lost-use weight by ability, so adding an id to `_DRIFT_COOLDOWNS` can
# never silently price a GCD at an oGCD's full potency.
_NET_P: dict[int, int] = {_PHLEGMA: _PHLEGMA_NET_P, _PSYCHE: _PSYCHE_NET_P}

# DoT floor: three ticks of provable uncovered time before it is worth a card.
_DOT_LAPSE_MIN_S: float = 3.0 * gd.EUKRASIAN_DOSIS_DOT_TICK_S
# Phlegma floor: half a recharge of wasted regen (a jittered but healthy pool
# stays silent).
_PHLEGMA_BANK_MIN_S: float = 0.5 * gd.PHLEGMA_CD_S
# A capped stretch shorter than one GCD was never a decision the player had.
_BANK_LOCATE_MIN_S: float = gd.SGE_GCD_S
# Nominal GCD slot, used only to size the rez cast bar the pardon already paid
# for (mirrors heal_locks._SLOT_S).
_SLOT_S: float = gd.SGE_GCD_S


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em dashes),
# no generic filler, hold/spend advice scoped to the measured stretch, and for a
# healer never a word that reads as blaming healing or raising. Run new dialogue
# copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "dot_lapse": {
        "summary": "Eukrasian Dosis III was off the target for {lapse:.0f}s",
        "prescription": ("Start Eukrasia one GCD before the DoT runs out so "
                         "the refresh lands on time. Longest uncovered stretch "
                         "at {when}, {worst:.0f}s; the refresh still fits "
                         "around the healing there."),
        "uncovered_v": "{lapse:.0f}s",
        "uncovered_note": "about {value:.0f}p of ticks that never landed",
        "gap_v": "{worst:.0f}s",
        "gap_note": "the single longest stretch with no DoT running",
    },
    "phlegma_bank": {
        "summary": ("Phlegma III sat at both charges for {banked:.0f}s, "
                    "{deficit} cast{plural} lost"),
        "prescription": ("Spend a Phlegma III charge as soon as the healing "
                         "here allows. The recharge stops while both charges "
                         "sit banked; the pool was already full at {when}."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "banked_v": "{banked:.0f}s",
        "banked_note": "about {recharges:.1f} full recharges of stopped regen",
    },
    "cooldown_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Weave {name} as soon as it comes up. Biggest drift "
                         "at {when}, {worst:.1f}s late; it weaves under a "
                         "heal GCD just as well as under a Dosis III."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "about {recasts:.1f} full recasts of idle time",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). DELIBERATELY EMPTY. `simulator.SimState` adds
# exactly two public scalars over the engine base, and neither is a spendable
# resource: `dosis_dot_end` is an absolute expiry clock (a player-vs-ideal delta
# there is mostly refresh phase, not a mistake) and `eukrasia_active` is a
# transient two-GCD sequence flag. The orchestrator phrases allowlisted resources
# as spendable ("Use excess X right away here.", sidecar/advice.py), which is
# nonsense for a timer or a flag, so SGE keeps the allowlist closed and lets the
# generic state-delta evidence speak through the always-safe cooldown and charge
# rows (real ability names: Psyche late, Phlegma charges banked). The causes below
# carry their own tiles instead.
GAUGE_TEXT: dict[str, GaugeText] = {}

# Bespoke resource tiles for the causes below (icon tile + label on the card).
# NOT part of the GAUGE_TEXT allowlist, so the generic sequencing card can never
# phrase a DoT timer or a charge pool as something to "use up".
PHLEGMA_TILE = GaugeText(
    label="Phlegma", short="PHL",
    over_note="both charges sat unused while the recharge idled",
    under_note=None,
    min_delta=1.0)
DOT_TILE = GaugeText(
    label="Dosis DoT", short="DOT",
    over_note=None,
    under_note="the DoT was left off the target through this stretch",
    min_delta=1.0)


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _blocked_s(lo: float, hi: float,
               windows: list[tuple[float, float]]) -> float:
    """Seconds of [lo, hi] covered by the given windows, subtracted from every
    ledger so a boss-away stretch, a death, or a resurrection the ceiling already
    pays for is never counted as a damage-side slip."""
    if hi <= lo:
        return 0.0
    total = 0.0
    for s, e in windows:
        total += max(0.0, min(hi, float(e)) - max(lo, float(s)))
    return total


def _first_open(lo: float, hi: float,
                windows: list[tuple[float, float]]) -> float:
    """The first instant of [lo, hi) not covered by an excused window, so a card
    is never LOCATED at a moment the player could not act (boss away, dead, mid
    raise). Falls back to `lo` when the whole stretch is covered."""
    t = lo
    moved = True
    while moved and t < hi:
        moved = False
        for s, e in sorted(windows):
            if float(s) <= t < float(e):
                t = float(e)
                moved = True
    return t if t < hi else lo


def _rez_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    """The cast bars of this pull's pardoned resurrections, from the Scoring
    state's rez block (`heal_lock_rez_casts`: [t, ability_id, locked_slots] —
    jobs/_core/heal_locks). Empty on a rez-less pull, and on any run whose
    scoring state predates the block."""
    out: list[tuple[float, float]] = []
    for row in (ctx.scoring_state or {}).get("heal_lock_rez_casts") or ():
        try:
            t = float(row[0])
            slots = int(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        out.append((t, t + max(1, slots) * _SLOT_S))
    return sorted(out)


def _excused(ctx: AdviceContext) -> list[tuple[float, float]]:
    """Downtime + deaths + pardoned rez cast bars, as one window list."""
    return (list(ctx.downtime_windows or []) + list(ctx.death_windows or [])
            + _rez_windows(ctx))


def _dot_lapse_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Eukrasian Dosis III left off the target: every gap between consecutive
    applications longer than the DoT's own duration, net of excused stretches.
    Measured BETWEEN applications only — the stretch before the first refresh and
    the tail after the last one are left alone (a phase-continuation pull can
    enter with the DoT already running, and the last application credits to the
    end either way), so the ledger only ever speaks about time it can prove.
    An Eukrasian Dyskrasia application counts too (`_DOT_APPLY_IDS`)."""
    casts = sorted(t for t, a in ctx.norm_casts
                   if a in _DOT_APPLY_IDS and t >= 0)
    if len(casts) < 2:
        return None
    dur = float(gd.EUKRASIAN_DOSIS_DOT_DURATION_S)
    blocked = _excused(ctx)
    total = 0.0
    worst = (0.0, casts[0])                      # (uncovered_s, located time)
    for a, b in zip(casts, casts[1:]):
        expiry = a + dur
        over = (b - expiry) - _blocked_s(expiry, b, blocked)
        if over > 0:
            total += over
            if over > worst[0]:
                worst = (over, _first_open(expiry, b, blocked))
    if total < _DOT_LAPSE_MIN_S:
        return None
    value = total / float(gd.EUKRASIAN_DOSIS_DOT_TICK_S) \
        * float(gd.EUKRASIAN_DOSIS_DOT_TICK_P)
    when = max(0.0, min(worst[1], float(ctx.fight_duration_s)))
    t = TEXT["dot_lapse"]
    rows = [EvidenceRow(k="Uncovered",
                        v=t["uncovered_v"].format(lapse=total),
                        note=t["uncovered_note"].format(value=value))]
    # Only worth a second row when the lapse happened more than once: with a
    # single gap the two rows would carry the same number.
    if worst[0] < total - 0.05:
        rows.append(EvidenceRow(k="Longest gap",
                                v=t["gap_v"].format(worst=worst[0]),
                                note=t["gap_note"]))
    return (value, RootCause(
        kind="cascade_lost_use", ability_id=_EK_DOSIS,
        ability_name=_name(_EK_DOSIS),
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(lapse=total),
        prescription=t["prescription"].format(when=_mmss(when),
                                              worst=worst[0]),
        evidence=rows,
        resources=[DOT_TILE]))


def _phlegma_bank_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """The Phlegma III charge pool walked forward over the delivered stream:
    every second spent at full charges is regen that stopped. Gated on a real
    deficit against the sim's own count (which is built with this pull's heal
    locks already applied), so a healer who banked a charge through a mechanic
    and still fit every cast stays silent."""
    recast, cap = gd.COOLDOWNS[_PHLEGMA]
    dur = float(ctx.fight_duration_s)
    times = sorted(t for t, a in ctx.norm_casts if a == _PHLEGMA and 0 <= t <= dur)
    ideal_times = sorted(t for t, a in ctx.idealized if a == _PHLEGMA and t >= 0)
    ideal_n = len(ideal_times)
    deficit = ideal_n - len(times)
    if deficit < 1:
        return None
    # Opener grace: the pool starts full and the sim's own line spends the DoT
    # sequence first, so the stretch before the sim's FIRST Phlegma is banked on
    # both sides. Counting it would blame 0:00 on every pull.
    blocked = _excused(ctx) + ([(0.0, ideal_times[0])] if ideal_times else [])
    charges = float(cap)
    prev = 0.0
    banked = 0.0
    first_at: float | None = None                # first capped stretch >= 1 GCD
    first_any: float | None = None
    for i, t in enumerate(times + [dur]):
        gap = max(0.0, t - prev)
        to_cap = (float(cap) - charges) * recast
        if gap > to_cap:
            lo = prev + to_cap
            span = (t - lo) - _blocked_s(lo, t, blocked)
            if span > 0:
                banked += span
                # Locate at the first instant the player could actually have
                # spent the charge, not at a capped moment spent boss-away.
                open_at = _first_open(lo, t, blocked)
                if first_any is None:
                    first_any = open_at
                if first_at is None and span >= _BANK_LOCATE_MIN_S:
                    first_at = open_at
            charges = float(cap)
        else:
            charges = min(float(cap), charges + gap / recast)
        if i < len(times):
            charges = max(0.0, charges - 1.0)
        prev = t
    if banked < _PHLEGMA_BANK_MIN_S:
        return None
    when = max(0.0, min(float(first_at if first_at is not None
                              else (first_any or 0.0)), dur))
    name = _name(_PHLEGMA)
    t = TEXT["phlegma_bank"]
    return (float(deficit * _PHLEGMA_NET_P), RootCause(
        kind="cascade_lost_use", ability_id=_PHLEGMA, ability_name=name,
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(banked=banked, deficit=deficit,
                                    plural="s" if deficit != 1 else ""),
        prescription=t["prescription"].format(when=_mmss(when)),
        evidence=[
            EvidenceRow(
                k=name,
                v=t["count_v"].format(player=len(times), ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Banked",
                v=t["banked_v"].format(banked=banked),
                note=t["banked_note"].format(recharges=banked / recast)),
        ],
        resources=[PHLEGMA_TILE]))


def _cooldown_drift_causes(ctx: AdviceContext
                           ) -> list[tuple[float, RootCause]]:
    """(value, cause) per drifted damage cooldown (Psyche today): the sim fit
    more uses than the player cast AND the delivered stream shows real
    accumulated gap-over-recast, net of excused stretches. Located at the start
    of the worst slip."""
    ideal_counts: dict[int, int] = {}
    for t, a in ctx.idealized:
        if t >= 0:
            ideal_counts[a] = ideal_counts.get(a, 0) + 1
    blocked = _excused(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_COOLDOWNS:
        recast, _ch = gd.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        deficit = ideal_counts.get(aid, 0) - len(times)
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (slip_s, gap start)
        for a_t, b_t in zip(times, times[1:]):
            over = (b_t - a_t) - recast - _blocked_s(a_t + recast, b_t, blocked)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a_t)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        t = TEXT["cooldown_drift"]
        out.append((float(deficit * _NET_P.get(aid, 0)), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0]),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=len(times),
                                          ideal=ideal_counts.get(aid, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(recasts=drift_total / recast)),
            ])))
    return out


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """SGE probe set. Deterministic; no ProbeItems. RootCause order is the
    priority order the orchestrator's first-in-segment-wins matching consumes:
    every cause carries its lost-value weight, sorted descending with the ability
    id as the stable tie-break."""
    pairs: list[tuple[float, RootCause]] = []
    dot = _dot_lapse_cause(ctx)
    if dot is not None:
        pairs.append(dot)
    phlegma = _phlegma_bank_cause(ctx)
    if phlegma is not None:
        pairs.append(phlegma)
    pairs.extend(_cooldown_drift_causes(ctx))
    pairs.sort(key=lambda p: (-p[0], p[1].ability_id))
    return [], [c for _v, c in pairs]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
