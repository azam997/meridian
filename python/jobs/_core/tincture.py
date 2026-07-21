"""Tinctures (damage potions): value, detection, and placement.

A tincture is a flat **+main-stat** buff lasting 30s on a 4:30 cooldown. Main stat
enters FFXIV damage *only* through the multiplicative attack-power term

    f(MAIN) = floor(coeff * (MAIN - MAIN_LV) / MAIN_LV) + 100          (then /100)

so a tincture is a **pure multiplicative buff on all damage** — it touches neither
crit/DH nor determination — and composes with raid buffs through the exact same
`BuffWindow` / `multiplier_intervals` machinery. Its value is the ratio

    M = f(base + delta) / f(base)

`base` is the player's effective (post-food/party) main stat and `delta` is the
tincture's flat +stat for the tier. FFLogs does **not** expose per-player main
stat (gear/stats come back empty on the client-credentials API), so `base` is a
per-job constant (`JobData.tincture_main_stat`); see `tincture_multiplier`.

Detection: every tincture applies the uniform "Medicated" status, surfaced by
FFLogs as aura gameID **1000049** (status namespace 1000000 + 49). It already
rides inside the player's `Buffs` aura stream, so reading the player's real pot
windows costs no extra round-trip. Opener pots are often popped *pre-pull*,
surfacing as an orphan `removebuff` with no in-window apply — paired here as
"active from pull (t=0)".

Reusable across jobs — nothing here is job-specific. The per-job scorer decides
the multiplier and how the windows feed `multiplier_at`.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from .buff_windows import BuffWindow, multiplier_intervals

# FFLogs aura gameID for "Medicated" (in-game status 49; FFLogs offsets statuses
# by 1000000). Confirmed via the masterData ability name-map against live logs.
MEDICATED_STATUS_ID: int = 1000049

# Sentinel "ability id" for the SIM's pot marker. Real FFLogs ability ids are
# positive, so this negative value can never collide with a player cast or appear
# in `POTENCIES` — the scorers' `base <= 0` guard skips it for direct damage, while
# `tincture_windows_from_timeline` reads it to derive the in-sim tincture window
# (the same way PLD derives Fight or Flight windows from FoF casts). The marker is a
# scoring/optimizer signal only — filtered out of the rendered cast lane + the
# missed-cast diff in the sidecar.
TINCTURE_ACTION_ID: int = -1000049

# Level-100 damage-formula constants (the 2780-divisor family). The main-stat
# baseline MAIN_LV is what `f(MAIN)` subtracts; the coefficient is the per-role
# main-attribute slope (non-tank ~237, tank ~190). The tincture *ratio* is robust
# to the coefficient (~0.06pp between 190 and 237); it's dominated by base + delta.
MAIN_STAT_LV: int = 440
DEFAULT_ROLE_COEFF: int = 237

# Current-tier tincture +main-stat (the flat cap; it binds for every job at BiS,
# so it's shared — only `base` differs per job). Bump per tier.
TINCTURE_DELTA: int = 541

TINCTURE_DURATION_S: float = 30.0
TINCTURE_COOLDOWN_S: float = 270.0   # 4:30


def tincture_multiplier(base_main: int,
                        delta: int = TINCTURE_DELTA,
                        coeff: int = DEFAULT_ROLE_COEFF,
                        main_lv: int = MAIN_STAT_LV) -> float:
    """The tincture damage multiplier `M = f(base+delta)/f(base)` for a job whose
    effective main stat is `base`. Exact integer-floor `f` so it matches the game.

    MCH this tier (base 6838 incl. party bonus, delta 541, coeff 237): M ~=
    1.0821 (+8.21%). Lower `base` -> higher M (less-geared players gain more)."""
    def f(main: int) -> int:
        return (coeff * (main - main_lv)) // main_lv + 100
    return f(base_main + delta) / f(base_main)


@dataclass(frozen=True)
class TinctureSpec:
    """A job's resolved tincture: the (per-job-constant) multiplier plus the
    fixed 30s / 4:30 timing. `opener_offset_s` is where the idealized opener pot
    is anchored (≈0 — popped during the countdown)."""
    multiplier: float
    duration_s: float = TINCTURE_DURATION_S
    cooldown_s: float = TINCTURE_COOLDOWN_S
    opener_offset_s: float = 0.0


def spec_for_job(main_stat: int | None, role_coeff: int = DEFAULT_ROLE_COEFF,
                 *, delta: int = TINCTURE_DELTA,
                 opener_offset_s: float = 0.0) -> TinctureSpec | None:
    """Build the `TinctureSpec` for a job from its per-job base main stat, or
    `None` when the job declares no tincture (`tincture_main_stat is None`) — the
    caller then leaves its scoring path tincture-free / byte-identical."""
    if main_stat is None:
        return None
    return TinctureSpec(
        multiplier=tincture_multiplier(main_stat, delta=delta, coeff=role_coeff),
        opener_offset_s=opener_offset_s,
    )


# --- Observed windows (player's actual pots, from the Medicated aura) --------

def _pair_medicated_intervals(events: list[dict], fight_start_ms: int,
                              fight_end_ms: int) -> list[tuple[float, float]]:
    """Pair Medicated apply/refresh -> remove events into (start_s, end_s)
    relative to fight start. Unlike the generic `pair_aura_intervals`, an orphan
    `removebuff` (no in-window apply) is treated as a pot popped *pre-pull*:
    active from t=0. An interval left open at fight end auto-closes."""
    fight_end_s = (fight_end_ms - fight_start_ms) / 1000.0
    evs = sorted(events, key=lambda e: e["timestamp"])
    out: list[tuple[float, float]] = []
    open_start: float | None = None
    for ev in evs:
        typ = ev.get("type", "")
        t = (ev["timestamp"] - fight_start_ms) / 1000.0
        if typ in ("applybuff", "refreshbuff"):
            if open_start is None:
                open_start = t
        elif typ == "removebuff":
            start = open_start if open_start is not None else 0.0  # pre-pull pot
            if t > start:
                out.append((start, t))
            open_start = None
    if open_start is not None and fight_end_s > open_start:
        out.append((open_start, fight_end_s))
    return out


def fetch_observed_tincture_windows(client: Any, code: str,
                                    fight: dict[str, Any], player_id: int,
                                    multiplier: float) -> list[BuffWindow]:
    """The player's *actual* tincture windows for this pull. Reads the same
    player `Buffs` aura stream `fetch_observed_buff_windows` does (cache hit — no
    extra round-trip), keeps only Medicated, and pairs with pre-pull handling.
    Returns [] on any failure (tinctures are a bonus, never fatal)."""
    if multiplier <= 1.0:
        return []
    s, e = fight["startTime"], fight["endTime"]
    try:
        evs = client.get_aura_events(code, s, e, player_id, "Buffs")
    except Exception:
        return []
    med = [ev for ev in evs if ev.get("abilityGameID") == MEDICATED_STATUS_ID]
    return [BuffWindow(a, b, multiplier, "Tincture")
            for a, b in _pair_medicated_intervals(med, s, e)]


# --- Idealized placement (candidate starts + best-subset selection) ----------

def candidate_pot_starts(duration_s: float,
                         raid_buff_intervals: list[tuple[float, float, float]]
                         | None,
                         spec: TinctureSpec,
                         grid_s: float = 30.0) -> list[float]:
    """Candidate start times for one pot: the opener, every raid-buff window
    start (so a pot can be held to a burst), and a coarse grid (the only source
    in the buff-agnostic strict scenario, and a fallback between bursts)."""
    latest = max(spec.opener_offset_s, duration_s - 1.0)
    cands: set[float] = {round(spec.opener_offset_s, 1)}
    for st, _e, _m in (raid_buff_intervals or []):
        if spec.opener_offset_s <= st <= latest:
            cands.add(round(st, 1))
    t = spec.opener_offset_s
    while t <= latest:
        cands.add(round(t, 1))
        t += grid_s
    return sorted(c for c in cands if spec.opener_offset_s <= c <= latest)


def max_pots(duration_s: float, spec: TinctureSpec) -> int:
    """How many pots can fit: opener + one per cooldown that still starts in-fight."""
    if duration_s <= spec.opener_offset_s:
        return 0
    return int((duration_s - spec.opener_offset_s) // spec.cooldown_s) + 1


def select_best_starts(starts: list[float],
                       window_value: Callable[[float], float],
                       spec: TinctureSpec, n_pots: int) -> list[float]:
    """Pick up to `n_pots` start times, each >= `cooldown_s` after the previous,
    maximizing total `window_value(start)`. Exact DP (max-weight gap-constrained
    subset) over the candidate starts — cheap for the handful of candidates and
    <=3 pots a fight allows. `window_value` is the caller's estimate of the
    potency a 30s window at that start would buff (so the choice IS scored, per
    the sweep design); a non-positive-value pot is never added."""
    starts = sorted(set(round(s, 3) for s in starts))
    n = len(starts)
    if n == 0 or n_pots <= 0:
        return []
    vals = [window_value(s) for s in starts]
    # First index whose start is >= starts[i] + cooldown (next legal pot).
    nxt = [bisect.bisect_left(starts, s + spec.cooldown_s) for s in starts]

    @lru_cache(maxsize=None)
    def dp(i: int, k: int) -> tuple[float, tuple[float, ...]]:
        if i >= n or k == 0:
            return (0.0, ())
        skip = dp(i + 1, k)
        if vals[i] <= 1e-9:
            return skip
        tv, tchosen = dp(nxt[i], k - 1)
        take = (vals[i] + tv, (starts[i], *tchosen))
        return take if take[0] > skip[0] + 1e-9 else skip

    chosen = dp(0, n_pots)[1]
    dp.cache_clear()
    return list(chosen)


def make_tincture_windows(starts: list[float],
                          spec: TinctureSpec) -> list[BuffWindow]:
    """BuffWindows for a chosen set of pot start times."""
    return [BuffWindow(s, s + spec.duration_s, spec.multiplier, "Tincture")
            for s in starts]


# Below this, a GCD is "fast" (MCH Overheated 1.5s, RPR Enshroud 1.5s, Viper
# Reawaken combo ~1.7s): it has a single weave slot, and that slot is always taken
# by the burst oGCD that drives those windows (Gauss/Ricochet, Lemure's Slice, the
# Reawaken Legacy) — so it's never an available pot slot, even when a sim doesn't
# model that oGCD explicitly. A standard GCD fits two weaves (double-weaving is safe
# between GCDs); the pot takes the second.
_FAST_GCD_S = 2.05


def _free_weave_slot_starts(rot: list[tuple[float, int]],
                            fight_duration_s: float) -> list[float]:
    """The times a tincture can actually be used. A pot is an oGCD WEAVE, so it has
    to land in a GCD gap that still has a free weave slot — wedging it into an
    already-full double-weave (or a single-weave fast GCD) would clip the next GCD.
    Returns the start of every standard-GCD gap with a weave slot to spare, plus the
    pre-pull opener (t=0, popped during the countdown — never a weave). Fast-GCD gaps
    are excluded wholesale (their lone weave belongs to the burst oGCD). This is the
    candidate set for the pot search, so the chosen optimum is always a usable slot."""
    from . import ability_metadata

    def is_ogcd(aid: int) -> bool:
        m = ability_metadata.get_metadata(aid)
        return m is not None and m.is_ogcd

    casts = sorted(rot)
    gcd_times = [t for t, a in casts if not is_ogcd(a)]
    ogcd_times = sorted(t for t, a in casts if is_ogcd(a))
    starts = {0.0}
    for i, g in enumerate(gcd_times):
        nxt = gcd_times[i + 1] if i + 1 < len(gcd_times) else fight_duration_s
        if (nxt - g) < _FAST_GCD_S:
            continue   # fast GCD — its single weave is the burst oGCD's, not a pot's
        n_woven = sum(1 for o in ogcd_times if g < o < nxt)
        if n_woven < 2:
            starts.add(round(g, 2))
    return sorted(starts)


def place_optimal_pots(
        timeline: list[tuple[float, int]],
        fight_duration_s: float,
        spec: "TinctureSpec | None",
        buff_intervals: list[tuple[float, float, float]] | None,
        window_score: Callable[[list[tuple[float, int]]], float],
        *, grid_s: float = 5.0) -> list[tuple[float, int]]:
    """Replace a SIM timeline's tincture markers with the COVERAGE-OPTIMAL set: the
    `max_pots` cooldown-spaced start times that maximize the potency the 30s window
    multiplies, via the gap-constrained DP (`select_best_starts`). The shared,
    job-agnostic pot placement — used by every potting job through `build_scoring`
    (it supersedes the sim's greedy in-rotation `should_pot` markers).

    The rotation is pot-independent (the tincture is a pure damage-multiplier
    overlay), so this is an EXACT post-hoc choice, not a re-sim: it lands each pot on
    the highest-value bursts, and — because the search is provably optimal over the
    candidates — it naturally DELAYS a gauge job's first pot to a 2-min double-burst
    (Viper double-Reawaken, RPR double-Enshroud) when that wins, and keeps the full
    pot count otherwise. The candidates are the FREE WEAVE SLOTS
    (`_free_weave_slot_starts`): the pot is an oGCD, so it can only be used where a
    weave slot is open, never wedged into a full double-weave. `window_score`
    (bound by the caller to the job's scorer + the scenario's coverage/buffs) scores
    a candidate's marginal as the timeline with one extra pot marker minus the base,
    so raid-buff alignment falls out for free. No-op when the job doesn't pot
    (`spec` None / multiplier <= 1)."""
    if spec is None or spec.multiplier <= 1.0:
        return timeline
    rot = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    base = window_score(rot)

    candidates = _free_weave_slot_starts(rot, fight_duration_s)
    if len(candidates) <= 1:   # degenerate timeline (no usable in-fight slot)
        candidates = candidate_pot_starts(
            fight_duration_s, buff_intervals, spec, grid_s=grid_s)

    # The exact marginal of one pot is `window_score(rot + [pot@s]) - base` — an O(N)
    # rescan per candidate, so O(N^2) over the whole candidate set. But a tincture is a
    # flat multiplier over [s, s+duration], so for a per-cast-ADDITIVE scorer the
    # marginal depends only on the casts INSIDE that window: scoring that ~30s slice (a
    # dozen casts) is the same number at ~O(N) total. We verify the equivalence on a few
    # spread candidates; a scorer whose cast value couples beyond the window (MCH's
    # Queen payload, a DoT valued by time-to-next) fails the check and falls back to the
    # exact full-rotation marginal — so the chosen placement is identical either way.
    rot_sorted = sorted(rot)
    cast_ts = [t for t, _a in rot_sorted]
    dur_s = spec.duration_s

    def full_marginal(s: float) -> float:
        return window_score(rot + [(s, TINCTURE_ACTION_ID)]) - base

    def window_marginal(s: float) -> float:
        # Superset slice (1s margin each side): casts outside [s, s+dur] get multiplier
        # 1.0 in both terms and cancel, so the extras don't change the result — which
        # makes this independent of the scorer's window-boundary convention.
        lo = bisect.bisect_left(cast_ts, s - 1.0)
        hi = bisect.bisect_right(cast_ts, s + dur_s + 1.0)
        win = rot_sorted[lo:hi]
        return window_score(win + [(s, TINCTURE_ACTION_ID)]) - window_score(win)

    window_value = full_marginal
    if candidates:
        probes = {candidates[i] for i in
                  (0, len(candidates) // 3, 2 * len(candidates) // 3, len(candidates) - 1)}
        if all(abs(window_marginal(s) - full_marginal(s)) <= 1e-6 for s in probes):
            window_value = window_marginal   # window-local scorer -> fast exact path

    starts = select_best_starts(
        candidates, window_value, spec, max_pots(fight_duration_s, spec))
    return sorted(rot + [(s, TINCTURE_ACTION_ID) for s in starts],
                  key=lambda ta: ta[0])


def tincture_windows_from_timeline(
        timeline: list[tuple[float, int]],
        spec: TinctureSpec | None) -> list[BuffWindow]:
    """The tincture windows the SIM placed, read from the `TINCTURE_ACTION_ID`
    markers in its timeline: one window `(t, t+duration, multiplier)` per marker.

    This is the in-sim analog of reading the player's observed Medicated windows —
    the direct counterpart of how PLD derives Fight or Flight windows from FoF casts
    (`paladin/scoring.py::_fof_windows`). It replaces the old post-hoc density sweep:
    the optimizer *places* the pot inside the sim, and scoring derives its window
    from where it landed. Empty when the job doesn't pot (`spec is None`) or the
    timeline carries no marker (the player's real rotation)."""
    if spec is None or spec.multiplier <= 1.0:
        return []
    return [BuffWindow(t, t + spec.duration_s, spec.multiplier, "Tincture")
            for t, aid in timeline if aid == TINCTURE_ACTION_ID]


def merge_tincture_markers(
        timeline: list[tuple[float, int]],
        buff_intervals: list[tuple[float, float, float]] | None,
        spec: TinctureSpec | None) -> list[tuple[float, float, float]] | None:
    """Fold the sim's in-timeline pot markers into the `(start, end, multiplier)`
    overlay a scorer applies per cast, so the tincture multiplier scales every
    covered cast (incl. snapshot payloads / DoTs / pet summons) AT CAST TIME, right
    where the optimizer placed the pot — no overlay sweep, no >100% guard.

    Idempotent for a timeline with no marker (the player's fixed delivered rotation):
    returns `buff_intervals` unchanged, so the delivered path is untouched. Called at
    the top of every job's `score_delivered_potency`, so both the optimizer's
    `score_fn` (which drives pot placement) and the cached ceiling see the pot."""
    tw = tincture_windows_from_timeline(timeline, spec)
    if not tw:
        return buff_intervals
    raid = [BuffWindow(s, e, m, "raid") for s, e, m in (buff_intervals or [])]
    return multiplier_intervals(raid + tw) or None


# --- Pot-timing loss (the Potential-Improvements card) ----------------------

# Truthy identity overlay (multiplier 1.0 everywhere) so every score_with() call
# takes the SAME scorer branch — matters for MCH, whose delivered scorer credits
# Queen per-summon under a (truthy) overlay vs a deterministic total under None.
# Scoring base/observed/optimal all under an overlay keeps the delta clean.
_IDENTITY_OVERLAY: list[tuple[float, float, float]] = [(-1e6, 1e6, 1.0)]


def tincture_timing_loss(spec: TinctureSpec, fight_duration_s: float,
                         score_with: Callable[
                             [list[tuple[float, float, float]]], float],
                         observed_windows: list[BuffWindow],
                         ) -> tuple[float, int, float]:
    """Strict-basis pot-*timing* loss on the player's OWN rotation: how much more
    potency the best pot placement would add over the player's actual pots,
    holding the rotation fixed (so it isolates timing/usage from rotation
    quality — the part the player can act on). `score_with(intervals)` scores the
    player's in-fight casts under a multiplier-interval overlay. Returns
    (lost_potency, optimal_pot_count, located_time_s)."""
    base = score_with(_IDENTITY_OVERLAY)
    obs_iv = multiplier_intervals(observed_windows) or _IDENTITY_OVERLAY
    observed_value = score_with(obs_iv) - base

    cands = candidate_pot_starts(fight_duration_s, None, spec)

    def window_value(s: float) -> float:
        return score_with(multiplier_intervals(
            make_tincture_windows([s], spec))) - base

    starts = select_best_starts(cands, window_value, spec,
                                max_pots(fight_duration_s, spec))
    opt_iv = multiplier_intervals(make_tincture_windows(starts, spec)) \
        or _IDENTITY_OVERLAY
    optimal_value = score_with(opt_iv) - base

    loss = max(0.0, optimal_value - observed_value)
    # Locate at the first optimal pot the player didn't have one near (a skipped
    # pot); else the first optimal slot (a misalignment).
    obs_starts = [w.start_s for w in observed_windows]
    missed = [s for s in starts
              if all(abs(s - o) >= spec.duration_s for o in obs_starts)]
    located = missed[0] if missed else (starts[0] if starts else 0.0)
    return loss, len(starts), located
