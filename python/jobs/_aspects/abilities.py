"""AbilityTimelineAspect — every player cast as an icon-tagged event.

Job-agnostic: drives the Timeline view and the Cast counts view for every
supported job. Reads ability metadata from `jobs._core.ability_metadata`.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Any

from fflogs_api import FFLogsClient
from icon_cache import ICONS

from jobs._core import ability_metadata
from jobs._core.aspect import (
    AspectComparison,
    AspectResult,
    PRE_PULL_LOOKBACK_S,
    Track,
    TrackEvent,
)
from jobs._core.casts import fetch_norm_casts


# A neutral fill behind icons (only seen when icon download fails).
_FALLBACK_COLOR = "#777777"


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


class AbilityTimelineAspect:
    """Every player cast, as an icon-tagged event on a single track."""

    name = "Abilities"

    def __init__(self, prepull_buff_ids: dict[int, int] | None = None):
        # ability_id -> buff status id for clipped-instant pre-pull casts
        # (JobData.prepull_buff_ids). Empty for most jobs — only set where an
        # instant precast (MCH Reassemble) is dropped by FFLogs but its buff
        # survives, so we can reconstruct the cast in the pre-pull zone.
        self._prepull_buff_ids = dict(prepull_buff_ids or {})

    def _detect_prepull_buffs(self, client, code: str, fight: dict[str, Any],
                              actor: dict[str, Any]) -> list[int]:
        """Ability ids whose buff is PRE-APPLIED at the pull (its first event in
        the fight is a remove/refresh, never an apply) — proof the player precast
        the (clipped) instant ability. FFLogs encodes a status as either its raw
        id or `1000000 + id`, so match both."""
        if not self._prepull_buff_ids:
            return []
        start, end = fight["startTime"], fight["endTime"]
        fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
        try:
            auras = client.get_aura_events(code, fetch_start, end, actor["id"],
                                           data_type="Buffs")
        except Exception:
            return []
        inferred: list[int] = []
        for ability_id, status_id in self._prepull_buff_ids.items():
            toks = {status_id, 1_000_000 + status_id}
            evs = sorted((e for e in auras if e.get("abilityGameID") in toks),
                         key=lambda e: e.get("timestamp", 0))
            if evs and evs[0].get("type") in ("removebuff", "refreshbuff"):
                inferred.append(ability_id)
        return inferred

    def analyze(self, client: FFLogsClient, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        # Use the shared, GCD-START-aligned cast stream (begincast for hardcasts,
        # cast for instants) so the player lane lines up with the simulated lane
        # and with scoring/drift — instead of raw `cast`-completion timestamps,
        # which for a caster put every hardcast ~a cast-time later than its GCD
        # slot. Includes the pre-pull look-back (negative t) and is cached, so
        # this collapses onto the same fetch the other aspects already make.
        norm_casts = fetch_norm_casts(client, code, fight, actor)

        events: list[TrackEvent] = []
        ability_counts: dict[int, int] = {}
        icon_paths: set[str] = set()

        for t_rel, aid in norm_casts:
            meta = ability_metadata.get_metadata(aid)
            if meta is None:
                # Unresolvable — skip silently (could be a buff/proc cast).
                continue
            ability_counts[aid] = ability_counts.get(aid, 0) + 1
            kind = "oGCD" if meta.is_ogcd else "GCD"
            events.append(TrackEvent(
                start_s=t_rel,
                end_s=t_rel + 0.5,
                color=_FALLBACK_COLOR,
                label=meta.name[:3] if meta.name else "?",
                tooltip=f"{meta.name}  ({kind})  @  {_mmss(t_rel)}",
                icon_path=meta.icon,
                # Lift oGCDs to the upper sub-row; GCDs sit at lane center.
                y_offset=-0.55 if meta.is_ogcd else 0.0,
                ability_id=aid,
            ))
            icon_paths.add(meta.icon)

        # Pre-warm the icon disk cache from this worker thread so the GUI
        # thread doesn't block downloading icons one-by-one during rendering.
        ICONS.warm(icon_paths)

        state: dict[str, Any] = {
            "ability_counts": ability_counts,
            "total_casts": len(events),
        }
        # Clipped-instant pre-pull casts the player made (proven by a pre-applied
        # buff) — the sidecar reconstructs them in the Timeline's pre-pull zone.
        inferred = self._detect_prepull_buffs(client, code, fight, actor)
        if inferred:
            state["prepull_inferred_casts"] = inferred

        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=events),
            state=state,
        )

    def compare(self, you: AspectResult, refs: list[AspectResult]) -> AspectComparison:
        you_counts: dict[int, int] = you.state.get("ability_counts", {}) or {}
        ref_counts_list: list[dict[int, int]] = [r.state.get("ability_counts", {}) or {} for r in refs]

        findings: list[str] = []
        if not refs:
            findings.append("No reference runs to compare against.")
        else:
            # Surface big count gaps for abilities top players use that you barely do.
            for aid, ref_counts in _aggregate_counts(ref_counts_list).items():
                ref_med = median(ref_counts) if ref_counts else 0
                you_n = you_counts.get(aid, 0)
                meta = ability_metadata.get_metadata(aid)
                name = meta.name if meta else f"action {aid}"
                if ref_med >= 3 and you_n <= ref_med // 2:
                    findings.append(
                        f"Low {name}: you cast {you_n}, top performers cast ~{ref_med}."
                    )

        if not findings:
            findings.append("Visual comparison — look at the timeline for differences.")

        # Detail tab: per-ability count, your N vs ref median.
        detail_columns = ["Ability", "Your casts", "Ref median"]
        rows: list[list[Any]] = []
        all_ids = set(you_counts.keys())
        for c in ref_counts_list:
            all_ids.update(c.keys())
        ref_agg = _aggregate_counts(ref_counts_list)
        for aid in sorted(all_ids, key=lambda i: -you_counts.get(i, 0)):
            meta = ability_metadata.get_metadata(aid)
            name = meta.name if meta else f"action {aid}"
            ref_med = int(round(median(ref_agg[aid]))) if ref_agg.get(aid) else 0
            rows.append([name, you_counts.get(aid, 0), ref_med])

        summary_lines = [f"Your total casts: {you.state.get('total_casts', 0)}"]
        if refs:
            ref_totals = [r.state.get("total_casts", 0) for r in refs]
            summary_lines.append(
                f"Reference total casts: median={int(round(median(ref_totals)))} "
                f"avg={mean(ref_totals):.0f}"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=findings,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
            text_timeline_rows=[],
        )


def _aggregate_counts(per_run_counts: list[dict[int, int]]) -> dict[int, list[int]]:
    """Flatten per-run counts into {ability_id: [count_in_each_run]}."""
    out: dict[int, list[int]] = {}
    for counts in per_run_counts:
        for aid, n in counts.items():
            out.setdefault(aid, []).append(n)
    # Pad with zeros for runs where the ability wasn't used at all
    for aid in out:
        deficit = len(per_run_counts) - len(out[aid])
        if deficit > 0:
            out[aid].extend([0] * deficit)
    return out
