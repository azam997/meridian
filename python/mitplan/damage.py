"""IO layer: top-ranked kill logs -> the encounter's DamageModel.

Fetch shape per log (ONE aliased get_event_bundle round trip, dev-disk-cached
by args): 8x DamageTaken (sourceID = each friendly, includeResources for HP)
+ 1x unsourced Healing + 1x unsourced Buffs + 1x enemy Casts + 1x
targetability. The DamageTaken sourceID quirk (it selects the friendly TAKING
the damage) and every field consumed here were live-verified by
scripts/probe_damage_taken.py — see its header for the findings.

The model is duo-independent: rankings come from a fixed spec preference list
so the cache key is just the encounter.
"""
from __future__ import annotations

import statistics
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fflogs_api import BundleStream
from encounters import ALL_ENCOUNTERS, encounter_difficulty
from jobs._core.buff_windows import _friendly_actor_jobs
from jobs._core.downtime_sources import (
    compute_downtime_windows, resolve_boss_actor_ids, resolve_enemy_actor_ids,
)

from .classify import DamageModel, Hit, LogData, classify
from .library import (
    ACTIONS, HP_PER_POTENCY_DEFAULT, ROLE_MAX_HP_DEFAULT, Target,
    internal_job_name, role_for_job,
)

# Rankings only locate top kill reports — any populated spec works; the
# preference order just favors specs likely already dev-cached.
RANKINGS_SPEC_PREFERENCE = ("Machinist", "Samurai", "Dark Knight",
                            "Red Mage", "Dragoon")
N_LOGS = 10
MIN_LOGS = 4
FETCH_WORKERS = 4

# Shield-pool `absorb` values below this fraction of role max HP are unit
# artifacts (Divine Benison logs 500), not HP pools — see the probe findings.
_ABSORB_TRUST_MIN_FRAC = 0.01

# HP-set mechanic signature (they emit no damage events): within one bucket,
# this many friendlies healed FROM effectively-zero HP.
_HP1_EFFECTIVE_HP = 2_000.0
_HP1_MIN_TARGETS = 6
_HP1_BUCKET_MS = 2_000

Progress = Callable[[int, str, list[dict] | None], None]

# Direct (non-tick) heals with an authored potency — the per-log
# potency->HP calibration set.
_HEAL_POTENCY_BY_NAME: dict[str, float] = {
    a.name: a.heal_potency for a in ACTIONS
    if a.heal_potency > 0 and a.target in (Target.PARTY, Target.SINGLE)
}


def _progress_or_noop(progress: Progress | None) -> Progress:
    if progress is None:
        return lambda pct, stage, tasks=None: None
    return progress


def _pick_logs(client: Any, encounter_id: int) -> list[tuple[str, int]]:
    """Top-ranked distinct (code, fight_id) kills for the encounter."""
    diff = encounter_difficulty(encounter_id)
    last_err: Exception | None = None
    for spec in RANKINGS_SPEC_PREFERENCE:
        try:
            blob = client.get_rankings(encounter_id, spec, spec, difficulty=diff)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        ranks = (blob or {}).get("rankings") or []
        picked: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for r in ranks:
            rep = r.get("report") or {}
            key = (rep.get("code"), rep.get("fightID"))
            if not key[0] or key[1] is None or key in seen:
                continue
            seen.add(key)
            picked.append(key)
            if len(picked) >= N_LOGS:
                break
        if picked:
            return picked
    if last_err is not None:
        raise RuntimeError(f"rankings unavailable for encounter {encounter_id}: "
                           f"{last_err}") from last_err
    raise RuntimeError(f"no ranked kills found for encounter {encounter_id}")


class _LogFetch:
    """One log's normalized payload (LogData + calibration samples)."""

    def __init__(self) -> None:
        self.log: LogData | None = None
        self.role_hp: dict[str, list[float]] = {}
        self.heal_samples: list[tuple[str, str, float]] = []   # (job, name, amount)
        self.shield_pools: dict[str, list[float]] = {}          # status name -> absorbs
        self.jobs_present: set[str] = set()
        self.downtime: list[tuple[float, float]] = []
        self.ability_names: dict[int, str] = {}
        self.ability_types: dict[int, int | None] = {}


def _fetch_log(client: Any, log_idx: int, code: str, fight_id: int,
               summary: dict) -> _LogFetch:
    out = _LogFetch()
    fight = next((f for f in (summary.get("fights") or [])
                  if f.get("id") == fight_id), None)
    if fight is None:
        raise RuntimeError(f"fight {fight_id} not in report {code}")
    start, end = fight["startTime"], fight["endTime"]
    kill_s = (end - start) / 1000.0

    actors = _friendly_actor_jobs(summary, fight)  # [(actor_id, subType)]
    party: list[tuple[int, str, str]] = []          # (id, job, role)
    for aid, sub in actors:
        job = internal_job_name(sub)
        if job is not None:
            party.append((aid, job, role_for_job(job)))
    if len(party) < 8:
        raise RuntimeError(f"{code}#{fight_id}: only {len(party)} resolvable "
                           "players — skipped")
    party = party[:8]
    out.jobs_present = {job for _, job, _ in party}
    role_by_actor = {aid: role for aid, _, role in party}
    job_by_actor = {aid: job for aid, job, _ in party}

    for a in (summary.get("masterData") or {}).get("abilities") or []:
        gid = a.get("gameID")
        if gid is None:
            continue
        out.ability_names[gid] = a.get("name") or ""
        out.ability_types[gid] = a.get("type")

    streams = [BundleStream("DamageTaken", start, end, source_id=aid,
                            include_resources=True)
               for aid, _, _ in party]
    # Resources on heals reveal HP-set mechanics: the party healed from ~0 HP.
    streams.append(BundleStream("Healing", start, end, include_resources=True))
    streams.append(BundleStream("Buffs", start, end))
    streams.append(BundleStream("Casts", start, end, hostility="Enemies"))
    streams.append(BundleStream("All", start, end,
                                filter_expression='type="targetabilityupdate"'))
    bundles = client.get_event_bundle(code, streams)
    dt_lists, heal_evs, buff_evs, enemy_casts, targetability = (
        bundles[:8], bundles[8], bundles[9], bundles[10], bundles[11])

    # ---- damage-taken -> Hits (+ per-role max HP samples)
    hits: list[Hit] = []
    for (aid, _, role), evs in zip(party, dt_lists):
        for ev in evs:
            if ev.get("type") != "damage":
                continue
            amount = float(ev.get("amount") or 0.0)
            mitigated = float(ev.get("mitigated") or 0.0)
            absorbed = float(ev.get("absorbed") or 0.0)
            unmit = float(ev.get("unmitigatedAmount")
                          or (amount + mitigated + absorbed))
            hits.append(Hit(
                t=(ev["timestamp"] - start) / 1000.0,
                target=aid, role=role,
                ability_id=int(ev.get("abilityGameID") or 0),
                unmit=unmit, amount=amount, absorbed=absorbed,
                multiplier=float(ev.get("multiplier") or 1.0),
                tick=bool(ev.get("tick")),
                pid=ev.get("packetID"),
            ))
            res = ev.get("targetResources")
            if isinstance(res, dict) and res.get("maxHitPoints"):
                out.role_hp.setdefault(role, []).append(
                    float(res["maxHitPoints"]))

    # ---- healing stream -> potency calibration samples + HP-set windows
    hp1_buckets: dict[int, set[int]] = {}
    for ev in heal_evs:
        if ev.get("type") != "heal":
            continue
        tid = ev.get("targetID")
        res = ev.get("targetResources")
        amount_raw = float(ev.get("amount") or 0.0)
        if (tid in role_by_actor and isinstance(res, dict)
                and res.get("hitPoints") is not None
                and float(res["hitPoints"]) - amount_raw <= _HP1_EFFECTIVE_HP):
            # `hitPoints` is post-heal — this heal landed on a ~zero-HP player.
            hp1_buckets.setdefault(
                int((ev["timestamp"] - start) // _HP1_BUCKET_MS), set()).add(tid)
        if ev.get("tick") or ev.get("hitType") == 2:
            continue
        amount = amount_raw + float(ev.get("overheal") or 0.0)
        if amount <= 0:
            continue
        job = job_by_actor.get(ev.get("sourceID"))
        name = out.ability_names.get(ev.get("abilityGameID") or 0, "")
        if job and name in _HEAL_POTENCY_BY_NAME:
            out.heal_samples.append((job, name, amount))
    hp1_windows = sorted(b * (_HP1_BUCKET_MS / 1000.0)
                         for b, tids in hp1_buckets.items()
                         if len(tids) >= _HP1_MIN_TARGETS)
    # Collapse adjacent buckets into one window (the re-heal spans several).
    collapsed: list[float] = []
    for t in hp1_windows:
        if not collapsed or t - collapsed[-1] > _HP1_BUCKET_MS / 1000.0 * 2:
            collapsed.append(t)

    # ---- buffs stream -> shield-pool sizes
    for ev in buff_evs:
        absorb = ev.get("absorb")
        if not absorb or ev.get("type") not in ("applybuff", "refreshbuff"):
            continue
        name = out.ability_names.get(ev.get("abilityGameID") or 0, "")
        if name:
            out.shield_pools.setdefault(name, []).append(float(absorb))

    # ---- enemy begincasts (mechanic naming anchors)
    casts = [((ev["timestamp"] - start) / 1000.0,
              int(ev.get("abilityGameID") or 0))
             for ev in enemy_casts if ev.get("type") == "begincast"]

    # ---- downtime context bands
    try:
        out.downtime = compute_downtime_windows(
            targetability, start, end,
            resolve_enemy_actor_ids(fight),
            resolve_boss_actor_ids(summary, fight))
    except Exception:  # noqa: BLE001 — context-only; never fails the model
        out.downtime = []

    out.log = LogData(code=code, fight_id=fight_id, kill_s=kill_s,
                      party_size=len(party), hits=hits, enemy_casts=casts,
                      hp1_windows=collapsed)
    return out


def build_damage_model(client: Any, encounter_id: int,
                       progress: Progress | None = None) -> DamageModel:
    emit = _progress_or_noop(progress)
    emit(5, "Fetching encounter rankings…", None)
    picks = _pick_logs(client, encounter_id)
    if len(picks) < MIN_LOGS:
        raise RuntimeError(
            f"only {len(picks)} ranked kills available (need {MIN_LOGS})")

    codes = [c for c, _ in picks]
    summaries = client.get_report_summaries(codes)

    tasks = [{"id": f"{c}#{f}", "label": f"{c} fight {f}", "state": "pending"}
             for c, f in picks]

    def _emit_tasks(pct: int) -> None:
        emit(pct, f"Downloading top logs "
                  f"({sum(1 for t in tasks if t['state'] == 'done')}"
                  f"/{len(tasks)})…", list(tasks))

    fetches: list[_LogFetch] = []
    warnings: list[str] = []

    def _work(i: int) -> _LogFetch | None:
        code, fid = picks[i]
        tasks[i]["state"] = "in_flight"
        try:
            got = _fetch_log(client, i, code, fid, summaries.get(code) or {})
            tasks[i]["state"] = "done"
            return got
        except Exception as e:  # noqa: BLE001
            tasks[i]["state"] = "failed"
            warnings.append(f"log {code}#{fid} skipped: {e}")
            traceback.print_exc(file=sys.stderr)
            return None

    _emit_tasks(10)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for got in pool.map(_work, range(len(picks))):
            if got is not None:
                fetches.append(got)
            done = sum(1 for t in tasks if t["state"] in ("done", "failed"))
            _emit_tasks(10 + int(60 * done / len(tasks)))

    if len(fetches) < MIN_LOGS:
        raise RuntimeError(
            f"only {len(fetches)} of {len(picks)} top logs usable — "
            "try again later")

    emit(75, "Aggregating damage timeline…", None)
    logs = [f.log for f in fetches if f.log is not None]
    ability_names: dict[int, str] = {}
    ability_types: dict[int, int | None] = {}
    for f in fetches:
        ability_names.update(f.ability_names)
        for gid, t in f.ability_types.items():
            if ability_types.get(gid) is None:
                ability_types[gid] = t

    # Per-role max HP (probe: targetResources rides every damage-taken event).
    role_hp_samples: dict[str, list[float]] = {}
    for f in fetches:
        for role, vals in f.role_hp.items():
            role_hp_samples.setdefault(role, []).extend(vals)
    if all(role_hp_samples.get(r) for r in ("tank", "healer", "dps")):
        role_hp = {r: float(statistics.median(role_hp_samples[r]))
                   for r in ("tank", "healer", "dps")}
        hp_source = "logs"
    else:
        role_hp = dict(ROLE_MAX_HP_DEFAULT)
        hp_source = "constants"
        warnings.append("Player max HP missing from logs — using tier defaults.")

    # Healing-potency calibration: HP restored per point of cure potency.
    ratios_by_job: dict[str, list[float]] = {}
    for f in fetches:
        for job, name, amount in f.heal_samples:
            ratios_by_job.setdefault(job, []).append(
                amount / _HEAL_POTENCY_BY_NAME[name])
    hp_per_potency: dict[str, float] = {
        job: float(statistics.median(vals))
        for job, vals in ratios_by_job.items() if len(vals) >= 5
    }
    all_ratios = [v for vals in ratios_by_job.values() for v in vals]
    hp_per_potency["_default"] = (float(statistics.median(all_ratios))
                                  if len(all_ratios) >= 5
                                  else HP_PER_POTENCY_DEFAULT)

    # Empirical shield pools (median per status), unit-artifact guard applied.
    absorb_floor = _ABSORB_TRUST_MIN_FRAC * min(role_hp.values())
    pool_samples: dict[str, list[float]] = {}
    for f in fetches:
        for name, vals in f.shield_pools.items():
            pool_samples.setdefault(name, []).extend(
                v for v in vals if v >= absorb_floor)
    shield_hp_by_status = {name: float(statistics.median(vals))
                           for name, vals in pool_samples.items()
                           if len(vals) >= 3}

    emit(85, "Classifying mechanics…", None)
    mechanics, avoidable, tank_drain, cls_warnings = classify(
        logs, ability_names, ability_types)
    warnings.extend(cls_warnings)

    kill_times = sorted(log.kill_s for log in logs)
    model_kill = float(statistics.median(kill_times))
    rep = min(fetches, key=lambda f: abs(f.log.kill_s - model_kill))

    enc_name = next((n for i, n in ALL_ENCOUNTERS if i == encounter_id),
                    f"Encounter {encounter_id}")
    return DamageModel(
        mechanics=mechanics,
        avoidable_count=avoidable,
        ref_count=len(logs),
        model_kill_s=model_kill,
        ref_avg_kill_s=float(sum(kill_times) / len(kill_times)),
        role_hp=role_hp,
        hp_source=hp_source,
        tank_drain_hps=tank_drain,
        magnitudes={"shield_hp_by_status": shield_hp_by_status},
        hp_per_potency=hp_per_potency,
        downtime_windows=list(rep.downtime),
        encounter_id=encounter_id,
        encounter_name=enc_name,
        warnings=warnings,
    )
