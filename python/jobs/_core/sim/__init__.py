"""Job-agnostic idealized-rotation engine + scoring scaffolding.

A per-job simulator is now a data file + a `RotationModel` (picker / apply /
state) + a small scoring adapter. The shared time loop, downtime/weave/charge
handling, parameter sweep, local-search refinement and canonical buff alignment
live in `engine`; the cast-time archetype layer in `timing`; the LRU-cached
ceiling, the `IdealizedSimulator` wrapper and the `Scoring` aspect flow in
`scoring`.

See `jobs/machinist/simulator.py` (instant GCD) for the canonical example.
"""
from __future__ import annotations

from . import engine, scoring, timing
from .engine import BaseRotationModel, SimParamsBase, SimStateBase
from .scoring import ScoringAspectBase, ScoringFns, build_scoring, make_simulator
from .timing import GcdTiming, HardcastGCD, InstantGCD

__all__ = [
    "engine",
    "scoring",
    "timing",
    "BaseRotationModel",
    "SimStateBase",
    "SimParamsBase",
    "GcdTiming",
    "InstantGCD",
    "HardcastGCD",
    "ScoringAspectBase",
    "ScoringFns",
    "build_scoring",
    "make_simulator",
]
