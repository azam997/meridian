"""Shared types for the deep-advice pass (run inline by `run_analysis`).

The orchestrator lives in `sidecar/advice.py`; per-job probe sets register an
`AdvicePack` via `Job.advice_probes` (next to `improvement_contributors`) and
receive an `AdviceContext`. This module sits in `jobs/_core` so job packages
can import the types without touching `sidecar/` (jobs must never import the
sidecar).

Two output currencies, kept strictly apart:

* `ProbeItem` — in-place enrichment of an EXISTING card, joined by the
  `(kind, ability_id, time_sec)` triple; the orchestrator merges it straight
  into the card's `prescription`/`details` before the response ships.
* `RootCause` — a candidate CONCRETE card for the cascade re-attribution:
  `measured_p` is a weight (greedy-cascade currency), which the orchestrator
  scales into the panel's actual residual budget before promoting — so the
  examined card list conserves the original top-level sum exactly.

All USER-FACING copy lives in data, not inline f-strings: a job's probe
module keeps its templates in a module-level `TEXT` dict and its gauge
glossary in `GaugeText` entries — the best-possible-feedback wording is a
data edit, never a logic change. `GaugeText` is an ALLOWLIST: state fields
without an entry produce no evidence line at all, so raw sim-state names
(`queen_battery_spent`…) can never leak into the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from jobs._core.job import JobData


@dataclass(frozen=True)
class GaugeText:
    """Human copy for ONE sim-state gauge field in the player-vs-ideal state
    delta. Evidence renders as a labelled row (`LABEL  {delta} over ideal
    {note}`), so the note is a clause, not a full sentence. `over_note`
    renders when the player holds MORE than the ideal line at the segment's
    end, `under_note` when less; either may be None to stay silent in that
    direction. `short` is the 3-4-char icon-tile tag (BAT / HEAT)."""
    label: str
    short: str = ""
    over_note: Optional[str] = None
    under_note: Optional[str] = None
    min_delta: float = 15.0


@dataclass(frozen=True)
class EvidenceRow:
    """One labelled evidence line: KEY / mono value / prose note. Serialized
    onto cards as `evidence: [{k, v, note}]` — the UI renders a 3-column grid
    instead of sentence soup."""
    k: str
    v: str
    note: str = ""

    def wire(self) -> dict:
        return {"k": self.k, "v": self.v, "note": self.note}


@dataclass(frozen=True)
class AdvicePack:
    """A job's complete advice registration: the probe callable plus the text
    data the orchestrator needs to speak about this job like a person."""
    probes: Callable[..., tuple]                 # (ctx, cards, progress) -> (items, causes)
    gauge_text: dict[str, GaugeText] = field(default_factory=dict)


@dataclass
class AdviceContext:
    """Everything a probe (and the cascade pass) may read for one pull. Built
    by `sidecar/main.py::_run_deep_pass` from the analyzed `you`
    ModuleResult, inline with the response build."""
    job: str
    data: JobData
    norm_casts: list[tuple[float, int]]          # delivered, incl. t<0 prepull
    idealized: list[tuple[float, int]]           # strict sim timeline, pot-free
    fight_duration_s: float
    downtime_windows: list[tuple[float, float]]
    death_windows: list[tuple[float, float]]
    clipping_state: dict
    scoring_state: dict
    enabler_values: dict[int, float]
    sim_context: Any                             # _user_sim_context(you)
    sim_module: str                              # e.g. "jobs.machinist.simulator"
    runner: Any = None                           # counterfactual.Runner | None
    gcd_ids: frozenset[int] = frozenset()        # ids that roll the GCD
    gauge_text: dict[str, GaugeText] = field(default_factory=dict)


@dataclass
class ProbeItem:
    """In-place enrichment for one existing card, joined by its triple.
    `summary` (optional) replaces the card's title too — probes that measure
    the situation precisely may retitle ("Wildfire caught 5 of 6
    weaponskills")."""
    kind: str
    ability_id: int
    time_sec: float
    prescription: str
    evidence: list[EvidenceRow] = field(default_factory=list)
    summary: Optional[str] = None


@dataclass
class RootCause:
    """A candidate concrete card the re-attribution may promote. `measured_p`
    is a pre-scaling weight in the cascade currency; `segment` ties it to the
    cascade segment that measured it (for evidence + share splitting);
    `resources` tags the implicated gauges (icon tiles + the category
    recurrence note)."""
    kind: str                      # "cascade_lost_use" | "cascade_burst" | "cascade_pacing"
    ability_id: int
    ability_name: str
    time_sec: float
    measured_p: float
    summary: str
    prescription: str
    evidence: list[EvidenceRow] = field(default_factory=list)
    resources: list[GaugeText] = field(default_factory=list)
    segment: Optional[tuple[float, float]] = None
