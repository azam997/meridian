"""Deep-advice — the deterministic "improvements algorithm", run INLINE by
run_analysis's response build (`main.py::_run_deep_pass`).

Two passes over the pull's re-derived card list (the exact wire dicts
`_build_improvements` shipped):

**Analytic probes** (v1, kept): concrete situation-specific feedback keyed back
onto EXISTING cards by their `(kind, abilityId, timeSec)` triple —
missed-cast fit (the weave gap / idle stretch the cast would have slotted
into) and the residual count-diff table. Job-specific probes (e.g. the MCH
wildfire/hypercharge window shift, which used to live here) register via
`Job.advice_probes` and contribute `ProbeItem`s into the same list.

**Cascade pass** (v2): the telescoping prefix-cascade decomposition. Cut the
fight at burst-cycle boundaries, compute

    C(t) = score(replay(player casts <= t) ⊕ greedy-continue(state@t → end))

at every cut (jobs/_core/sim/counterfactual.py, pooled), and read each
segment's loss `L_k = C(t_k) − C(t_{k+1})` — which contains every downstream
consequence of how the segment was played, because the later cut's
continuation re-optimizes from the mistake-laden state. The top segments are
re-cut finer, their unexplained loss (what no located card already prices) is
measured, and that mass is MOVED out of the "Spacing & sequencing" residual
into concrete root-cause cards (job `RootCause` candidates, or a generic
`cascade_pacing` card named by the player-vs-ideal state delta). Same
conservation discipline as `split_residual`: shares are scaled into the
residual's actual budget, the residual shrinks by exactly what moves, and the
examined top-level sum equals the original sum — asserted before emitting,
degrading to `examined: None` on any failure, never a request error.

Currencies: cascade sims run strict (buff-agnostic) and pot-free; segment
losses are attribution WEIGHTS scaled onto the panel's residual mass, never
headline prices. `buffAlignment` cards are a separate currency and are never
probed or re-attributed here.

Everything is deterministic: no RNG, stable sort keys, rounded floats — the
same input yields byte-identical output.
"""
from __future__ import annotations

import math
import sys
import traceback
from collections import Counter

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs._core.job import JobData

_WEAVE_GAP_MIN_S = 0.75
_FIT_SCAN_S = 15.0
_IDLE_SCAN_S = 20.0

# --- User-facing copy (data, not code) --------------------------------------
# Every generic line the pass can emit lives here so wording improvements are
# data edits. Job-specific copy lives in the job's advice module (TEXT /
# GAUGE_TEXT — see jobs/machinist/advice.py).

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes; readers flag them as machine prose and disregard the advice), no
# generic filler ("tighten the stretch"), and hold advice stays scoped to the
# stretch it was measured in ("right away HERE") so holding for buffs
# elsewhere stays legitimate. Run new dialogue copy by the user before
# shipping it.
TEXT: dict[str, str] = {
    "weave_fit": ("Weave it after {name} at {when}; the {gap:.2f}s opening "
                  "is already there."),
    "idle_fit": "It fits free in your {secs:.1f}s gap at {when}.",
    "displace_filler": ("Tight GCDs here; displacing a filler still nets "
                        "~{per:.0f}p."),
    "residual_deficits": "Biggest count gaps: {names}.",
    "residual_trade": (" Extra {surplus} casts took those slots at lower "
                       "value."),
    "residual_no_deficits": ("No ability ran behind the sim's counts. The "
                             "diffuse cost is burst spacing and GCD timing, "
                             "not which buttons you pressed."),
    # Evidence-row fragments (k / mono value / prose note; see EvidenceRow).
    "gauge_over_v": "{delta} over ideal",
    "gauge_under_v": "{delta} under ideal",
    "cd_drift_v": "{delta:.1f}s late",
    "cd_drift_note": "ran behind the ideal line through this stretch",
    "charge_banked_v": "{delta:.1f} banked",
    "charge_banked_note": "charges sat unused",
    "also_note": "also in this stretch: {summary}",
    # Sequencing-slip cards. The title names the held resources; the
    # prescription is direct and stretch-scoped, never a copy of an evidence
    # line.
    "pacing_summary_res": "{resources} held from {start} to {end}",
    "pacing_summary": "Sequencing slip from {start} to {end}",
    "pacing_rx_spend": "Use excess {resources} right away here.",
    "pacing_rx_plain": "Close the idle gaps after {start}.",
    "note_moved": ("{moved:,.0f}p of the diffuse loss resolved into {n} root "
                   "cause{plural}."),
    "note_scaled": ("Cascade weights scaled ×{scale:.2f} to fit the residual "
                    "budget."),
}

# Cascade-pass tuning.
_COARSE_GRID_S = 30.0          # fallback/backbone cut cadence
_COARSE_DEDUP_S = 5.0
_FINE_DEDUP_S = 2.0
_FINE_SEGMENTS = 3             # how many coarse segments get re-cut
_FINE_MAX_SUBCUTS = 8
_SEG_MIN_UNEXPLAINED = 100.0   # weight floor: segments below this attribute nothing
_BUCKET_FLOOR = 150.0          # post-scale minimum for a promoted card
_RESIDUAL_KEEP = 60.0          # the residual never drains below this
_CD_DRIFT_MIN_S = 2.0          # state-delta evidence thresholds
_CHARGE_DRIFT_MIN = 0.5


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _is_ogcd(aid: int) -> bool:
    m = ability_metadata.get_metadata(aid)
    return m is not None and m.is_ogcd


def _item(card: dict, prescription: str,
          evidence: list[EvidenceRow] | None = None,
          summary: str | None = None,
          count_gaps: list[dict] | None = None) -> dict:
    """An advice item carrying the card's exact matching triple (verbatim).
    `evidence` becomes the card's labelled rows, `summary` (when given)
    retitles it, `count_gaps` attaches the cast-vs-sim bar data."""
    out: dict = {
        "kind": card.get("kind"),
        "abilityId": card.get("abilityId", 0),
        "timeSec": card.get("timeSec", 0.0),
        "prescription": prescription,
        "evidence": [r.wire() for r in (evidence or [])],
    }
    if summary is not None:
        out["summary"] = summary
    if count_gaps:
        out["countGaps"] = count_gaps
    return out


def _triple(card: dict) -> tuple:
    return (card.get("kind"), int(card.get("abilityId", 0) or 0),
            round(float(card.get("timeSec", 0.0) or 0.0), 1))


# --- Analytic probes (v1) ---------------------------------------------------

def _probe_missed_fit(card: dict, casts: list[tuple[float, int]],
                      worst_idle: list[tuple[float, float]]) -> dict | None:
    """Where the missed cast would actually have fit: the nearest weave gap
    (oGCD) or idle stretch (GCD) around the card's located time."""
    t0 = float(card.get("timeSec", 0.0) or 0.0)
    aid = int(card.get("abilityId", 0) or 0)
    if t0 <= 0 or aid <= 0:
        return None
    if _is_ogcd(aid):
        best: tuple[float, float, int] | None = None   # (dist, gap_s, idx)
        for i in range(len(casts) - 1):
            t_a, _ = casts[i]
            t_b, _ = casts[i + 1]
            gap = t_b - t_a
            if gap < _WEAVE_GAP_MIN_S:
                continue
            dist = abs(t_a - t0)
            if dist > _FIT_SCAN_S:
                continue
            if best is None or dist < best[0]:
                best = (dist, gap, i)
        if best is None:
            return None
        _dist, gap, i = best
        t_a, a_aid = casts[i]
        return _item(card, TEXT["weave_fit"].format(
            name=_name(a_aid), when=_mmss(t_a), gap=gap), [])
    # GCD tool: the nearest idle stretch it would have filled.
    near = [(abs(t - t0), t, secs) for t, secs in worst_idle
            if abs(t - t0) <= _IDLE_SCAN_S]
    if near:
        _d, t, secs = min(near)
        return _item(card, TEXT["idle_fit"].format(
            secs=secs, when=_mmss(t)), [])
    n = max(1, len(card.get("children") or []) or 1)
    per = float(card.get("lostPotency", 0.0) or 0.0) / n
    if per <= 0:
        return None
    return _item(card, TEXT["displace_filler"].format(per=per), [])


def _probe_residual_table(card: dict, player_tl: list[tuple[float, int]],
                          ideal_tl: list[tuple[float, int]],
                          data: JobData) -> dict | None:
    """The per-ability count diff behind the diffuse card, as `countGaps` bar
    data ({name, you, sim} rows — the UI draws your count as the fill and the
    sim's as a tick).

    DEFICITS (the sim fit more — the actual gaps) lead and drive the headline;
    the player's SURPLUS casts trail as context — an extra Drill is where the
    displaced casts went, not a loss. Per-row pricing is deliberately omitted
    — the card's own lostPotency is the priced number; this table is the
    count evidence behind it."""
    pc = Counter(a for t, a in player_tl if t >= 0)
    cc = Counter(a for t, a in ideal_tl if t >= 0)
    deficits: list[tuple[float, str, dict]] = []   # (value, name, row)
    surpluses: list[tuple[float, str, dict]] = []
    for aid in set(pc) | set(cc):
        d = cc.get(aid, 0) - pc.get(aid, 0)
        if d == 0:
            continue
        pot = data.potencies.get(aid, 0)
        name = _name(aid)
        row = {"name": name, "you": pc.get(aid, 0), "sim": cc.get(aid, 0)}
        if d > 0:
            deficits.append((float(d * pot), name, row))
        else:
            surpluses.append((float(-d * pot), name, row))
    deficits.sort(key=lambda r: (-r[0], r[1]))
    surpluses.sort(key=lambda r: (-r[0], r[1]))
    if not deficits and not surpluses:
        return None
    gaps = [r[2] for r in deficits[:4]] + [r[2] for r in surpluses[:2]]
    if deficits:
        top = [r[1] for r in deficits[:2]]
        rx = TEXT["residual_deficits"].format(names=" and ".join(top))
        if surpluses:
            # Name the trade: extras are not free gains, they came out of the
            # deficit slots (user-approved copy 2026-08-10).
            rx += TEXT["residual_trade"].format(surplus=surpluses[0][1])
    else:
        rx = TEXT["residual_no_deficits"]
    return _item(card, rx, count_gaps=gaps[:5])


def _analytic_items(cards: list[dict], casts: list[tuple[float, int]],
                    worst_idle: list[tuple[float, float]],
                    idealized: list[tuple[float, int]], data: JobData,
                    job_items: dict[tuple, dict],
                    progress=None) -> list[dict]:
    """The per-card probe loop. `job_items` are `ProbeItem`s from the job's
    `advice_probes`, keyed by card triple — a job item wins its card's slot
    (list order stays the card order either way)."""
    out: list[dict] = []
    n = max(1, len(cards))
    for i, card in enumerate(cards):
        if progress is not None:
            progress(55 + int(10 * i / n), "Probing the cards…")
        item = job_items.get(_triple(card))
        if item is None:
            kind = card.get("kind")
            if kind in ("missed_cast", "missed_enabler"):
                item = _probe_missed_fit(card, casts, worst_idle)
            elif kind == "residual":
                # The parent card only — `residual_tail` rows already carry
                # targeted prescriptions from split_residual, and repeating
                # the same count table on three sibling cards reads as noise.
                item = _probe_residual_table(card, casts, idealized, data)
        if item is not None:
            out.append(item)
    return out


def compute_advice(cards: list[dict], norm_casts: list[tuple[float, int]],
                   idealized: list[tuple[float, int]],
                   clipping_state: dict, data: JobData,
                   progress=None) -> list[dict]:
    """The analytic-only pass (v1 entry, kept for tests and non-probe jobs).
    Cards without an applicable or improving probe simply get no item — their
    static prescription stands."""
    casts = sorted((t, a) for t, a in norm_casts if t >= 0)
    f = (clipping_state or {}).get("clipping")
    worst_idle = list(getattr(f, "worst_idle", []) or [])
    return _analytic_items(cards, casts, worst_idle, idealized, data, {},
                           progress=progress)


# --- Cascade pass (v2) ------------------------------------------------------

def _snap_to_gap(t: float, cast_times: list[float]) -> float:
    """Move a candidate cut to the midpoint of the inter-cast interval that
    contains it, so no cut sits exactly on a cast time (the replay's
    `t <= cut` boundary stays unambiguous)."""
    import bisect
    i = bisect.bisect_right(cast_times, t)
    if i == 0 or i >= len(cast_times):
        return round(t, 2)
    return round((cast_times[i - 1] + cast_times[i]) / 2.0, 2)


def _dedup(sorted_ts: list[float], min_gap: float) -> list[float]:
    out: list[float] = []
    for t in sorted_ts:
        if not out or t - out[-1] >= min_gap:
            out.append(t)
    return out


def _coarse_cuts(ctx: AdviceContext) -> list[float]:
    """Fight-spanning cut set: the player's burst-ability cast times
    (`JobData.burst_abilities` — the job's cycle anchors) unioned with a 30s
    backbone grid, snapped to inter-cast gaps, deduped, bracketed by 0 and
    fight end (both exact)."""
    dur = float(ctx.fight_duration_s)
    cast_times = sorted(t for t, _a in ctx.norm_casts if t >= 0)
    burst_ids = set(getattr(ctx.data, "burst_abilities", ()) or ())
    cands = [t for t, a in ctx.norm_casts if t > 0 and a in burst_ids]
    g = _COARSE_GRID_S
    while g < dur:
        cands.append(g)
        g += _COARSE_GRID_S
    snapped = sorted(_snap_to_gap(t, cast_times) for t in cands)
    inner = [t for t in _dedup(snapped, _COARSE_DEDUP_S)
             if _COARSE_DEDUP_S <= t <= dur - _COARSE_DEDUP_S]
    return [0.0] + inner + [round(dur, 2)]


def _fine_cuts(ctx: AdviceContext, seg: tuple[float, float],
               cards: list[dict]) -> list[float]:
    """Sub-cuts inside one expensive segment: located card times + the largest
    inter-cast gaps' midpoints, capped, deduped."""
    t0, t1 = seg
    cast_times = sorted(t for t, _a in ctx.norm_casts if t >= 0)
    cands = [float(c.get("timeSec", 0.0) or 0.0) for c in cards
             if t0 < float(c.get("timeSec", 0.0) or 0.0) < t1]
    gaps: list[tuple[float, float]] = []      # (gap_s, midpoint)
    for a, b in zip(cast_times, cast_times[1:]):
        if t0 < a and b < t1:
            gaps.append((b - a, (a + b) / 2.0))
    gaps.sort(key=lambda g: (-g[0], g[1]))
    cands.extend(mid for _g, mid in gaps[:_FINE_MAX_SUBCUTS])
    snapped = sorted(_snap_to_gap(t, cast_times) for t in cands)
    inner = [t for t in _dedup(snapped, _FINE_DEDUP_S)
             if t0 + _FINE_DEDUP_S <= t <= t1 - _FINE_DEDUP_S]
    return inner[:_FINE_MAX_SUBCUTS]


def _located_claims(cards: list[dict], t0: float, t1: float) -> float:
    """Potency already priced by located cards inside [t0, t1) — top-level
    cards with a real time (residual family excluded), plus the located
    children of a `pacing` umbrella. The cascade only attributes what these
    do NOT explain."""
    claimed = 0.0
    for c in cards:
        kind = c.get("kind")
        if kind in ("residual", "residual_tail"):
            continue
        rows = c.get("children") or [] if kind == "pacing" else [c]
        for r in rows:
            ts = float(r.get("timeSec", 0.0) or 0.0)
            if t0 <= ts < t1:
                claimed += float(r.get("lostPotency", 0.0) or 0.0)
    return claimed


def _delta_evidence(delta: dict, gauge_text: dict | None = None
                    ) -> tuple[list[EvidenceRow], list[GaugeText]]:
    """Labelled evidence rows from a player-vs-ideal `state_delta` snapshot
    pair — which gauges diverge, which cooldowns sit drifted, which charges
    are banked unused — plus the implicated gauges (for the card's resource
    tags). Deterministic order: biggest drift first, gauges leading.

    Gauge fields are STRICTLY allowlisted through the job's `gauge_text`
    glossary (jobs._core.advice.GaugeText) — a field without an entry (or
    without a note for its direction) renders nothing, so raw sim-state names
    can never leak into the UI. Cooldown/charge rows use real ability names
    and are always safe."""
    p, i = delta.get("player") or {}, delta.get("ideal") or {}
    rows: list[tuple[float, EvidenceRow]] = []
    resources: list[tuple[float, GaugeText]] = []
    p_g = p.get("gauges") or {}
    i_g = i.get("gauges") or {}
    for k in sorted(set(p_g) | set(i_g)):
        gt = (gauge_text or {}).get(k)
        if gt is None:
            continue                     # allowlist: undescribed fields stay silent
        d = float(p_g.get(k, 0.0)) - float(i_g.get(k, 0.0))
        if abs(d) < gt.min_delta:
            continue
        note = gt.over_note if d > 0 else gt.under_note
        if not note:
            continue
        v = (TEXT["gauge_over_v"] if d > 0
             else TEXT["gauge_under_v"]).format(delta=f"{abs(d):.0f}")
        rows.append((abs(d) + 1e6, EvidenceRow(k=gt.label, v=v, note=note)))
        resources.append((abs(d), gt))
    p_cd = p.get("cd_remaining") or {}
    i_cd = i.get("cd_remaining") or {}
    for aid in sorted(set(p_cd) | set(i_cd)):
        d = float(p_cd.get(aid, 0.0)) - float(i_cd.get(aid, 0.0))
        if d >= _CD_DRIFT_MIN_S:
            rows.append((d, EvidenceRow(
                k=_name(int(aid)), v=TEXT["cd_drift_v"].format(delta=d),
                note=TEXT["cd_drift_note"])))
    p_ch = p.get("charges") or {}
    i_ch = i.get("charges") or {}
    for aid in sorted(set(p_ch) | set(i_ch)):
        d = float(p_ch.get(aid, 0.0)) - float(i_ch.get(aid, 0.0))
        if d >= _CHARGE_DRIFT_MIN:
            rows.append((d * 10.0, EvidenceRow(
                k=_name(int(aid)),
                v=TEXT["charge_banked_v"].format(delta=d),
                note=TEXT["charge_banked_note"])))
    rows.sort(key=lambda r: -r[0])
    resources.sort(key=lambda r: -r[0])
    return [r for _v, r in rows[:3]], [g for _v, g in resources]


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _promote(cause: RootCause, priced: float,
             extra_rows: list[EvidenceRow]) -> dict:
    """A RootCause as an examined-list wire card (camelCase, run_analysis
    Improvement shape + labelled `evidence` rows + `resources` tags)."""
    seen = {(r.k, r.v) for r in cause.evidence}
    rows = list(cause.evidence) + [r for r in extra_rows
                                   if (r.k, r.v) not in seen]
    out = {
        "kind": cause.kind,
        "abilityId": int(cause.ability_id),
        "abilityName": cause.ability_name,
        "timeSec": round(float(cause.time_sec), 1),
        "lostPotency": round(priced, 1),
        "summary": cause.summary,
        "children": [],
        "prescription": cause.prescription,
        "evidence": [r.wire() for r in rows[:3]],
    }
    if cause.resources:
        out["resources"] = [{"label": g.label, "short": g.short}
                            for g in cause.resources[:2]]
    return out


def _generic_cause(seg: tuple[float, float], rows: list[EvidenceRow],
                   resources: list[GaugeText]) -> RootCause:
    """The sequencing-slip card for a segment no job cause claimed. The title
    names the held resources; the prescription names what to spend — never a
    copy of an evidence row."""
    t0, t1 = seg
    if resources:
        labels = _join_names([g.label for g in resources[:2]])
        summary = TEXT["pacing_summary_res"].format(
            resources=labels, start=_mmss(t0), end=_mmss(t1))
        rx = TEXT["pacing_rx_spend"].format(resources=labels)
    else:
        summary = TEXT["pacing_summary"].format(start=_mmss(t0),
                                                end=_mmss(t1))
        rx = TEXT["pacing_rx_plain"].format(start=_mmss(t0))
    return RootCause(
        kind="cascade_pacing", ability_id=0, ability_name="",
        time_sec=round(t0, 1), measured_p=0.0,
        summary=summary, prescription=rx,
        evidence=rows, resources=list(resources), segment=seg)


def _cascade_examined(ctx: AdviceContext, cards: list[dict],
                      causes: list[RootCause], progress=None) -> dict | None:
    """The cascade measurement + conservation-preserving restructure.
    Returns the `examined` payload, or None when there is nothing to move
    (no residual, no measurable segments)."""
    residual_idx = next((i for i, c in enumerate(cards)
                         if c.get("kind") == "residual"), None)
    if residual_idx is None:
        return None
    residual_mass = float(cards[residual_idx].get("lostPotency", 0.0) or 0.0)
    avail = residual_mass - _RESIDUAL_KEEP
    if avail < _BUCKET_FLOOR:
        return None

    if progress is not None:
        progress(65, "Re-simulating counterfactuals…")
    coarse = _coarse_cuts(ctx)
    scores = ctx.runner.scores(coarse)
    coarse = [c for c in coarse if c in scores]
    segs = list(zip(coarse, coarse[1:]))
    raw = {s: scores[s[0]] - scores[s[1]] for s in segs}
    losses = {s: max(0.0, v) for s, v in raw.items()}

    # Fine pass over the top segments.
    top = sorted(segs, key=lambda s: (-losses[s], s[0]))[:_FINE_SEGMENTS]
    top = [s for s in top if losses[s] > _SEG_MIN_UNEXPLAINED]
    fine_cut_list: list[float] = []
    for s in top:
        fine_cut_list.extend(_fine_cuts(ctx, s, cards))
    if progress is not None:
        progress(72, "Re-simulating counterfactuals… (fine pass)")
    fine_scores = ctx.runner.scores(fine_cut_list) if fine_cut_list else {}
    all_scores = dict(scores)
    all_scores.update(fine_scores)

    # Re-partition the examined segments with their sub-cuts.
    examined_segs: list[tuple[float, float]] = []
    for t0, t1 in sorted(top, key=lambda s: s[0]):
        inner = sorted(c for c in fine_scores if t0 < c < t1)
        bounds = [t0] + inner + [t1]
        examined_segs.extend(zip(bounds, bounds[1:]))
    for t0, t1 in sorted(segs, key=lambda s: s[0]):
        if (t0, t1) not in top:
            examined_segs.append((t0, t1))
    examined_segs.sort(key=lambda s: s[0])

    # Unexplained loss per segment: cascade loss minus what located cards
    # already price.
    unexplained: list[tuple[tuple[float, float], float]] = []
    for t0, t1 in examined_segs:
        loss = max(0.0, all_scores.get(t0, 0.0) - all_scores.get(t1, 0.0))
        rest = max(0.0, loss - _located_claims(cards, t0, t1))
        if rest >= _SEG_MIN_UNEXPLAINED:
            unexplained.append(((t0, t1), rest))
    if not unexplained:
        return None
    unexplained.sort(key=lambda r: (-r[1], r[0][0]))
    unexplained = unexplained[:_FINE_SEGMENTS]

    # Evidence deltas for the winning segments (pooled, ≤3 calls).
    if progress is not None:
        progress(82, "Reading the state deltas…")
    deltas = ctx.runner.deltas([s for s, _v in unexplained])
    evidence: dict[tuple[float, float],
                   tuple[list[EvidenceRow], list[GaugeText]]] = {
        s: _delta_evidence(d, ctx.gauge_text)
        for (s, _v), d in zip(unexplained, deltas)}

    # Match job RootCauses to segments (first candidate in a segment wins the
    # share; later ones join its evidence). Segments with no candidate get the
    # generic sequencing card, titled by the held resources.
    weighted: list[tuple[RootCause, float]] = []
    for seg, rest in unexplained:
        t0, t1 = seg
        inside = [c for c in causes if t0 <= float(c.time_sec) < t1]
        rows, resources = evidence.get(seg, ([], []))
        if inside:
            lead, extra = inside[0], inside[1:]
            merged = list(lead.evidence)
            merged += [EvidenceRow(k="Also", v="",
                                   note=TEXT["also_note"].format(
                                       summary=c.summary))
                       for c in extra]
            lead = RootCause(**{**lead.__dict__, "evidence": merged,
                                "segment": seg})
            weighted.append((lead, rest))
        else:
            weighted.append((_generic_cause(seg, rows, resources), rest))

    # Conservation: scale the weights into the residual's actual budget.
    # Prices floor to the 0.1 grid so Σ moved can never round past `avail`
    # (the residual keeps its 60p floor exactly); each card carries the same
    # gridded number that `moved` absorbs, so the total is conserved by
    # construction.
    total = sum(v for _c, v in weighted)
    if total <= 0:
        return None
    scale = min(1.0, avail / total)
    promoted: list[dict] = []
    moved = 0.0
    for cause, weight in weighted:
        priced = math.floor(weight * scale * 10.0) / 10.0
        if priced < _BUCKET_FLOOR:
            continue
        extra_rows, _res = evidence.get(cause.segment or (0.0, 0.0), ([], []))
        promoted.append(_promote(cause, priced, extra_rows))
        moved += priced
    if not promoted:
        return None

    if progress is not None:
        progress(90, "Re-attributing the diffuse mass…")
    original_sum = round(sum(float(c.get("lostPotency", 0.0) or 0.0)
                             for c in cards), 1)
    examined_cards: list[dict] = []
    for i, c in enumerate(cards):
        if i == residual_idx:
            shrunk = dict(c)
            shrunk["lostPotency"] = round(residual_mass - moved, 1)
            examined_cards.append(shrunk)
        else:
            examined_cards.append(dict(c))
    examined_cards.extend(promoted)
    examined_cards.sort(key=lambda c: (-float(c.get("lostPotency", 0.0) or 0.0),
                                       str(c.get("kind")),
                                       int(c.get("abilityId", 0) or 0),
                                       float(c.get("timeSec", 0.0) or 0.0)))
    new_sum = round(sum(float(c.get("lostPotency", 0.0) or 0.0)
                        for c in examined_cards), 1)
    if abs(new_sum - original_sum) > 0.25:      # cent-level tolerance on rounds
        raise AssertionError(
            f"examined sum {new_sum} != original {original_sum}")

    notes = [TEXT["note_moved"].format(
        moved=moved, n=len(promoted),
        plural="s" if len(promoted) != 1 else "")]
    if scale < 1.0:
        notes.append(TEXT["note_scaled"].format(scale=scale))
    basis = ("multiTarget"
             if (ctx.scoring_state or {}).get("multi_target_credited")
             else "strict")
    return {
        "improvements": examined_cards,
        "recoverable": original_sum,
        "basis": basis,
        "notes": notes,
    }


def resolve_pack(job: str):
    """The job's registered AdvicePack, or None. A bare-callable registration
    (tests / minimal jobs) is wrapped on the fly."""
    if not job:
        return None
    from jobs import get_job
    from jobs._core.advice import AdvicePack
    reg = getattr(get_job(job), "advice_probes", None)
    if reg is None:
        return None
    return reg if isinstance(reg, AdvicePack) else AdvicePack(probes=reg)


def _merge_items_into_cards(cards: list[dict], items: list[dict]) -> None:
    """Fold advice items straight into their cards (triple-keyed): the probe
    prescription replaces the static one; labelled evidence rows, a sharper
    title, and count-gap bar data ride along when the probe produced them.
    This is what lets the response ship enriched cards directly — no
    client-side merge."""
    by_key = {(it["kind"], int(it["abilityId"]),
               round(float(it["timeSec"]), 1)): it for it in items}
    for card in cards:
        hit = by_key.get(_triple(card))
        if hit is None:
            continue
        card["prescription"] = hit["prescription"]
        if hit.get("evidence"):
            card["evidence"] = list(hit["evidence"])[:3]
        if hit.get("summary"):
            card["summary"] = hit["summary"]
        if hit.get("countGaps"):
            card["countGaps"] = list(hit["countGaps"])


def compute_advice_v2(ctx: AdviceContext, cards: list[dict],
                      progress=None) -> dict:
    """The full deep pass, run inline by run_analysis's response build:
    analytic + job probes computed as `advice` items and MERGED into `cards`
    in place (prescriptions upgraded, details attached), then the cascade
    restructure into `examined` (None whenever the job has no pack, the sim
    modules don't support the cascade, or nothing is worth moving). The
    examined list is built from the already-enriched cards, so both views
    carry the pointed feedback."""
    casts = sorted((t, a) for t, a in ctx.norm_casts if t >= 0)
    f = (ctx.clipping_state or {}).get("clipping")
    worst_idle = list(getattr(f, "worst_idle", []) or [])

    pack = resolve_pack(ctx.job)
    job_items: dict[tuple, dict] = {}
    causes: list[RootCause] = []
    if pack is not None:
        try:
            items, causes = pack.probes(ctx, cards, progress)
            for it in items:
                d = _item({"kind": it.kind, "abilityId": it.ability_id,
                           "timeSec": it.time_sec},
                          it.prescription, evidence=list(it.evidence),
                          summary=it.summary)
                job_items[(it.kind, int(it.ability_id),
                           round(float(it.time_sec), 1))] = d
        except Exception:
            traceback.print_exc(file=sys.stderr)
            job_items, causes = {}, []

    advice = _analytic_items(cards, casts, worst_idle, ctx.idealized,
                             ctx.data, job_items, progress=progress)
    _merge_items_into_cards(cards, advice)

    examined = None
    if ctx.runner is not None and pack is not None:
        try:
            examined = _cascade_examined(ctx, cards, causes, progress=progress)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            examined = None
    return {"advice": advice, "examined": examined}
