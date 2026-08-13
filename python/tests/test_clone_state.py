"""_clone_state invariants across every registered job (jobs/_core/sim/engine.py).

The v1.1 fast clone (copy.copy + a cached per-class container plan) relies on
one invariant: a SimState's container fields hold only IMMUTABLE elements
(id -> float dicts, float lists, tuples of scalars). A field that nests a
mutable gets deepcopy as its plan copier, so it stays correct — but it also
stays slow, and a container that STARTS flat and later nests would alias
across beam branches. This file turns the invariant into a gate:

  * every job's init_state fields are flat containers (or get flagged here so
    the plan's deepcopy fallback is a conscious choice),
  * a clone is deep-equal to its source, and mutating the clone's containers
    never touches the source (the aliasing check),
  * the timeline is value-copied and the constant downtime/buff lists shared.

Run from python/:  python tests/test_clone_state.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import ALL_JOBS, get_job
from jobs._core.sim.engine import _clone_state

_MUTABLE = (dict, list, set)


def _model_of(job_name: str):
    job = get_job(job_name)
    if job.simulator is None:
        return None
    # Every job package exposes _model_for on its simulator module.
    import importlib
    pkg = job_name.replace(" ", "").lower()
    try:
        mod = importlib.import_module(f"jobs.{pkg}.simulator")
    except ImportError:
        return None
    model_for = getattr(mod, "_model_for", None)
    if model_for is None:
        return None
    # Signatures vary: (sim_context), (duration_s, sim_context),
    # (sim_context, duration_s) — bind by parameter name.
    import inspect
    kwargs = {p.name: (300.0 if "duration" in p.name else None)
              for p in inspect.signature(model_for).parameters.values()}
    return model_for(**kwargs)


def _states():
    for name in ALL_JOBS:
        model = _model_of(name)
        if model is None:
            continue
        state = model.init_state()
        state.timeline.append((1.0, 42))
        yield name, state


def test_container_fields_are_flat() -> None:
    for name, state in _states():
        for field, val in vars(state).items():
            if isinstance(val, dict):
                nested = [k for k, v in val.items() if isinstance(v, _MUTABLE)]
            elif isinstance(val, (list, tuple, set)):
                nested = [v for v in val if isinstance(v, _MUTABLE)]
            else:
                continue
            assert not nested, (
                f"{name}.{field} nests mutables {nested!r} — the clone plan "
                f"will deepcopy it (correct but slow); flatten it or accept "
                f"deliberately by updating this test")


def test_clone_is_deep_equal_and_unaliased() -> None:
    for name, state in _states():
        clone = _clone_state(state)
        assert vars(clone) == vars(state), f"{name}: clone differs"
        # Mutate every container on the CLONE; the source must not move.
        before = {k: (dict(v) if isinstance(v, dict) else list(v))
                  for k, v in vars(state).items()
                  if isinstance(v, (dict, list)) and k != "downtime_windows"
                  and k != "buff_intervals"}
        for field, val in list(vars(clone).items()):
            if field in ("downtime_windows", "buff_intervals"):
                continue
            if isinstance(val, dict):
                val[999_999] = 123.0
            elif isinstance(val, list):
                val.append(("sentinel", 0))
        for field, snap in before.items():
            src = vars(state)[field]
            if isinstance(src, dict):
                assert src == snap, f"{name}.{field} aliased into the clone"
            else:
                assert src == snap, f"{name}.{field} aliased into the clone"


def test_engine_lists_shared_or_copied_as_documented() -> None:
    for name, state in _states():
        state.downtime_windows = [(10.0, 20.0)]
        state.buff_intervals = [(0.0, 20.0, 1.05)]
        clone = _clone_state(state)
        assert clone.timeline == state.timeline
        assert clone.timeline is not state.timeline, f"{name}: timeline aliased"
        assert clone.downtime_windows is state.downtime_windows
        assert clone.buff_intervals is state.buff_intervals


def main() -> None:
    test_container_fields_are_flat()
    test_clone_is_deep_equal_and_unaliased()
    test_engine_lists_shared_or_copied_as_documented()
    print("test_clone_state: all checks passed")


if __name__ == "__main__":
    main()
