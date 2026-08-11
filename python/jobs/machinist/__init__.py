"""Machinist aspects: Queen, Wildfire, Hypercharge, Tools."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median
from typing import Any

from fflogs_api import FFLogsClient
from icon_cache import ICONS

from jobs._core.aspect import (
    AspectComparison, AspectResult, PRE_PULL_LOOKBACK_S, Track, TrackEvent,
)
from jobs._core import ability_metadata
from jobs._core.deaths import read_deaths_from_report
from jobs._core.downtime import read_downtime_from_report

# Register Machinist with the Job registry. The aspects + simulator wire-up
# happens at module bottom (after JOB_ASPECTS is defined).
from jobs._core.job import register as _register
from jobs.machinist.data import JOB_DATA

# --- Constants ---------------------------------------------------------------

QUEEN_ABILITY_ID = 16501
QUEEN_PET_NAME = "Automaton Queen"
CROWNED_COLLIDER_ID = 25787

BATTERY_GENERATORS: dict[int, int] = {
    16500: 20,  # Air Anchor
    36981: 20,  # Excavator
    25788: 20,  # Chain Saw
    7413:  10,  # Heated Clean Shot
}
BATTERY_MAX = 100

BURST_WINDOW_S = 30.0
WINDOW_TOLERANCE_S = 12.0


# --- Internal Queen model ---------------------------------------------------

@dataclass
class QueenCast:
    time_s: float
    bucket: int
    battery: int = 0
    pet_damage: int = 0
    duration_s: float = 0.0
    finished: bool = False


# --- Actor / fight helpers ---------------------------------------------------
# Actor + fight lookup live in jobs._core.actors; only Queen-pet lookup is MCH-specific.

def find_queen_pets(report: dict[str, Any], owner_id: int) -> list[dict[str, Any]]:
    return [
        a for a in report["masterData"]["actors"]
        if a["type"] == "Pet" and a.get("petOwner") == owner_id
        and a.get("name") == QUEEN_PET_NAME
    ]


# --- Color helpers -----------------------------------------------------------

def _battery_color(battery: int) -> str:
    """Linear interpolation 50→100 over red→amber→green."""
    b = max(50, min(100, battery))
    t = (b - 50) / 50.0
    if t < 0.5:
        u = t / 0.5
        r, g, bl = _lerp((0xc0, 0x39, 0x2b), (0xe6, 0x7e, 0x22), u)
    else:
        u = (t - 0.5) / 0.5
        r, g, bl = _lerp((0xe6, 0x7e, 0x22), (0x27, 0xae, 0x60), u)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _cluster_times(sorted_times: list[float], gap: float) -> list[list[float]]:
    clusters: list[list[float]] = []
    for t in sorted_times:
        if clusters and t - clusters[-1][-1] <= gap:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return clusters


# --- Queen aspect ------------------------------------------------------------

class QueenAspect:
    name = "Queen"

    def analyze(self, client: FFLogsClient, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        queens = _scan_queens(client, code, fight, actor, report)
        events = [_queen_to_event(i + 1, q) for i, q in enumerate(queens)]
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=events),
            state={
                "queens": queens,
                "total_queen_damage": sum(q.pet_damage for q in queens),
            },
        )

    def compare(self, you: AspectResult, refs: list[AspectResult]) -> AspectComparison:
        you_queens: list[QueenCast] = you.state.get("queens", [])
        ref_queen_lists: list[list[QueenCast]] = [r.state.get("queens", []) for r in refs]

        findings: list[str] = []

        if not refs:
            findings.append("No reference runs to compare against.")
        else:
            ref_counts = [len(qs) for qs in ref_queen_lists]
            ref_median_count = int(round(median(ref_counts)))
            delta = ref_median_count - len(you_queens)
            if delta >= 1:
                findings.append(
                    f"Queen count low: you cast {len(you_queens)}, "
                    f"top performers typically cast {ref_median_count} "
                    f"(range {min(ref_counts)}-{max(ref_counts)})."
                )
            elif delta <= -1:
                findings.append(
                    f"Queen count high: you cast {len(you_queens)} vs median {ref_median_count}. "
                    "Not necessarily wrong, but unusual."
                )

            ref_cast_times = sorted(q.time_s for qs in ref_queen_lists for q in qs)
            if ref_cast_times:
                clusters = _cluster_times(ref_cast_times, gap=BURST_WINDOW_S)
                ref_windows = [(mean(c), len(c) / len(refs)) for c in clusters]
                ref_windows = [(t, freq) for t, freq in ref_windows if freq >= 0.4]
                your_times = [q.time_s for q in you_queens]
                for win_t, freq in ref_windows:
                    if not any(abs(t - win_t) <= WINDOW_TOLERANCE_S for t in your_times):
                        pct = min(100, int(freq * 100))
                        findings.append(
                            f"Missing Queen near ~{int(win_t)}s (used by {pct}% of references)."
                        )
                for t in your_times:
                    if not any(abs(t - win_t) <= WINDOW_TOLERANCE_S for win_t, _ in ref_windows):
                        findings.append(
                            f"Extra/off-window Queen at {int(t)}s "
                            f"(no reference window within {int(WINDOW_TOLERANCE_S)}s)."
                        )

            all_ref_batteries = sorted(q.battery for qs in ref_queen_lists for q in qs)
            if all_ref_batteries:
                ref_p25 = all_ref_batteries[max(0, len(all_ref_batteries) // 4 - 1)]
                for q in you_queens:
                    if q.battery and q.battery + 10 < ref_p25:
                        findings.append(
                            f"Low-battery Queen at {int(q.time_s)}s: {q.battery} battery "
                            f"vs reference 25th percentile {ref_p25}."
                        )

        # Cut-off Queens — skip the last one if it ran into a kill.
        you_dur = max((q.time_s + q.duration_s for q in you_queens), default=0.0)
        for i, q in enumerate(you_queens):
            if q.finished:
                continue
            is_last = (i == len(you_queens) - 1)
            near_kill = (you_dur - q.time_s) < 12.0
            if is_last and near_kill:
                continue
            findings.append(
                f"Queen at {int(q.time_s)}s ({q.battery} bat) was cut off — "
                f"no Crowned Collider fired (duration {q.duration_s:.1f}s)."
            )

        if refs and not findings:
            findings.append("No big pattern differences detected. Nice.")

        # Detail table.
        detail_columns = ["Q#", "Time", "Battery", "Duration", "End", "Damage"]
        your_rows: list[list[Any]] = []
        row_colors: list[str | None] = []
        for i, q in enumerate(you_queens, 1):
            your_rows.append([
                f"Q{i:02d}", _mmss(q.time_s), q.battery,
                f"{q.duration_s:.1f}s", "CC" if q.finished else "CUT",
                f"{q.pet_damage:,}",
            ])
            row_colors.append(_battery_color(q.battery))

        # Summary lines (rendered in the bottom footer).
        summary_lines = [f"Your queens: {len(you_queens)}"]
        if refs:
            ref_counts = [len(qs) for qs in ref_queen_lists]
            summary_lines.append(
                f"Reference count avg: {sum(ref_counts)/len(ref_counts):.1f}  "
                f"median={int(round(median(ref_counts)))}  "
                f"range={min(ref_counts)}-{max(ref_counts)}"
            )
        if you_queens:
            summary_lines.append(f"Your batteries: {[q.battery for q in you_queens]}")
        if you.state.get("total_queen_damage"):
            summary_lines.append(
                f"Your queen damage: {you.state['total_queen_damage']:,}  (info; high RNG)"
            )

        # Text-timeline rows: one per run, battery@m:ss in cast order.
        text_rows: list[tuple[str, str]] = [
            (you.run_label or "You", _fmt_row(you_queens)),
        ]
        for r_ar, r_qs in zip(refs, ref_queen_lists):
            text_rows.append((r_ar.run_label or "ref", _fmt_row(r_qs)))

        return AspectComparison(
            aspect_name=self.name,
            findings=findings,
            detail_columns=detail_columns,
            your_detail_rows=your_rows,
            your_detail_row_colors=row_colors,
            summary_lines=summary_lines,
            text_timeline_rows=text_rows,
        )


def _scan_queens(client: FFLogsClient, code: str, fight: dict[str, Any],
                 actor: dict[str, Any], report: dict[str, Any]) -> list[QueenCast]:
    start, end = fight["startTime"], fight["endTime"]
    # Pre-pull lookback: pre-cast Air Anchor's +20 battery should be visible
    # to the battery tracker, so the first Queen's recorded battery is right.
    fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
    cast_events = client.get_events(code, fetch_start, end, actor["id"], data_type="Casts")
    cast_events.sort(key=lambda e: e["timestamp"])

    battery = 0
    queens: list[QueenCast] = []
    for ev in cast_events:
        if ev.get("type") != "cast":
            continue
        aid = ev.get("abilityGameID")
        if aid in BATTERY_GENERATORS:
            battery = min(BATTERY_MAX, battery + BATTERY_GENERATORS[aid])
        elif aid == QUEEN_ABILITY_ID:
            t_rel = (ev["timestamp"] - start) / 1000.0
            queens.append(QueenCast(
                time_s=t_rel,
                bucket=int(t_rel // BURST_WINDOW_S),
                battery=battery,
            ))
            battery = 0

    queen_pets = find_queen_pets(report, actor["id"])
    pet_damage_events: list[dict[str, Any]] = []
    for pet in queen_pets:
        pet_damage_events.extend(
            client.get_events(code, start, end, pet["id"], data_type="DamageDone")
        )
    pet_damage_events.sort(key=lambda d: d["timestamp"])
    cc_timestamps = [d["timestamp"] for d in pet_damage_events
                     if d.get("abilityGameID") == CROWNED_COLLIDER_ID]
    cc_pos = 0

    for i, q in enumerate(queens):
        q_start = int(start + q.time_s * 1000)
        next_q_start = int(start + queens[i + 1].time_s * 1000) if i + 1 < len(queens) else end
        while cc_pos < len(cc_timestamps) and cc_timestamps[cc_pos] < q_start:
            cc_pos += 1
        if cc_pos < len(cc_timestamps) and cc_timestamps[cc_pos] < next_q_start:
            window_end = cc_timestamps[cc_pos]
            q.finished = True
            cc_pos += 1
        else:
            window_end = next_q_start
            q.finished = False
        q.duration_s = (window_end - q_start) / 1000.0
        for d in pet_damage_events:
            ts = d["timestamp"]
            if q_start <= ts <= window_end:
                q.pet_damage += int(d.get("amount", 0) or 0)

    return queens


def _queen_to_event(idx: int, q: QueenCast) -> TrackEvent:
    end_marker = "CC" if q.finished else "CUT"
    return TrackEvent(
        start_s=q.time_s,
        end_s=q.time_s + q.duration_s,
        color=_battery_color(q.battery),
        label=str(q.battery),
        tooltip=(f"Q{idx:02d}  bat={q.battery}  dur={q.duration_s:.1f}s  "
                 f"{end_marker}  dmg={q.pet_damage:,}"),
    )


def _fmt_row(queens: list[QueenCast]) -> str:
    if not queens:
        return "(none)"
    return "  ".join(f"{q.battery:>3}@{_mmss(q.time_s)}" for q in queens)


# --- Wildfire aspect ---------------------------------------------------------
# Wildfire is an oGCD that places a debuff; 10 seconds later it deals damage
# proportional to the number of weaponskill hits the target took during the
# window, capped at 6. The actionable metric is "hits captured": 6/6 is the
# ceiling, anything below is leakage.

WILDFIRE_ABILITY_ID = 2878
WILDFIRE_WINDOW_S = 10.0
WILDFIRE_HIT_CAP = 6


@dataclass
class WildfireWindow:
    cast_time_s: float
    hits: int           # weaponskill GCDs that landed in the 10s window (≤ cap)
    bucket: int


class WildfireAspect:
    name = "Wildfire"

    def analyze(self, client: FFLogsClient, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        start, end = fight["startTime"], fight["endTime"]
        fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
        cast_events = client.get_events(code, fetch_start, end, actor["id"], data_type="Casts")
        cast_events.sort(key=lambda e: e["timestamp"])

        # Single pass to gather (t_rel, ability_id) for casts, in time order.
        timeline: list[tuple[float, int]] = []
        wf_times: list[float] = []
        for ev in cast_events:
            if ev.get("type") != "cast":
                continue
            aid = ev.get("abilityGameID")
            if not aid:
                continue
            t_rel = (ev["timestamp"] - start) / 1000.0
            timeline.append((t_rel, aid))
            if aid == WILDFIRE_ABILITY_ID:
                wf_times.append(t_rel)

        windows: list[WildfireWindow] = []
        for t in wf_times:
            hits = 0
            window_end = t + WILDFIRE_WINDOW_S
            for t_rel, aid in timeline:
                if t_rel <= t:
                    continue
                if t_rel > window_end:
                    break  # timeline is sorted; nothing further can fall in window
                if aid == WILDFIRE_ABILITY_ID:
                    continue
                meta = ability_metadata.get_metadata(aid)
                # Weaponskills (and spells) are GCDs in xivapi terms — is_ogcd=False.
                # MCH has no spells, so this is the weaponskill set.
                if meta and not meta.is_ogcd:
                    hits += 1
            windows.append(WildfireWindow(
                cast_time_s=t,
                hits=min(hits, WILDFIRE_HIT_CAP),
                bucket=int(t // BURST_WINDOW_S),
            ))

        events = [_wildfire_to_event(i + 1, w) for i, w in enumerate(windows)]
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=events),
            state={"windows": windows},
        )

    def compare(self, you: AspectResult, refs: list[AspectResult]) -> AspectComparison:
        you_windows: list[WildfireWindow] = you.state.get("windows", [])
        ref_window_lists: list[list[WildfireWindow]] = [
            r.state.get("windows", []) for r in refs
        ]

        findings: list[str] = []

        if not refs:
            findings.append("No reference runs to compare against.")
        else:
            ref_counts = [len(ws) for ws in ref_window_lists]
            ref_median_count = int(round(median(ref_counts)))
            delta = ref_median_count - len(you_windows)
            if delta >= 1:
                findings.append(
                    f"Wildfire count low: you cast {len(you_windows)}, "
                    f"top performers typically cast {ref_median_count} "
                    f"(range {min(ref_counts)}-{max(ref_counts)})."
                )
            elif delta <= -1:
                findings.append(
                    f"Wildfire count high: you cast {len(you_windows)} vs median {ref_median_count}. "
                    "Not necessarily wrong, but unusual."
                )

            ref_hit_avgs = [mean(w.hits for w in ws) for ws in ref_window_lists if ws]
            if ref_hit_avgs and you_windows:
                ref_avg = mean(ref_hit_avgs)
                your_avg = mean(w.hits for w in you_windows)
                if your_avg + 0.4 < ref_avg:
                    findings.append(
                        f"Avg hits/Wildfire low: {your_avg:.1f} vs reference avg {ref_avg:.1f}."
                    )

        # Per-window undercut findings (regardless of refs — the cap is in-game).
        for i, w in enumerate(you_windows, 1):
            if w.hits < WILDFIRE_HIT_CAP:
                findings.append(
                    f"Wildfire #{i} at {_mmss(w.cast_time_s)} captured "
                    f"{w.hits}/{WILDFIRE_HIT_CAP} weaponskills "
                    f"(short by {WILDFIRE_HIT_CAP - w.hits})."
                )

        if refs and not findings:
            findings.append("No big pattern differences detected. Nice.")

        detail_columns = ["WF#", "Time", "Hits", "Short by"]
        your_rows: list[list[Any]] = []
        row_colors: list[str | None] = []
        for i, w in enumerate(you_windows, 1):
            short = WILDFIRE_HIT_CAP - w.hits
            your_rows.append([
                f"WF{i:02d}", _mmss(w.cast_time_s),
                f"{w.hits}/{WILDFIRE_HIT_CAP}", short,
            ])
            row_colors.append(_wildfire_color(w.hits))

        summary_lines = [f"Your Wildfires: {len(you_windows)}"]
        if you_windows:
            avg_hits = mean(w.hits for w in you_windows)
            summary_lines.append(f"Average hits/WF: {avg_hits:.1f}/{WILDFIRE_HIT_CAP}")
        if refs:
            ref_counts = [len(ws) for ws in ref_window_lists]
            summary_lines.append(
                f"Reference count avg: {sum(ref_counts)/len(ref_counts):.1f}  "
                f"median={int(round(median(ref_counts)))}  "
                f"range={min(ref_counts)}-{max(ref_counts)}"
            )
            ref_hit_avgs = [mean(w.hits for w in ws) for ws in ref_window_lists if ws]
            if ref_hit_avgs:
                summary_lines.append(f"Reference avg hits/WF: {mean(ref_hit_avgs):.1f}")

        text_rows: list[tuple[str, str]] = [
            (you.run_label or "You", _wildfire_fmt_row(you_windows)),
        ]
        for r_ar, r_ws in zip(refs, ref_window_lists):
            text_rows.append((r_ar.run_label or "ref", _wildfire_fmt_row(r_ws)))

        return AspectComparison(
            aspect_name=self.name,
            findings=findings,
            detail_columns=detail_columns,
            your_detail_rows=your_rows,
            your_detail_row_colors=row_colors,
            summary_lines=summary_lines,
            text_timeline_rows=text_rows,
        )


def _wildfire_color(hits: int) -> str:
    """0..6 hits → red→amber→green, same shape as `_battery_color`."""
    t = max(0, min(WILDFIRE_HIT_CAP, hits)) / WILDFIRE_HIT_CAP
    if t < 0.5:
        u = t / 0.5
        r, g, bl = _lerp((0xc0, 0x39, 0x2b), (0xe6, 0x7e, 0x22), u)
    else:
        u = (t - 0.5) / 0.5
        r, g, bl = _lerp((0xe6, 0x7e, 0x22), (0x27, 0xae, 0x60), u)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _wildfire_to_event(idx: int, w: WildfireWindow) -> TrackEvent:
    return TrackEvent(
        start_s=w.cast_time_s,
        end_s=w.cast_time_s + WILDFIRE_WINDOW_S,
        color=_wildfire_color(w.hits),
        label=f"{w.hits}/{WILDFIRE_HIT_CAP}",
        tooltip=(f"WF{idx:02d}  hits={w.hits}/{WILDFIRE_HIT_CAP}  "
                 f"start={_mmss(w.cast_time_s)}"),
    )


def _wildfire_fmt_row(windows: list[WildfireWindow]) -> str:
    if not windows:
        return "(none)"
    return "  ".join(f"{w.hits}@{_mmss(w.cast_time_s)}" for w in windows)


# --- Hypercharge aspect ------------------------------------------------------
# Hypercharge spends 50 Heat to grant Overheated: 5 stacks, each fired off as a
# Blazing Shot (1.5s recast). A full Hypercharge = 5 Blazing Shots; firing fewer
# (e.g. drifting out of the window, an oGCD eating a stack's worth of time, or
# the window ending) leaks the accelerated GCDs. Like Wildfire, the actionable
# metric is "Blazing Shots captured": 5/5 is the cap, anything below is leakage —
# but only blamed on the pilot when the window had room (not cut by the kill,
# downtime, or death).

HYPERCHARGE_ABILITY_ID = 17209
BLAZING_SHOT_ID = 36978
HYPERCHARGE_WINDOW_S = 8.0   # 5 Blazing Shots @ 1.5s + resolution buffer
HYPERCHARGE_BLAZING_CAP = 5


@dataclass
class HyperchargeWindow:
    cast_time_s: float
    hits: int            # Blazing Shots fired in the window (≤ cap)
    bucket: int
    cut_short: bool      # window curtailed by fight-end / downtime / death
    last_shot_s: float = 0.0   # cast time of the last Blazing Shot (for the bar)


def _overlaps_any(start: float, end: float,
                  windows: list[tuple[float, float]]) -> bool:
    return any(start < w_end and end > w_start for w_start, w_end in windows)


class HyperchargeAspect:
    name = "Hypercharge"

    def analyze(self, client: FFLogsClient, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        start, end = fight["startTime"], fight["endTime"]
        fight_duration_s = (end - start) / 1000.0
        fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
        cast_events = client.get_events(code, fetch_start, end, actor["id"], data_type="Casts")
        cast_events.sort(key=lambda e: e["timestamp"])

        timeline: list[tuple[float, int]] = []
        hc_times: list[float] = []
        for ev in cast_events:
            if ev.get("type") != "cast":
                continue
            aid = ev.get("abilityGameID")
            if not aid:
                continue
            t_rel = (ev["timestamp"] - start) / 1000.0
            timeline.append((t_rel, aid))
            if aid == HYPERCHARGE_ABILITY_ID and t_rel >= 0:
                hc_times.append(t_rel)

        # No-fault windows: while dead or with no target the player can't fire
        # Blazing Shots, so an underfill there isn't actionable.
        downtime, _src = read_downtime_from_report(report, timeline, fight_duration_s)
        no_fault = list(downtime) + list(read_deaths_from_report(report))

        windows: list[HyperchargeWindow] = []
        for idx, t in enumerate(hc_times):
            # Count Blazing Shots until the NEXT Hypercharge, not over a fixed
            # wall-clock window. Blazing Shot is castable only while Overheated,
            # and all 5 stacks from one Hypercharge resolve before the next is
            # pressed — so every Blazing Shot between two Hypercharges belongs to
            # the first. A fixed 8s window undercounted the 5th shot, whose cast
            # time drifts past +8s with skill speed and how the oGCD weaves in.
            next_hc = hc_times[idx + 1] if idx + 1 < len(hc_times) else float("inf")
            hits = 0
            last_shot_s = t
            for t_rel, aid in timeline:
                if t_rel <= t:
                    continue
                if t_rel >= next_hc:
                    break  # timeline sorted — into the next Hypercharge's window
                if aid == BLAZING_SHOT_ID:
                    hits += 1
                    last_shot_s = t_rel
            # cut_short: the window lacked room for a full 5 (fight ended or a
            # no-fault gap opened before the chain could finish).
            nominal_end = t + HYPERCHARGE_WINDOW_S
            cut_short = (nominal_end > fight_duration_s
                         or _overlaps_any(t, nominal_end, no_fault))
            windows.append(HyperchargeWindow(
                cast_time_s=t,
                hits=min(hits, HYPERCHARGE_BLAZING_CAP),
                bucket=int(t // BURST_WINDOW_S),
                cut_short=cut_short,
                last_shot_s=last_shot_s,
            ))

        events = [_hypercharge_to_event(i + 1, w) for i, w in enumerate(windows)]
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=events),
            state={"windows": windows},
        )

    def compare(self, you: AspectResult, refs: list[AspectResult]) -> AspectComparison:
        you_windows: list[HyperchargeWindow] = you.state.get("windows", [])
        ref_window_lists: list[list[HyperchargeWindow]] = [
            r.state.get("windows", []) for r in refs
        ]

        findings: list[str] = []
        # Per-window undercut findings (the in-game cap is absolute). Windows cut
        # by the kill / downtime / death are reported but flagged, not blamed.
        for i, w in enumerate(you_windows, 1):
            if w.hits >= HYPERCHARGE_BLAZING_CAP:
                continue
            short = HYPERCHARGE_BLAZING_CAP - w.hits
            tail = " (window cut short — not counted)" if w.cut_short else ""
            findings.append(
                f"Hypercharge #{i} at {_mmss(w.cast_time_s)} fired "
                f"{w.hits}/{HYPERCHARGE_BLAZING_CAP} Blazing Shots "
                f"(short by {short}){tail}."
            )
        if refs and not findings:
            findings.append("Every Hypercharge fired a full 5 Blazing Shots. Clean.")
        elif not refs and not findings:
            findings.append("Every Hypercharge fired a full 5 Blazing Shots.")

        detail_columns = ["HC#", "Time", "Blazing Shots", "Short by"]
        your_rows: list[list[Any]] = []
        row_colors: list[str | None] = []
        for i, w in enumerate(you_windows, 1):
            short = HYPERCHARGE_BLAZING_CAP - w.hits
            your_rows.append([
                f"HC{i:02d}", _mmss(w.cast_time_s),
                f"{w.hits}/{HYPERCHARGE_BLAZING_CAP}", short if not w.cut_short else "—",
            ])
            row_colors.append(_hypercharge_color(w.hits))

        summary_lines = [f"Your Hypercharges: {len(you_windows)}"]
        if you_windows:
            avg_hits = mean(w.hits for w in you_windows)
            summary_lines.append(
                f"Average Blazing Shots/HC: {avg_hits:.1f}/{HYPERCHARGE_BLAZING_CAP}")
        if ref_window_lists:
            ref_avgs = [mean(w.hits for w in ws) for ws in ref_window_lists if ws]
            if ref_avgs:
                summary_lines.append(
                    f"Reference avg Blazing Shots/HC: {mean(ref_avgs):.1f}")

        return AspectComparison(
            aspect_name=self.name,
            findings=findings,
            detail_columns=detail_columns,
            your_detail_rows=your_rows,
            your_detail_row_colors=row_colors,
            summary_lines=summary_lines,
        )


def _hypercharge_color(hits: int) -> str:
    """0..5 Blazing Shots → red→amber→green, same shape as `_wildfire_color`."""
    t = max(0, min(HYPERCHARGE_BLAZING_CAP, hits)) / HYPERCHARGE_BLAZING_CAP
    if t < 0.5:
        u = t / 0.5
        r, g, bl = _lerp((0xc0, 0x39, 0x2b), (0xe6, 0x7e, 0x22), u)
    else:
        u = (t - 0.5) / 0.5
        r, g, bl = _lerp((0xe6, 0x7e, 0x22), (0x27, 0xae, 0x60), u)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _hypercharge_to_event(idx: int, w: HyperchargeWindow) -> TrackEvent:
    # Bar spans the actual Blazing Shots (last shot + a hair), with the nominal
    # window as a floor so a 0-hit window still draws something.
    end_s = max(w.cast_time_s + HYPERCHARGE_WINDOW_S, w.last_shot_s + 0.5)
    return TrackEvent(
        start_s=w.cast_time_s,
        end_s=end_s,
        color=_hypercharge_color(w.hits),
        label=f"{w.hits}/{HYPERCHARGE_BLAZING_CAP}",
        tooltip=(f"HC{idx:02d}  blazing={w.hits}/{HYPERCHARGE_BLAZING_CAP}  "
                 f"start={_mmss(w.cast_time_s)}"
                 f"{'  (cut short)' if w.cut_short else ''}"),
    )


# --- Tools aspect ------------------------------------------------------------
# "Tools" = MCH's high-potency GCDs (Drill / Air Anchor / Chain Saw) plus the
# proc-chain follow-ups (Excavator / Full Metal Field). These should be cast
# on cooldown / on proc — undercount vs references is the headline finding.

# Ordered: cooldown tools first, then proc-chain follow-ups.
TOOL_IDS: list[int] = [
    16498,   # Drill
    16500,   # Air Anchor
    25788,   # Chain Saw
    36981,   # Excavator        (proc after Chain Saw)
    36982,   # Full Metal Field (proc after Excavator)
]
TOOL_ID_SET = set(TOOL_IDS)

# Single-letter shorthand for the compact text-timeline row.
_TOOL_LETTERS: dict[int, str] = {
    16498: "D",  # Drill
    16500: "A",  # Air Anchor
    25788: "S",  # Chain Saw
    36981: "X",  # Excavator
    36982: "F",  # Full Metal Field
}


@dataclass
class ToolCast:
    time_s: float
    ability_id: int


class ToolsAspect:
    name = "Tools"

    def analyze(self, client: FFLogsClient, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        start, end = fight["startTime"], fight["endTime"]
        fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
        cast_events = client.get_events(code, fetch_start, end, actor["id"], data_type="Casts")
        cast_events.sort(key=lambda e: e["timestamp"])

        tool_casts: list[ToolCast] = []
        for ev in cast_events:
            if ev.get("type") != "cast":
                continue
            aid = ev.get("abilityGameID")
            if aid in TOOL_ID_SET:
                t_rel = (ev["timestamp"] - start) / 1000.0
                tool_casts.append(ToolCast(time_s=t_rel, ability_id=aid))

        events: list[TrackEvent] = []
        icon_paths: set[str] = set()
        for tc in tool_casts:
            meta = ability_metadata.get_metadata(tc.ability_id)
            name = meta.name if meta else f"action {tc.ability_id}"
            events.append(TrackEvent(
                start_s=tc.time_s,
                end_s=tc.time_s + 0.5,
                color="#5d6d7e",  # neutral; icon overlays unless missing
                label=(name[:3] if name else "?"),
                tooltip=f"{name}  @  {_mmss(tc.time_s)}",
                icon_path=meta.icon if meta else "",
            ))
            if meta and meta.icon:
                icon_paths.add(meta.icon)

        # Same pre-warm pattern as AbilityTimelineAspect — keeps the GUI thread
        # from blocking on per-icon downloads during the first paint.
        ICONS.warm(icon_paths)

        counts_by_id: dict[int, int] = {}
        for tc in tool_casts:
            counts_by_id[tc.ability_id] = counts_by_id.get(tc.ability_id, 0) + 1

        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=events),
            state={
                "tool_casts": tool_casts,
                "counts_by_id": counts_by_id,
            },
        )

    def compare(self, you: AspectResult, refs: list[AspectResult]) -> AspectComparison:
        you_counts: dict[int, int] = you.state.get("counts_by_id", {}) or {}
        ref_counts_list: list[dict[int, int]] = [
            r.state.get("counts_by_id", {}) or {} for r in refs
        ]

        findings: list[str] = []

        if not refs:
            findings.append("No reference runs to compare against.")
        else:
            for tool_id in TOOL_IDS:
                ref_n = [c.get(tool_id, 0) for c in ref_counts_list]
                ref_med = int(round(median(ref_n))) if ref_n else 0
                you_n = you_counts.get(tool_id, 0)
                meta = ability_metadata.get_metadata(tool_id)
                name = meta.name if meta else f"action {tool_id}"
                if ref_med >= 2 and you_n < ref_med:
                    short = ref_med - you_n
                    findings.append(
                        f"Low {name}: you cast {you_n}, references median "
                        f"{ref_med} (short by {short})."
                    )
                elif ref_med > 0 and you_n > ref_med + 1:
                    findings.append(
                        f"{name} high: you cast {you_n} vs median {ref_med}. "
                        "Unusual — confirm intentional."
                    )

        if refs and not findings:
            findings.append("Tool usage looks aligned with references. Nice.")

        detail_columns = ["Tool", "Your casts", "Ref median", "Diff"]
        your_rows: list[list[Any]] = []
        for tool_id in TOOL_IDS:
            meta = ability_metadata.get_metadata(tool_id)
            name = meta.name if meta else f"action {tool_id}"
            you_n = you_counts.get(tool_id, 0)
            ref_n = [c.get(tool_id, 0) for c in ref_counts_list]
            ref_med = int(round(median(ref_n))) if ref_n else 0
            diff = you_n - ref_med
            your_rows.append([name, you_n, ref_med, f"{diff:+d}"])

        summary_lines = [f"Your tool casts: {sum(you_counts.values())}"]
        # Compact per-tool breakdown — same data as the detail table, but
        # always visible in the footer for at-a-glance reading.
        parts: list[str] = []
        for tool_id in TOOL_IDS:
            meta = ability_metadata.get_metadata(tool_id)
            name = meta.name if meta else f"action {tool_id}"
            you_n = you_counts.get(tool_id, 0)
            if refs:
                ref_n = [c.get(tool_id, 0) for c in ref_counts_list]
                ref_med = int(round(median(ref_n))) if ref_n else 0
                parts.append(f"{name} {you_n}/{ref_med}")
            else:
                parts.append(f"{name} {you_n}")
        label = "Per tool (you/ref med): " if refs else "Per tool: "
        summary_lines.append(label + ", ".join(parts))
        if refs:
            ref_totals = [sum(c.values()) for c in ref_counts_list]
            summary_lines.append(
                f"Reference total: avg={sum(ref_totals)/len(ref_totals):.1f}  "
                f"median={int(round(median(ref_totals)))}  "
                f"range={min(ref_totals)}-{max(ref_totals)}"
            )

        text_rows: list[tuple[str, str]] = [
            (you.run_label or "You", _tools_fmt_row(you.state.get("tool_casts", []))),
        ]
        for r_ar in refs:
            text_rows.append((
                r_ar.run_label or "ref",
                _tools_fmt_row(r_ar.state.get("tool_casts", [])),
            ))

        return AspectComparison(
            aspect_name=self.name,
            findings=findings,
            detail_columns=detail_columns,
            your_detail_rows=your_rows,
            summary_lines=summary_lines,
            text_timeline_rows=text_rows,
        )


def _tools_fmt_row(tool_casts: list[ToolCast]) -> str:
    if not tool_casts:
        return "(none)"
    return "  ".join(
        f"{_TOOL_LETTERS.get(tc.ability_id, '?')}@{_mmss(tc.time_s)}"
        for tc in tool_casts
    )


# MCH-specific aspects (the common Abilities aspect is prepended by jobs.aspects_for).
JOB_ASPECTS = [QueenAspect(), WildfireAspect(), HyperchargeAspect(), ToolsAspect()]


# --- Job registration ------------------------------------------------------
# Deferred to a function so per-job imports stay lazy. `jobs._ensure_loaded`
# calls `_register_self()` once after the package is first imported.

def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.machinist.reassemble import ReassembleAspect
    from jobs.machinist.scoring import MCHScoringAspect
    return (
        AbilityTimelineAspect(prepull_buff_ids=JOB_DATA.prepull_buff_ids),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        ReassembleAspect(),
        MCHScoringAspect(),
        *JOB_ASPECTS,
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs) is simulated once (one perfect-sim per key
    instead of one per consumer). All boilerplate lives in the shared
    `make_simulator`; `simulate_canonical` is the 'hold burst for the 2-min
    window' comparison lane (not a ceiling)."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.machinist import scoring as sc
    from jobs.machinist.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


_registered = False


def _bundle_extra_streams(report: dict[str, Any], fight: dict[str, Any],
                          actor: dict[str, Any]) -> list:
    """Queen pet DamageDone streams for the per-pull prefetch bundle, matching
    the `get_events` call in `_scan_queens` (fight start/end, no abilityID) so
    the seeded cache key is identical. The PLAYER's own DamageDone (for the
    multi-target packetID grouping + Queen scan) is folded in generically via
    `JobData.prebundle_damage_done`, so it's not duplicated here."""
    from fflogs_api import BundleStream
    start, end = fight["startTime"], fight["endTime"]
    return [
        BundleStream(data_type="DamageDone", start=start, end=end,
                     source_id=pet["id"])
        for pet in find_queen_pets(report, actor["id"])
    ]


def _register_self():
    """Idempotent registration. Called once by `jobs.get_job('Machinist')`
    after this package is loaded."""
    global _registered
    if _registered:
        return
    from jobs._core.job import Job as _Job
    _register(_Job(
        name="Machinist",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        bundle_extra_streams=_bundle_extra_streams,
    ))
    _registered = True
