"""Public surface of the analyzer.

Stable entry points:
- `analyze_pull(job, client, code, fight_id, ranking_name, label)`
- `compare_aspect(job, aspect_name, you, refs)`
- `get_job(name)` — registry lookup; per-job packages register themselves
  the first time they're imported (lazily via this module's `_JOB_PACKAGES`).

Plus the dataclasses (TrackEvent, Track, AspectResult, AspectComparison,
ModuleResult) and the job-name list (ALL_JOBS / is_supported / registered_jobs).
Anything else is private (lives under `jobs._core` / `jobs._aspects`, or
in a per-job package).
"""
from __future__ import annotations

import importlib

# --- Re-exports (stable public types) ----------------------------------------

from jobs._core.aspect import (
    Aspect,
    AspectComparison,
    AspectResult,
    PRE_PULL_LOOKBACK_S,
    Track,
    TrackEvent,
)
from jobs._core.cached_client import CachedEventsClient, _CachedEventsClient
from jobs._core.job import (
    CDRRule,
    GaugeModel,
    IdealizedSimulator,
    Job,
    JobData,
    RaidBuff,
    SimResult,
    get_job as _get_job_raw,
    register,
    registered_jobs,
)
from jobs._core.module_result import ALL_JOBS, ModuleResult

__all__ = [
    # Pipeline
    "analyze_pull",
    "compare_aspect",
    "compute_tier_b_for_user",
    "compute_tier_b_tiered_for_user",
    "aspects_for",
    "list_aspect_names",
    # Registry
    "get_job",
    "is_supported",
    "register",
    "registered_jobs",
    "ALL_JOBS",
    # Dataclasses
    "Aspect",
    "AspectComparison",
    "AspectResult",
    "ModuleResult",
    "Track",
    "TrackEvent",
    "PRE_PULL_LOOKBACK_S",
    # JobData + helpers
    "Job",
    "JobData",
    "GaugeModel",
    "RaidBuff",
    "CDRRule",
    "IdealizedSimulator",
    "SimResult",
    # Cached client
    "CachedEventsClient",
    "_CachedEventsClient",
]


# --- Per-job package registry ------------------------------------------------
# Lazily imported on first `get_job(name)` call. Avoids circular imports at
# module load time and keeps cold-start cheap when only one job is in use.

_JOB_PACKAGES: dict[str, str] = {
    "Bard":      "jobs.bard",
    "Machinist": "jobs.machinist",
    "Samurai":   "jobs.samurai",
    "Reaper":    "jobs.reaper",
    "Red Mage":  "jobs.redmage",
    "Paladin":   "jobs.paladin",
    "Warrior":   "jobs.warrior",
    "White Mage": "jobs.whitemage",
    "Astrologian": "jobs.astrologian",
    "Scholar":   "jobs.scholar",
    "Sage":      "jobs.sage",
    "Dancer":    "jobs.dancer",
    "Black Mage": "jobs.blackmage",
    "Viper":     "jobs.viper",
    "Dragoon":   "jobs.dragoon",
    "Gunbreaker": "jobs.gunbreaker",
    "Ninja":     "jobs.ninja",
    "Monk":      "jobs.monk",
    "Pictomancer": "jobs.pictomancer",
    "Summoner":  "jobs.summoner",
    "Dark Knight": "jobs.darkknight",
}


def _ensure_loaded(name: str) -> None:
    pkg = _JOB_PACKAGES.get(name)
    if pkg is None:
        return
    mod = importlib.import_module(pkg)
    # Per-job packages defer registration to a `_register_self()` function so
    # that importing the package (e.g. for a data shim) doesn't trigger the
    # full aspect-build cascade and a circular import.
    register_fn = getattr(mod, "_register_self", None)
    if register_fn is not None:
        register_fn()


def get_job(name: str) -> Job:
    """Resolve a Job by canonical name. Triggers lazy import of the per-job
    package on first lookup, which is when the package calls `register()`."""
    try:
        return _get_job_raw(name)
    except KeyError:
        _ensure_loaded(name)
        return _get_job_raw(name)


def is_supported(job: str) -> bool:
    """Has analyzer support (i.e. a per-job package that registers a Job)."""
    return job in _JOB_PACKAGES


def aspects_for(job: str) -> list[Aspect]:
    """Aspects to run for `job`, in order. Backwards-compatible wrapper
    around the new registry."""
    if not is_supported(job):
        if job not in ALL_JOBS:
            raise NotImplementedError(f"{job} is not a known FFXIV job")
        raise NotImplementedError(f"{job} has no analyzer support yet")
    return list(get_job(job).aspects)


def list_aspect_names(job: str) -> list[str]:
    return [a.name for a in aspects_for(job)]


# --- Analysis pipeline -------------------------------------------------------

def _prime_pull_bundle(client, job: str, code: str, report: dict,
                       fight: dict, actor: dict) -> None:
    """Prefetch this pull's predictable event streams in one aliased request and
    seed the per-pull cache. The generic streams (actor casts with the pre-pull
    look-back, targetability, player buffs) plus any job-declared extras (e.g.
    MCH Queen pet damage) cover everything the aspects fetch except the rare,
    comp-dependent on-enemy provider casts. Best-effort: any failure leaves the
    cache empty and aspects fetch individually (no behavior change)."""
    prime = getattr(client, "prime_bundle", None)
    if prime is None:
        return
    try:
        from fflogs_api import BundleStream
    except Exception:
        return
    start, end = fight["startTime"], fight["endTime"]
    fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
    aid = actor["id"]
    job_obj = get_job(job)
    streams = [
        BundleStream(data_type="Casts", start=fetch_start, end=end, source_id=aid),
        BundleStream(data_type="All", start=start, end=end,
                     filter_expression='type="targetabilityupdate"'),
        # Enemy activity — the silent-despawn tail evidence Tier A reads
        # alongside targetability (keyed as get_enemy_cast_events).
        BundleStream(data_type="Casts", start=start, end=end,
                     hostility="Enemies"),
        # Player buffs, widened to the pre-pull look-back: the exact-fight
        # consumers (buff windows / tincture / procs) are served by narrowing
        # in CachedEventsClient.get_aura_events, and the pre-pull detectors
        # (`_detect_prepull_buffs`) hit it directly — so no job pays a separate
        # per-pull aura round trip (MCH/MNK used to).
        BundleStream(data_type="Buffs", start=fetch_start, end=end,
                     source_id=aid),
        # Player deaths — keyed identically to resolve_deaths' get_events call
        # (fight-relative, no pre-pull look-back) so that fetch becomes a hit.
        BundleStream(data_type="Deaths", start=start, end=end, source_id=aid),
    ]
    # Player DamageDone — for jobs that reconstruct buff/debuff coverage from each
    # hit's snapshot every pull (RPR/SAM/WAR/MCH; the multi-target-only readers
    # leave the flag off and fetch on demand on MT pulls). Same key as those
    # `get_events(..., "DamageDone")` calls so they become hits.
    if job_obj.data.prebundle_damage_done:
        streams.append(BundleStream(data_type="DamageDone", start=start, end=end,
                                    source_id=aid))
    # On-enemy raid-buff provider casts (Chain Stratagem / Dokumori) — job-agnostic,
    # comp-dependent. Folds the per-provider cast fetch in fetch_observed_buff_windows
    # into this one round trip. Empty for the common no-SCH/NIN comp.
    from jobs._core.buff_windows import provider_cast_streams
    streams += provider_cast_streams(report, fight)
    extra = job_obj.bundle_extra_streams
    if extra is not None:
        try:
            streams += list(extra(report, fight, actor) or [])
        except Exception:
            pass
    try:
        prime(code, streams)
    except Exception:
        pass


def analyze_pull(job: str, client, code: str, fight_id: int | None,
                 ranking_name: str | None, label: str,
                 extra_report: dict | None = None) -> ModuleResult:
    """Fetch the report, locate the player, run every aspect.

    `extra_report` merges extra private keys into the per-pull report dict the
    aspects read (the `__x__` idiom) — the sidecar stages the healer mit-plan
    locks there (`__heal_locks__`) for the user's run only; refs pass nothing
    and stay byte-identical."""
    if not is_supported(job):
        raise NotImplementedError(f"{job} has no analyzer support yet")

    # Wrap the client so per-aspect duplicate event fetches collapse.
    client = CachedEventsClient(client)

    from jobs._core.actors import find_fight, find_player_actor
    from jobs._core.casts import fetch_norm_casts
    from jobs._core.deaths import resolve_deaths
    from jobs._core.downtime import resolve_downtime
    report = client.get_report_summary(code)
    fight = find_fight(report, fight_id)
    if not fight:
        raise RuntimeError(f"fight {fight_id} not found in report {code}")
    actor = find_player_actor(report, fight=fight, job_name=job,
                              player_name=ranking_name)
    if not actor:
        raise RuntimeError(f"no {job} actor in fight {fight_id} of {code}")

    # Prog (wipe) pull: clamp the scored window at the player's terminal
    # death BEFORE anything reads fight["endTime"] — every duration
    # computation, event fetch window, and aspect then sees the truncated
    # fight consistently, and the terminal death collapses to a zero-length
    # (unpriced) death window. The pre-pass also captures the FULL wipe span
    # + its Tier-A downtime for the kill-time projector. Kills (and every
    # ref) never enter this branch. The session-cached summary is never
    # mutated — the clamp lives on a copy.
    # Full (unclamped) fight end — the wipe span for a prog pull — captured
    # BEFORE the terminal-death clamp so phase segments cover every phase the
    # raid reached, not just the scored window.
    orig_end_ms = fight["endTime"]
    prog_dict: dict | None = None
    if fight.get("kill") is False:
        from dataclasses import asdict

        from jobs._core.prog import build_prog_context
        prog_ctx = build_prog_context(client, code, report, fight, actor)
        prog_dict = asdict(prog_ctx)
        fight = dict(fight)
        fight["endTime"] = prog_ctx.scored_end_ms

    duration = (fight["endTime"] - fight["startTime"]) / 1000.0

    # Fold this pull's predictable event streams into ONE round trip and seed
    # the per-pull cache, so the fetches below (casts, targetability, buffs,
    # pet damage) become cache hits. Best-effort — falls back to per-stream
    # fetches on any error.
    _prime_pull_bundle(client, job, code, report, fight, actor)

    # Compute Tier-A downtime once per pull. Stash on the `report` dict
    # under a private key so every aspect sees the same windows without
    # re-fetching. Falls back to the legacy cast-gap heuristic only when
    # the Tier-A fetch itself fails (network error / missing data).
    norm_casts = fetch_norm_casts(client, code, fight, actor)
    windows, source = resolve_downtime(
        client, code, report, fight, norm_casts,
        policy=get_job(job).data.role_policy,
        actor=actor,
    )
    # Player-death windows. Stashed alongside downtime so Clipping / Drift can
    # exclude dead time without re-fetching. Distinct from downtime: the
    # idealized ceiling intentionally ignores these (death is the player's
    # fault, so its cost stays in the recoverable gap).
    death_windows = resolve_deaths(client, code, fight, actor, norm_casts)
    # Candidate multi-target windows (>= 2 enemies simultaneously targetable).
    # Reuses the cached targetability fetch from resolve_downtime above, so this
    # is a cache hit (no extra round trip). Pure per-pull data; the sidecar
    # confirms windows post-refs by intersecting with ref-consensus.
    from jobs._core.downtime_sources import fetch_multi_target_windows
    mt_windows = fetch_multi_target_windows(client, code, report, fight)
    # Observed multi-target casts (packetID-grouped player DamageDone). Worth
    # fetching when the job has ANY multi-target kit (free-splash OR a dedicated
    # AoE rotation) AND this pull actually affords multi-target (>= 2 targetable)
    # — skips the work on single-target pulls and pure-ST jobs. The DamageDone
    # stream is warmed by the prefetch bundle, so this is a cache hit. The
    # `observed_multi_target_casts` set drives the ref-consensus + the delivered
    # measured-N credit; `splash_casts` (splash-only) drives the per-cast UI dots.
    observed_mt: tuple[tuple[float, int, int], ...] = ()
    splash_casts: tuple[tuple[float, int, int], ...] = ()
    job_data = get_job(job).data
    splash_ids = job_data.splash_potencies
    if (splash_ids or job_data.aoe_potencies) and mt_windows:
        from jobs._core.multi_target import (
            observed_multi_target_casts,
            observed_splash_casts,
        )
        observed_mt = observed_multi_target_casts(client, code, fight, actor)
        if splash_ids:
            splash_casts = observed_splash_casts(
                client, code, fight, actor, frozenset(splash_ids))
    # Observed party composition (friendly-player jobs) — seeds the Kill Time
    # Theorizer's comp selector default. Cheap masterData read; no extra fetch.
    from jobs._core.buff_windows import party_jobs_in_fight
    party_jobs = tuple(party_jobs_in_fight(report, fight))
    # Boss phase segments (phasic analysis). Uses the FULL wipe span so a prog
    # pull's phases still cover the phases the raid reached; () for a
    # single-phase Savage fight, which then emits no phase data. Zero extra
    # round trips — phaseTransitions + the report `phases` block ride the
    # already-fetched summary. Computed for the subject and every ref alike.
    from jobs._core.phases import phase_segments
    phases = phase_segments(report, fight, full_end_ms=orig_end_ms)
    report = dict(report)
    report["__downtime__"] = {"windows": windows, "source": source}
    report["__deaths__"] = {"windows": death_windows}
    report["__multitarget__"] = {"windows": mt_windows}
    # Phase segments for the Scoring aspect's per-phase delivered potency (only
    # when phased — Savage never sets the key, so its aspects run byte-identical).
    if phases:
        report["__phases__"] = phases
    if prog_dict is not None:
        report["__prog__"] = prog_dict
    if extra_report:
        report.update(extra_report)

    result = ModuleResult(label=label, fight_duration_s=duration,
                           downtime_windows=list(windows),
                           downtime_source=source,
                           death_windows=list(death_windows),
                           norm_casts=tuple(norm_casts),
                           multi_target_windows=tuple(mt_windows),
                           observed_multi_target_casts=observed_mt,
                           splash_casts=splash_casts,
                           party_jobs=party_jobs,
                           prog=prog_dict,
                           phases=phases)
    for aspect in get_job(job).aspects:
        ar = aspect.analyze(client, code, fight, actor, report)
        ar.run_label = label
        result.aspects[ar.name] = ar
    return result


def compute_tier_b_for_user(
    job: str,
    you: ModuleResult,
    refs: list[ModuleResult],
):
    """Top-level helper invoked by the sidecar after every ref completes.
    Returns the Tier-B consensus windows on the user's fight duration.

    Refs whose duration is wildly different from the user's are still
    included — discretization handles per-tick presence — but a future
    refinement could bucket by duration proximity if M11S-style fights
    show alignment drift.
    """
    from jobs._core.downtime_sources import (
        RefRun,
        consensus_windows_from_refs,
    )
    data = get_job(job).data
    ref_runs = [
        RefRun(label=r.label, norm_casts=r.norm_casts,
               fight_duration_s=r.fight_duration_s)
        for r in refs
    ]
    return consensus_windows_from_refs(
        ref_runs=ref_runs,
        fight_duration_s=you.fight_duration_s,
        policy=data.role_policy,
        data=data,
        tier_a_windows=you.downtime_windows,
    )


def compute_tier_b_tiered_for_user(
    job: str,
    you: ModuleResult,
    refs: list[ModuleResult],
):
    """`(tier_b, high_confidence)` consensus windows on the user's fight duration.
    Tier B (`consensus_pct`) is the suspected-forced band; high-confidence
    (`consensus_high_pct`) is the genuinely-forced core the idealized rotation
    skips and never scores against the player. See `consensus_windows_tiered`."""
    from jobs._core.downtime_sources import (
        RefRun,
        consensus_windows_tiered,
    )
    data = get_job(job).data
    ref_runs = [
        RefRun(label=r.label, norm_casts=r.norm_casts,
               fight_duration_s=r.fight_duration_s)
        for r in refs
    ]
    return consensus_windows_tiered(
        ref_runs=ref_runs,
        fight_duration_s=you.fight_duration_s,
        policy=data.role_policy,
        data=data,
        tier_a_windows=you.downtime_windows,
    )


def compute_ranged_windows_for_user(
    job: str,
    you: ModuleResult,
    refs: list[ModuleResult],
    tier_b_windows: list[tuple[float, float]] | None = None,
):
    """Consensus ranged-filler windows (forced melee disconnects the refs
    bridged with the job's ranged filler, e.g. RPR Harpe — or ate as a short
    idle: the union votes) on the user's fight duration. Tier-B's sibling —
    consumed by the LENIENT ceiling only; `tier_b_windows` are subtracted so
    a stretch already pardoned as downtime is never double-counted. [] when
    the job declares no `ranged_filler_id` (every job but RPR today)."""
    from jobs._core.downtime_sources import (
        RefRun,
        ranged_filler_windows_from_refs,
    )
    data = get_job(job).data
    if data.ranged_filler_id is None:
        return []
    ref_runs = [
        RefRun(label=r.label, norm_casts=r.norm_casts,
               fight_duration_s=r.fight_duration_s)
        for r in refs
    ]
    return ranged_filler_windows_from_refs(
        ref_runs=ref_runs,
        fight_duration_s=you.fight_duration_s,
        policy=data.role_policy,
        filler_id=data.ranged_filler_id,
        tier_a_windows=you.downtime_windows,
        data=data,
        exclude_windows=tier_b_windows,
    )


def compare_aspect(job: str, aspect_name: str,
                   you: ModuleResult,
                   refs: list[ModuleResult]) -> AspectComparison:
    for aspect in get_job(job).aspects:
        if aspect.name == aspect_name:
            you_ar = you.aspects.get(aspect_name)
            ref_ars = [r.aspects[aspect_name] for r in refs if aspect_name in r.aspects]
            if you_ar is None:
                return AspectComparison(aspect_name=aspect_name,
                                        findings=[f"{aspect_name}: no data in your run."])
            return aspect.compare(you_ar, ref_ars)
    raise KeyError(f"unknown aspect {aspect_name!r} for job {job!r}")
