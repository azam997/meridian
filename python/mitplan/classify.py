"""Pure classification: per-log damage-taken hits -> the encounter's forced-
damage mechanics. No IO — damage.py normalizes the FFLogs streams into
`LogData` and this module votes/clusters/labels across logs.

Pipeline (thresholds in the tunables block):
  1. Per log + ability id (tick and non-tick kept apart): hits -> bursts
     (packetID, else a time window) -> instances (bursts chained while close;
     tick rows chain on the 3s server-tick cadence into one bleed).
  2. Auto-attacks excluded (folded into a background tank drain rate).
  3. Cross-log alignment per (ability, tick): pool every log's instances on
     the fight-relative time axis, split where the gap is large, re-split any
     cluster holding two instances from the same log.
  4. Forced voting: present in enough eligible logs (a log only votes on
     mechanics its kill reached) + a noise floor.
  5. Kind (raidwide / tankbuster / bleed / multiHit / other), naming via
     masterData + enemy begincast correlation, school via ability type.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

# --- tunables ---------------------------------------------------------------
BURST_WINDOW_S = 0.5        # same-application grouping when packetID absent
INSTANCE_GAP_S = 2.0        # bursts closer than this merge into one instance
TICK_GAP_S = 3.5            # DoT ticks chain on the 3s server tick
ALIGN_GAP_MIN_S = 8.0       # cross-log cluster split floor
ALIGN_GAP_SPACING_MULT = 0.3
FORCED_PRESENCE = 0.7       # fraction of eligible logs that must contain it
NOISE_FLOOR_UNMIT = 1_000.0
AUTO_CADENCE_S = 5.0        # faster than this + small + tank-only = auto
AUTO_UNMIT_CEIL = 30_000.0
TB_UNMIT_MIN = 50_000.0     # tank-only hits below this are residual autos
RAIDWIDE_BREADTH = 0.75     # fraction of party hit (tolerates deaths)
OTHER_PRESENCE = 0.9        # mid-breadth (stack/spread) forced threshold
MULTI_HIT_MIN_GAP_S = 0.8   # internal bursts this far apart => multiHit
CAST_CORRELATE_S = 12.0     # begincast within this before the first hit names it
ELIGIBLE_SLACK_S = 5.0      # kill must reach median start - slack to vote
HPSET_CLUSTER_GAP_S = 8.0   # cross-log alignment of party-at-1-HP windows
HPSET_CAST_CORRELATE_S = 14.0  # begincast lead that names an HP-set mechanic

# FFLogs ability `type` -> damage school (xivanalysis AbilityType enum).
_SCHOOL_BY_TYPE = {1: "physical", 128: "physical",
                   64: "magical", 1024: "magical",
                   32: "special"}

ROLE_KEYS = ("tank", "healer", "dps")


@dataclass(frozen=True)
class Hit:
    t: float                # fight-relative seconds
    target: int
    role: str               # tank | healer | dps
    ability_id: int
    unmit: float
    amount: float           # applied damage (post-mit, post-shield)
    absorbed: float
    multiplier: float
    tick: bool
    pid: int | None = None  # packetID — groups one application's hits


@dataclass
class LogData:
    code: str
    fight_id: int
    kill_s: float
    party_size: int
    hits: list[Hit]
    enemy_casts: list[tuple[float, int]] = field(default_factory=list)
    # Moments where most of the party was healed FROM ~zero HP — the signature
    # of an HP-set mechanic (they emit no damage events; see damage.py).
    hp1_windows: list[float] = field(default_factory=list)


@dataclass
class Instance:
    """One occurrence of an ability's damage in one log."""
    log_idx: int
    start: float
    end: float
    bursts: list[list[Hit]]

    @property
    def all_hits(self) -> list[Hit]:
        return [h for b in self.bursts for h in b]


@dataclass
class Mechanic:
    id: str
    time_s: float
    end_s: float
    name: str
    boss_ability_ids: list[int]
    kind: str               # raidwide | tankbuster | bleed | multiHit | other | hpSet
    school: str             # physical | magical | special | mixed | unknown
    hits: list[dict]        # [{time_s, unmitigated: {role: amt}}] per burst/tick
    unmitigated: dict[str, float]
    unmitigated_p90: dict[str, float]
    observed_mit_pct: float
    presence_ratio: float
    tank_targets: int = 1   # 1- vs 2-target buster mode
    targets_hit: int = 0    # distinct players hit (breadth); 0 = unknown/none
    roles_hit: frozenset[str] = frozenset()  # roles with nonzero unmitigated
    notes: list[str] = field(default_factory=list)


@dataclass
class DamageModel:
    mechanics: list[Mechanic]
    avoidable_count: int
    ref_count: int
    model_kill_s: float
    ref_avg_kill_s: float
    role_hp: dict[str, float]
    hp_source: str                       # "logs" | "constants"
    tank_drain_hps: float
    magnitudes: dict            # empirical heal/shield sizes (see damage.py)
    hp_per_potency: dict[str, float]     # per healer job (+ "_default")
    downtime_windows: list[tuple[float, float]]
    encounter_id: int = 0
    encounter_name: str = ""
    warnings: list[str] = field(default_factory=list)


# --- step 1: in-log instancing ----------------------------------------------

def _group_bursts(hits: list[Hit]) -> list[list[Hit]]:
    """Hits of one (log, ability) -> application bursts. packetID groups when
    present; a hit without one joins the current burst inside BURST_WINDOW_S."""
    bursts: list[list[Hit]] = []
    by_packet: dict[int, list[Hit]] = {}
    loose: list[Hit] = []
    for h in hits:
        if h.pid is not None:
            by_packet.setdefault(h.pid, []).append(h)
        else:
            loose.append(h)
    bursts.extend(by_packet.values())
    loose.sort(key=lambda h: h.t)
    for h in loose:
        if bursts and not by_packet and h.t - bursts[-1][-1].t <= BURST_WINDOW_S:
            bursts[-1].append(h)
        else:
            bursts.append([h])
    bursts.sort(key=lambda b: min(h.t for h in b))
    return bursts


def _instances_for(hits: list[Hit], log_idx: int, tick: bool) -> list[Instance]:
    bursts = _group_bursts(hits)
    gap = TICK_GAP_S if tick else INSTANCE_GAP_S
    out: list[Instance] = []
    for b in bursts:
        b_start = min(h.t for h in b)
        b_end = max(h.t for h in b)
        if out and b_start - out[-1].end <= gap:
            out[-1].bursts.append(b)
            out[-1].end = max(out[-1].end, b_end)
        else:
            out.append(Instance(log_idx, b_start, b_end, [b]))
    return out


# --- step 3: cross-log clustering -------------------------------------------

def _cluster(instances: list[Instance]) -> list[list[Instance]]:
    """Pool all logs' instances of one (ability, tick) and split on time gaps."""
    if not instances:
        return []
    inst = sorted(instances, key=lambda i: i.start)
    spacings: list[float] = []
    by_log: dict[int, list[Instance]] = {}
    for i in inst:
        by_log.setdefault(i.log_idx, []).append(i)
    for rows in by_log.values():
        for a, b in zip(rows, rows[1:]):
            spacings.append(b.start - a.start)
    med_spacing = statistics.median(spacings) if spacings else 0.0
    split_gap = max(ALIGN_GAP_MIN_S, ALIGN_GAP_SPACING_MULT * med_spacing)

    clusters: list[list[Instance]] = [[inst[0]]]
    for i in inst[1:]:
        if i.start - clusters[-1][-1].start > split_gap:
            clusters.append([i])
        else:
            clusters[-1].append(i)

    # Guard: a cluster holding two instances from the SAME log means the gap
    # under-split — re-split at the largest internal gap until clean.
    out: list[list[Instance]] = []
    work = clusters
    for _ in range(64):
        redo: list[list[Instance]] = []
        for c in work:
            logs = [i.log_idx for i in c]
            if len(logs) == len(set(logs)) or len(c) < 2:
                out.append(c)
                continue
            gaps = [(c[k + 1].start - c[k].start, k) for k in range(len(c) - 1)]
            g, k = max(gaps)
            if g <= 0:
                out.append(c)  # simultaneous duplicates — unsplittable
                continue
            redo.append(c[:k + 1])
            redo.append(c[k + 1:])
        if not redo:
            break
        work = redo
    else:
        out.extend(work)
    out.sort(key=lambda c: statistics.median(i.start for i in c))
    return out


# --- helpers -----------------------------------------------------------------

def _role_stat(values_by_role: dict[str, list[float]], fn) -> dict[str, float]:
    return {r: (fn(vals) if vals else 0.0)
            for r, vals in ((k, values_by_role.get(k, [])) for k in ROLE_KEYS)}


def _p90(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def school_for(ability_ids: list[int],
               ability_types: dict[int, int | str | None]) -> str:
    schools = set()
    for aid in ability_ids:
        t = ability_types.get(aid)
        try:
            t = int(t) if t is not None else None   # the API returns strings
        except (TypeError, ValueError):
            t = None
        schools.add(_SCHOOL_BY_TYPE.get(t) if t is not None else None)
    schools.discard(None)
    if not schools:
        return "unknown"
    if len(schools) > 1:
        return "mixed"
    return next(iter(schools))


# --- HP-set mechanics ---------------------------------------------------------

def _detect_hp_set(logs: list[LogData],
                   ability_names: dict[int, str]) -> list[Mechanic]:
    """Mechanics that SET the party's HP (to ~1) emit no damage events, so the
    per-log evidence is `hp1_windows` (most of the party healed from ~zero HP
    at once — computed in damage.py from the Healing stream's resources).
    Cluster the windows across logs, vote with the usual eligibility rule, and
    name the survivor via the nearest enemy begincast (M11S: Charybdistopia)."""
    windows = [(li, t) for li, log in enumerate(logs) for t in log.hp1_windows]
    if not windows:
        return []
    windows.sort(key=lambda w: w[1])
    clusters: list[list[tuple[int, float]]] = [[windows[0]]]
    for w in windows[1:]:
        if w[1] - clusters[-1][-1][1] > HPSET_CLUSTER_GAP_S:
            clusters.append([w])
        else:
            clusters[-1].append(w)

    out: list[Mechanic] = []
    ordinal = 0
    zeros = {r: 0.0 for r in ROLE_KEYS}
    for c in clusters:
        med = statistics.median(t for _, t in c)
        eligible_idx = {li for li, log in enumerate(logs)
                        if log.kill_s >= med - ELIGIBLE_SLACK_S}
        if not eligible_idx:
            continue
        presence = len({li for li, _ in c} & eligible_idx) / len(eligible_idx)
        if presence < FORCED_PRESENCE:
            continue
        best: tuple[float, str, int] | None = None
        for li in sorted(eligible_idx):
            for ct, cid in logs[li].enemy_casts:
                if 0 <= med - ct <= HPSET_CAST_CORRELATE_S:
                    cn = (ability_names.get(cid) or "").strip()
                    if cn and cn.lower() != "attack" \
                            and (best is None or ct > best[0]):
                        best = (ct, cn, cid)
        name = best[1] if best else "HP set to 1"
        aid = best[2] if best else 0
        out.append(Mechanic(
            id=f"hpset#{ordinal}", time_s=med, end_s=med + 1.0, name=name,
            boss_ability_ids=[aid] if aid else [], kind="hpSet",
            school="unknown",
            hits=[{"time_s": med, "unmitigated": dict(zeros)}],
            unmitigated=dict(zeros), unmitigated_p90=dict(zeros),
            observed_mit_pct=0.0, presence_ratio=presence,
            notes=["Sets the party to 1 HP — unmitigable; heal up after."],
        ))
        ordinal += 1
    return out


# --- the classifier -----------------------------------------------------------

def classify(logs: list[LogData],
             ability_names: dict[int, str],
             ability_types: dict[int, int | None],
             ) -> tuple[list[Mechanic], int, float, list[str]]:
    """-> (mechanics, avoidable_count, tank_drain_hps, warnings)."""
    warnings: list[str] = []
    n_logs = len(logs)
    if n_logs == 0:
        return [], 0, 0.0, ["no logs to classify"]

    # ---- per-(ability, tick) instances across logs
    per_key: dict[tuple[int, bool], list[Instance]] = {}
    for li, log in enumerate(logs):
        by_key: dict[tuple[int, bool], list[Hit]] = {}
        for h in log.hits:
            by_key.setdefault((h.ability_id, h.tick), []).append(h)
        for (aid, tick), hits in by_key.items():
            hits.sort(key=lambda h: h.t)
            per_key.setdefault((aid, tick), []).extend(
                _instances_for(hits, li, tick))

    # ---- auto-attack detection (ability-level, non-tick only)
    autos: set[int] = set()
    for (aid, tick), instances in per_key.items():
        if tick:
            continue
        name = (ability_names.get(aid) or "").strip().lower()
        if name == "attack":
            autos.add(aid)
            continue
        starts = sorted(i.start for i in instances)
        if len(starts) < 8:
            continue
        by_log: dict[int, list[float]] = {}
        for i in instances:
            by_log.setdefault(i.log_idx, []).append(i.start)
        cadences = []
        for rows in by_log.values():
            rows.sort()
            cadences.extend(b - a for a, b in zip(rows, rows[1:]))
        if not cadences:
            continue
        all_hits = [h for i in instances for h in i.all_hits]
        tank_only = all(h.role == "tank" for h in all_hits)
        med_unmit = statistics.median(h.unmit for h in all_hits)
        if (statistics.median(cadences) < AUTO_CADENCE_S
                and med_unmit < AUTO_UNMIT_CEIL and tank_only):
            autos.add(aid)

    # ---- background tank drain (autos, post-mit + absorbed), per log
    drains: list[float] = []
    for li, log in enumerate(logs):
        auto_dmg = sum(h.amount + h.absorbed for h in log.hits
                       if h.ability_id in autos and h.role == "tank")
        if log.kill_s > 0:
            drains.append(auto_dmg / log.kill_s / 2.0)  # per tank
    tank_drain_hps = statistics.median(drains) if drains else 0.0

    # ---- cluster + vote + label
    mechanics: list[Mechanic] = []
    avoidable = 0
    ordinals: dict[int, int] = {}
    party_size = max(log.party_size for log in logs)

    for (aid, tick), instances in sorted(per_key.items()):
        if aid in autos:
            continue
        for cluster in _cluster(instances):
            med_start = statistics.median(i.start for i in cluster)
            eligible_idx = {li for li, log in enumerate(logs)
                            if log.kill_s >= med_start - ELIGIBLE_SLACK_S}
            if not eligible_idx:
                continue
            eligible = [logs[li] for li in sorted(eligible_idx)]
            present_logs = {i.log_idx for i in cluster}
            presence = len(present_logs & eligible_idx) / len(eligible_idx)

            all_hits = [h for i in cluster for h in i.all_hits]
            per_target = [h.unmit for h in all_hits]
            med_unmit = statistics.median(per_target) if per_target else 0.0

            # breadth: distinct targets per instance
            breadths = [len({h.target for h in i.all_hits}) for i in cluster]
            med_breadth = statistics.median(breadths) if breadths else 0
            tank_only_frac = (sum(1 for i in cluster
                                  if all(h.role == "tank" for h in i.all_hits))
                              / len(cluster))

            forced = presence >= FORCED_PRESENCE and med_unmit >= NOISE_FLOOR_UNMIT
            # kind
            if tick:
                kind = "bleed"
            elif med_breadth >= math.ceil(RAIDWIDE_BREADTH * party_size):
                kind = "raidwide"
            elif tank_only_frac >= 0.8 and med_unmit >= TB_UNMIT_MIN:
                kind = "tankbuster"
            elif med_breadth >= 3:
                kind = "other"
                forced = forced and presence >= OTHER_PRESENCE
            else:
                forced = False
                kind = "other"
            if not forced:
                avoidable += 1
                continue

            # multi-hit: median internal burst count with real separation
            def _sep_bursts(inst: Instance) -> int:
                times = sorted(min(h.t for h in b) for b in inst.bursts)
                n = 1
                for a, b in zip(times, times[1:]):
                    if b - a >= MULTI_HIT_MIN_GAP_S:
                        n += 1
                return n
            med_burst_n = int(statistics.median(_sep_bursts(i) for i in cluster))
            if kind == "raidwide" and med_burst_n >= 2:
                kind = "multiHit"

            # per-PERSON totals: what one hit player of each role takes from
            # the whole instance (sum per (instance, target), median per role).
            # This is the planning number for raidwides, spreads, busters and
            # bleeds alike — a person either eats the mechanic or isn't hit.
            by_role: dict[str, list[float]] = {}
            for i in cluster:
                per_tgt: dict[tuple[int, str], float] = {}
                for h in i.all_hits:
                    key = (h.target, h.role)
                    per_tgt[key] = per_tgt.get(key, 0.0) + h.unmit
                for (_, role), total in per_tgt.items():
                    by_role.setdefault(role, []).append(total)
            unmitigated = _role_stat(by_role, statistics.median)
            unmit_p90 = _role_stat(by_role, _p90)

            mults = [h.multiplier for h in all_hits if h.multiplier > 0]
            observed_mit = 1.0 - statistics.median(mults) if mults else 0.0

            # per-hit profile (burst offsets, k-th-burst per-target medians),
            # then scaled per role so the rows SUM to the per-person total —
            # keeps shield-depletion pacing honest for spreads where one
            # person only eats a subset of the bursts.
            hit_rows: list[dict] = []
            max_bursts = max(len(i.bursts) for i in cluster)
            for k in range(max_bursts):
                offs, role_vals = [], {}
                for i in cluster:
                    if k < len(i.bursts):
                        b = i.bursts[k]
                        offs.append(min(h.t for h in b) - i.start)
                        for h in b:
                            role_vals.setdefault(h.role, []).append(h.unmit)
                if len(offs) < max(1, len(cluster) // 2):
                    continue  # a straggler burst most logs don't have
                hit_rows.append({
                    "time_s": med_start + statistics.median(offs),
                    "unmitigated": _role_stat(role_vals, statistics.median),
                })
            if not hit_rows:
                hit_rows = [{"time_s": med_start, "unmitigated": unmitigated}]
            for r in ROLE_KEYS:
                row_sum = sum(float(row["unmitigated"].get(r) or 0.0)
                              for row in hit_rows)
                scale = (unmitigated.get(r, 0.0) / row_sum) if row_sum > 0 else 0.0
                for row in hit_rows:
                    row["unmitigated"][r] = (
                        float(row["unmitigated"].get(r) or 0.0) * scale)

            # name + school
            name = (ability_names.get(aid) or "").strip()
            if not name or name.lower() in ("attack", "unknown", ""):
                best = None
                for log in eligible:
                    for (ct, cid) in log.enemy_casts:
                        if 0 <= med_start - ct <= CAST_CORRELATE_S:
                            cn = (ability_names.get(cid) or "").strip()
                            if cn and cn.lower() != "attack":
                                if best is None or ct > best[0]:
                                    best = (ct, cn)
                name = best[1] if best else (name or f"Ability {aid}")
            school = school_for([aid], ability_types)

            tank_targets = 1
            if kind == "tankbuster":
                tank_targets = int(statistics.median(
                    len({h.target for h in i.all_hits}) for i in cluster)) or 1

            ordinal = ordinals.get(aid, 0)
            ordinals[aid] = ordinal + 1
            end_s = med_start + statistics.median(i.end - i.start for i in cluster)
            mechanics.append(Mechanic(
                id=f"{aid}#{ordinal}", time_s=med_start, end_s=end_s,
                name=name, boss_ability_ids=[aid], kind=kind, school=school,
                hits=hit_rows, unmitigated=unmitigated,
                unmitigated_p90=unmit_p90, observed_mit_pct=observed_mit,
                presence_ratio=presence, tank_targets=tank_targets,
                targets_hit=int(round(med_breadth)),
                roles_hit=frozenset(r for r in ROLE_KEYS
                                    if (unmitigated.get(r) or 0.0) > 0),
            ))

    if any(ability_types.get(m.boss_ability_ids[0]) is None for m in mechanics):
        warnings.append(
            "Some abilities lack school data (stale cached summaries) — "
            "mitigation credited conservatively for those mechanics.")

    mechanics.extend(_detect_hp_set(logs, ability_names))
    mechanics.sort(key=lambda m: (m.time_s, m.id))
    model_kill = statistics.median(log.kill_s for log in logs)
    for m in mechanics:
        if m.time_s > model_kill:
            m.notes.append("Only reached in slower kills.")
    return mechanics, avoidable, tank_drain_hps, warnings
