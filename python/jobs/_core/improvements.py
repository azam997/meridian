"""Potential Improvements — located, actionable suggestions from the sim.

Diffs the player's actual casts against the idealized sim timeline and emits
concrete, clickable suggestions ("Missed Drill — fit one around 4:32"). The
sim is the source of truth: its idealized cast counts are the realistic,
phantom-free achievable maximum (a real simulated rotation, not a cooldown
formula), so a cast the idealized fits that the player didn't is a genuine
missed opportunity — and the idealized cast's time tells the user *where* it
belonged (the UI links it to the timeline).

Job-agnostic. Valuation respects tool fungibility: a missed GCD tool costs
only its potency *above the filler GCD* that backfills the slot
(`JobData.filler_gcd_potency`); a missed oGCD costs its full value (it
doesn't displace a GCD). Buff windows scale the value at the cast's time.

Reconciliation model
--------------------
For a sim-backed job the *only* ground-truth number for "where the potency
went" is the gap `recoverable = idealized_strict - delivered`. Every card is an
**attribution of a slice of that gap**, so the cards must decompose it, never
sum past it. Two failure modes this guards against:

  * Double-counting — the sim plays a full fight and counts casts, so a mistake
    that actually dropped a cast is already in the missed-cast diff; adding a
    heuristic aspect's independent estimate on top counts the same loss twice.
  * Phantom losses — pricing *ordering* (opener slot order) or *throughput
    enablers* (Hypercharge, Wildfire, Barrel Stabilizer, Reassemble — zero
    direct potency in the scoring model) at their notional value, without
    subtracting the potency the slot actually delivered.

So we price only **direct damage pinnable to a cast** (tools net of the filler
that backfills them, Double Check / Checkmate, overcap waste, Reassemble
misuse), emit ordering / enabler findings as **zero-priced diagnostics** (the
*where*, no double-counted number), and `reconcile_to_budget` bounds the priced
total by `recoverable` — folding any remainder into one "other" residual so the
panel always sums to the measured gap.
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field

from . import ability_metadata
from .buff_windows import BuffWindow, multiplier_at, multiplier_intervals
from .job import JobData


@dataclass
class Improvement:
    kind: str             # "missed_cast" (v1)
    ability_id: int
    ability_name: str
    time_s: float         # where it belonged — the UI links this to the timeline
    lost_potency: float
    summary: str
    # Optional breakdown for aggregate cards (the grouped "×N" rows, the idle /
    # clip totals, and the "Other" residual). Each child is a located,
    # individually-priced contributor the UI reveals in an expandable dropdown.
    # Empty for leaf cards. `asdict`/`_camelize` serialize these recursively;
    # the UI renders one level (children of the clicked card).
    children: list["Improvement"] = field(default_factory=list)
    # Short imperative advice — what to change next pull, templated from the
    # numbers the producer already has (how short, by how many seconds, against
    # what). None ⇒ the UI renders nothing (never filler copy); the sidecar
    # strips unset ones from the wire. Producers that lack a real rule leave it
    # unset, and job contributors may fill their own (never overwritten).
    prescription: str | None = None


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _unmatched_idealized(ideal_times: list[float],
                         actual_times: list[float]) -> list[float]:
    """Greedily pair each actual cast to its nearest idealized cast; return
    the idealized cast times left unpaired (the genuinely missed ones)."""
    remaining = sorted(ideal_times)
    for a in sorted(actual_times):
        if not remaining:
            break
        j = min(range(len(remaining)), key=lambda i: abs(remaining[i] - a))
        remaining.pop(j)
    return remaining


def compute_missed_cast_improvements(
    actual_casts: list[tuple[float, int]],
    idealized_timeline: list[tuple[float, int]],
    data: JobData,
    buff_intervals: list[tuple[float, float, float]] | None,
    fight_duration_s: float,
    min_potency: float = 150.0,
    enabler_values: dict[int, float] | None = None,
) -> list[Improvement]:
    """For each cooldown the idealized fits more often than the player did, emit
    one item per missed cast, located at the idealized cast time.

    Pricing is by **direct damage only** (`data.potencies`):

      * GCD tools are fungible — the slot backfills with a filler, so only the
        potency *above* `filler_gcd_potency` is truly lost.
      * Damaging oGCDs (Double Check / Checkmate) displace nothing → full value.
      * Enabler / throughput cooldowns with no direct potency (Hypercharge,
        Wildfire, Barrel Stabilizer) are priced at their **sim-derived net
        value** — `enabler_values[aid]`, the potency the idealized rotation
        loses per missed cast (see scoring.enabler_net_values). That already
        nets out the casts they enable, so it neither double-counts nor zeroes
        a genuinely costly miss (a skipped Wildfire is its full payload). With
        no `enabler_values` (or a sub-`min_potency` value) the miss falls back
        to a zero-priced diagnostic — the *where* without a number.

    Priced items below `min_potency` are dropped as noise (enabler misses stay
    as a zero-priced note instead, since skipping one is still worth flagging).
    `buff_intervals` scales priced values when supplied (the sidecar passes
    None for the strict, buff-agnostic panel that matches the headline gap)."""
    enabler_values = enabler_values or {}
    out: list[Improvement] = []
    # RNG-gated abilities (RDM Verfire/Verstone) can't be cast on demand, so a
    # sim/player count mismatch on them isn't a missed cast — drop them.
    abilities = {aid for _t, aid in idealized_timeline
                 if aid in data.cooldowns and aid not in data.rng_proc_ids}
    for aid in abilities:
        ideal_times = sorted(t for t, a in idealized_timeline
                             if a == aid and 0.0 <= t <= fight_duration_s)
        actual_times = sorted(t for t, a in actual_casts
                              if a == aid and t >= 0.0)
        if len(ideal_times) <= len(actual_times):
            continue   # you fit as many as the ideal — nothing missed
        meta = ability_metadata.get_metadata(aid)
        name = meta.name if meta else f"action {aid}"
        is_ogcd = meta.is_ogcd if meta else False
        base = data.potencies.get(aid, 0)
        unmatched = _unmatched_idealized(ideal_times, actual_times)

        if base <= 0:
            # Enabler / throughput cooldown — price at its sim-derived net value
            # if we have one above the noise floor, else a zero-priced note.
            net = enabler_values.get(aid, 0.0)
            priced = net if net >= min_potency else 0.0
            for t_i in unmatched:
                out.append(Improvement(
                    kind="missed_enabler", ability_id=aid, ability_name=name,
                    time_s=t_i, lost_potency=priced,
                    summary=f"Missed {name}, fit one around {_mmss(t_i)}",
                    prescription=f"Keep {name} on cooldown; one more fits "
                                 f"here."))
            continue

        for t_i in unmatched:
            gross = base * multiplier_at(t_i, buff_intervals or [])
            lost = gross if is_ogcd else max(0.0, gross - data.filler_gcd_potency)
            if lost < min_potency:
                continue
            rx = (f"Weave one more {name} around {_mmss(t_i)}. Free "
                  f"weave, ~{lost:.0f}p."
                  if is_ogcd else
                  f"Fit one more {name} around {_mmss(t_i)}. "
                  f"~{lost:.0f}p over a filler GCD.")
            out.append(Improvement(
                kind="missed_cast", ability_id=aid, ability_name=name,
                time_s=t_i, lost_potency=lost,
                summary=f"Missed {name}, fit one around {_mmss(t_i)}",
                prescription=rx))
    out.sort(key=lambda im: -im.lost_potency)
    return out


def _in_any(t: float, windows: list[tuple[float, float]]) -> bool:
    return any(s <= t < e for s, e in windows)


_FLAMETHROWER_ID = 7418


def improvements_from_flamethrower(
    actual_casts: list[tuple[float, int]],
    idealized_timeline: list[tuple[float, int]],
    data: JobData,
    buff_intervals: list[tuple[float, float, float]] | None,
    fight_duration_s: float,
) -> list[Improvement]:
    """A Flamethrower squeeze the idealized fit (at a boss-untargetable edge)
    that the player didn't take. Priced at FULL potency — unlike a normal missed
    GCD it isn't net-of-filler, because the squeeze fills otherwise-dead downtime
    where nothing else can be cast. Small (one tick) and below the usual
    missed-cast floor, but surfaced as its own card since it's a distinct,
    located opportunity. Skipped where it isn't in `data.cooldowns`-land at all
    (Flamethrower is emitted by the sim only at downtime boundaries)."""
    base = data.potencies.get(_FLAMETHROWER_ID, 0)
    if base <= 0:
        return []
    ideal_times = sorted(t for t, a in idealized_timeline
                         if a == _FLAMETHROWER_ID and 0.0 <= t <= fight_duration_s)
    actual_times = sorted(t for t, a in actual_casts
                          if a == _FLAMETHROWER_ID and t >= 0.0)
    if len(ideal_times) <= len(actual_times):
        return []
    meta = ability_metadata.get_metadata(_FLAMETHROWER_ID)
    name = meta.name if meta else "Flamethrower"
    out: list[Improvement] = []
    for t_i in _unmatched_idealized(ideal_times, actual_times):
        out.append(Improvement(
            kind="flamethrower", ability_id=_FLAMETHROWER_ID, ability_name=name,
            time_s=t_i, lost_potency=base * multiplier_at(t_i, buff_intervals or []),
            summary=f"Missed Flamethrower squeeze, fit one around {_mmss(t_i)} "
                    f"as the boss goes untargetable",
            prescription=f"Squeeze {name} into the downtime edge at "
                         f"{_mmss(t_i)}. It ticks while the boss is "
                         f"untargetable."))
    return out


def reviewable_windows_from_idealized(
    display_timeline: list[tuple[float, int]],
    data: JobData,
) -> list[dict]:
    """Situational, ceiling-only squeezes the idealized sim *assumed* but the
    player must confirm were actually possible — currently just the MCH
    Flamethrower downtime-edge tick (the sim can't see movement / mechanics that
    would have made it impossible). Each is rendered as a generic WindowReview on
    the dashboard; denying one drops its potency from the idealized ceiling only.

    Returns the wire shape directly (camelCase, bypasses the camelizer like the
    other hand-built response fields): a list of groups
    `{kind, side, windows:[{id, timeSec, abilityId, potency, title}]}`. The `id`
    is what the frontend's denied-set keys on (`ft@{t:.1f}`, matching the time of
    the corresponding cast on the idealized lane). Empty for jobs/pulls without
    such windows. Frontend prose + icons live in src/views/reviewableWindows.

    Job-isolated like `improvements_from_flamethrower` above — a second job that
    grows a reviewable window adds its own producer here rather than the sidecar
    learning the specifics."""
    base = data.potencies.get(_FLAMETHROWER_ID, 0)
    if base <= 0:
        return []
    meta = ability_metadata.get_metadata(_FLAMETHROWER_ID)
    name = meta.name if meta else "Flamethrower"
    windows = [
        {"id": f"ft@{t:.1f}", "timeSec": float(t), "abilityId": _FLAMETHROWER_ID,
         "potency": float(base), "title": f"{name} opportunity detected"}
        for t, aid in display_timeline
        if aid == _FLAMETHROWER_ID and t >= 0.0
    ]
    if not windows:
        return []
    return [{"kind": "flamethrower", "side": "ceiling", "windows": windows}]


def improvements_from_deaths(
    death_windows: list[tuple[float, float]],
    idealized_timeline: list[tuple[float, int]],
    data: JobData,
    enabler_values: dict[int, float] | None = None,
    buff_intervals: list[tuple[float, float, float]] | None = None,
) -> list[Improvement]:
    """One priced, located card per death.

    A death is the dominant loss it looks like: while dead the player casts
    *nothing*, so the whole idealized rotation over the dead window is lost —
    at **full** value, not net-of-filler (a missed cooldown backfills with a
    filler GCD; a dead slot backfills with nothing). The cost is therefore the
    sum of every idealized cast falling in the window: `data.potencies[aid]`
    for damaging actions, `enabler_values[aid]` for zero-potency throughput
    enablers (Wildfire / Hypercharge / …). That sum is exactly the death's
    share of the recoverable gap (delivered is 0 across the window, idealized
    is full), so it decomposes the gap rather than estimating on top of it.

    The sidecar pairs this with suppression of any missed-cast improvement
    inside a dead window, so the cooldowns lost to the death are counted once
    (here, at full value), never twice."""
    enabler_values = enabler_values or {}
    bi = buff_intervals or None
    out: list[Improvement] = []
    for s, e in death_windows:
        total = 0.0
        gcd_count = 0
        for t, aid in idealized_timeline:
            if not (s <= t < e):
                continue
            base = data.potencies.get(aid, 0)
            if base > 0:
                total += base * (multiplier_at(t, bi) if bi else 1.0)
                meta = ability_metadata.get_metadata(aid)
                if meta is None or not meta.is_ogcd:
                    gcd_count += 1
            else:
                total += enabler_values.get(aid, 0.0)
        dur = e - s
        out.append(Improvement(
            kind="death", ability_id=0, ability_name="",
            time_s=s, lost_potency=total,
            summary=f"Died at {_mmss(s)}: {dur:.0f}s recovering, "
                    f"~{gcd_count} GCDs lost",
            prescription="Survive this hit."))
    return out


def improvements_from_gcd_quality(
    actual_casts: list[tuple[float, int]],
    idealized_timeline: list[tuple[float, int]],
    data: JobData,
    fight_duration_s: float,
    buff_intervals: list[tuple[float, float, float]] | None = None,
) -> list[Improvement]:
    """Filler-composition loss the cooldown missed-cast diff structurally can't
    see (it only walks `data.cooldowns`).

    For the interchangeable high-value *filler* GCDs a job declares in
    `data.filler_quality_gcds` (RDM: the Dualcasted 440s + the enchanted
    combo/finisher chain), compare the player's in-fight count to the idealized
    rotation's and price each shortfall at its potency **above the filler that
    backfills the slot** (`data.filler_gcd_potency`) — the same fungibility basis
    a missed tool uses. Emits ONE aggregate "Filler quality" card whose children
    break the loss down per ability (the *where-ish*: which GCD you under-ran),
    framed as diffuse quality rather than a pinpoint mistake — some of it is free
    instants (Swiftcast / Acceleration) the player legitimately spends on
    movement, so it is never presented as a hard "you should have cast X here."

    Empty `filler_quality_gcds` (the default) → no card; the gap stays in the
    reconcile "Other" residual exactly as before (MCH/RPR/SAM unchanged). Like
    drift it's aggregate (no single time), so it renders non-clickable at
    `time_s = 0`. `reconcile_to_budget` still bounds the panel by the measured
    gap, so this only ever moves potency out of "Other" into a named card."""
    if not data.filler_quality_gcds:
        return []
    children: list[Improvement] = []
    for aid in data.filler_quality_gcds:
        base = data.potencies.get(aid, 0)
        per_unit = max(0.0, base - data.filler_gcd_potency)
        if per_unit <= 0:
            continue   # at/below filler value — no net loss when backfilled
        ideal_n = sum(1 for t, a in idealized_timeline
                      if a == aid and 0.0 <= t <= fight_duration_s)
        actual_n = sum(1 for t, a in actual_casts if a == aid and t >= 0.0)
        shortfall = ideal_n - actual_n
        if shortfall <= 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        name = meta.name if meta else f"action {aid}"
        children.append(Improvement(
            kind="filler", ability_id=aid, ability_name=name, time_s=0.0,
            lost_potency=shortfall * per_unit,
            summary=f"{shortfall}× fewer {name} than the ideal line "
                    f"(−{shortfall * per_unit:.0f}p)"))
    if not children:
        return []
    children.sort(key=lambda x: -x.lost_potency)
    total = sum(c.lost_potency for c in children)
    return [Improvement(
        kind="filler", ability_id=0, ability_name="", time_s=0.0,
        lost_potency=total,
        summary=f"Filler quality: high-value GCDs run below the ideal line "
                f"(−{total:.0f}p) from scattered GCD selection, free instants "
                f"spent on movement, and fewer enchanted combos",
        children=children)]


# --- Folding the heuristic aspects into the same Improvement shape ----------
#
# For jobs with a simulator, missed casts come from the sim-diff above; the
# remaining loss categories (clipping, overcap, alignment, opener) are still
# heuristic, but the UI wants them as one unified, located, ranked list. Each
# converter turns an aspect's `state` into Improvements. Aspects expose
# dataclass instances on their state dicts (see jobs/_aspects/*), so these
# read attributes directly. All are time-located except drift (aggregate per
# ability) — a `time_s <= 0` Improvement is rendered non-clickable by the UI.


def improvements_from_clipping(state: dict) -> list[Improvement]:
    """Up to two aggregate cards from the pacing aspect — *time spent idle* and
    true *GCD clipping* — each located at its worst instance and expandable into
    its individual contributors (the per-pair stretches/clips, located so the UI
    can jump to each). They're separate mistakes with separate fixes, so the
    panel reports them apart rather than as one diffuse "clipping" line."""
    f = state.get("clipping")
    if f is None:
        return []
    eff = getattr(f, "effective_gcd_s", 2.5) or 2.5
    avg = getattr(f, "avg_gcd_potency", 0.0) or 0.0

    def _price(secs: float) -> float:
        return (secs / eff) * avg if eff > 0 else 0.0

    out: list[Improvement] = []

    if getattr(f, "total_idle_s", 0.0) >= 0.5 and f.idle_lost_potency > 0:
        children = [
            Improvement(kind="idle", ability_id=0, ability_name="",
                        time_s=t, lost_potency=_price(secs),
                        summary=f"Idle {secs:.1f}s at {_mmss(t)}")
            for t, secs in f.worst_idle
        ]
        t0 = f.worst_idle[0][0] if f.worst_idle else 0.0
        worst_secs = f.worst_idle[0][1] if f.worst_idle else 0.0
        out.append(Improvement(
            kind="idle", ability_id=0, ability_name="",
            time_s=t0, lost_potency=f.idle_lost_potency,
            summary=f"Time spent idle: {f.total_idle_s:.1f}s "
                    f"({f.idle_lost_gcds:.1f} GCDs)",
            children=children,
            prescription=f"Close the {worst_secs:.1f}s gap at {_mmss(t0)}, "
                         f"your longest."))

    if getattr(f, "total_clip_s", 0.0) >= 0.3 and f.clip_lost_potency > 0:
        children = [
            Improvement(kind="clip", ability_id=0, ability_name="",
                        time_s=t, lost_potency=_price(secs),
                        summary=f"Clipped {secs:.2f}s, {n} oGCDs weaved "
                                f"at {_mmss(t)}")
            for t, secs, n in f.worst_clips
        ]
        t0 = f.worst_clips[0][0] if f.worst_clips else 0.0
        w_secs = f.worst_clips[0][1] if f.worst_clips else 0.0
        w_n = f.worst_clips[0][2] if f.worst_clips else 0
        out.append(Improvement(
            kind="clip", ability_id=0, ability_name="",
            time_s=t0, lost_potency=f.clip_lost_potency,
            summary=f"GCD clipping: {f.total_clip_s:.1f}s "
                    f"({f.clip_lost_gcds:.1f} GCDs) from over-weaving",
            children=children,
            prescription=f"Trim a weave at {_mmss(t0)}. {w_n} "
                         f"oGCD{'s' if w_n != 1 else ''} pushed the next GCD "
                         f"{w_secs:.2f}s late."))

    return out


def minor_pacing_from_clipping(state: dict) -> list[Improvement]:
    """Located idle / clip stretches too small to clear the aggregate pacing
    cards (`improvements_from_clipping`'s 0.5s idle / 0.3s clip gates) but still
    real, time-stamped potency. They'd otherwise vanish into the "Other"
    residual's diffuse remainder; surfaced here as `extra_children` so the
    residual's expandable breakdown lists every located piece we can pin, not
    just sub-floor missed casts.

    Gated to the *sub-card-floor* regime (`< 0.5s` / `< 0.3s` total) so a stretch
    is never shown both here and under its own aggregate idle/clip card — above
    the gate the whole total is owned by that card (kept or folded into Other)."""
    f = state.get("clipping")
    if f is None:
        return []
    eff = getattr(f, "effective_gcd_s", 2.5) or 2.5
    avg = getattr(f, "avg_gcd_potency", 0.0) or 0.0
    if eff <= 0 or avg <= 0:
        return []
    out: list[Improvement] = []
    if 0.0 < getattr(f, "total_idle_s", 0.0) < 0.5:
        for t, secs in getattr(f, "worst_idle", []) or []:
            out.append(Improvement(
                kind="idle", ability_id=0, ability_name="", time_s=t,
                lost_potency=(secs / eff) * avg,
                summary=f"Idle {secs:.1f}s at {_mmss(t)}"))
    if 0.0 < getattr(f, "total_clip_s", 0.0) < 0.3:
        for t, secs, n in getattr(f, "worst_clips", []) or []:
            out.append(Improvement(
                kind="clip", ability_id=0, ability_name="", time_s=t,
                lost_potency=(secs / eff) * avg,
                summary=f"Clipped {secs:.2f}s, {n} oGCDs weaved at {_mmss(t)}"))
    return out


def improvements_from_overcap(state: dict) -> list[Improvement]:
    out: list[Improvement] = []
    for o in state.get("findings", []) or []:
        if o.lost_potency <= 0:
            continue
        out.append(Improvement(
            kind="overcap", ability_id=o.ability_id, ability_name=o.ability_name,
            time_s=o.time_s, lost_potency=o.lost_potency,
            summary=f"{o.gauge[:1].upper()}{o.gauge[1:]} overcap at {_mmss(o.time_s)}: "
                    f"{o.ability_name} (wasted {o.wasted})",
            prescription=f"Spend {o.gauge} before {_mmss(o.time_s)}. "
                         f"{o.ability_name} wasted {o.wasted}."))
    return out


def improvements_from_multi_target(scoring_state: dict) -> list[Improvement]:
    """A credited multi-target pull's AoE under-delivery, as ONE grouped top-level
    card with a child per confirmed window.

    The shortfall is per-TARGET (you cast the splashy ability but hit fewer enemies
    than the optimal line assumes), so it has no missed-cast anchor and would
    otherwise vanish into the "Other" residual — yet on an add fight it's frequently
    the single largest loss. The per-window magnitudes are already computed for the
    WindowReview UI (`ceilingSplash − deliveredSplash`), so this just promotes them
    into the ranked panel. [] unless the pull was multi-target-credited (single-target
    / disclaimed pulls stay byte-identical). The total equals
    `mt_ceiling_delta − mt_delivered_delta`, a slice of the credited
    `idealized_multitarget − delivered_multitarget` gap, so it never over-attributes."""
    if not scoring_state.get("multi_target_credited"):
        return []
    children: list[Improvement] = []
    total = 0.0
    for w in scoring_state.get("multi_target_windows") or []:
        short = float(w.get("ceilingSplash", 0.0)) - float(w.get("deliveredSplash", 0.0))
        if short <= 0:
            continue
        total += short
        s, e = float(w.get("startSec", 0.0)), float(w.get("endSec", 0.0))
        n = int(w.get("targetCount", 2) or 2)
        children.append(Improvement(
            kind="multi_target", ability_id=0, ability_name="",
            time_s=s, lost_potency=short,
            summary=f"{_mmss(s)}–{_mmss(e)}: hit fewer than {n} targets, "
                    f"~{short:.0f}p of cleave left on the table"))
    if not children:
        return []
    children.sort(key=lambda c: -c.lost_potency)
    n = len(children)
    return [Improvement(
        kind="multitarget", ability_id=0, ability_name="",
        time_s=children[0].time_s, lost_potency=total,
        summary=(f"Multi-target: fewer enemies hit than the AoE line allows, "
                 f"across {n} window{'s' if n != 1 else ''}"),
        children=children,
        prescription=(f"Swap to your AoE combo in the multi-target window at "
                      f"{_mmss(children[0].time_s)}; the splash is free."))]


# Wildfire payload: each weaponskill caught in the 10s window adds this much,
# capped at 6 hits. MCH-specific, but Wildfire is an MCH-only mechanic and this
# converter only fires when the (MCH) Wildfire aspect emitted windows.
_WILDFIRE_ID = 2878
_WILDFIRE_HIT_POTENCY = 240.0
_WILDFIRE_HIT_CAP = 6


def improvements_from_wildfire_windows(state: dict) -> list[Improvement]:
    """A Wildfire you *did* cast but underfilled — caught only N of 6
    weaponskills — is a tangible, located loss of `(6 - N) × 240p`, straight
    off the in-game cap (no ref needed). Distinct from a missed Wildfire *cast*
    (counted by the sim-diff): this prices the windows you opened but didn't
    saturate."""
    out: list[Improvement] = []
    for w in state.get("windows", []) or []:
        hits = int(getattr(w, "hits", _WILDFIRE_HIT_CAP))
        short = _WILDFIRE_HIT_CAP - hits
        if short <= 0:
            continue
        t = float(getattr(w, "cast_time_s", 0.0))
        # Inside a Wildfire the weaponskills are Blazing Shots on a ~1.5s
        # cadence, so each missing hit ≈ 1.5s of lead the Hypercharge needed.
        lead = short * 1.5
        nth = {1: "sixth", 2: "fifth", 3: "fourth"}.get(short)
        tail = (f"the {nth} weaponskill lands" if nth else
                f"all {_WILDFIRE_HIT_CAP} weaponskills land")
        # Title carries the one fact ("caught 5 of 6") — the row's time column
        # says when, and "5 of 6" already says how short.
        out.append(Improvement(
            kind="wildfire", ability_id=_WILDFIRE_ID, ability_name="Wildfire",
            time_s=t, lost_potency=short * _WILDFIRE_HIT_POTENCY,
            summary=f"Wildfire caught {hits} of {_WILDFIRE_HIT_CAP} "
                    f"weaponskills",
            prescription=f"Hypercharge ~{lead:.1f}s earlier so {tail} inside "
                         f"the window."))
    return out


# Hypercharge / Overheated: 5 Blazing Shots per cast. MCH-specific, but this
# converter only fires when the (MCH) Hypercharge aspect emitted windows.
_HYPERCHARGE_ID = 17209
_HYPERCHARGE_BLAZING_CAP = 5


def improvements_from_hypercharge_windows(
        state: dict, enabler_values: dict[int, float] | None) -> list[Improvement]:
    """A Hypercharge you *cast* but underfilled — fired only N of 5 Blazing Shots
    — leaks (5 − N) accelerated GCDs. Priced per missing shot at the **sim-derived**
    value of one Blazing Shot in the chain: `enabler_net_values[Hypercharge] ÷ 5`
    (the rotation's marginal value of one Hypercharge, spread across its 5 shots —
    not a hand-tuned constant). Distinct from a *missed* Hypercharge cast (priced
    by the sim-diff); this catches windows you opened but didn't saturate.

    Windows the aspect flagged `cut_short` (curtailed by the kill / downtime /
    death) are skipped — the pilot couldn't have fired more, so it isn't
    actionable feedback. With no enabler value (no simulator), emits nothing."""
    per_shot = (enabler_values or {}).get(_HYPERCHARGE_ID, 0.0) / _HYPERCHARGE_BLAZING_CAP
    if per_shot <= 0:
        return []
    out: list[Improvement] = []
    for w in state.get("windows", []) or []:
        if getattr(w, "cut_short", False):
            continue
        hits = int(getattr(w, "hits", _HYPERCHARGE_BLAZING_CAP))
        short = _HYPERCHARGE_BLAZING_CAP - hits
        if short <= 0:
            continue
        t = float(getattr(w, "cast_time_s", 0.0))
        last = float(getattr(w, "last_shot_s", 0.0) or 0.0)
        rx = (f"Fire all {_HYPERCHARGE_BLAZING_CAP} Blazing Shots before "
              f"Overheat ends. This window stopped {short} short after "
              f"{_mmss(last)}." if last > 0 else
              f"Fire all {_HYPERCHARGE_BLAZING_CAP} Blazing Shots before "
              f"Overheat ends. This window stopped {short} short.")
        out.append(Improvement(
            kind="hypercharge", ability_id=_HYPERCHARGE_ID,
            ability_name="Hypercharge", time_s=t, lost_potency=short * per_shot,
            summary=f"Hypercharge fired {hits} of "
                    f"{_HYPERCHARGE_BLAZING_CAP} Blazing Shots",
            prescription=rx))
    return out


def improvements_from_located(state: dict, kind: str) -> list[Improvement]:
    """Alignment + Reassemble: both expose findings with `time_s`, `summary`,
    `lost_potency` (and a pre-formatted summary)."""
    out: list[Improvement] = []
    for a in state.get("findings", []) or []:
        if a.lost_potency <= 0:
            continue
        out.append(Improvement(
            kind=kind, ability_id=0, ability_name="",
            time_s=a.time_s, lost_potency=a.lost_potency, summary=a.summary))
    return out


def improvements_from_tincture(state: dict) -> list[Improvement]:
    """A tincture you under-used — skipped a pot, or popped one off your burst —
    leaves potency on the table. Priced (strict basis, on the player's OWN
    rotation) as `tincture_loss` from the Scoring aspect: the gain from optimal
    pot placement over the pots actually used, which isolates pot TIMING from
    rotation quality (the actionable part). Empty for jobs that don't pot (no
    tincture_spec) or a well-potted pull (loss ~0)."""
    loss = float(state.get("tincture_loss", 0.0) or 0.0)
    if loss <= 0:
        return []
    n_obs = len(state.get("observed_tincture_windows", []) or [])
    n_opt = int(state.get("tincture_optimal_count", 0) or 0)
    t = float(state.get("tincture_loss_time_s", 0.0) or 0.0)
    if n_obs < n_opt:
        summary = f"Used {n_obs} of {n_opt} tinctures"
        rx = (f"Pot again the moment it's back up. {n_opt - n_obs} more "
              f"fit on cooldown.")
    else:
        summary = "Tincture landed off your burst"
        rx = f"Shift the pot into your burst at {_mmss(t)}."
    return [Improvement(
        kind="tincture", ability_id=0, ability_name="Tincture",
        time_s=t, lost_potency=loss, summary=summary, prescription=rx)]


def diagnostics_from_opener(state: dict) -> list[Improvement]:
    """Opener deviations are *ordering*, not net loss: you cast the same
    abilities in a different order, so over the fight your total potency is
    unchanged. A reorder only costs potency if it pushes a cast off the end of
    the fight — and *that* the sim-diff already counts as a missed cast. So
    opener findings are emitted as zero-priced diagnostic notes (the where, no
    double-counted number). Same-potency reorders aren't even worth a note."""
    out: list[Improvement] = []
    for o in state.get("findings", []) or []:
        if getattr(o, "lost_potency", 0.0) <= 0:
            continue
        em = ability_metadata.get_metadata(getattr(o, "expected_id", 0) or 0)
        am = ability_metadata.get_metadata(getattr(o, "actual_id", 0) or 0)
        expected_name = em.name if em else "the canonical action"
        actual_name = am.name if am else "another action"
        out.append(Improvement(
            kind="opener", ability_id=getattr(o, "expected_id", 0) or 0,
            ability_name=expected_name, time_s=0.0, lost_potency=0.0,
            summary=f"Opener slot #{o.position}: {actual_name} instead of "
                    f"{expected_name} (ordering only)"))
    return out


def improvements_from_drift(state: dict) -> list[Improvement]:
    """Sim-less fallback only. Drift is aggregate per ability (no single
    time), so these emit `time_s = 0` and render non-clickable. Jobs with a
    simulator get located missed-cast Improvements instead."""
    out: list[Improvement] = []
    for d in state.get("findings", []) or []:
        if d.lost_potency <= 0:
            continue
        out.append(Improvement(
            kind="drift", ability_id=d.ability_id, ability_name=d.ability_name,
            time_s=0.0, lost_potency=d.lost_potency,
            summary=f"{d.ability_name} drifted {d.capped_seconds:.1f}s across "
                    f"{d.casts} casts",
            prescription=f"Press {d.ability_name} closer to cooldown."))
    return out


_GROUPABLE_KINDS = frozenset({"missed_cast", "overcap"})


def group_improvements(items: list[Improvement],
                       nit_threshold: float = 300.0) -> list[Improvement]:
    """Collapse clustered small same-(kind, ability) items into one aggregate
    so the panel leads with high-impact items (a 1200p Hypercharge) instead of
    a stack of 180p Double Check / Checkmate oGCD nits. Items at or above
    `nit_threshold`, and kinds that don't stack meaningfully, stay individual.
    Re-sorted by lost potency (desc)."""
    big: list[Improvement] = []
    small: list[Improvement] = []
    for im in items:
        if im.lost_potency >= nit_threshold or im.kind not in _GROUPABLE_KINDS:
            big.append(im)
        else:
            small.append(im)

    groups: dict[tuple[str, int], list[Improvement]] = {}
    for im in small:
        groups.setdefault((im.kind, im.ability_id), []).append(im)

    merged: list[Improvement] = list(big)
    for (kind, aid), grp in groups.items():
        if len(grp) == 1:
            merged.append(grp[0])
            continue
        grp.sort(key=lambda x: x.time_s)
        total = sum(g.lost_potency for g in grp)
        name = grp[0].ability_name or "casts"
        first = grp[0].time_s
        head = (f"Missed {name} ×{len(grp)}" if kind == "missed_cast"
                else f"{name} overcap ×{len(grp)}")
        rx = (f"Fit {len(grp)} more {name}. One belonged around "
              f"{_mmss(first)}, each worth ~{total / len(grp):.0f}p."
              if kind == "missed_cast" else grp[0].prescription)
        merged.append(Improvement(
            kind=kind, ability_id=aid, ability_name=grp[0].ability_name,
            time_s=first, lost_potency=total,
            summary=f"{head}, first around {_mmss(first)}",
            children=list(grp), prescription=rx))

    merged.sort(key=lambda im: -im.lost_potency)
    return merged


def reconcile_to_budget(priced: list[Improvement],
                        recoverable: float,
                        *, residual_floor: float = 60.0,
                        extra_children: list[Improvement] | None = None,
                        ) -> list[Improvement]:
    """Bound the itemized losses by the measured `recoverable` potency
    (idealized_strict − delivered). The cards are *attributions* of that gap, so
    their sum must not exceed it.

    Keep highest-impact items while they fit the budget; stop at the first that
    doesn't (the ranked prefix is what we're most confident pins real potency).
    Absorb the remainder into a single "other" residual so the panel always sums
    to the gap — that residual is the honest home for the diffuse long tail (GCD
    pacing, ability/filler choice, and rotation/enabler timing) we deliberately
    don't price per-cast. Under the strict, buff-agnostic potency basis this gap
    excludes raid-buff alignment (zero here — surfaced in the Party-buff card)
    and crit/direct-hit RNG (efficiency scores potency, not damage), so neither
    belongs in this residual. When our per-item estimates collectively
    *over*-explain the gap (residual double-counting), the tail falls into
    "other" rather than inflating the total.

    Only meaningful for sim-backed jobs (a real `recoverable`); callers without
    a simulator skip it. `recoverable <= 0` ⇒ a clean run, so drop the noise."""
    if recoverable <= 0:
        return []
    ranked = sorted(priced, key=lambda x: -x.lost_potency)
    kept: list[Improvement] = []
    folded: list[Improvement] = []
    spent = 0.0
    for idx, im in enumerate(ranked):
        if spent + im.lost_potency <= recoverable + 1e-6:
            kept.append(im)
            spent += im.lost_potency
        else:
            # The ranked tail that didn't fit the gap — its individual
            # attributions become the residual's expandable breakdown.
            folded = ranked[idx:]
            break
    residual = recoverable - spent
    if residual >= residual_floor:
        # The residual's breakdown = the over-budget ranked tail (folded) plus
        # any sub-card-floor located losses the caller passes in (extra_children).
        # Strip grandchildren — the UI renders one level under "Other".
        children = [
            Improvement(im.kind, im.ability_id, im.ability_name,
                        im.time_s, im.lost_potency, im.summary,
                        prescription=im.prescription)
            for im in [*folded, *(extra_children or [])]
        ]
        children.sort(key=lambda x: -x.lost_potency)
        if children:
            n = len(children)
            summary = (f"Spacing & sequencing: {n} small located loss"
                       f"{'es' if n != 1 else ''} below the listing threshold")
        else:
            summary = "Spacing & sequencing"
        kept.append(Improvement(
            kind="residual", ability_id=0, ability_name="",
            time_s=0.0, lost_potency=residual, summary=summary,
            children=children,
            prescription=("No single cast is at fault; this is spread "
                          "across resource timing, burst spacing, and "
                          "filler picks.")))
    kept.sort(key=lambda im: -im.lost_potency)
    return kept


_CADENCE_FLOOR = 60.0


def _gcd_count(timeline: list[tuple[float, int]]) -> int:
    n = 0
    for t, a in timeline:
        if t < 0:
            continue
        m = ability_metadata.get_metadata(a)
        if m is not None and not m.is_ogcd:
            n += 1
    return n


def improvements_from_cadence(player_tl: list[tuple[float, int]],
                              ideal_tl: list[tuple[float, int]],
                              data: JobData,
                              clipping_state: dict) -> list[Improvement]:
    """A top-level GCD-cadence card: the GCDs the optimal line fit that the player
    didn't, *beyond* the discrete idle gaps the idle card already owns — i.e. loose
    sub-GCD pacing spread across the fight with no single anchor (slightly-late casts,
    conservative weaving). Valued at `filler_gcd_potency` (the marginal slot's net
    worth, matching missed-GCD pricing). [] when negligible. `group_families` then
    folds this together with idle / over-weaving (they lean on each other) into one
    "GCD uptime & pacing" card. Net of the idle GCDs, so no double-count with idle;
    reconcile_to_budget still bounds the panel total to the gap."""
    f = (clipping_state or {}).get("clipping")
    eff = getattr(f, "effective_gcd_s", 0.0) or 0.0
    idle_s = getattr(f, "total_idle_s", 0.0) or 0.0
    idle_gcds = (idle_s / eff) if eff > 0 else 0.0
    deficit = max(0, _gcd_count(ideal_tl) - _gcd_count(player_tl))
    cadence_gcds = max(0.0, deficit - idle_gcds)
    val = cadence_gcds * data.filler_gcd_potency
    if val < _CADENCE_FLOOR:
        return []
    return [Improvement(
        kind="cadence", ability_id=0, ability_name="", time_s=0.0,
        lost_potency=round(val, 1),
        summary=(f"Loose GCD pacing: ~{cadence_gcds:.0f} GCDs of slightly-late "
                 f"casts, no single gap"),
        prescription="Press the next GCD the moment it's ready.")]


# Causally-linked pacing kinds: a gap (idle), an over-weave (clip) and loose
# sub-GCD timing (cadence) are the same failure at different granularities, so they
# read better as one umbrella than scattered across the panel.
_PACING_KINDS = ("idle", "clip", "cadence")


def group_families(cards: list[Improvement]) -> list[Improvement]:
    """Fold the causally-linked pacing cards (idle / over-weaving / loose pacing —
    they lean on each other: a gap or a clip is just a coarser loose-pacing) into a
    single "GCD uptime & pacing" umbrella, so the panel groups by *cause* rather than
    scattering related losses. Ordering stays by potency total — the umbrella competes
    by its sum, and everything is re-sorted. Only groups when >= 2 members are present
    (a lone pacing card stays standalone); distinct major cards (gauge overcap, missed
    cooldowns, tincture) are deliberately left alone. The umbrella renders one level,
    so members' own sub-stretches are dropped — each keeps a jump to its worst
    instance via its `time_s`."""
    members = [c for c in cards if c.kind in _PACING_KINDS]
    if len(members) < 2:
        return cards
    rest = [c for c in cards if c.kind not in _PACING_KINDS]
    members.sort(key=lambda c: -c.lost_potency)
    total = sum(c.lost_potency for c in members)
    kids = [Improvement(c.kind, c.ability_id, c.ability_name, c.time_s,
                        c.lost_potency, c.summary,
                        prescription=c.prescription) for c in members]
    present = {c.kind for c in members}
    causes = []
    if "idle" in present:
        causes.append("idle gaps")
    if "cadence" in present:
        causes.append("loose GCD timing")
    if "clip" in present:
        causes.append("over-weaving")
    lead = ", ".join(causes[:-1]) + (" and " + causes[-1] if len(causes) > 1 else
                                     (causes[0] if causes else "loose pacing"))
    top_rx = kids[0].prescription
    umbrella = Improvement(
        kind="pacing", ability_id=0, ability_name="",
        time_s=kids[0].time_s, lost_potency=round(total, 1),
        summary=(f"GCD uptime & pacing: {lead}. One late GCD pushes every "
                 f"weave and burst after it."),
        children=kids,
        prescription=(f"Start with the biggest piece: "
                      f"{top_rx[:1].lower()}{top_rx[1:]}" if top_rx else None))
    out = [*rest, umbrella]
    out.sort(key=lambda c: -c.lost_potency)
    return out


# --- Residual attribution ---------------------------------------------------
#
# The reconcile residual ("Spacing & sequencing") is real, diffuse loss — but
# part of it IS coarsely attributable from the per-ability count diff between
# the player's casts and the strict idealized timeline. split_residual moves
# the attributable slices out into named `residual_tail` sibling cards so the
# biggest number on the panel stops being an opaque bucket, without claiming
# per-cast precision the model doesn't have.

# A bucket below this (after capping) is noise — it stays in the remainder.
# Matches _CARD_FLOOR / compute_missed_cast_improvements' default min_potency.
_SPLIT_BUCKET_FLOOR = 150.0
# The residual row always keeps at least this much — mirrors reconcile's
# residual_floor, so the split never leaves a degenerate near-zero remainder
# row behind (the truly diffuse part is genuinely diffuse).
_SPLIT_KEEP_FLOOR = 60.0


def split_residual(cards: list[Improvement],
                   player_tl: list[tuple[float, int]],
                   ideal_tl: list[tuple[float, int]],
                   data: JobData) -> list[Improvement]:
    """Decompose the reconcile residual into named `residual_tail` top-level
    siblings using the per-ability count diff (delivered vs strict idealized).

    Double-count guardrails — each bucket prices only what NO existing card
    can see: abilities in `data.cooldowns` are owned by the missed-cast diff,
    `rng_proc_ids` can't be cast on demand, `filler_quality_gcds` are owned by
    the Filler-quality card, and the raw GCD-count deficit at filler value is
    owned by the cadence/idle cards. What's left is the *above-filler premium*
    of non-cooldown GCDs the sim lands more often (gauge spenders picked late,
    weaker filler chosen) and damaging non-cooldown oGCDs the sim fits.

    The emitted total is capped at `residual − _SPLIT_KEEP_FLOOR` (scaled
    proportionally when over) so the panel's top-level sum is conserved: the
    buckets' potency is subtracted from the residual card, never added on top.
    Passthrough when there's no residual or nothing worth splitting."""
    idx = next((i for i, c in enumerate(cards) if c.kind == "residual"), None)
    if idx is None:
        return cards
    residual = cards[idx]
    if residual.lost_potency < _SPLIT_BUCKET_FLOOR + _SPLIT_KEEP_FLOOR:
        return cards

    pc = Counter(a for t, a in player_tl if t >= 0)
    cc = Counter(a for t, a in ideal_tl if t >= 0)
    gcd_rows: list[tuple[float, int, str, int]] = []   # (value, aid, name, d)
    ogcd_rows: list[tuple[float, int, str, int]] = []
    for aid in set(pc) | set(cc):
        d = cc.get(aid, 0) - pc.get(aid, 0)
        if d <= 0:
            continue
        if (aid in data.cooldowns or aid in data.rng_proc_ids
                or aid in data.filler_quality_gcds):
            continue
        base = data.potencies.get(aid, 0)
        if base <= 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        name = meta.name if meta else f"action {aid}"
        if meta is not None and meta.is_ogcd:
            ogcd_rows.append((d * float(base), aid, name, d))
        else:
            premium = max(0.0, float(base) - data.filler_gcd_potency)
            if premium > 0:
                gcd_rows.append((d * premium, aid, name, d))

    gcd_val = sum(v for v, *_ in gcd_rows)
    ogcd_val = sum(v for v, *_ in ogcd_rows)
    total = gcd_val + ogcd_val
    if total <= 0:
        return cards
    avail = residual.lost_potency - _SPLIT_KEEP_FLOOR
    scale = min(1.0, avail / total)

    buckets: list[Improvement] = []
    if gcd_rows and gcd_val * scale >= _SPLIT_BUCKET_FLOOR:
        gcd_rows.sort(key=lambda r: -r[0])
        _v, top_aid, top_name, top_d = gcd_rows[0]
        n_more = sum(r[3] for r in gcd_rows)
        val = round(gcd_val * scale, 1)
        if len(gcd_rows) == 1:
            summary = (f"Resource & filler mix: the sim lands {top_d} more "
                       f"{top_name} (~{val:.0f}p over the filler that replaced "
                       f"{'it' if top_d == 1 else 'them'})")
        else:
            summary = (f"Resource & filler mix: the sim lands {n_more} more "
                       f"high-value GCDs than you (most: {top_name} ×{top_d}), "
                       f"~{val:.0f}p over the filler that replaced them")
        buckets.append(Improvement(
            kind="residual_tail", ability_id=top_aid, ability_name=top_name,
            time_s=0.0, lost_potency=val, summary=summary,
            prescription=("Spending gauge and procs sooner fits more of "
                          "these.")))
    if ogcd_rows and ogcd_val * scale >= _SPLIT_BUCKET_FLOOR:
        ogcd_rows.sort(key=lambda r: -r[0])
        _v, top_aid, top_name, top_d = ogcd_rows[0]
        n_more = sum(r[3] for r in ogcd_rows)
        val = round(ogcd_val * scale, 1)
        summary = (f"Damaging oGCDs the sim fit: {n_more} more "
                   f"{top_name if len(ogcd_rows) == 1 else 'weaves'} "
                   f"(~{val:.0f}p, free)")
        buckets.append(Improvement(
            kind="residual_tail", ability_id=top_aid, ability_name=top_name,
            time_s=0.0, lost_potency=val, summary=summary,
            prescription="An extra weave is a gain wherever it fits."))
    if not buckets:
        return cards

    emitted = sum(b.lost_potency for b in buckets)
    shrunk = dataclasses.replace(
        residual, lost_potency=round(residual.lost_potency - emitted, 1))
    out = [*cards[:idx], *buckets, shrunk, *cards[idx + 1:]]
    out.sort(key=lambda c: -c.lost_potency)
    return out


# --- Buff-window timing (observed-lens currency) -----------------------------
#
# A SEPARATE currency from the strict panel above: the strict recoverable gap
# is buff-agnostic by construction (buff alignment is zero under that basis),
# so "this cast landed just outside a party-buff window" can never be a slice
# of it. It IS real loss under the observed lens — delivered scored with the
# buffs that actually landed vs the buff-aware idealized — so these cards ship
# under their own response key with their own budget, and the two lists must
# never visually sum.

def buff_window_improvements(
    norm_casts: list[tuple[float, int]],
    observed_windows: list[tuple[float, float, float, str]],
    data: JobData,
    budget: float,
    shift_window_s: float = 8.0,
    noise_floor: float = 40.0,
) -> list[Improvement]:
    """Damage tools (`data.burst_abilities` — the alignable high-potency casts)
    delivered OUTSIDE every observed party-buff window but within
    `shift_window_s` BEFORE one: holding the cast that long would have landed
    it buffed. Forward-shift only — "hold it ~3s" is actionable next pull;
    "should have cast it earlier" rarely is.

    Value per cast = base potency × (window multiplier − 1), floored at
    `noise_floor`; the list is capped at `budget` (the observed-lens gap,
    idealized_observed − delivered_observed) via `reconcile_to_budget` with an
    infinite residual floor — over-budget tails are dropped, never binned.
    Empty for buffless pulls, jobs with no `burst_abilities`, or zero budget."""
    if budget <= 0 or not observed_windows or not data.burst_abilities:
        return []
    intervals = multiplier_intervals(
        [BuffWindow(s, e, m, lbl) for s, e, m, lbl in observed_windows])
    if not intervals:
        return []
    # Window starts, for the forward-shift scan; label lookup by start time.
    starts = sorted((s, e, m) for s, e, m in intervals)
    labels_by_start: dict[float, list[str]] = {}
    for s, e, m, lbl in observed_windows:
        labels_by_start.setdefault(round(s, 1), []).append(lbl)
    out: list[Improvement] = []
    for t, aid in norm_casts:
        if t < 0 or aid not in data.burst_abilities:
            continue
        base = data.potencies.get(aid, 0)
        if base <= 0:
            continue
        if multiplier_at(t, intervals) != 1.0:
            continue   # already inside a window
        nxt = next(((s, m) for s, _e, m in starts if s > t), None)
        if nxt is None:
            continue
        ws, mult = nxt
        gap = ws - t
        if not (0.0 < gap <= shift_window_s):
            continue
        val = base * (mult - 1.0)
        if val < noise_floor:
            continue
        meta = ability_metadata.get_metadata(aid)
        name = meta.name if meta else f"action {aid}"
        who = ", ".join(labels_by_start.get(round(ws, 1), [])) or "the party"
        out.append(Improvement(
            kind="buff_window", ability_id=aid, ability_name=name,
            time_s=t, lost_potency=round(val, 1),
            summary=(f"{name} at {_mmss(t)} landed just before {who}'s window "
                     f"at {_mmss(ws)}. Holding ~{gap:.0f}s gains ~{val:.0f}p"),
            prescription=f"Hold {name} ~{gap:.0f}s for the buff window."))
    if not out:
        return []
    # Cap at the observed-lens budget; float('inf') residual floor ⇒ the
    # over-budget tail is dropped, no residual card is minted here.
    return reconcile_to_budget(out, budget, residual_floor=float("inf"))
