"""Aspect protocol + visualization + result dataclasses.

An *Aspect* analyzes one slice of a pull's behavior (per-ability counts,
cooldown drift, GCD clipping, etc.) and compares it against a set of
reference pulls. Aspects are job-agnostic at this layer; per-job aspects
live in `jobs/{job}/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# Seconds to scan before the fight's official start time when fetching cast
# events. Captures common pre-pull patterns (Reassemble at -5s, pre-pull
# Air Anchor at countdown 0, occasional pre-pull tinctures). t_rel for these
# events is naturally negative when computed as (timestamp - fight_start)/1000.
PRE_PULL_LOOKBACK_S: float = 10.0


# --- Drawing primitives (used by the viz layer) ------------------------------

@dataclass
class TrackEvent:
    start_s: float
    end_s: float
    color: str              # hex, e.g. "#22aa22" (also fallback if no icon)
    label: str              # short text rendered on the bar (fallback if no icon)
    tooltip: str = ""       # full info (per-event detail line)
    icon_path: str = ""     # XIVAPI relative path; if set, viz draws icon
    # Vertical sub-row within a lane (e.g. oGCDs lifted above GCDs). -1.0..+1.0
    # where 0 = lane center, -1 = top of bar height, +1 = bottom.
    y_offset: float = 0.0
    # The FFLogs ability id this event represents, when known. Set directly by
    # the Abilities aspect (it has the cast's abilityGameID) so the serializer
    # need not reverse-engineer it from the icon path — that lookup fails for
    # jobs whose icons aren't in the bundled map (e.g. RPR).
    ability_id: int | None = None


@dataclass
class Track:
    name: str               # mirrors the aspect's name
    events: list[TrackEvent] = field(default_factory=list)


# --- Per-aspect results & comparison -----------------------------------------

@dataclass
class AspectResult:
    """Per-run output from one Aspect.analyze() call."""
    name: str
    track: Track
    state: dict[str, Any] = field(default_factory=dict)  # opaque per-aspect payload
    run_label: str = ""   # set by analyze_pull after analyze() returns


@dataclass
class AspectComparison:
    """Per-aspect comparison output — drives the UI's results tabs."""
    aspect_name: str
    findings: list[str] = field(default_factory=list)
    detail_columns: list[str] = field(default_factory=list)
    your_detail_rows: list[list[Any]] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    # (run_label, compact one-line per-run timeline) — for the text-timeline tab
    text_timeline_rows: list[tuple[str, str]] = field(default_factory=list)
    # Optional per-row color (parallel to your_detail_rows) for the detail table.
    your_detail_row_colors: list[str | None] = field(default_factory=list)


class Aspect(Protocol):
    """Per-aspect analyzer + comparator. Implement one per analysis topic."""
    name: str

    def analyze(self, client, code: str, fight: dict,
                actor: dict, report: dict) -> AspectResult: ...

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison: ...
