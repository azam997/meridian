"""Dancer proc utilization — DNC's signature RNG mechanic.

DNC has four proc statuses:
  * **Silken Symmetry** (from Cascade) -> Reverse Cascade, and
  * **Silken Flow** (from Fountain) -> Fountainfall — the GCD procs (the Verfire /
    Verstone analog). A wasted one means a basic Cascade/Fountain was cast where a
    Reverse Cascade/Fountainfall could have been: ~60p + a forfeited feather/esprit.
  * **Threefold Fan Dance** (from Fan Dance) -> Fan Dance III, and
  * **Fourfold Fan Dance** (from Flourish) -> Fan Dance IV — free oGCD procs. A
    wasted one loses the WHOLE button (200 / 420p), since nothing substitutes for it.

A proc is **wasted** when it's overwritten (re-proced before being spent — a
`refreshbuff`) or expires unused. This aspect drives the dashboard's Proc
Utilization panel + a priced [proc] improvement card.

The buff stream is the player's own (`get_aura_events(..., "Buffs")`), already
warmed by `analyze_pull`'s prefetch bundle, so this is a cache hit. Falls back to
"all granted procs were used" (no waste, no penalty) when the buff stream is
unavailable or the status ids don't resolve — never penalize on missing data.
"""
from __future__ import annotations

from typing import Any

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.improvements import Improvement, _mmss
from jobs.dancer import data as dd


# A wasted Silken proc costs the premium of the Reverse Cascade/Fountainfall it
# would have been over the basic combo GCD that filled the slot instead.
_SILKEN_VALUE_P = dd.POTENCIES[dd.REVERSE_CASCADE] - dd.POTENCIES[dd.CASCADE]   # ~60
# A wasted Fan Dance proc loses the whole oGCD (nothing substitutes for it).
_FAN3_VALUE_P = dd.POTENCIES[dd.FAN_DANCE_III]   # 200
_FAN4_VALUE_P = dd.POTENCIES[dd.FAN_DANCE_IV]    # 420

# FFLogs surfaces a status in aura events under the ability id (1000000 +
# statusID). Match either form so the aspect is robust to the encoding.
_FFLOGS_BUFF_OFFSET = 1000000

# (status_id, status name, consuming_ability_id, consumer name, per-waste
# potency) for each proc.
_PROCS: tuple[tuple[int, str, int, str, int], ...] = (
    (dd.SILKEN_SYMMETRY_STATUS_ID, "Silken Symmetry",
     dd.REVERSE_CASCADE, "Reverse Cascade", _SILKEN_VALUE_P),
    (dd.SILKEN_FLOW_STATUS_ID, "Silken Flow",
     dd.FOUNTAINFALL, "Fountainfall", _SILKEN_VALUE_P),
    (dd.THREEFOLD_FAN_DANCE_STATUS_ID, "Threefold Fan Dance",
     dd.FAN_DANCE_III, "Fan Dance III", _FAN3_VALUE_P),
    (dd.FOURFOLD_FAN_DANCE_STATUS_ID, "Fourfold Fan Dance",
     dd.FAN_DANCE_IV, "Fan Dance IV", _FAN4_VALUE_P),
)


def _grants(events: list[dict], status_id: int, fight_start_ms: int
            ) -> tuple[int, int, list[float]]:
    """(total grants, overwrites, overwrite times) for one proc status from its
    buff events. A `refresh` (re-applied while active) is an overwrite — a fresh
    proc on top of an unspent one — and counts as both a grant and an overwrite;
    its timestamp locates the waste (expiry waste, by contrast, is inferred by
    count subtraction and has no event to point at)."""
    want = {status_id, _FFLOGS_BUFF_OFFSET + status_id}
    grants = overwrites = 0
    overwrite_times: list[float] = []
    for ev in events:
        if ev.get("abilityGameID") not in want:
            continue
        typ = ev.get("type", "")
        if typ in ("applybuff", "applydebuff"):
            grants += 1
        elif typ in ("refreshbuff", "refreshdebuff"):
            grants += 1
            overwrites += 1
            overwrite_times.append(
                (ev.get("timestamp", fight_start_ms) - fight_start_ms) / 1000.0)
    return grants, overwrites, overwrite_times


def _proc_stats(client, code: str, fight: dict[str, Any], actor: dict[str, Any],
                norm_casts) -> dict[str, Any]:
    s, e = fight["startTime"], fight["endTime"]
    try:
        buffs = client.get_aura_events(code, s, e, actor["id"], "Buffs")
    except Exception:
        buffs = []

    grants = used = wasted = overwrites = 0
    lost = 0.0
    overwrite_events: list[dict[str, Any]] = []
    for status_id, status_name, consume_id, consumer_name, value_p in _PROCS:
        u = sum(1 for t, aid in norm_casts if t >= 0 and aid == consume_id)
        g, ow, owt = _grants(buffs, status_id, s)
        # Guard against wrong/absent status ids: can't use more than granted.
        g = max(g, u)
        w = max(0, g - u)
        grants += g
        used += u
        wasted += w
        overwrites += ow
        lost += w * value_p
        # Located overwrite records (additive) — feed the improvement card's
        # children. Expiry waste stays count-only (no event to point at).
        overwrite_events.extend(
            {"time_s": t, "status_name": status_name,
             "consumer_name": consumer_name, "consumer_id": consume_id,
             "lost_potency": float(value_p)} for t in owt)

    util = round(100.0 * used / grants, 1) if grants else 100.0
    return {
        "total_grants": grants,
        "total_used": used,
        "total_wasted": wasted,
        "overwrites": overwrites,
        "overwrite_events": sorted(overwrite_events, key=lambda m: m["time_s"]),
        "utilization_pct": util,
        "lost_potency": round(lost, 1),
    }


class ProcsAspect:
    """Measures Silken / Fan Dance proc utilization. Drives the dashboard Proc
    Utilization panel and a small priced [proc] improvement card."""

    name = "Procs"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        state = _proc_stats(client, code, fight, actor, norm_casts)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state=state,
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        st = you.state
        wasted = int(st.get("total_wasted", 0) or 0)
        util = float(st.get("utilization_pct", 100.0))
        findings: list[str] = []
        if wasted >= 2:
            findings.append(
                f"[proc] Proc utilization {util:.0f}% — {wasted} "
                f"Silken/Fan Dance proc{'s' if wasted != 1 else ''} wasted "
                f"(overwritten or expired)")
        return AspectComparison(aspect_name=self.name, findings=findings)


def improvements_from_procs(state: dict) -> list[Improvement]:
    """A priced card for procs wasted by overwrite/expiry. Overwrites are
    located (the refreshbuff timestamp): a single one makes the card itself
    jumpable, 2+ become located children; expiry waste is count-inferred and
    stays unlocated. Zero-priced (no card) at full utilization."""
    lost = float(state.get("lost_potency", 0.0) or 0.0)
    wasted = int(state.get("total_wasted", 0) or 0)
    if lost <= 0.0 or wasted <= 0:
        return []
    util = float(state.get("utilization_pct", 100.0))
    # Old-shape states (no `overwrite_events`) degrade to the unlocated card.
    ows = state.get("overwrite_events") or []
    expired = max(0, wasted - len(ows))
    children: list[Improvement] = []
    if len(ows) >= 2:
        for m in ows:
            t = float(m.get("time_s", 0.0) or 0.0)
            status = m.get("status_name") or "a proc"
            consumer = m.get("consumer_name") or "the proc"
            children.append(Improvement(
                kind="proc", ability_id=int(m.get("consumer_id", 0) or 0),
                ability_name=consumer, time_s=t,
                lost_potency=float(m.get("lost_potency", 0.0) or 0.0),
                summary=f"{_mmss(t)} — overwrote {status}: use {consumer} first"))
    # With children the card is expandable; with exactly one overwrite it jumps
    # straight there — carrying that overwrite's ACTUAL consumer so the
    # timeline's ability-aware highlight targets the right proc; all-expiry
    # waste keeps the historic unlocated card. Whenever any waste is unlocated
    # expiry, say so — the located time only represents overwrites.
    t0 = float(ows[0].get("time_s", 0.0) or 0.0) if ows else 0.0
    lead_id, lead_name = dd.REVERSE_CASCADE, "Silken / Fan Dance procs"
    if len(ows) == 1:
        lead_id = int(ows[0].get("consumer_id", 0) or 0)
        lead_name = ows[0].get("consumer_name") or lead_name
    expired_note = (
        f", plus {expired} expired unused" if ows and expired > 0 else "")
    return [Improvement(
        kind="proc", ability_id=lead_id, ability_name=lead_name,
        time_s=t0, lost_potency=lost,
        summary=f"{wasted} proc{'s' if wasted != 1 else ''} wasted "
                f"(utilization {util:.0f}%) — spend Reverse Cascade / Fountainfall / "
                f"Fan Dance III before re-procing{expired_note}",
        children=children)]
