"""Healing/Mitigation planner — party-scoped (peer of jobs/, not per-job).

Public surface:
    build_damage_model(client, encounter_id, progress) -> DamageModel
    plan(model, shield_healer, regen_healer, tanks, dps) -> Plan

The damage model (top-log fetch + forced-damage classification) is
duo-independent and cached by the sidecar per encounter; the planner is a pure
function over (model, comp) so duo/comp changes re-plan instantly.
"""
from __future__ import annotations

from typing import Any


def build_damage_model(client: Any, encounter_id: int, progress=None):
    from .damage import build_damage_model as _build
    return _build(client, encounter_id, progress=progress)


def plan(model, shield_healer: str, regen_healer: str,
         tanks: list[str], dps: list[str], pinned=None):
    from .planner import plan as _plan
    return _plan(model, shield_healer, regen_healer, tanks, dps, pinned=pinned)
