"""Phase-continuation entry state — shared scaffolding.

A continuous fight logged as a later phase (e.g. M12S-P2) starts MID-COMBAT: the
player carries gauge AND opener readiness out of the prior phase. A sim that
cold-starts it as a fresh pull under-models that loaded opener, so an elite
continuation parse beats the ceiling (>100%). This module is the job-agnostic half
of modeling it:

  * `measure_entry_gauge` — infer carried gauge from the player's opening (the
    deepest-deficit method: the resource they must have started with to afford their
    early spends). Generic over a job's `GaugeModel`s; 0 for a cold start -> no-op.
  * `seed_entry_gauge` — seed those values onto the sim's start state (relying on the
    `GaugeModel.name == SimState field` convention every job follows).
  * `measure_opener_start` — the ceiling's opener start = min(role engage default,
    the player's first in-fight GCD excluding the pre-pull channel). `None` on a
    fresh pull (no override); earlier on a continuation (already on the boss).
  * `EntryState` — the per-pull `sim_context` payload (carried gauge + opener start).

First proven on SAM (kenki) + RPR (soul/shroud); this generalizes the pattern so
every gauge job (MCH heat/battery, WAR beast, …) gets it without a fourth copy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class EntryState:
    """Per-pull phase-continuation entry state, threaded as `sim_context`. A falsy
    instance (or `None`) is a cold-start fresh pull -> the sim stays byte-identical.
    Hashable -> joins the perfect-sim cache key."""
    gauges: tuple[tuple[str, int], ...] = ()    # (gauge_name, carried_value), sorted
    opener_start_s: Optional[float] = None      # ceiling opener start (continuation)

    @property
    def gauge_map(self) -> dict[str, int]:
        return dict(self.gauges)

    def __bool__(self) -> bool:
        return bool(self.gauges) or self.opener_start_s is not None


# spend_hook(gauge_name, ability_id, scratch) -> effective spend to subtract, or None
# to fall back to the GaugeModel's flat spender. Called for EVERY cast (not just
# spenders) so it can track conditional state in the per-gauge `scratch` dict — e.g.
# RPR Ideal-Host: Plentiful Harvest (a non-spender) arms a later FREE Enshroud.
SpendHook = Callable[[str, int, dict], Optional[int]]


def measure_entry_gauge(norm_casts, gauges, *, spend_hook: SpendHook | None = None,
                        window_s: float | None = None) -> dict[str, int]:
    """Per-`GaugeModel` deepest-deficit over the player's casts = the gauge they must
    have carried into the pull. Run each gauge's balance forward from 0; the entry is
    `max(0, -min)`, clamped to the cap. A cold start never goes negative -> 0 (a
    no-op outside phased fights). The ceiling is seeded with the same value, so a
    loaded opener is matched symmetrically (preserving the <=100% guard).

    `spend_hook` overrides a conditional/free spend (RPR's Ideal-Host Enshroud, MCH
    battery's Queen "all"-spend) — return the effective spend, or None for the flat
    GaugeModel amount.

    `window_s` caps the measurement to the opening N seconds. Leave None (full fight)
    when every spend is covered by a modeled generator (RPR/MCH/WAR) — the deepest
    deficit anywhere is then a true lower bound on carried gauge. Set it when a job
    has an UN-modeled generation source (SAM's Meditate channel adds Kenki the cast
    stream can't see) so later spends would otherwise read as phantom carried gauge."""
    casts = sorted((t, a) for t, a in norm_casts
                   if t >= 0.0 and (window_s is None or t <= window_s))
    out: dict[str, int] = {}
    for g in gauges:
        bal = mn = 0
        scratch: dict = {}
        for _t, aid in casts:
            delta = g.generators.get(aid, 0)
            eff = spend_hook(g.name, aid, scratch) if spend_hook else None
            if eff is not None:
                delta -= int(eff)
            else:
                sp = g.spenders.get(aid)
                if sp is not None:
                    delta -= sp if isinstance(sp, int) else 0   # "all"-spend w/o hook: skip
            bal += delta
            mn = min(mn, bal)
        out[g.name] = min(g.cap, max(0, -mn))
    return out


def seed_entry_gauge(state, entry: dict[str, int], gauges) -> None:
    """Seed each gauge's `SimState` field from `entry` (the `GaugeModel.name` ==
    state-field convention every job follows). No-op for absent/zero values."""
    for g in gauges:
        v = entry.get(g.name, 0)
        if v:
            setattr(state, g.name, min(g.cap, max(0, int(v))))


def measure_opener_start(norm_casts, prepull_channel_ids: frozenset[int],
                         default_engage_s: float) -> Optional[float]:
    """The ceiling's opener start for this pull, or `None` to keep the role/fresh
    default. = min(default_engage, the player's first in-fight GCD), skipping the
    single pre-pull *channel* (RPR Harpe / PLD Holy Spirit) that resolves near t=0.
    A fresh pull's first GCD is ~the default -> `None` (no change, byte-identical);
    a continuation (already on the boss, no countdown) opens earlier, so the ceiling
    should too. Capped at the default so a *slow* opener never lowers the ceiling."""
    first: float | None = None
    for t, aid in sorted((t, a) for t, a in norm_casts if t >= 0.0):
        if aid in prepull_channel_ids and t < default_engage_s:
            continue        # the pre-pull channel's ~t=0 resolution, not the loop start
        first = t
        break
    if first is None:
        return None
    start = min(default_engage_s, first)
    return start if start < default_engage_s - 1e-6 else None


def entry_state(norm_casts, gauges, *, spend_hook: SpendHook | None = None,
                prepull_channel_ids: frozenset[int] = frozenset(),
                default_engage_s: float = 0.0) -> Optional[EntryState]:
    """Build the per-pull `EntryState`, or `None` when nothing was carried (a cold
    start) so the job's `sim_context` stays None and the sim byte-identical."""
    g = {k: v for k, v in measure_entry_gauge(
        norm_casts, gauges, spend_hook=spend_hook).items() if v}
    opener = measure_opener_start(norm_casts, prepull_channel_ids, default_engage_s)
    if not g and opener is None:
        return None
    return EntryState(gauges=tuple(sorted(g.items())), opener_start_s=opener)
