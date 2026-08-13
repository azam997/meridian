"""Dancer deep-advice pack (`Job.advice_probes`).

The DNC probe set, following the Machinist registry pattern. Causes only — DNC
has no card enrichment as measurable as MCH's window-shift probe, so the pack
ships `RootCause` candidates for the cascade re-attribution, all deterministic
ledger walks over the delivered cast stream:

* **Cooldown drift** that cost an end-of-fight use of Standard Step (the
  Finishing Move charge-sharing quirk mirrored — a Finishing Move cast consumes
  the shared Standard Step cooldown, exactly as `data.CHARGE_SHARING` and the
  simulator's `apply_cast` say), Technical Step, Flourish or Devilment.
* **Dropped burst follow-ups**: a Devilment whose Starfall Dance never fired
  inside its 20s window, or a Technical Finish whose Tillana never fired inside
  its 30s window. Deterministic from the cast stream (the granting cast and the
  consumer are both real casts); no existing card owns these — `RNG_PROC_IDS`
  excludes them from the missed-cast diff and the ProcsAspect only tracks the
  four buff-stream procs.
* **Dance of the Dawn skipped for Saber Dance**: a Saber Dance inside a
  Devilment window while Dance of the Dawn stayed unused proves 50 esprit was
  in hand and went to the 540p button instead of the 1000p one.

What is deliberately NOT here: esprit / feather overcap ledgers. DNC's two
gauges are budgets in this model (party-fed esprit, ~50% RNG feathers — see
`data.py`'s module docstring), so gauge generation is invisible in the player's
cast stream and any overcap ledger would be guesswork. Silence beats a false
positive. Silken / Fan Dance proc waste is already carded by the ProcsAspect.

`measured_p` stays 0 on every cause — the orchestrator prices causes from each
cascade segment's unexplained loss. ALL user-facing copy lives in `TEXT` /
`GAUGE_TEXT` / the `_FOLLOWUPS` noun fragments below — improving the feedback
wording is a data edit, never a logic change. `GAUGE_TEXT` is an allowlist:
sim-state fields without an entry (combo_step, in_dance, the proc expiry
clocks…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.dancer import data as dd

# Flourishing Starfall is a 20s window (mirrors the simulator's private
# `_STARFALL_DURATION_S`), unlike the 30s `PROC_DURATION_S` the other
# follow-ups get.
_STARFALL_WINDOW_S = 20.0
# One GCD of log-timing slack before a follow-up is called dropped, so a
# consumer landing right on the model's expiry edge never reads as a drop.
_FOLLOWUP_GRACE_S = 2.5

# Follow-up families: (source_id, source noun, consumer_id, window_s, value_p).
# Sources sit on 120s cooldowns, so windows never overlap and a plain
# any-consumer-in-window check is an exact one-to-one match.
_FOLLOWUPS: tuple[tuple[int, str, int, float, int], ...] = (
    (dd.DEVILMENT, "Devilment", dd.STARFALL_DANCE,
     _STARFALL_WINDOW_S, dd.POTENCIES[dd.STARFALL_DANCE]),
    (dd.TECHNICAL_FINISH, "Technical Finish", dd.TILLANA,
     dd.PROC_DURATION_S, dd.POTENCIES[dd.TILLANA]),
)
# Dance of the Dawn skipped for Saber Dance: the premium lost per swap.
_DAWN_PREMIUM_P = dd.POTENCIES[dd.DANCE_OF_THE_DAWN] - dd.POTENCIES[dd.SABER_DANCE]


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
    "dropped_followup": {
        "summary": "{name} dropped after {source}, {count} window{plural} unused",
        "prescription": ("Use {name} inside the {window:.0f}s after {source}. "
                         "The {source} at {when} never got its {name}; each "
                         "drop costs ~{value:.0f}p."),
        "dropped_v": "{count} of {grants}",
        "dropped_note": "{source} windows with no {name} before they faded",
        "first_v": "{when}",
        "first_note": "the first window that went unused",
    },
    "dawn_swap": {
        "summary": ("Dance of the Dawn skipped for Saber Dance, "
                    "{count} window{plural}"),
        "prescription": ("Spend the first 50 esprit after Devilment on Dance "
                         "of the Dawn here. After the Devilment at {when} a "
                         "Saber Dance took the esprit instead; the swap "
                         "costs ~{value:.0f}p."),
        "swap_v": "{count} window{plural}",
        "swap_note": ("Saber Dance used here while Dance of the Dawn stayed "
                      "unused"),
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). The three fields are REMAINING budget counts on
# the sim state, so "over ideal" means spends the ideal line had already fired
# were still sitting in hand. Running lean (under) is not a mistake by itself:
# front-loading spends is legitimate, so under_note stays None everywhere.
GAUGE_TEXT: dict[str, GaugeText] = {
    "procs_remaining": GaugeText(
        label="Silken procs", short="SILK",
        over_note="Reverse Cascade and Fountainfall casts ran behind here",
        under_note=None,
        min_delta=2.0),
    "feathers_remaining": GaugeText(
        label="Feathers", short="FTHR",
        over_note="Fan Dances were ready",
        under_note=None,
        min_delta=2.0),
    "sabers_remaining": GaugeText(
        label="Saber Dances", short="SABR",
        over_note="esprit spends were ready",
        under_note=None,
        min_delta=2.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlaps(a: float, b: float,
              windows: list[tuple[float, float]]) -> bool:
    """Does [a, b] overlap any (start, end) window at all?"""
    return any(s < b and a < e for s, e in windows or [])


def _overlap_total(a: float, b: float,
                   windows: list[tuple[float, float]]) -> float:
    """Total length of [a, b] covered by the (start, end) windows."""
    return sum(max(0.0, min(b, e) - max(a, s)) for s, e in windows or [])


def _cooldown_drift_causes(ctx: AdviceContext) -> list[tuple[float, RootCause]]:
    """A recast-gated cooldown the sim fit more of than the player cast, with
    the drift ledger that shows where the use was lost. Two fairness rules
    mirror the sim's own bookkeeping:

    * Charge sharing (`data.CHARGE_SHARING`): Finishing Move consumes the
      Standard Step cooldown, so Finishing Move casts count as Standard Step
      consumptions on BOTH sides of the ledger — otherwise every Flourish
      window reads as fake Standard Step drift.
    * Downtime discount: the stretch of a gap where the cooldown was ready but
      the boss was gone (`ctx.downtime_windows` inside [ready, next cast]) is
      subtracted — the player could not have pressed into nothing.
    Gaps overlapping a death window are skipped entirely (deaths are priced by
    their own card)."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for cd_id in sorted(dd.COOLDOWNS):
        recast, _ch = dd.COOLDOWNS[cd_id]
        consume_ids = {cd_id} | {k for k, v in dd.CHARGE_SHARING.items()
                                 if v == cd_id}
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume_ids and t >= 0)
        player_n = len(times)
        ideal_n = sum(ideal_counts.get(a, 0) for a in consume_ids)
        deficit = ideal_n - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            if _overlaps(a, b, ctx.death_windows):
                continue
            over = (b - a) - recast
            if over <= 0:
                continue
            over -= _overlap_total(a + recast, b, ctx.downtime_windows)
            if over <= 0:
                continue
            drift_total += over
            if over > worst[0]:
                worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(cd_id)
        value = deficit * dd.COOLDOWN_VALUE_P.get(cd_id, 0)
        t = TEXT["cooldown_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=cd_id, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=dd.COOLDOWN_VALUE_P.get(cd_id, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n, ideal=ideal_n),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(recasts=drift_total / recast)),
            ])))
    return out


def _dropped_windows(ctx: AdviceContext, source_id: int, consumer_id: int,
                     window_s: float) -> tuple[list[float], int]:
    """(grant times whose window went unconsumed, total judged grants) for one
    follow-up family. A grant is only judged when its full window fits inside
    the fight (a kill-truncated window is never a drop) and touches neither a
    death window nor downtime (the death card / the fight own those)."""
    grants = sorted(t for t, a in ctx.norm_casts if a == source_id and t >= 0)
    consumed = sorted(t for t, a in ctx.norm_casts
                      if a == consumer_id and t >= 0)
    dropped: list[float] = []
    judged = 0
    for g in grants:
        end = g + window_s
        if end > ctx.fight_duration_s:
            continue
        if _overlaps(g, end, ctx.death_windows):
            continue
        if _overlaps(g, end, ctx.downtime_windows):
            continue
        judged += 1
        if not any(g < c <= end + _FOLLOWUP_GRACE_S for c in consumed):
            dropped.append(g)
    return dropped, judged


def _dropped_followup_causes(ctx: AdviceContext) -> list[tuple[float, RootCause]]:
    """Starfall Dance / Tillana grants that expired unused. Both are use-or-lose
    GCDs granted by a real cast (Devilment / Technical Finish), so a window with
    no consumer is a whole button lost — deterministic, and owned by no other
    card (RNG_PROC_IDS keeps them out of the missed-cast diff)."""
    out: list[tuple[float, RootCause]] = []
    for source_id, source, consumer_id, window_s, value_p in _FOLLOWUPS:
        dropped, judged = _dropped_windows(ctx, source_id, consumer_id, window_s)
        if not dropped:
            continue
        name = _name(consumer_id)
        n = len(dropped)
        t = TEXT["dropped_followup"]
        out.append((float(n * value_p), RootCause(
            kind="cascade_lost_use", ability_id=consumer_id,
            ability_name=name,
            time_sec=round(dropped[0], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, source=source, count=n,
                plural="s" if n != 1 else ""),
            prescription=t["prescription"].format(
                name=name, source=source, window=window_s,
                when=_mmss(dropped[0]), value=value_p),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["dropped_v"].format(count=n, grants=judged),
                    note=t["dropped_note"].format(source=source, name=name)),
                EvidenceRow(
                    k="First",
                    v=t["first_v"].format(when=_mmss(dropped[0])),
                    note=t["first_note"]),
            ])))
    return out


def _dawn_swap_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Dance of the Dawn skipped while a Saber Dance inside the same Devilment
    window proves 50 esprit was in hand: the esprit went to the 540p button
    instead of the 1000p one. Windows with no Saber Dance stay silent — with
    party-fed esprit invisible in the cast stream, only a spend proves the
    resource existed."""
    devs = sorted(t for t, a in ctx.norm_casts if a == dd.DEVILMENT and t >= 0)
    dawns = sorted(t for t, a in ctx.norm_casts
                   if a == dd.DANCE_OF_THE_DAWN and t >= 0)
    sabers = sorted(t for t, a in ctx.norm_casts
                    if a == dd.SABER_DANCE and t >= 0)
    swapped: list[float] = []
    for g in devs:
        end = g + dd.PROC_DURATION_S
        if end > ctx.fight_duration_s:
            continue
        if _overlaps(g, end, ctx.death_windows):
            continue
        if _overlaps(g, end, ctx.downtime_windows):
            continue
        if any(g < c <= end + _FOLLOWUP_GRACE_S for c in dawns):
            continue                              # Dawn was used
        if not any(g < c <= end for c in sabers):
            continue                              # esprit unproven -> silent
        swapped.append(g)
    if not swapped:
        return None
    n = len(swapped)
    t = TEXT["dawn_swap"]
    plural = "s" if n != 1 else ""
    return (float(n * _DAWN_PREMIUM_P), RootCause(
        kind="cascade_lost_use", ability_id=dd.DANCE_OF_THE_DAWN,
        ability_name=_name(dd.DANCE_OF_THE_DAWN),
        time_sec=round(swapped[0], 1), measured_p=0.0,
        summary=t["summary"].format(count=n, plural=plural),
        prescription=t["prescription"].format(
            when=_mmss(swapped[0]), value=_DAWN_PREMIUM_P),
        evidence=[EvidenceRow(
            k="Windows",
            v=t["swap_v"].format(count=n, plural=plural),
            note=t["swap_note"])],
        resources=[GAUGE_TEXT["sabers_remaining"]]))


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """DNC probe set. No ProbeItems (nothing here measures an existing card
    more precisely than its static copy). RootCauses are sorted by descending
    total lost value — that order is the priority the orchestrator's
    first-in-segment-wins matching consumes — with (ability_id, time) as the
    deterministic tie-break."""
    weighted: list[tuple[float, RootCause]] = []
    weighted.extend(_cooldown_drift_causes(ctx))
    weighted.extend(_dropped_followup_causes(ctx))
    dawn = _dawn_swap_cause(ctx)
    if dawn is not None:
        weighted.append(dawn)
    weighted.sort(key=lambda r: (-r[0], r[1].ability_id, r[1].time_sec))
    return [], [c for _v, c in weighted]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
