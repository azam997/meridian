"""Scholar deep-advice pack (`Job.advice_probes`).

The SCH probe set, on the Machinist registry pattern (the reference
implementation in `jobs/machinist/advice.py`). No `ProbeItem`s — SCH has no
measurable window-shift analog (Chain Stratagem is a party buff the shared
`buffAlignment` currency already owns) — so the pack ships `RootCause`s only,
all deterministic ledger walks over the delivered cast stream:

* **Cooldown drift** over the two recast-gated oGCDs the job data models
  (Chain Stratagem's 120s anchor and the 60s Aetherflow refill) that cost an
  end-of-fight use. Downtime, deaths and the ceiling's pardoned resurrection
  slots are subtracted from every inter-use gap, so a raise-wait never reads
  as drift, and Dissipation counts as an Aetherflow refill (it grants the same
  three stacks) so refilling with it never reads as a skipped Aetherflow.
* **Baneful Impaction left unfired** — every Chain Stratagem grants exactly
  one Baneful stack (the simulator's `baneful_ready` flag); a stack that never
  becomes a cast is the burst's whole payoff gone.
* **Aetherflow refilled over live stacks** — the refill overwrites whatever is
  still banked, so each wasted stack is an Energy Drain that never happened.
* **Aetherflow stranded** at the kill with time left to spend it.

**Healer discipline.** The ceiling already pays for this job's healing (the
mit-plan heal locks, the reconciled heal budget, and the resurrection pardon in
`jobs/_core/heal_locks.py`), so nothing here may read as blaming a heal or a
raise. Two concrete rules follow: the Aetherflow ledger counts Lustrate,
Indomitability, Excogitation and Sacred Soil as REAL spends (a stack that went
into healing is not "wasted", so only stacks that went nowhere are ever
flagged), and the drift ledger subtracts the locked slots the ceiling already
paid for each uptime resurrection (`heal_lock_rez_casts`), naming the raise in
its evidence instead of charging for it.

`measured_p` stays 0 on every cause — the orchestrator prices each from its
cascade segment's unexplained loss. Causes are ordered by descending ledger
value (the orchestrator's first-in-segment-wins priority).

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` / the word table below —
improving the feedback wording is a data edit here, never a logic change.
`GAUGE_TEXT` is an allowlist: sim-state fields without an entry never surface
in evidence lines, which is why `biolysis_end` stays silent (it is an absolute
DoT-expiry clock, so its player-vs-ideal delta is refresh-phase noise, not a
quality signal).
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.scholar import data as sd

# Ledger thresholds / guards (internal tuning, never user-facing).
_DRIFT_FLOOR_FRAC = 0.5            # of one recast — MCH's tool-drift noise floor
_REZ_SLOT_S = 2.5                  # one locked GCD slot the rez pardon already paid
_BANEFUL_TAIL_GUARD_S = 10.0       # a Chain this late leaves no room for the weave
_BANEFUL_DEATH_GRACE_S = 30.0      # death overlap checked this far past the unlock
_AF_WASTE_MIN_STACKS = 2           # below this the refill overlap is not worth a card
_AF_WASTE_TAIL_SKIP_S = 15.0       # a tail refill can be a net gain; stay silent
_AF_STRANDED_MIN_STACKS = 3        # the WHOLE gauge died unspent (never a heal reserve)
_AF_STRANDED_TAIL_GUARD_S = 15.0   # room to weave the spare Energy Drains

# Aetherflow economy (data.py ids). The refill overwrites the gauge, so the
# generators are the two abilities that set it back to full; the spenders
# include the oGCD HEALS, which spend a stack exactly like Energy Drain does —
# counting them is what keeps a healing SCH out of the waste ledger.
_AF_GENERATORS = frozenset({sd.AETHERFLOW, sd.DISSIPATION})
_AF_HEAL_SPENDERS = frozenset({
    sd.LUSTRATE, sd.INDOMITABILITY, sd.EXCOGITATION, sd.SACRED_SOIL,
})
_AF_SPENDERS = _AF_HEAL_SPENDERS | {sd.ENERGY_DRAIN}
# Recitation makes the next Aetherflow heal free; not modeled, so such a heal
# still counts as a spend here (the safe direction: fewer flagged stacks).
_AF_VALUE_P = float(sd.POTENCIES[sd.ENERGY_DRAIN])


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("here") so holding for buffs elsewhere stays legitimate. Healer register:
# healing and raising are paid for by the ceiling, never a loss. Run new
# dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cd_drift": {
        "summary": ("{label} sat idle {drift:.0f}s in total, {deficit} "
                    "{noun}{plural} lost"),
        "prescription": ("{action} Biggest drift at {when}, {worst:.1f}s "
                         "late; the drift adds up until a {noun} (~{value}p) "
                         "is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of idle time",
        "rez_v": "{when}",
        "rez_note": "the ceiling already pays for the raise in this stretch",
    },
    "baneful": {
        "summary": ("Baneful Impaction left unfired after {count} Chain "
                    "Stratagem{plural}"),
        "prescription": ("Weave Baneful Impaction in the seconds after Chain "
                         "Stratagem here; the stack it grants is the burst's "
                         "payoff (~{value:.0f}p each). First unfired at "
                         "{when}."),
        "count_v": "{player} / {chains}",
        "count_note": "Chain Stratagems whose stack was fired",
        "total_v": "~{value:.0f}p",
        "total_note": "across {count} unfired stack{plural}",
    },
    "af_waste": {
        "summary": ("Aetherflow refilled over live stacks, {total} Energy "
                    "Drain cast{plural} lost"),
        "prescription": ("Drain the leftover stacks before Aetherflow comes "
                         "back here. First refill over live stacks at {when}; "
                         "the refill overwrites whatever is still banked."),
        "worst_v": "{n} stack{plural}",
        "worst_note": "overwritten by the refill at {when}",
        "total_v": "~{value:.0f}p",
        "total_note": ("{total} stack{sp} lost across {count} refill{rp}; "
                       "Lustrate and the other Aetherflow heals count as "
                       "spends here"),
    },
    "af_stranded": {
        "summary": "Aetherflow left with {n} stacks at the kill",
        "prescription": ("Weave the spare Energy Drain casts before the pull "
                         "ends, as the healing plan allows "
                         "(~{value:.0f}p)."),
        "stacks_v": "{n} unspent",
        "stacks_note": "from the refill at {when} with time left to spend",
    },
}

# Per-cooldown copy for the drift cause: ability id -> (row label / summary
# subject, the lost-unit noun, the opening imperative). The drift walk iterates
# THIS table (intersected with data.COOLDOWNS), so a cooldown added to data.py
# later stays silent instead of raising.
_CD_WORDS: dict[int, tuple[str, str, str]] = {
    sd.CHAIN_STRATAGEM: (
        "Chain Stratagem", "use",
        "Press Chain Stratagem the moment it comes back."),
    sd.AETHERFLOW: (
        "Aetherflow", "refill",
        "Spend the last stacks and refill the moment Aetherflow comes back, "
        "as the healing plan allows."),
}

# Abilities that consume the same recast's ROLE, so the drift walk counts them
# as uses (the MCH Drill/Bioblaster shared-pool quirk). Dissipation refills the
# very same three stacks Aetherflow does, so a player who refilled with it did
# NOT skip the refill; counting it is what keeps a Dissipation pull out of the
# drift ledger. Ids absent here consume only themselves.
_CD_CONSUMERS: dict[int, frozenset[int]] = {
    sd.AETHERFLOW: frozenset({sd.AETHERFLOW, sd.DISSIPATION}),
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Every key is a public scalar field of simulator.SimState. Holding LESS than
# the ideal line is never a mistake for these, so under_note stays None; the
# Aetherflow note names the damage lever without charging the healing that
# legitimately eats the gauge.
GAUGE_TEXT: dict[str, GaugeText] = {
    "aetherflow": GaugeText(
        label="Aetherflow", short="AF",
        over_note="Energy Drain spends what the healing plan leaves over",
        under_note=None,
        min_delta=2.0),
    "baneful_ready": GaugeText(
        label="Baneful Impaction", short="BAN",
        over_note="Chain Stratagem had already unlocked it",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlap_s(a: float, b: float, windows) -> float:
    """Total overlap of [a, b] with a window list (downtime / deaths / rez)."""
    total = 0.0
    for s, e in windows or ():
        total += max(0.0, min(b, float(e)) - max(a, float(s)))
    return total


def _at(ctx: AdviceContext, t: float) -> float:
    """A located time, clamped into the fight and rounded to the emit grid."""
    return round(min(max(float(t), 0.0), float(ctx.fight_duration_s)), 1)


def _rez_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    """The locked GCD slots the resurrection pardon already charged to the
    ceiling (`heal_locks._rez_pardon` -> the Scoring state's rez block). An
    oGCD cannot be weaved through a raise cast bar, so these seconds are not
    the player's drift and are subtracted from every drift gap."""
    out: list[tuple[float, float]] = []
    for row in (ctx.scoring_state or {}).get("heal_lock_rez_casts") or ():
        try:
            t = float(row[0])
            slots = int(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        out.append((t, t + max(1, slots) * _REZ_SLOT_S))
    out.sort()
    return out


def _cooldown_drift_causes(ctx: AdviceContext
                           ) -> list[tuple[float, RootCause]]:
    """A recast-gated oGCD the sim fit more uses of than the player cast, with
    the drift ledger that shows where the use was lost. Downtime, death and
    pardoned-raise overlap are subtracted from every gap, so a boss phase, a
    death or a raise never reads as drift; `_CD_CONSUMERS` folds in the
    alternate presses of the same recast role (Dissipation for Aetherflow)."""
    rez = _rez_windows(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in sorted(set(_CD_WORDS) & set(sd.COOLDOWNS)):
        recast, _ch = sd.COOLDOWNS[aid]
        consume = _CD_CONSUMERS.get(aid) or frozenset({aid})
        times = sorted(t for t, a in ctx.norm_casts if a in consume and t >= 0)
        player_n = len(times)
        ideal_n = sum(1 for _t, a in ctx.idealized if a == aid)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        worst_rez = 0.0
        for a, b in zip(times, times[1:]):
            pardoned = _overlap_s(a, b, rez)
            over = ((b - a) - recast
                    - _overlap_s(a, b, ctx.downtime_windows)
                    - _overlap_s(a, b, ctx.death_windows)
                    - pardoned)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
                    worst_rez = pardoned
        if drift_total < recast * _DRIFT_FLOOR_FRAC:
            continue
        label, noun, action = _CD_WORDS[aid]
        per_use = sd.COOLDOWN_VALUE_P.get(aid, 0)
        value = float(deficit * per_use)
        t = TEXT["cd_drift"]
        rows = [
            EvidenceRow(
                k=label,
                v=t["count_v"].format(player=player_n, ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Idle",
                v=t["idle_v"].format(drift=drift_total),
                note=t["idle_note"].format(recasts=drift_total / recast)),
        ]
        if worst_rez > 0:
            rows.append(EvidenceRow(
                k="Raise",
                v=t["rez_v"].format(when=_mmss(worst[1])),
                note=t["rez_note"]))
        out.append((value, RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=_name(aid),
            time_sec=_at(ctx, worst[1]), measured_p=0.0,
            summary=t["summary"].format(
                label=label, drift=drift_total, deficit=deficit, noun=noun,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                action=action, when=_mmss(worst[1]), worst=worst[0],
                noun=noun, value=per_use),
            evidence=rows)))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return out


def _baneful_lost_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """A Chain Stratagem whose Baneful Impaction stack never became a cast.
    Every Chain grants exactly one stack (`SimState.baneful_ready`), so the
    pairing is exact: an unfired stack is the burst payoff outright lost.
    Silent when the ceiling itself never fires Baneful (an old log), when a
    death sits inside the unlock window, and for a Chain cast so late that the
    weave had no room."""
    if not any(a == sd.BANEFUL_IMPACTION for _t, a in ctx.idealized):
        return None
    chains = sorted(t for t, a in ctx.norm_casts
                    if a == sd.CHAIN_STRATAGEM and t >= 0)
    if not chains:
        return None
    fired = sorted(t for t, a in ctx.norm_casts
                   if a == sd.BANEFUL_IMPACTION and t >= 0)
    dur = float(ctx.fight_duration_s)
    missed: list[float] = []
    # Pairs, not raw counts: a Baneful cast can belong to a Chain outside the
    # scored window (a pre-pull press, a phase-continuation entry), so counting
    # the casts would contradict the summary ("1 / 1 fired" next to "left
    # unfired"). Every counted chain either has its stack fired or does not.
    paired = 0
    for i, ct in enumerate(chains):
        nxt = chains[i + 1] if i + 1 < len(chains) else dur
        if any(ct <= b < nxt for b in fired):
            paired += 1
            continue
        if ct > dur - _BANEFUL_TAIL_GUARD_S:
            continue
        if _overlap_s(ct, min(nxt, ct + _BANEFUL_DEATH_GRACE_S),
                      ctx.death_windows) > 0:
            continue
        missed.append(ct)
    if not missed:
        return None
    count = len(missed)
    value = count * float(sd.BANEFUL_TOTAL_P)
    t = TEXT["baneful"]
    return value, RootCause(
        kind="cascade_lost_use", ability_id=sd.BANEFUL_IMPACTION,
        ability_name=_name(sd.BANEFUL_IMPACTION),
        time_sec=_at(ctx, missed[0]), measured_p=0.0,
        summary=t["summary"].format(
            count=count, plural="s" if count != 1 else ""),
        prescription=t["prescription"].format(
            value=float(sd.BANEFUL_TOTAL_P), when=_mmss(missed[0])),
        evidence=[
            EvidenceRow(
                k="Baneful Impaction",
                v=t["count_v"].format(player=paired, chains=len(chains)),
                note=t["count_note"]),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(value=value),
                note=t["total_note"].format(
                    count=count, plural="s" if count != 1 else "")),
        ],
        resources=[GAUGE_TEXT["baneful_ready"]])


def _aetherflow_ledger(ctx: AdviceContext
                       ) -> tuple[list[tuple[float, int]], int, float | None]:
    """(waste_events, final_stacks, last_refill_t) from one walk of the
    delivered stream. Aetherflow and Dissipation both set the gauge back to
    full, so stacks still live when one lands are overwritten. The oGCD heals
    spend a stack exactly like Energy Drain, so they are counted as spends: a
    stack that went into healing can never surface as waste. A death zeroes the
    running gauge (job gauges reset on death), so a post-raise refill never
    reads as an overcap. Pre-pull casts are ignored, like every other ledger
    here, which can only under-count."""
    merged: list[tuple[float, int, int]] = [
        (float(s), 0, 0) for s, _e in (ctx.death_windows or ())]
    merged += [(float(t), 1, int(a)) for t, a in sorted(ctx.norm_casts)
               if t >= 0]
    merged.sort(key=lambda e: (e[0], e[1]))
    stacks = 0
    waste: list[tuple[float, int]] = []
    last_refill_t: float | None = None
    for t, prio, a in merged:
        if prio == 0:
            stacks = 0
        elif a in _AF_GENERATORS:
            if stacks > 0:
                waste.append((t, stacks))
            stacks = int(sd.AETHERFLOW_STACKS)
            last_refill_t = t
        elif a in _AF_SPENDERS:
            stacks = max(0, stacks - 1)
    return waste, stacks, last_refill_t


def _aetherflow_waste_cause(ctx: AdviceContext
                            ) -> tuple[float, RootCause] | None:
    """Aetherflow refilled while stacks were still banked: the refill wipes
    them, so each is an Energy Drain that never happened. Tail refills are
    skipped (topping the gauge up before the kill can be a net gain)."""
    waste, _stacks, _last = _aetherflow_ledger(ctx)
    dur = float(ctx.fight_duration_s)
    events = [(t, n) for t, n in waste if t <= dur - _AF_WASTE_TAIL_SKIP_S]
    total = sum(n for _t, n in events)
    if total < _AF_WASTE_MIN_STACKS:
        return None
    first_t = events[0][0]
    worst_t, worst_n = max(events, key=lambda e: (e[1], -e[0]))
    value = total * _AF_VALUE_P
    t = TEXT["af_waste"]
    return value, RootCause(
        kind="cascade_burst", ability_id=sd.ENERGY_DRAIN,
        ability_name=_name(sd.ENERGY_DRAIN),
        time_sec=_at(ctx, first_t), measured_p=0.0,
        summary=t["summary"].format(
            total=total, plural="s" if total != 1 else ""),
        prescription=t["prescription"].format(when=_mmss(first_t)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(
                    n=worst_n, plural="s" if worst_n != 1 else ""),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(value=value),
                note=t["total_note"].format(
                    total=total, sp="s" if total != 1 else "",
                    count=len(events),
                    rp="s" if len(events) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["aetherflow"]])


def _aetherflow_stranded_cause(ctx: AdviceContext
                               ) -> tuple[float, RootCause] | None:
    """The WHOLE gauge dead at fight end with time to have spent it: three
    stacks that went neither into healing nor into Energy Drain. Located at the
    refill that filled them."""
    _waste, stacks, last_refill_t = _aetherflow_ledger(ctx)
    if stacks < _AF_STRANDED_MIN_STACKS or last_refill_t is None:
        return None
    dur = float(ctx.fight_duration_s)
    if last_refill_t > dur - _AF_STRANDED_TAIL_GUARD_S:
        return None
    value = stacks * _AF_VALUE_P
    t = TEXT["af_stranded"]
    return value, RootCause(
        kind="cascade_lost_use", ability_id=sd.ENERGY_DRAIN,
        ability_name=_name(sd.ENERGY_DRAIN),
        time_sec=_at(ctx, last_refill_t), measured_p=0.0,
        summary=t["summary"].format(n=stacks),
        prescription=t["prescription"].format(value=value),
        evidence=[EvidenceRow(
            k="Stacks",
            v=t["stacks_v"].format(n=stacks),
            note=t["stacks_note"].format(when=_mmss(last_refill_t)))],
        resources=[GAUGE_TEXT["aetherflow"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """SCH probe set. Deterministic; no ProbeItems. RootCauses are ordered by
    descending ledger value (stable tie-break on ability id then time) — the
    priority order the orchestrator's first-in-segment-wins matching
    consumes."""
    weighted: list[tuple[float, RootCause]] = []
    weighted.extend(_cooldown_drift_causes(ctx))
    for got in (_baneful_lost_cause(ctx),
                _aetherflow_waste_cause(ctx),
                _aetherflow_stranded_cause(ctx)):
        if got is not None:
            weighted.append(got)
    weighted.sort(key=lambda w: (-w[0], w[1].ability_id, w[1].time_sec))
    return [], [c for _v, c in weighted]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
