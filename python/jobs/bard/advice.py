"""Bard deep-advice pack (`Job.advice_probes`).

The BRD probe set, mirroring the Machinist reference (`jobs/machinist/advice.py`).
No `ProbeItem`s — BRD has no card enrichment that meets the MCH window-shift bar
(the Raging Strikes window is already an alignment/buff-window story). Two
`RootCause` families, both deterministic ledger walks over the delivered cast
stream in `data.py`'s own tables:

* **Cooldown drift** (Barrage / Sidewinder / Empyreal Arrow): the recast-gated
  damage buttons the sim fit more of than the player cast, with the gap-over-
  recast ledger locating the worst slip. Downtime and death windows are
  subtracted from each gap (Empyreal/Sidewinder need a target; a boss-away
  stretch is not drift). Heartbreak Shot stays out by design — its cadence is
  the Mage's Ballad repertoire CDR (RNG), mirrored by `data.DRIFT_EXCLUSIONS`.
* **Song-cycle drift**: the WM -> MB -> AP cycle against `data.SONG_SPLITS`
  (the live-measured 43.5 / 40 / 36.5 splits that sum to the 120s recast). Late
  swaps accumulate; a lost song (deficit vs the sim's song count) plus real
  accumulated slip emits one cause at the worst late swap. Songs are targetless
  (castable through downtime), so only death windows excuse a slip.

Dropped hypothesis, on purpose: **Soul Voice overcap**. The model tracks no
Soul Voice gauge — Apex/Blast are measured *budgets* (Repertoire is an 80% RNG
tick; see data.py's module docstring), so no deterministic gauge ledger exists
and a "delayed Apex" walk would read proc luck as a mistake. Silence is
correct there.

`measured_p` stays 0 on every cause — the orchestrator prices causes from the
cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (the RNG budget counters, the
timer fields, `barrage_armed`…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.bard import data as bd

# The recast-gated damage cooldowns the drift ledger watches (all single-charge
# in data.COOLDOWNS; per-use values from data.COOLDOWN_VALUE_P). Heartbreak
# Shot is excluded (RNG Mage's Ballad CDR — see data.DRIFT_EXCLUSIONS); the
# songs get their own cycle-aware walk below.
_DRIFT_COOLDOWNS: tuple[int, ...] = (
    bd.BARRAGE, bd.SIDEWINDER, bd.EMPYREAL_ARROW,
)

# Noise floor for the song-cycle walk: half the shortest live-measured split.
# Losing a whole song takes roughly a full split of accumulated slip, so a
# clean-but-jittered cycle stays silent.
_SONG_SLIP_FLOOR_S: float = 0.5 * min(bd.SONG_SPLITS.values())


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch.
# Run new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cooldown_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Drifting {name} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s late; the drift adds up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "about {recasts:.1f} full recasts of idle time",
    },
    "song_cycle": {
        "summary": ("Song cycle ran {drift:.0f}s behind, {deficit} "
                    "song{plural} lost"),
        "prescription": ("Swap songs on their planned splits. {name} came "
                         "{worst:.1f}s late at {when}; every late swap "
                         "pushes Coda and the next Radiant Finale back."),
        "count_v": "{player} / {ideal}",
        "count_note": "songs vs the sim's line",
        "slip_v": "{drift:.0f}s",
        "slip_note": "late song swaps added together",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Keys are exact public scalar fields of
# `simulator.SimState`. The RNG budget counters (refulgent_remaining,
# pp_remaining, apex_remaining, blast_remaining, hb_remaining) are deliberately
# absent: a delta there is Repertoire / Hawk's Eye / Soul Voice luck, and luck
# must never read as a mistake. `song_idx` is also deliberately absent — the
# orchestrator's generic card phrases allowlisted resources as spendable
# ("Use excess X right away here."), and a songs-behind counter cannot be
# spent; the song-cycle cause below carries its own SONG tile instead.
GAUGE_TEXT: dict[str, GaugeText] = {
    "coda": GaugeText(
        label="Coda", short="CODA",
        over_note="Radiant Finale lagged while Coda sat unused",
        under_note=None,             # fewer Coda just means a fresher Finale
        min_delta=2.0),
}

# The song-cycle cause's resource tag (an icon tile + label on the card). NOT
# part of the GAUGE_TEXT allowlist: it tags the bespoke cause only, so the
# state-delta evidence can never phrase songs as a spendable resource.
SONG_TILE = GaugeText(
    label="Songs", short="SONG",
    over_note=None,
    under_note="the song cycle fell behind the ideal line",
    min_delta=1.0)


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _blocked_s(lo: float, hi: float,
               windows: list[tuple[float, float]]) -> float:
    """Seconds of [lo, hi] covered by the given windows (downtime/deaths) —
    stretches where a use was impossible, subtracted from drift so the ledger
    never blames a boss-away gap or a death."""
    if hi <= lo:
        return 0.0
    total = 0.0
    for s, e in windows:
        total += max(0.0, min(hi, float(e)) - max(lo, float(s)))
    return total


def _cooldown_drift_causes(ctx: AdviceContext
                           ) -> list[tuple[float, RootCause]]:
    """(value, cause) per drifted cooldown: the sim fit more uses than the
    player cast AND the delivered stream shows real accumulated gap-over-recast
    (net of downtime/death cover). Located at the start of the worst slip."""
    ideal_counts: dict[int, int] = {}
    for t, a in ctx.idealized:
        if t >= 0:
            ideal_counts[a] = ideal_counts.get(a, 0) + 1
    blocked = (list(ctx.downtime_windows or [])
               + list(ctx.death_windows or []))
    out: list[tuple[float, RootCause]] = []
    for aid in _DRIFT_COOLDOWNS:
        recast, _ch = bd.COOLDOWNS[aid]
        times = sorted(t for t, a in ctx.norm_casts if a == aid and t >= 0)
        deficit = ideal_counts.get(aid, 0) - len(times)
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (slip_s, gap start)
        for a_t, b_t in zip(times, times[1:]):
            over = (b_t - a_t) - recast - _blocked_s(a_t + recast, b_t,
                                                     blocked)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a_t)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        value = float(deficit * bd.COOLDOWN_VALUE_P.get(aid, 0))
        t = TEXT["cooldown_drift"]
        out.append((value, RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=bd.COOLDOWN_VALUE_P.get(aid, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=len(times),
                                          ideal=ideal_counts.get(aid, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    return out


def _song_cycle_cause(ctx: AdviceContext
                      ) -> tuple[float, RootCause] | None:
    """The WM -> MB -> AP cycle against `data.SONG_SPLITS`: after each song,
    the next swap is due at that song's split. Accumulated lateness (net of
    death cover — songs are targetless, so downtime never excuses a slip) plus
    a lost song vs the sim's count emits one cause at the worst late swap."""
    songs = sorted((t, a) for t, a in ctx.norm_casts
                   if a in bd.SONG_ORDER and t >= 0)
    ideal_n = sum(1 for t, a in ctx.idealized
                  if a in bd.SONG_ORDER and t >= 0)
    deficit = ideal_n - len(songs)
    if deficit < 1 or len(songs) < 2:
        return None
    deaths = list(ctx.death_windows or [])
    drift_total = 0.0
    worst: tuple[float, float, int] | None = None   # (slip_s, due_t, late id)
    for (a_t, a_id), (b_t, b_id) in zip(songs, songs[1:]):
        due = a_t + bd.SONG_SPLITS[a_id]
        over = (b_t - due) - _blocked_s(due, b_t, deaths)
        if over > 0:
            drift_total += over
            if worst is None or over > worst[0]:
                worst = (over, due, b_id)
    if worst is None or drift_total < _SONG_SLIP_FLOOR_S:
        return None
    slip, due_t, late_id = worst
    when = max(0.0, min(due_t, float(ctx.fight_duration_s)))
    name = _name(late_id)
    value = float(deficit * bd.COOLDOWN_VALUE_P.get(late_id, 0))
    t = TEXT["song_cycle"]
    return (value, RootCause(
        kind="cascade_lost_use", ability_id=late_id, ability_name=name,
        time_sec=round(when, 1), measured_p=0.0,
        summary=t["summary"].format(
            drift=drift_total, deficit=deficit,
            plural="s" if deficit != 1 else ""),
        prescription=t["prescription"].format(
            name=name, worst=slip, when=_mmss(when)),
        evidence=[
            EvidenceRow(
                k="Songs",
                v=t["count_v"].format(player=len(songs), ideal=ideal_n),
                note=t["count_note"]),
            EvidenceRow(
                k="Slip",
                v=t["slip_v"].format(drift=drift_total),
                note=t["slip_note"]),
        ],
        resources=[SONG_TILE]))


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """BRD probe set. Deterministic; no ProbeItems. RootCause order is the
    priority order the orchestrator's first-in-segment-wins matching consumes:
    every cause carries its deficit-times-value weight, sorted descending with
    the ability id as the stable tie-break."""
    pairs = list(_cooldown_drift_causes(ctx))
    song = _song_cycle_cause(ctx)
    if song is not None:
        pairs.append(song)
    pairs.sort(key=lambda p: (-p[0], p[1].ability_id))
    return [], [c for _v, c in pairs]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
