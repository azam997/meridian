"""Whole-run bundle + the canonical job name list."""
from __future__ import annotations

from dataclasses import dataclass, field

from .aspect import AspectResult, Track


@dataclass
class ModuleResult:
    """All aspects analyzed for a single run."""
    label: str
    fight_duration_s: float
    aspects: dict[str, AspectResult] = field(default_factory=dict)
    # Tier-A downtime windows (boss untargetable) attached at analyze_pull
    # time so every downstream consumer reads the same canonical list.
    # `downtime_source` is one of: "targetability", "fallback_heuristic".
    downtime_windows: list[tuple[float, float]] = field(default_factory=list)
    downtime_source: str = "fallback_heuristic"
    # Player-death windows (death -> resurrection/recovery), in fight-relative
    # seconds. Distinct from downtime: death is the player's fault, so the
    # idealized ceiling plays through it (the cost stays in the recoverable
    # gap), but the delivered-side fairness aspects (Clipping, Drift) exclude
    # it so a dead stretch isn't mis-scored as a giant clip or as drift.
    death_windows: list[tuple[float, float]] = field(default_factory=list)
    # Normalized cast stream for this run. Stashed so the Tier-B consensus
    # aggregator (run after every ref completes) can read it without a
    # second fetch. Tuple-of-tuples so refs stay hashable / immutable
    # across the ThreadPoolExecutor.
    norm_casts: tuple[tuple[float, int], ...] = ()
    # Candidate multi-target windows (>= 2 enemies simultaneously targetable),
    # in fight-relative seconds with the peak simultaneous enemy count:
    # (start_s, end_s, peak_n). The NECESSARY condition for crediting splash;
    # the sidecar intersects these with ref-consensus of multi-target casts to
    # confirm windows. Empty on a single-target pull. See
    # jobs._core.downtime_sources.fetch_multi_target_windows.
    multi_target_windows: tuple[tuple[float, float, int], ...] = ()
    # Player casts that hit >= 2 targets, as (t_s, ability_id, n_targets),
    # recovered by packetID-grouping the player's DamageDone. Drives both the
    # delivered-side splash credit (this run) and the ref-consensus signal
    # (across refs). Populated only on multi-target pulls of splash-bearing
    # jobs; empty otherwise. See jobs._core.multi_target.
    observed_multi_target_casts: tuple[tuple[float, int, int], ...] = ()
    # Every cast of a splash-bearing ability and its target count INCLUDING
    # single-target hits (n=1), so the timeline can flag casts that hit fewer
    # targets than the window afforded. Populated like the above. See
    # jobs._core.multi_target.observed_splash_casts.
    splash_casts: tuple[tuple[float, int, int], ...] = ()
    # Friendly-player job names in this fight (party composition), used to seed
    # the Kill Time Theorizer's comp selector with the observed comp. See
    # jobs._core.buff_windows.party_jobs_in_fight.
    party_jobs: tuple[str, ...] = ()
    # Prog (wipe) pull context — asdict(ProgContext) when the analyzed fight
    # was not a kill (scored window clamped at the terminal death; the full
    # wipe span + Tier-A downtime feed the kill-time projector). None on
    # kills and on every ref, which stay byte-identical. See jobs._core.prog.
    prog: dict | None = None
    # Boss phase segments for phasic (per-phase) analysis, in fight-relative
    # seconds. Populated only when the fight carries `phaseTransitions`
    # (ultimates and multi-phase encounters); empty for single-phase Savage
    # pulls, which then emit no phase data at all. Runs identically for the
    # subject and every ref, so refs are segmentable by their own phases.
    # See jobs._core.phases.Phase / phase_segments.
    phases: tuple = ()

    @property
    def tracks(self) -> list[Track]:
        return [a.track for a in self.aspects.values()]


# Canonical list of FFXIV jobs (combat). Used by the Setup UI to populate the
# job picker even for jobs that don't have analyzer support yet. Whether a
# given job is *supported* (i.e., has at least one Aspect registered) is
# checked separately via `jobs.is_supported`.
ALL_JOBS: list[str] = [
    "Paladin", "Warrior", "Dark Knight", "Gunbreaker",
    "White Mage", "Scholar", "Astrologian", "Sage",
    "Monk", "Dragoon", "Ninja", "Samurai", "Reaper", "Viper",
    "Bard", "Machinist", "Dancer",
    "Black Mage", "Summoner", "Red Mage", "Pictomancer",
]
