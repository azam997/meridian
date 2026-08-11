"""Verfire / Verstone proc utilization — RDM's signature RNG mechanic.

Verthunder III / Veraero III (and the finishers / Acceleration) grant Verfire
Ready / Verstone Ready, which unlock the 2 s-cast instants Verfire / Verstone.
A proc is **wasted** when it's overwritten (you re-proc the same color before
spending the one you had — a `refreshbuff`) or expires unused. Each wasted proc
means a Jolt III filler (360) was cast where a Verfire/Verstone (380) could have
been — so the direct potency cost is small (~20p each), but the *count* is the
story this aspect tells, and it drives the dashboard's Proc Utilization panel.

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
from jobs.redmage import data as rd


# A wasted proc costs the premium of the Verfire/Verstone it would have been over
# the Jolt III that filled the slot instead.
PROC_VALUE_P = rd.POTENCIES[rd.VERFIRE] - rd.POTENCIES[rd.JOLT_III]   # 380 - 360

# FFLogs surfaces a status in aura events under the ability id (1000000 +
# statusID) — so Verfire Ready (status 1234) arrives as 1001234. Match either
# form so the aspect is robust to the encoding (same quirk as death_design.py).
_FFLOGS_BUFF_OFFSET = 1000000


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

    used_fire = sum(1 for t, aid in norm_casts if t >= 0 and aid == rd.VERFIRE)
    used_stone = sum(1 for t, aid in norm_casts if t >= 0 and aid == rd.VERSTONE)

    g_fire, ow_fire, owt_fire = _grants(buffs, rd.VERFIRE_READY_STATUS_ID, s)
    g_stone, ow_stone, owt_stone = _grants(buffs, rd.VERSTONE_READY_STATUS_ID, s)
    # Located overwrite records (additive) — feed the improvement card's
    # children. Expiry waste stays count-only (no event to point at).
    overwrite_events = sorted(
        [{"time_s": t, "status_name": "Verfire Ready",
          "consumer_name": "Verfire", "consumer_id": rd.VERFIRE,
          "lost_potency": float(PROC_VALUE_P)} for t in owt_fire]
        + [{"time_s": t, "status_name": "Verstone Ready",
            "consumer_name": "Verstone", "consumer_id": rd.VERSTONE,
            "lost_potency": float(PROC_VALUE_P)} for t in owt_stone],
        key=lambda m: m["time_s"])
    # Guard against wrong/absent status ids (no buff events): a player can't use
    # more procs than they were granted, so floor grants at the used count and
    # report zero waste rather than a nonsensical negative.
    g_fire = max(g_fire, used_fire)
    g_stone = max(g_stone, used_stone)

    waste_fire = max(0, g_fire - used_fire)
    waste_stone = max(0, g_stone - used_stone)
    grants = g_fire + g_stone
    used = used_fire + used_stone
    wasted = waste_fire + waste_stone
    util = round(100.0 * used / grants, 1) if grants else 100.0

    return {
        "verfire_grants": g_fire,
        "verfire_used": used_fire,
        "verfire_wasted": waste_fire,
        "verstone_grants": g_stone,
        "verstone_used": used_stone,
        "verstone_wasted": waste_stone,
        "overwrites": ow_fire + ow_stone,
        "overwrite_events": overwrite_events,
        "total_grants": grants,
        "total_used": used,
        "total_wasted": wasted,
        "utilization_pct": util,
        "lost_potency": round(wasted * PROC_VALUE_P, 1),
    }


class ProcsAspect:
    """Measures Verfire/Verstone proc utilization. Drives the dashboard Proc
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
                f"[proc] Proc utilization {util:.0f}% — {wasted} Verfire/Verstone "
                f"proc{'s' if wasted != 1 else ''} wasted (overwritten or expired)")
        return AspectComparison(aspect_name=self.name, findings=findings)


def improvements_from_procs(state: dict) -> list[Improvement]:
    """A priced card for procs wasted by overwrite/expiry. Low-magnitude by
    nature (a proc is ~20p over its Jolt III filler), so it usually folds into
    the 'Other' breakdown rather than leading — but it makes the loss explicit.
    Overwrites are located (the refreshbuff timestamp): a single one makes the
    card itself jumpable, 2+ become located children; expiry waste is
    count-inferred and stays unlocated. Zero-priced (no card) at full
    utilization."""
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
                summary=f"{_mmss(t)} — overwrote {status}: spend {consumer} "
                        f"before granting a new proc of that color"))
    # With children the card is expandable; with exactly one overwrite it jumps
    # straight there — carrying that overwrite's ACTUAL consumer so the
    # timeline's ability-aware highlight targets the right proc color; all-
    # expiry waste keeps the historic unlocated card. Whenever any waste is
    # unlocated expiry, say so — the located time only represents overwrites.
    t0 = float(ows[0].get("time_s", 0.0) or 0.0) if ows else 0.0
    lead_id, lead_name = rd.VERFIRE, "Verfire / Verstone"
    if len(ows) == 1:
        lead_id = int(ows[0].get("consumer_id", 0) or 0)
        lead_name = ows[0].get("consumer_name") or lead_name
    expired_note = (
        f", plus {expired} expired unused" if ows and expired > 0 else "")
    return [Improvement(
        kind="proc", ability_id=lead_id, ability_name=lead_name,
        time_s=t0, lost_potency=lost,
        summary=f"{wasted} proc{'s' if wasted != 1 else ''} wasted "
                f"(utilization {util:.0f}%) — spend Verfire/Verstone before "
                f"re-procing the same color{expired_note}",
        children=children)]
