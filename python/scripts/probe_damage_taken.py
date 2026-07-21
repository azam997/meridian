"""M0 probe for the Healing/Mitigation planner: verify the FFLogs v2 facts the
damage-model design depends on but the app has never fetched (this machine's
caches hold only player-sourced DamageDone/Casts/Buffs — see the plan).

Questions (each maps to a design toggle in the plan's risk table):
  1. Does `dataType: DamageTaken, sourceID: <friendly>` select the VICTIM
     (events where targetID == friendly), and do those events carry
     `unmitigatedAmount` / `multiplier` / `mitigated` / `absorbed` / `tick`?
  2. What does the `buffs` status-id string mean on a damage-TAKEN event —
     attacker-side (boss) or victim-side (the mit statuses we care about)?
     Cross-referenced against aura intervals reconstructed from Buffs events.
  3. Does one UNSOURCED `dataType: Buffs` stream return apply/remove events for
     all 8 friendlies, and do shield applies carry `absorb` pool sizes?
  4. Does `includeResources: true` add `targetResources` (hitPoints /
     maxHitPoints) to damage-taken events?
  5. What does one unsourced `dataType: Healing` stream contain — heal /
     calculatedheal / absorbed event types, `amount` / `overheal` / `tick`?

Run from python/:
    python scripts/probe_damage_taken.py [--enc 101] [--rank-spec Machinist]

FINDINGS (run 2026-07-16 vs M9S top kill DFm9brj421X6T73k, all toggles resolved):
  1. YES — `sourceID` on a DamageTaken stream selects the VICTIM (targetID==self
     on 100% of events). Applied `damage` rows carry amount / unmitigatedAmount /
     mitigated / multiplier / absorbed / tick / hitType / packetID / buffs, and
     the accounting closes exactly: amount + mitigated + absorbed == unmitigated
     (fields omitted when zero; multiplier omitted means 1.0; `blocked` folds
     into mitigated). directHit never appears on the taken side.
     The filterExpression `target.id = N` fallback returns 0 events — unneeded.
  2. YES++ — `buffs` on a damage-taken event is the hit's MITIGATION SNAPSHOT:
     exactly the statuses that affected the calculation, both boss-side debuffs
     (Reprisal/Feint/Addle/Dismantled) and victim-side buffs/shields (Sacred
     Soil, Temperance, Galvanize, Tactician, Divine Veil, ...) plus Well Fed.
     Ids are aura-form (1,000,000 + status id) and resolve via
     masterData.abilities names. No aura reconstruction needed for validation.
  3. YES — one unsourced Buffs stream covers all 8 friendlies (apply/remove/
     refresh/stacks); shield applies carry `absorb`. CAVEAT: absorb is HP-scale
     for some statuses (The Blackest Night 81261) but potency-ish for others
     (Divine Benison 500) — trust applybuff.absorb only when >= ~1% of role max
     HP, else fall back to potency x calibration.
  4. YES — `includeResources: true` adds targetResources (hitPoints /
     maxHitPoints / absorb / mp / x / y / facing) to every damage-taken event.
     hpSource: 'logs'.
  5. YES — one unsourced Healing stream covers all 8 sources: heal /
     calculatedheal rows with amount / overheal / tick / hitType(crit), plus
     `absorbed` events attributing each shield's per-hit soak
     ({sourceID: shield caster, attackerID, abilityGameID: aura-form status,
     extraAbilityGameID: the damaging ability, amount}). Synthetic "Combined
     HoTs" rows exist — skip by name in magnitude extraction.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import encounter_difficulty  # noqa: E402
from fflogs_api import BundleStream  # noqa: E402
from sidecar.main import _client  # noqa: E402

TANK_SUBTYPES = {"Paladin", "Warrior", "DarkKnight", "Gunbreaker"}
HEALER_SUBTYPES = {"WhiteMage", "Scholar", "Astrologian", "Sage"}


def role_of(sub_type: str) -> str:
    st = (sub_type or "").replace(" ", "")
    if st in TANK_SUBTYPES:
        return "tank"
    if st in HEALER_SUBTYPES:
        return "healer"
    return "dps"


def field_presence(events: list[dict], fields: list[str]) -> list[str]:
    """Presence table rows for `fields` over `events` (count + one sample)."""
    rows = []
    for f in fields:
        vals = [ev[f] for ev in events if f in ev and ev[f] is not None]
        if vals:
            rows.append(f"  {f:<20} {len(vals):>7}/{len(events):<7} sample={vals[0]!r}")
        else:
            rows.append(f"  {f:<20} {'0':>7}/{len(events):<7} ABSENT")
    return rows


def pick_fight(client, encounter_id: int, spec: str) -> tuple[str, dict, dict]:
    """A top-ranked kill (code, fight, summary) for the encounter."""
    diff = encounter_difficulty(encounter_id)
    blob = client.get_rankings(encounter_id, spec, spec, difficulty=diff)
    ranks = (blob or {}).get("rankings") or []
    if not ranks:
        raise SystemExit(f"no rankings for encounter {encounter_id} spec {spec}")
    for r in ranks:
        code = r["report"]["code"]
        fid = r["report"]["fightID"]
        summary = client.get_report_summary(code)
        for f in summary.get("fights") or []:
            if f.get("id") == fid and f.get("kill"):
                return code, f, summary
    raise SystemExit("no usable ranked kill found")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enc", type=int, default=101)
    ap.add_argument("--rank-spec", default="Machinist")
    args = ap.parse_args()

    client = _client()
    code, fight, summary = pick_fight(client, args.enc, args.rank_spec)
    start, end = fight["startTime"], fight["endTime"]
    dur = (end - start) / 1000.0

    actors = (summary.get("masterData") or {}).get("actors") or []
    by_id = {a["id"]: a for a in actors}
    friendly_ids = [i for i in (fight.get("friendlyPlayers") or [])
                    if by_id.get(i, {}).get("type") == "Player"
                    and by_id.get(i, {}).get("subType") not in (None, "Unknown", "LimitBreak")]
    party = [(i, by_id[i].get("subType") or "?") for i in friendly_ids]
    ability_names = {a["gameID"]: a.get("name") or ""
                     for a in (summary.get("masterData") or {}).get("abilities") or []}

    print(f"=== Probe report {code} fight {fight['id']} "
          f"({fight.get('name')}, enc {args.enc}, {dur:.0f}s kill) ===")
    print("party:", ", ".join(f"{i}:{st}({role_of(st)})" for i, st in party))
    if len(party) < 8:
        print(f"NOTE: only {len(party)} friendly players resolved")

    # One representative per role for the sourced probes.
    probes: dict[str, tuple[int, str]] = {}
    for i, st in party:
        probes.setdefault(role_of(st), (i, st))
    print("probe actors:", probes)

    # ---- 1. DamageTaken, sourceID=<friendly> --------------------------------
    print("\n--- [1] DamageTaken sourced (per-role) ---")
    dt_streams = [BundleStream("DamageTaken", start, end, source_id=aid)
                  for aid, _ in probes.values()]
    dt_lists = client.get_event_bundle(code, dt_streams)
    dt_by_role: dict[str, list[dict]] = {}
    for (role, (aid, st)), evs in zip(probes.items(), dt_lists):
        dt_by_role[role] = evs
        types = Counter(ev.get("type") for ev in evs)
        n = len(evs)
        tgt_self = sum(1 for ev in evs if ev.get("targetID") == aid)
        src_self = sum(1 for ev in evs if ev.get("sourceID") == aid)
        print(f"\n{role} ({st}, id {aid}): {n} events, types={dict(types)}")
        if n:
            print(f"  direction: targetID==self {tgt_self}/{n}, sourceID==self {src_self}/{n}"
                  f"  -> sourceID selects the {'VICTIM' if tgt_self > src_self else 'ATTACKER?'}")
            dmg = [ev for ev in evs if ev.get("type") == "damage"]
            calc = [ev for ev in evs if ev.get("type") == "calculateddamage"]
            print(f"  applied 'damage' rows: {len(dmg)}, 'calculateddamage' rows: {len(calc)}")
            for row in field_presence(dmg or evs,
                                      ["amount", "unmitigatedAmount", "mitigated",
                                       "multiplier", "absorbed", "tick", "overkill",
                                       "buffs", "hitType", "directHit", "packetID",
                                       "targetResources", "sourceResources"]):
                print(row)
            mults = [ev["multiplier"] for ev in dmg if isinstance(ev.get("multiplier"), (int, float))]
            if mults:
                print(f"  multiplier: min={min(mults):.3f} median={statistics.median(mults):.3f} "
                      f"max={max(mults):.3f}")
            big = max(dmg or evs, key=lambda e: e.get("unmitigatedAmount") or e.get("amount") or 0)
            print("  biggest hit:", json.dumps(big)[:600])

    # ---- 1b. Fallback: unsourced DamageTaken + filterExpression -------------
    print("\n--- [1b] DamageTaken via filterExpression target.id (fallback check) ---")
    try:
        role, (aid, st) = next(iter(probes.items()))
        fe = client.get_event_bundle(code, [BundleStream(
            "DamageTaken", start, end, filter_expression=f"target.id = {aid}")])[0]
        base = len(dt_by_role.get(role) or [])
        print(f"filterExpression target.id={aid}: {len(fe)} events (sourced probe saw {base})")
    except Exception as e:  # noqa: BLE001
        print("filterExpression fallback FAILED:", e)

    # ---- 2. buffs-string semantics on damage-taken --------------------------
    print("\n--- [2] victim-side `buffs` semantics ---")
    print("(cross-reference below, after Buffs stream is fetched)")

    # ---- 3. Unsourced Buffs stream ------------------------------------------
    print("\n--- [3] unsourced Buffs stream ---")
    buffs_events: list[dict] = []
    try:
        buffs_events = client.get_event_bundle(
            code, [BundleStream("Buffs", start, end)])[0]
        types = Counter(ev.get("type") for ev in buffs_events)
        recip = {ev.get("targetID") for ev in buffs_events}
        friendly_recip = recip & set(friendly_ids)
        print(f"{len(buffs_events)} events, types={dict(types)}")
        print(f"distinct targetIDs: {len(recip)}; covers {len(friendly_recip)}/{len(friendly_ids)} friendlies")
        absorbs = [ev for ev in buffs_events if ev.get("absorb")]
        print(f"applies with absorb (shield pools): {len(absorbs)}")
        for ev in absorbs[:3]:
            nm = ability_names.get(ev.get("abilityGameID"), "?")
            print(f"  sample: status {ev.get('abilityGameID')} ({nm}) absorb={ev.get('absorb')} "
                  f"target={ev.get('targetID')}")
    except Exception as e:  # noqa: BLE001
        print("unsourced Buffs FAILED:", e)

    # buffs-string cross-reference: active statuses (from [3]) at hit time vs
    # the ids embedded in each damage-taken event's `buffs` field.
    try:
        role, (aid, st) = next(iter(probes.items()))
        evs = [ev for ev in dt_by_role.get(role) or []
               if ev.get("type") == "damage" and ev.get("buffs")]
        if evs and buffs_events:
            active: dict[int, list[tuple[int, int]]] = defaultdict(list)  # sid -> [(t_on, t_off)]
            opens: dict[int, int] = {}
            for ev in sorted((e for e in buffs_events if e.get("targetID") == aid),
                             key=lambda e: e["timestamp"]):
                sid, t = ev.get("abilityGameID"), ev["timestamp"]
                if ev.get("type") in ("applybuff", "refreshbuff"):
                    opens.setdefault(sid, t)
                elif ev.get("type") == "removebuff" and sid in opens:
                    active[sid].append((opens.pop(sid), t))
            for sid, t in opens.items():
                active[sid].append((t, end))

            def active_at(ts: int) -> set[int]:
                return {sid for sid, spans in active.items()
                        if any(a <= ts <= b for a, b in spans)}

            hits = evs[:200]
            overlap_n = match_n = 0
            for ev in hits:
                ids = {int(x) for x in str(ev["buffs"]).split(".") if x}
                act = active_at(ev["timestamp"])
                if ids:
                    overlap_n += 1
                    if ids & act:
                        match_n += 1
            print(f"buffs-string vs victim-aura overlap: {match_n}/{overlap_n} hits share >=1 status"
                  f" -> buffs lists the {'VICTIM' if match_n > overlap_n * 0.6 else 'ATTACKER (boss)?'} side")
            sample = hits[0]
            print(f"  sample buffs={sample.get('buffs')!r} victim-active={sorted(active_at(sample['timestamp']))[:8]}")
        else:
            print("no damage rows with `buffs` or no Buffs events — semantics UNRESOLVED")
    except Exception as e:  # noqa: BLE001
        print("buffs cross-reference FAILED:", e)

    # ---- 4. includeResources ------------------------------------------------
    print("\n--- [4] includeResources -> targetResources ---")
    try:
        role, (aid, st) = next(iter(probes.items()))
        q = ("query($code: String!) { reportData { report(code: $code) { "
             f"events(startTime: {float(start)}, endTime: {float(end)}, "
             f"dataType: DamageTaken, sourceID: {aid}, includeResources: true, "
             "limit: 10000) { data nextPageTimestamp } } } }")
        evs = ((client.query(q, {"code": code}) or {}).get("reportData") or {}) \
            .get("report", {}).get("events", {}).get("data") or []
        with_res = [ev for ev in evs if isinstance(ev.get("targetResources"), dict)]
        print(f"{len(evs)} events, {len(with_res)} with targetResources")
        if with_res:
            tr = with_res[0]["targetResources"]
            print("  targetResources keys:", sorted(tr.keys()))
            hps = [ev["targetResources"].get("maxHitPoints") for ev in with_res
                   if ev["targetResources"].get("maxHitPoints")]
            if hps:
                print(f"  {role} maxHitPoints median={int(statistics.median(hps))}")
    except Exception as e:  # noqa: BLE001
        print("includeResources FAILED:", e)

    # ---- 5. Unsourced Healing stream ----------------------------------------
    print("\n--- [5] unsourced Healing stream ---")
    try:
        heal = client.get_event_bundle(code, [BundleStream("Healing", start, end)])[0]
        types = Counter(ev.get("type") for ev in heal)
        srcs = {ev.get("sourceID") for ev in heal} & set(friendly_ids)
        print(f"{len(heal)} events, types={dict(types)}; friendly sources: {len(srcs)}")
        applied = [ev for ev in heal if ev.get("type") == "heal"]
        for row in field_presence(applied or heal,
                                  ["amount", "overheal", "tick", "absorbed",
                                   "abilityGameID", "sourceID", "targetID",
                                   "hitType", "targetResources"]):
            print(row)
        by_ability = Counter()
        for ev in applied:
            by_ability[ev.get("abilityGameID")] += 1
        top = [(ability_names.get(a, str(a)), n) for a, n in by_ability.most_common(8)]
        print("  top heal abilities:", top)
        absorbed_evs = [ev for ev in heal if ev.get("type") == "absorbed"]
        if absorbed_evs:
            print(f"  'absorbed' events: {len(absorbed_evs)}, sample:",
                  json.dumps(absorbed_evs[0])[:400])
    except Exception as e:  # noqa: BLE001
        print("unsourced Healing FAILED:", e)

    print("\n=== probe complete ===")


if __name__ == "__main__":
    main()
