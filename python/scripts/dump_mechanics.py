"""Dump an encounter's classified forced-damage mechanics — the authoring aid
for premade ("PF") mit plans (python/mitplan/premade/<id>.json).

Prints each mechanic's composite id, boss ability id, median time, kind, school
and per-role unmitigated damage, so a plan author can:
  - map a sheet mechanic to the stable `boss_ability_id` (recommended match key),
  - verify the sheet's `name` matches `Mechanic.name`, and
  - read the occurrence order of a mechanic that recurs (its `#N` ordinal).

Run from python/:  python scripts/dump_mechanics.py 1085
Needs FFLogs credentials in ~/.fflogs_efficiency_analyzer/config.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/dump_mechanics.py <encounter_id>",
              file=sys.stderr)
        raise SystemExit(2)
    encounter_id = int(sys.argv[1])

    import mitplan
    from sidecar.main import _client

    def progress(pct, stage, tasks=None):
        print(f"  [{pct:3d}%] {stage}", file=sys.stderr)

    model = mitplan.build_damage_model(_client(), encounter_id,
                                       progress=progress)
    print(f"\n{model.encounter_name} (id {model.encounter_id}) — "
          f"{len(model.mechanics)} forced mechanics "
          f"({model.ref_count} ref kills, median {model.model_kill_s:.0f}s)\n")
    header = f"{'time':>6}  {'boss_id':>8}  {'#id':>10}  {'kind':<11} " \
             f"{'school':<9} {'unmit(t/h/d)':>20}  name"
    print(header)
    print("-" * len(header))
    for m in sorted(model.mechanics, key=lambda x: x.time_s):
        boss = m.boss_ability_ids[0] if m.boss_ability_ids else 0
        mm, ss = int(m.time_s // 60), int(m.time_s % 60)
        u = m.unmitigated
        unmit = (f"{u.get('tank', 0) / 1000:.0f}/"
                 f"{u.get('healer', 0) / 1000:.0f}/"
                 f"{u.get('dps', 0) / 1000:.0f}k")
        print(f"{mm:>3}:{ss:02d}  {boss:>8}  {m.id:>10}  {m.kind:<11} "
              f"{m.school:<9} {unmit:>20}  {m.name}")


if __name__ == "__main__":
    main()
