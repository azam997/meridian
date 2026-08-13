"""Summoner deep-advice pack (`Job.advice_probes`).

The SMN probe set, on the Machinist registry pattern (the reference
implementation in `jobs/machinist/advice.py`). No `ProbeItem`s — SMN has no
measurable window-shift analog (the demi windows are self-made, not
buff-catching) — so the pack ships `RootCause`s only, all deterministic
ledger walks over the delivered cast stream:

* **Cooldown drift** over the three recast-gated pools (the shared 60s demi
  summon, Energy Drain, Searing Light) that cost an end-of-fight use.
  Downtime and death overlap are subtracted from every inter-use gap, so the
  sim's own demi downtime hold and a raise-wait never read as drift.
* **Arcanum gems overwritten** — each demi summon grants all three gems and
  REPLACES whatever is still held (the simulator's `apply_cast` rule), so a
  primal phase left unrun inside the cycle is outright lost.
* **Aetherflow overcap** — Energy Drain refilled while stacks were still
  live in the gauge; each wasted stack is a Necrotize that never happened.
* **Aetherflow stranded** at the kill with time left to spend it.

`measured_p` stays 0 on every cause — the orchestrator prices each from its
cascade segment's unexplained loss. Causes are ordered by descending ledger
value (the orchestrator's first-in-segment-wins priority).

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` / the word tables below —
improving the feedback wording is a data edit here, never a logic change.
`GAUGE_TEXT` is an allowlist: sim-state fields without an entry (demi_idx,
active_demi, attunement, attunement_rite, instant_this_slot…) never surface
in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.summoner import data as sd

# Ledger thresholds / guards (internal tuning, never user-facing).
_DRIFT_FLOOR_FRAC = 0.5           # of one recast — MCH's tool-drift noise floor
_GEM_DOWNTIME_SKIP_S = 8.0        # the cycle ate real downtime; the sim may waste too
_GEM_DEATH_SKIP_S = 3.0           # deaths are priced by their own card
_AF_OVERCAP_TAIL_SKIP_S = 15.0    # a tail refill can be a net gain; stay silent
_AF_STRANDED_TAIL_GUARD_S = 10.0  # room to weave the spare Necrotize

_AF_GENERATORS = frozenset(sd.AETHERFLOW_GAUGE.generators)
_AF_SPENDERS = frozenset(sd.AETHERFLOW_GAUGE.spenders)
_AF_VALUE_P = float(sd.AETHERFLOW_GAUGE.value_p_per_unit)

# Recast pool -> every consumer that spends the shared recast (the demi pool
# is keyed on Solar Bahamut; Bahamut / Phoenix spend it via CHARGE_SHARING,
# Energy Siphon spends Energy Drain's).
_POOL_CONSUMERS: dict[int, frozenset[int]] = {
    pool: frozenset({pool} | {c for c, src in sd.CHARGE_SHARING.items()
                              if src == pool})
    for pool in sd.COOLDOWNS
}

# Primal summon -> the gem sim-state field it spends (also the GAUGE_TEXT key).
_GEM_FIELDS: dict[int, str] = {
    sd.SUMMON_IFRIT_II:  "ruby_gem",
    sd.SUMMON_TITAN_II:  "topaz_gem",
    sd.SUMMON_GARUDA_II: "emerald_gem",
}


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("here") so holding for buffs elsewhere stays legitimate. Run new dialogue
# copy by the user before shipping it.
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
    },
    "gem_waste": {
        "summary": ("Arcanum gems overwritten unused, {count} primal "
                    "phase{plural} lost"),
        "prescription": ("Run the Garuda, Ifrit and Titan phases before the "
                         "next demi. First overwrite at {when}; a demi "
                         "summon replaces any gems still held."),
        "first_v": "{names}",
        "first_note": "still unstarted when the demi landed at {when}",
        "total_v": "~{value:.0f}p",
        "total_note": "across {count} lost phase{plural}",
    },
    "af_overcap": {
        "summary": ("Energy Drain refilled over live stacks, {total} "
                    "Necrotize cast{plural} lost"),
        "prescription": ("Weave out both Necrotize casts before Energy Drain "
                         "comes back here. First refill over live stacks at "
                         "{when}; each stack still in the gauge when it "
                         "lands is a Necrotize gone."),
        "worst_v": "{n} stack{plural}",
        "worst_note": "lost to the refill at {when}",
        "total_v": "~{value:.0f}p",
        "total_note": "{total} stack{sp} wasted across {count} "
                      "refill{rp}",
    },
    "af_stranded": {
        "summary": "Aetherflow left with {n} stack{plural} at the kill",
        "prescription": ("Weave the remaining Necrotize cast{plural} before "
                         "the pull ends (~{value:.0f}p)."),
        "stacks_v": "{n} unspent",
        "stacks_note": "from the Energy Drain at {when} with time left to "
                       "spend",
    },
}

# Per-pool copy for the drift cause: pool key -> (row label / summary
# subject, the lost-unit noun, the opening imperative).
_CD_WORDS: dict[int, tuple[str, str, str]] = {
    sd.SUMMON_SOLAR_BAHAMUT: (
        "Demi summons", "window",
        "Summon the next demi the moment the shared 60s timer ends."),
    sd.ENERGY_DRAIN: (
        "Energy Drain", "use",
        "Use Energy Drain the moment it comes back."),
    sd.SEARING_LIGHT: (
        "Searing Light", "use",
        "Use Searing Light the moment it comes back."),
}

# Primal display names for the gem-waste evidence row.
_PRIMAL_WORDS: dict[int, str] = {
    sd.SUMMON_IFRIT_II:  "Ifrit",
    sd.SUMMON_TITAN_II:  "Titan",
    sd.SUMMON_GARUDA_II: "Garuda",
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# Every key is a public scalar field of simulator.SimState. Holding LESS than
# the ideal line is never a mistake for these, so under_note stays None.
GAUGE_TEXT: dict[str, GaugeText] = {
    "aetherflow": GaugeText(
        label="Aetherflow", short="AF",
        over_note="stacks sat unspent that Necrotize could have used",
        under_note=None,
        min_delta=1.0),
    "further_ruin": GaugeText(
        label="Further Ruin", short="R4",
        over_note="a Ruin IV sat ready and unused",
        under_note=None,
        min_delta=1.0),
    "searing_flash_ready": GaugeText(
        label="Searing Flash", short="FLSH",
        over_note="Searing Flash was ready",
        under_note=None,
        min_delta=1.0),
    "enkindle_ready": GaugeText(
        label="Enkindle", short="ENK",
        over_note="the window's Enkindle went unspent",
        under_note=None,
        min_delta=1.0),
    "flare_ready": GaugeText(
        label="Demi flare", short="FLR",
        over_note="the window's Deathflare or Sunflare went unspent",
        under_note=None,
        min_delta=1.0),
    "ruby_gem": GaugeText(
        label="Ruby Arcanum", short="RUBY",
        over_note="the Ifrit phase had not run yet",
        under_note=None,
        min_delta=1.0),
    "topaz_gem": GaugeText(
        label="Topaz Arcanum", short="TPZ",
        over_note="the Titan phase had not run yet",
        under_note=None,
        min_delta=1.0),
    "emerald_gem": GaugeText(
        label="Emerald Arcanum", short="EMLD",
        over_note="the Garuda phase had not run yet",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _overlap_s(a: float, b: float, windows) -> float:
    """Total overlap of [a, b] with a window list (downtime / deaths)."""
    total = 0.0
    for s, e in windows or ():
        total += max(0.0, min(b, float(e)) - max(a, float(s)))
    return total


def _cooldown_drift_causes(ctx: AdviceContext
                           ) -> list[tuple[float, RootCause]]:
    """A recast pool the sim fit more uses of than the player cast, with the
    drift ledger that shows where the use was lost. Consumer sets come from
    CHARGE_SHARING (the demi pool counts Bahamut / Phoenix, Energy Drain
    counts Energy Siphon) on BOTH sides of the count. Downtime and death
    overlap are subtracted from every gap, so the sim's own demi downtime
    hold and a raise-wait never read as drift."""
    out: list[tuple[float, RootCause]] = []
    for pool in sorted(sd.COOLDOWNS):
        recast, _ch = sd.COOLDOWNS[pool]
        consume = _POOL_CONSUMERS[pool]
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume and t >= 0)
        player_n = len(times)
        ideal_n = sum(1 for _t, a in ctx.idealized if a in consume)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            over = ((b - a) - recast
                    - _overlap_s(a, b, ctx.downtime_windows)
                    - _overlap_s(a, b, ctx.death_windows))
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * _DRIFT_FLOOR_FRAC:
            continue
        label, noun, action = _CD_WORDS[pool]
        per_use = sd.COOLDOWN_VALUE_P.get(pool, 0)
        value = float(deficit * per_use)
        t = TEXT["cd_drift"]
        out.append((value, RootCause(
            kind="cascade_lost_use", ability_id=pool,
            ability_name=_name(pool),
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                label=label, drift=drift_total, deficit=deficit, noun=noun,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                action=action, when=_mmss(worst[1]), worst=worst[0],
                noun=noun, value=per_use),
            evidence=[
                EvidenceRow(
                    k=label,
                    v=t["count_v"].format(player=player_n, ideal=ideal_n),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return out


def _gem_waste_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Arcanum gems still held when the next demi summon landed: the summon
    replaces them (the simulator's `apply_cast` rule), so each is an unrun
    primal phase outright lost. Cycles that ate real downtime or a death are
    skipped (the sim itself can waste gems there / the death card owns it);
    gems held at the fight tail are never counted (the ideal line strands
    what does not fit too)."""
    held: set[int] = set()
    prev_demi_t: float | None = None
    events: list[tuple[float, frozenset[int]]] = []
    for t, a in sorted(ctx.norm_casts):
        if t < 0:
            continue
        if a in sd.DEMI_SUMMON_IDS:
            if held and prev_demi_t is not None:
                clean = (
                    _overlap_s(prev_demi_t, t, ctx.downtime_windows)
                    <= _GEM_DOWNTIME_SKIP_S
                    and _overlap_s(prev_demi_t, t, ctx.death_windows)
                    <= _GEM_DEATH_SKIP_S)
                if clean:
                    events.append((t, frozenset(held)))
            held = set(_GEM_FIELDS)
            prev_demi_t = t
        elif a in _GEM_FIELDS:
            held.discard(a)
    if not events:
        return None
    count = sum(len(w) for _t, w in events)
    value = float(sum(sd.PRIMAL_PHASE_VALUE_P[p]
                      for _t, w in events for p in w))
    first_t, first_wasted = events[0]
    ordered = sorted(first_wasted,
                     key=lambda p: (-sd.PRIMAL_PHASE_VALUE_P[p], p))
    lead = ordered[0]
    t = TEXT["gem_waste"]
    return value, RootCause(
        kind="cascade_lost_use", ability_id=lead,
        ability_name=_name(lead),
        time_sec=round(first_t, 1), measured_p=0.0,
        summary=t["summary"].format(
            count=count, plural="s" if count != 1 else ""),
        prescription=t["prescription"].format(when=_mmss(first_t)),
        evidence=[
            EvidenceRow(
                k="Phases",
                v=t["first_v"].format(
                    names=_join_names([_PRIMAL_WORDS[p] for p in ordered])),
                note=t["first_note"].format(when=_mmss(first_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(value=value),
                note=t["total_note"].format(
                    count=count, plural="s" if count != 1 else "")),
        ],
        resources=[GAUGE_TEXT[_GEM_FIELDS[p]] for p in ordered[:2]])


def _aetherflow_ledger(ctx: AdviceContext
                       ) -> tuple[list[tuple[float, int]], int, float | None]:
    """(overflow_events, final_stacks, last_generator_t) from one walk of the
    delivered stream. A death zeroes the running stacks (the game removes
    Aetherflow on death), so a post-raise Energy Drain never reads as an
    overcap and post-death stacks never read as stranded."""
    merged: list[tuple[float, int, int]] = [
        (float(s), 0, 0) for s, _e in (ctx.death_windows or ())]
    merged += [(float(t), 1, a) for t, a in sorted(ctx.norm_casts) if t >= 0]
    merged.sort(key=lambda e: (e[0], e[1]))
    stacks = 0
    overflow: list[tuple[float, int]] = []
    last_gen_t: float | None = None
    for t, prio, a in merged:
        if prio == 0:
            stacks = 0
        elif a in _AF_GENERATORS:
            if stacks > 0:
                overflow.append((t, stacks))
            stacks = sd.AETHERFLOW_CAP
            last_gen_t = t
        elif a in _AF_SPENDERS:
            stacks = max(0, stacks - 1)
    return overflow, stacks, last_gen_t


def _aetherflow_overcap_cause(ctx: AdviceContext
                              ) -> tuple[float, RootCause] | None:
    """Energy Drain refilled while stacks were still live: the refill wipes
    them, so each is a Necrotize that never happened. Tail refills are
    skipped (squeezing fresh stacks in before the kill can be a net gain)."""
    overflow, _stacks, _last = _aetherflow_ledger(ctx)
    dur = float(ctx.fight_duration_s)
    events = [(t, n) for t, n in overflow
              if t <= dur - _AF_OVERCAP_TAIL_SKIP_S]
    total = sum(n for _t, n in events)
    if total < 1:
        return None
    first_t = events[0][0]
    worst_t, worst_n = max(events, key=lambda e: (e[1], -e[0]))
    value = total * _AF_VALUE_P
    t = TEXT["af_overcap"]
    return value, RootCause(
        kind="cascade_burst", ability_id=sd.ENERGY_DRAIN,
        ability_name=_name(sd.ENERGY_DRAIN),
        time_sec=round(first_t, 1), measured_p=0.0,
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
    """Stacks dead in the gauge at fight end with time to have spent them
    (each a free oGCD Necrotize). Located at the last Energy Drain."""
    _overflow, stacks, last_gen_t = _aetherflow_ledger(ctx)
    if stacks < 1 or last_gen_t is None:
        return None
    dur = float(ctx.fight_duration_s)
    if last_gen_t > dur - _AF_STRANDED_TAIL_GUARD_S:
        return None
    value = stacks * _AF_VALUE_P
    t = TEXT["af_stranded"]
    return value, RootCause(
        kind="cascade_lost_use", ability_id=sd.NECROTIZE,
        ability_name=_name(sd.NECROTIZE),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(
            n=stacks, plural="s" if stacks != 1 else ""),
        prescription=t["prescription"].format(
            plural="s" if stacks != 1 else "", value=value),
        evidence=[EvidenceRow(
            k="Stacks",
            v=t["stacks_v"].format(n=stacks),
            note=t["stacks_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["aetherflow"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """SMN probe set. Deterministic; no ProbeItems. RootCauses are ordered by
    descending ledger value (stable tie-break on ability id then time) — the
    priority order the orchestrator's first-in-segment-wins matching
    consumes."""
    weighted: list[tuple[float, RootCause]] = []
    weighted.extend(_cooldown_drift_causes(ctx))
    for got in (_gem_waste_cause(ctx),
                _aetherflow_overcap_cause(ctx),
                _aetherflow_stranded_cause(ctx)):
        if got is not None:
            weighted.append(got)
    weighted.sort(key=lambda w: (-w[0], w[1].ability_id, w[1].time_sec))
    return [], [c for _v, c in weighted]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
