"""Per-phase (phasic) metrics for ultimate analysis — pure and job-agnostic.

Consumes `norm_casts` + `JobData.gauges` + the pull's `Phase` segments. No
client, no network, no per-job branching (it reads the declarative `GaugeModel`
the same way `overcap`/`entry_gauge` do). Two products:

  * per-pull `PhaseMetrics` — gauge banking (entry/exit/generated/spent/
    overcapped per phase), GCD/total cast counts, per-ability counts, pot
    placement, active time;
  * `aggregate_phase_metrics` + `detect_deviations` — cross-ref medians/IQR and
    the "you're saving/spending abnormally vs the top clears" callouts
    ("refs bank ~80 Kenki into P4; you entered with 20").

Gauge replay is the same forward-walk family as
`overcap.compute_overcap_for_gauge` / `entry_gauge.measure_entry_gauge`: seed
the entry from the deepest-deficit method (so a mid-phase continuation never
goes negative), clamp to ``[0, cap]``, ``"all"`` spenders spend the current
balance. `cap_boosts` / conditional `spend_hook` spends are v1-ignored
(documented; the deviation thresholds absorb the resulting noise). The per-phase
conservation invariant holds: ``entry + generated - spent - overcapped == exit``.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from jobs._core.entry_gauge import measure_entry_gauge
from jobs._core.job import GaugeModel
from jobs._core.phases import Phase, downtime_overlap_s, split_casts_by_phase


@dataclass(frozen=True)
class GaugePhaseStats:
    name: str
    entry: int
    exit: int
    generated: int
    spent: int
    overcapped: int


@dataclass(frozen=True)
class PhaseMetrics:
    phase_id: int
    partial: bool            # phase not fully inside the scored window (wipe tail)
    active_s: float          # phase span (clipped to end_s) minus downtime overlap
    gcd_casts: int
    total_casts: int
    casts_by_ability: dict[int, int]
    gauges: tuple[GaugePhaseStats, ...]
    pot_used: bool


def _replay_gauge(buckets: list[list], gauge: GaugeModel, entry0: int) -> list[GaugePhaseStats]:
    """Forward-walk one `GaugeModel` phase by phase (buckets = casts per phase,
    in time order), recording entry/exit balances at each boundary and the units
    generated/spent/overcapped inside each phase. Seeded with `entry0` (the
    deepest-deficit carried gauge). Clamps to ``[0, cap]``; ``"all"`` spends the
    live balance."""
    cur = max(0, min(gauge.cap, int(entry0)))
    out: list[GaugePhaseStats] = []
    for pc in buckets:
        entry = cur
        gen = spent = over = 0
        for cast in pc:
            aid = cast[1]
            if aid in gauge.generators:
                add = gauge.generators[aid]
                gen += add
                proj = cur + add
                if proj > gauge.cap:
                    over += proj - gauge.cap
                cur = min(gauge.cap, proj)
            elif aid in gauge.spenders:
                sp = gauge.spenders[aid]
                if isinstance(sp, str) and sp == "all":
                    spent += cur
                    cur = 0
                elif isinstance(sp, (int, float)):
                    used = min(cur, int(sp))
                    spent += used
                    cur = max(0, cur - int(sp))
        out.append(GaugePhaseStats(gauge.name, entry, cur, gen, spent, over))
    return out


def compute_phase_metrics(
    norm_casts,
    phases: tuple[Phase, ...],
    gauges,
    downtime_windows,
    *,
    end_s: float | None = None,
    tincture_windows=(),
    is_gcd=None,
) -> list[PhaseMetrics]:
    """Per-phase metrics for one pull. `end_s` truncates the walk at the scored
    end (a wipe's terminal death) and is reused to build a ref's time-matched
    prefix for the subject's final partial phase. `is_gcd(aid) -> bool`
    distinguishes GCDs from oGCDs (defaults to counting everything as a GCD).
    `tincture_windows` is an iterable of ``(start_s, end_s)`` pot windows."""
    if not phases:
        return []
    casts = sorted((c for c in norm_casts
                    if c[0] >= 0.0 and (end_s is None or c[0] <= end_s)),
                   key=lambda c: c[0])
    entries = measure_entry_gauge([(c[0], c[1]) for c in casts], gauges)
    buckets = split_casts_by_phase(casts, phases)
    per_gauge = {
        g.name: _replay_gauge(buckets, g, entries.get(g.name, 0)) for g in gauges
    }
    pots = list(tincture_windows or ())

    out: list[PhaseMetrics] = []
    for i, ph in enumerate(phases):
        pc = buckets[i]
        gcd = sum(1 for c in pc if is_gcd is None or is_gcd(c[1]))
        by_ability: dict[int, int] = {}
        for c in pc:
            by_ability[c[1]] = by_ability.get(c[1], 0) + 1
        eff_end = min(ph.end_s, end_s) if end_s is not None else ph.end_s
        span = max(0.0, eff_end - ph.start_s)
        # Downtime clipped to the (possibly truncated) phase window.
        clipped = Phase(ph.id, ph.name, ph.start_s, eff_end, ph.is_intermission)
        active = max(0.0, span - downtime_overlap_s(clipped, downtime_windows))
        pot_used = any(w0 < eff_end and w1 > ph.start_s for w0, w1 in pots)
        partial = end_s is not None and ph.end_s > end_s + 0.01
        out.append(PhaseMetrics(
            phase_id=ph.id,
            partial=partial,
            active_s=active,
            gcd_casts=gcd,
            total_casts=len(pc),
            casts_by_ability=by_ability,
            gauges=tuple(per_gauge[g.name][i] for g in gauges),
            pot_used=pot_used,
        ))
    return out


# --- Cross-ref aggregation + deviation detection ----------------------------

def _stat3(values: list[float]) -> dict[str, float]:
    """median / p25 / p75 of a value list (empty -> zeros). Simple nearest-rank
    quartiles — the ref counts are small (~10)."""
    if not values:
        return {"median": 0.0, "p25": 0.0, "p75": 0.0}
    s = sorted(values)
    n = len(s)
    return {
        "median": float(median(s)),
        "p25": float(s[max(0, (n - 1) // 4)]),
        "p75": float(s[min(n - 1, (3 * (n - 1)) // 4)]),
    }


@dataclass(frozen=True)
class PhaseAgg:
    phase_id: int
    ref_count: int
    gcd_casts: dict[str, float]
    gcd_rate: dict[str, float]                       # GCDs per active second
    delivered: dict[str, float]                      # delivered potency (from Scoring)
    gauges: dict[str, dict[str, dict[str, float]]]   # gauge -> field -> stat3
    pot_pct: float                                   # fraction of refs that potted here
    ability_median: dict[int, float]                 # ability_id -> median casts


def aggregate_phase_metrics(
    per_ref: list[list[PhaseMetrics]],
    *,
    delivered_per_ref: list[dict[int, float]] | None = None,
) -> dict[int, PhaseAgg]:
    """Aggregate refs' per-phase metrics keyed by phase id. `delivered_per_ref`
    (optional) is each ref's `{phase_id: delivered_potency}` from its Scoring
    state, folded into the `delivered` stat3."""
    # Collect per phase id.
    by_phase: dict[int, list[PhaseMetrics]] = {}
    for metrics in per_ref:
        for m in metrics:
            by_phase.setdefault(m.phase_id, []).append(m)
    deliv_by_phase: dict[int, list[float]] = {}
    for ref_deliv in (delivered_per_ref or []):
        for pid, val in ref_deliv.items():
            deliv_by_phase.setdefault(pid, []).append(float(val))

    out: dict[int, PhaseAgg] = {}
    for pid, ms in by_phase.items():
        # Compare only complete (non-partial) ref phases for rate/gauge signals;
        # a ref whose logged pull ended mid-phase would otherwise skew low.
        full = [m for m in ms if not m.partial] or ms
        gauge_names = [g.name for g in full[0].gauges] if full and full[0].gauges else []
        gauge_stats: dict[str, dict[str, dict[str, float]]] = {}
        for gi, gname in enumerate(gauge_names):
            gauge_stats[gname] = {
                "exit": _stat3([m.gauges[gi].exit for m in full]),
                "overcapped": _stat3([m.gauges[gi].overcapped for m in full]),
                "spent": _stat3([m.gauges[gi].spent for m in full]),
                "generated": _stat3([m.gauges[gi].generated for m in full]),
            }
        # Per-ability median cast counts across refs.
        ability_counts: dict[int, list[float]] = {}
        for m in full:
            for aid, n in m.casts_by_ability.items():
                ability_counts.setdefault(aid, []).append(float(n))
        out[pid] = PhaseAgg(
            phase_id=pid,
            ref_count=len(ms),
            gcd_casts=_stat3([m.gcd_casts for m in full]),
            gcd_rate=_stat3([m.gcd_casts / m.active_s for m in full if m.active_s > 1.0]),
            delivered=_stat3(deliv_by_phase.get(pid, [])),
            gauges=gauge_stats,
            pot_pct=(sum(1 for m in full if m.pot_used) / len(full)) if full else 0.0,
            ability_median={aid: float(median(v)) for aid, v in ability_counts.items()},
        )
    return out


# Deviations are suppressed below this ref count — too few clears to call a
# pattern "abnormal" (mirrors the Tier-B consensus min_ref_count spirit).
_MIN_REF_COUNT = 5


def _cap_for_gauge(gauges, name: str) -> int:
    for g in gauges:
        if g.name == name:
            return g.cap
    return 100


def detect_deviations(
    user: list[PhaseMetrics],
    agg: dict[int, PhaseAgg],
    phases: tuple[Phase, ...],
    gauges,
    *,
    ref_count: int,
    user_delivered: dict[int, float] | None = None,
) -> list[dict]:
    """Flag where the subject's saving/spending diverges from the top clears.
    Suppressed entirely when `ref_count < _MIN_REF_COUNT`. Only phase ids present
    for both the subject and the refs are compared. Each deviation is a plain
    dict ready to camelize onto the wire."""
    if ref_count < _MIN_REF_COUNT:
        return []
    name_by_id = {ph.id: ph.name for ph in phases}
    is_final = phases[-1].id if phases else None
    user_deliv = user_delivered or {}
    out: list[dict] = []
    for m in user:
        a = agg.get(m.phase_id)
        if a is None or a.ref_count < _MIN_REF_COUNT:
            continue
        pname = name_by_id.get(m.phase_id, f"P{m.phase_id}")

        # 1. Gauge banking at phase exit (completed, non-final phases): are you
        #    entering the next phase with far more/less resource than the field?
        if not m.partial and m.phase_id != is_final:
            for gi, gs in enumerate(m.gauges):
                gagg = a.gauges.get(gs.name)
                if not gagg:
                    continue
                med = gagg["exit"]["median"]
                iqr = gagg["exit"]["p75"] - gagg["exit"]["p25"]
                cap = _cap_for_gauge(gauges, gs.name)
                thresh = max(0.25 * cap, 1.5 * iqr)
                if abs(gs.exit - med) > thresh and thresh > 0:
                    hi = gs.exit > med
                    out.append({
                        "phase_id": m.phase_id,
                        "kind": "gauge_exit",
                        "gauge": gs.name,
                        "your_value": float(gs.exit),
                        "ref_value": float(med),
                        "text": (
                            f"You {'left' if hi else 'entered the next phase with'} "
                            f"{gs.exit:.0f} {gs.name} at the end of {pname}; the top "
                            f"clears {'bank' if not hi else 'hold'} ~{med:.0f}. "
                            + ("Consider spending more into this phase's damage."
                               if hi else
                               "They carry more resource into the next burst — "
                               "you may be spending too early.")
                        ),
                    })

        # 2. Overcap waste vs the field (any reached phase).
        for gi, gs in enumerate(m.gauges):
            gagg = a.gauges.get(gs.name)
            if not gagg:
                continue
            p75 = gagg["overcapped"]["p75"]
            med = gagg["overcapped"]["median"]
            if gs.overcapped > p75 and gs.overcapped >= med + 2:
                out.append({
                    "phase_id": m.phase_id,
                    "kind": "overcap_phase",
                    "gauge": gs.name,
                    "your_value": float(gs.overcapped),
                    "ref_value": float(med),
                    "text": (
                        f"You overcapped {gs.overcapped:.0f} {gs.name} in {pname} "
                        f"(top clears waste ~{med:.0f}). Spend it down before it caps."
                    ),
                })

        # 3. Potion: the top clears reliably pot in this COMPLETED phase but you
        #    didn't. Gated to completed phases (not m.partial), which are entirely
        #    before your death — so a pot "due" only in the phase you wiped in, or
        #    after your death, is never flagged. The ref pot-rate IS the real kill
        #    cadence (top clears aiming to kill), which is what you're rehearsing.
        if not m.partial and a.pot_pct >= 0.70 and not m.pot_used:
            out.append({
                "phase_id": m.phase_id,
                "kind": "pot_phase",
                "your_value": 0.0,
                "ref_value": round(a.pot_pct * 100),
                "text": (
                    f"{round(a.pot_pct * 100)}% of the top clears pop a tincture in "
                    f"{pname} and you didn't — you had the whole phase to fit it."
                ),
            })

        # 4. GCD pacing on a completed, non-trivial phase.
        if not m.partial and m.active_s >= 20.0 and a.gcd_rate["median"] > 0:
            your_rate = m.gcd_casts / m.active_s if m.active_s > 0 else 0.0
            if your_rate < a.gcd_rate["median"] * 0.93:
                out.append({
                    "phase_id": m.phase_id,
                    "kind": "gcd_pace",
                    "your_value": round(your_rate, 3),
                    "ref_value": round(a.gcd_rate["median"], 3),
                    "text": (
                        f"Your GCD pace in {pname} ({m.gcd_casts} GCDs) trails the "
                        f"top clears — likely dropped uptime or extra movement."
                    ),
                })

        # 5. Delivered potency well under the field in a completed phase.
        a_del = a.delivered
        yd = user_deliv.get(m.phase_id)
        if not m.partial and yd is not None and a_del["p25"] > 0 and yd < a_del["p25"]:
            out.append({
                "phase_id": m.phase_id,
                "kind": "potency_low",
                "your_value": round(yd),
                "ref_value": round(a_del["median"]),
                "text": (
                    f"Your damage in {pname} ({yd:,.0f}p) is below the top clears' "
                    f"lower quartile ({a_del['p25']:,.0f}p, median {a_del['median']:,.0f}p)."
                ),
            })

    return out
