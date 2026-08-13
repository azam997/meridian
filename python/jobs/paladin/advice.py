"""Paladin deep-advice pack (`Job.advice_probes`).

RootCauses only, no ProbeItems: PLD has no window-shift-style card
enrichment that meets the measurement bar (MCH's wildfire probe computes a
concrete better placement; nothing on PLD is that shape). The pack's value
is three deterministic ledger walks over the delivered cast stream:

* `RootCause`s — candidates for the cascade re-attribution:
  - cooldown drift that cost an end-of-fight use of a recast-gated damage
    button (Fight or Flight / Imperator / Circle of Scorn / Expiacion —
    Intervene is charge-pooled, so consecutive-gap drift there is legitimate
    banking and stays silent),
  - Royal Authority overwriting unspent procs (pending Atonement-chain steps
    and the Divine Might free Holy Spirit) — PLD's analog of a gauge
    overcap: the spender came later than the proc allowed,
  - the Confiteor chain cut at the kill (the model's Requiescat-stacks
    story: `magic_combo` still mid-chain at fight end because the last
    Imperator landed too late for the Blades to fit).
  Their `measured_p` stays 0 — the orchestrator prices each from its
  cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (combo_step, magic_combo)
never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, RootCause,
)
from jobs.paladin import data as pd

# Single-charge recast-gated damage cooldowns the drift ledger watches.
# INTERVENE is deliberately absent: its 2-charge pool makes gap-over-recast
# between consecutive uses legitimate banking, not drift.
_DRIFT_COOLDOWNS: tuple[int, ...] = (
    pd.FIGHT_OR_FLIGHT, pd.IMPERATOR, pd.CIRCLE_OF_SCORN, pd.EXPIACION,
)
# Pre-rework button ids still consume the same cooldown slot (older logs):
# Requiescat -> Imperator, Spirits Within -> Expiacion.
_CD_CONSUMERS: dict[int, frozenset[int]] = {
    pd.FIGHT_OR_FLIGHT: frozenset({pd.FIGHT_OR_FLIGHT}),
    pd.IMPERATOR: frozenset({pd.IMPERATOR, pd.REQUIESCAT}),
    pd.CIRCLE_OF_SCORN: frozenset({pd.CIRCLE_OF_SCORN}),
    pd.EXPIACION: frozenset({pd.EXPIACION, pd.SPIRITS_WITHIN}),
}

# The magical combo after Imperator, in cast order (4 GCDs + the oGCD
# finisher). Potencies from data.POTENCIES price a cut chain's remainder.
_CHAIN: tuple[int, ...] = (
    pd.CONFITEOR, pd.BLADE_OF_FAITH, pd.BLADE_OF_TRUTH, pd.BLADE_OF_VALOR,
    pd.BLADE_OF_HONOR,
)

_OVERWRITE_MIN_P = 1000.0     # face potency of overwritten procs before a card
_CHAIN_CUT_MIN_P = 1000.0     # at least one full chain hit dead at the kill
# Real-game timer shared by everything these ledgers track (DT 7.x): the
# melee combo window, Atonement Ready / Supplication Ready / Sepulchre Ready,
# Divine Might, Requiescat stacks and Blade of Honor Ready all run 30s. The
# sim never needs it (it never idles that long), but the delivered stream
# does: a proc older than this expired on its own (downtime, deaths the
# window list missed), so a later Royal Authority overwrites nothing, and a
# combo continuation past the window grants nothing. Without it, a boss jump
# right after a Royal Authority reads as a phantom 2000p overwrite on clean
# recovery play.
_PROC_LIFETIME_S = 30.0
# The magical chain is 4 GCDs plus a weaved finisher, so it lands inside
# ~11s of the Imperator that opened it. A last Imperator further than this
# from the kill was NOT cut by the kill: an abandoned mid-fight chain is
# already a missed-cast card, and the "cut at the end" copy would point at
# the wrong moment.
_CHAIN_FIT_S = 15.0


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# so holding procs for a Fight or Flight window elsewhere stays legitimate.
# Run new dialogue copy by the user before shipping it.
TEXT: dict[str, dict[str, str]] = {
    "cd_drift": {
        "summary": ("{name} sat idle {drift:.0f}s in total, {deficit} "
                    "use{plural} lost"),
        "prescription": ("Drifting {name} is costly. Biggest drift at "
                         "{when}, {worst:.1f}s late; the drift adds up until "
                         "a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of idle time",
    },
    "proc_overwrite": {
        "summary": "Royal Authority overwrote {n} unspent proc{plural}",
        "prescription": ("Spend the Atonement steps and the free Holy "
                         "Spirit before the next Royal Authority. First "
                         "overwrite at {when}."),
        "wasted_v": "{n} proc{plural}",
        "wasted_note": "{parts} written over by a fresh Royal Authority",
        "part_chain": "{n} chain step{plural}",
        "part_dm": "{n} free Holy Spirit{plural}",
        "worst_v": "~{value:.0f}p",
        "worst_note": "the single biggest overwrite, at {when}",
    },
    "chain_cut": {
        "summary": "Confiteor chain cut at the end, ~{value:.0f}p uncast",
        "prescription": ("Use Imperator earlier in the final minute so "
                         "Confiteor and the Blades all land before the "
                         "kill. The last Imperator at {when} left {n} "
                         "hit{plural} uncast."),
        "chain_v": "{done} of {total} hits",
        "chain_note": "landed after the last Imperator at {when}",
        "uncast_v": "~{value:.0f}p",
        "uncast_note": "the remaining hits at their in-chain potency",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Rows read `LABEL  {delta} over ideal  note`.
# All PLD fields are proc flags (0/1), so min_delta is 1.0; running lean
# (under the ideal line) is never a mistake by itself, so under_note stays
# None everywhere.
GAUGE_TEXT: dict[str, GaugeText] = {
    "divine_might": GaugeText(
        label="Divine Might", short="DM",
        over_note="a free Holy Spirit sat unspent",
        under_note=None,
        min_delta=1.0),
    "atonement_ready": GaugeText(
        label="Atonement", short="ATN",
        over_note="an Atonement sat ready unused",
        under_note=None,
        min_delta=1.0),
    "supplication_ready": GaugeText(
        label="Supplication", short="SUP",
        over_note="a Supplication sat ready unused",
        under_note=None,
        min_delta=1.0),
    "sepulchre_ready": GaugeText(
        label="Sepulchre", short="SEP",
        over_note="a Sepulchre sat ready unused",
        under_note=None,
        min_delta=1.0),
    "goring_ready": GaugeText(
        label="Goring Blade", short="GOR",
        over_note="a Goring Blade sat ready unused",
        under_note=None,
        min_delta=1.0),
    "blade_of_honor_ready": GaugeText(
        label="Blade of Honor", short="BOH",
        over_note="the finisher sat ready unused",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _death_windows(ctx: AdviceContext) -> list[tuple[float, float]]:
    return sorted((float(s), float(e)) for s, e in (ctx.death_windows or []))


def _overlaps_death(a: float, b: float,
                    deaths: list[tuple[float, float]]) -> bool:
    return any(s < b and e > a for s, e in deaths)


def _covered_s(a: float, b: float,
               windows: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] that sit inside `windows` (downtime). Windows may
    overlap, so this clamps per window and sums the merged spans."""
    spans = sorted((max(a, float(s)), min(b, float(e)))
                   for s, e in windows if float(s) < b and float(e) > a)
    total = 0.0
    hi = a
    for s, e in spans:
        if e <= hi:
            continue
        total += e - max(s, hi)
        hi = e
    return total


def _sorted_casts(ctx: AdviceContext) -> list[tuple[float, int]]:
    """In-fight casts in stable time order (same-timestamp order preserved,
    the replay discipline; prepull t<0 ignored like MCH does)."""
    return sorted(((float(t), int(a)) for t, a in ctx.norm_casts
                   if float(t) >= 0.0), key=lambda c: c[0])


def _cooldown_drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A recast-gated damage button the sim fit more of than the player cast,
    with the drift ledger that shows where the use was lost. Gaps that overlap
    a death window are not counted (deaths are priced by their own card), and
    downtime inside a gap is discounted (the boss was gone, so that stretch is
    not idle time the player chose)."""
    deaths = _death_windows(ctx)
    downtime = sorted((float(s), float(e))
                      for s, e in (ctx.downtime_windows or []))
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for cd in _DRIFT_COOLDOWNS:
        recast, _ch = pd.COOLDOWNS[cd]
        consume_ids = _CD_CONSUMERS[cd]
        times = sorted(t for t, a in ctx.norm_casts
                       if a in consume_ids and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(cd, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            if _overlaps_death(a, b, deaths):
                continue
            over = (b - a) - recast - _covered_s(a, b, downtime)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(cd)
        value = deficit * pd.COOLDOWN_VALUE_P.get(cd, 0)
        t = TEXT["cd_drift"]
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=cd, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=pd.COOLDOWN_VALUE_P.get(cd, 0)),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal_counts.get(cd, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    out.sort(key=lambda r: (-r[0], r[1].ability_id))
    return [c for _v, c in out]


def _proc_overwrite_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the proc machine over the delivered stream: a combo'd
    Royal Authority re-grants the Atonement chain and Divine Might, so any
    step still pending at that moment is written over — the spender came
    later than the proc allowed. Mirrors the sim's own hold rule (the beam
    never advances into a Royal Authority while a chain step is pending).
    Procs drop on death, so the ledger resets at each death window and no
    phantom overwrite is charged there."""
    deaths = _death_windows(ctx)
    combo = 0
    at = sup = sep = dm = False
    combo_t = chain_t = dm_t = 0.0     # when each timer last started
    di = 0
    events: list[tuple[float, float, int, int]] = []  # (t, face_p, steps, dm)
    for t, a in _sorted_casts(ctx):
        while di < len(deaths) and t >= deaths[di][0]:
            combo, at, sup, sep, dm = 0, False, False, False, False
            di += 1
        # Everything here runs on a 30s timer, so a long gap (a boss jump, a
        # phase change) ends it on its own: an expired proc is overwritten by
        # nothing, and a combo step past the window grants nothing.
        if combo and t - combo_t > _PROC_LIFETIME_S:
            combo = 0
        if (at or sup or sep) and t - chain_t > _PROC_LIFETIME_S:
            at = sup = sep = False
        if dm and t - dm_t > _PROC_LIFETIME_S:
            dm = False
        if a == pd.FAST_BLADE:
            combo, combo_t = 1, t
        elif a == pd.RIOT_BLADE:
            combo, combo_t = (2 if combo == 1 else 0), t
        elif a == pd.ROYAL_AUTHORITY:
            if combo == 2:
                # Combo'd Royal: the fresh grant replaces whatever is pending.
                pend = 0.0
                steps = 0
                if at:
                    pend += (pd.POTENCIES[pd.ATONEMENT]
                             + pd.POTENCIES[pd.SUPPLICATION]
                             + pd.POTENCIES[pd.SEPULCHRE])
                    steps = 3
                elif sup:
                    pend += (pd.POTENCIES[pd.SUPPLICATION]
                             + pd.POTENCIES[pd.SEPULCHRE])
                    steps = 2
                elif sep:
                    pend += pd.POTENCIES[pd.SEPULCHRE]
                    steps = 1
                dm_lost = 1 if dm else 0
                if dm:
                    pend += pd.POTENCIES[pd.HOLY_SPIRIT]
                if pend > 0:
                    events.append((t, pend, steps, dm_lost))
                at, sup, sep, dm = True, False, False, True
                chain_t = dm_t = t
            combo = 0
        elif a == pd.ATONEMENT:
            at, sup = False, True
            chain_t = t
            combo = 0
        elif a == pd.SUPPLICATION:
            sup, sep = False, True
            chain_t = t
            combo = 0
        elif a == pd.SEPULCHRE:
            sep = False
            combo = 0
        elif a in (pd.HOLY_SPIRIT, pd.HOLY_CIRCLE):
            dm = False
        elif a in (pd.GORING_BLADE, pd.SHIELD_LOB,
                   pd.TOTAL_ECLIPSE, pd.PROMINENCE):
            # Weaponskills outside the Royal chain break the physical combo
            # (the in-game rule), so the next Royal Authority is uncombo'd and
            # grants nothing. Without this, a disconnect Shield Lob or an AoE
            # combo between Riot Blade and Royal Authority reads as a phantom
            # grant, and clean recovery play gets a false overwrite card.
            # (Prominence's own combo'd Divine Might grant is deliberately NOT
            # modeled: missing it only under-counts, never blames clean play.)
            combo = 0
    total = sum(p for _t, p, _s, _d in events)
    if not events or total < _OVERWRITE_MIN_P:
        return None
    first_t = events[0][0]
    worst_t, worst_p, _ws, _wd = max(events, key=lambda e: (e[1], -e[0]))
    n_steps = sum(s for _t, _p, s, _d in events)
    n_dm = sum(d for _t, _p, _s, d in events)
    n = n_steps + n_dm
    chain_p = total - n_dm * pd.POTENCIES[pd.HOLY_SPIRIT]
    t = TEXT["proc_overwrite"]
    parts: list[str] = []
    if n_steps:
        parts.append(t["part_chain"].format(
            n=n_steps, plural="s" if n_steps != 1 else ""))
    if n_dm:
        parts.append(t["part_dm"].format(
            n=n_dm, plural="s" if n_dm != 1 else ""))
    res_pairs = sorted(
        [(chain_p if n_steps else 0.0, "atonement_ready"),
         (float(n_dm * pd.POTENCIES[pd.HOLY_SPIRIT]), "divine_might")],
        key=lambda x: -x[0])
    resources = [GAUGE_TEXT[k] for p, k in res_pairs if p > 0]
    return RootCause(
        kind="cascade_burst", ability_id=pd.ROYAL_AUTHORITY,
        ability_name=_name(pd.ROYAL_AUTHORITY),
        time_sec=round(first_t, 1), measured_p=0.0,
        summary=t["summary"].format(n=n, plural="s" if n != 1 else ""),
        prescription=t["prescription"].format(when=_mmss(first_t)),
        evidence=[
            EvidenceRow(
                k="Wasted",
                v=t["wasted_v"].format(n=n, plural="s" if n != 1 else ""),
                note=t["wasted_note"].format(parts=" and ".join(parts))),
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(value=worst_p),
                note=t["worst_note"].format(when=_mmss(worst_t))),
        ],
        resources=resources)


def _chain_cut_cause(ctx: AdviceContext) -> RootCause | None:
    """The Confiteor chain dead at the kill: `magic_combo` (or a pending
    Blade of Honor) still open when the fight ends, because the last
    Imperator landed too late for the Blades to fit. Silent whenever the
    idealized timeline also ends mid-chain (then the cut is fight geometry,
    not a slip), a death sits after the last Imperator (the death card owns
    that story), or the kill came long enough after the last Imperator that
    the chain had room (a dropped chain is a missed-cast card, and this
    card's copy would point at the wrong moment). Requiescat stacks drop on
    death, so the walk resets at death windows."""
    deaths = _death_windows(ctx)

    def walk(casts: list[tuple[float, int]],
             reset_on_death: bool) -> tuple[float, int, float | None]:
        """(remaining face potency, hits done, last Imperator t)."""
        magic = 0
        boh = False
        last_open: float | None = None
        di = 0
        for t, a in casts:
            if reset_on_death:
                while di < len(deaths) and t >= deaths[di][0]:
                    magic, boh = 0, False
                    di += 1
            if a in (pd.IMPERATOR, pd.REQUIESCAT):
                # Mirrors apply_cast: opens the chain, leaves a pending
                # Blade of Honor untouched.
                magic = 1
                last_open = t
            elif a == pd.CONFITEOR:
                magic = 2
            elif a == pd.BLADE_OF_FAITH:
                magic = 3
            elif a == pd.BLADE_OF_TRUTH:
                magic = 4
            elif a == pd.BLADE_OF_VALOR:
                magic = 0
                boh = True
            elif a == pd.BLADE_OF_HONOR:
                boh = False
        if magic >= 1:
            remaining = _CHAIN[magic - 1:]
        elif boh:
            remaining = _CHAIN[-1:]
        else:
            remaining = ()
        rem_p = float(sum(pd.POTENCIES[x] for x in remaining))
        return rem_p, len(_CHAIN) - len(remaining), last_open

    player_p, done, last_imp = walk(_sorted_casts(ctx), True)
    if last_imp is None or player_p < _CHAIN_CUT_MIN_P:
        return None
    if float(ctx.fight_duration_s) - last_imp > _CHAIN_FIT_S:
        return None       # the chain had room and was dropped, not cut
    ideal_casts = sorted(((float(t), int(a)) for t, a in ctx.idealized
                          if float(t) >= 0.0), key=lambda c: c[0])
    ideal_p, _idone, _iopen = walk(ideal_casts, False)
    if ideal_p > 0:
        return None       # the sim's own last chain was cut by the fight end
    if any(s >= last_imp for s, _e in deaths):
        return None       # a death after the last Imperator cut the chain
    n_uncast = len(_CHAIN) - done
    t = TEXT["chain_cut"]
    return RootCause(
        kind="cascade_lost_use", ability_id=pd.IMPERATOR,
        ability_name=_name(pd.IMPERATOR),
        time_sec=round(last_imp, 1), measured_p=0.0,
        summary=t["summary"].format(value=player_p),
        prescription=t["prescription"].format(
            when=_mmss(last_imp), n=n_uncast,
            plural="s" if n_uncast != 1 else ""),
        evidence=[
            EvidenceRow(
                k="Chain",
                v=t["chain_v"].format(done=done, total=len(_CHAIN)),
                note=t["chain_note"].format(when=_mmss(last_imp))),
            EvidenceRow(
                k="Uncast",
                v=t["uncast_v"].format(value=player_p),
                note=t["uncast_note"]),
        ])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list, list[RootCause]]:
    """PLD probe set. Deterministic; RootCause order is the priority order
    the orchestrator's first-in-segment-wins matching consumes: lost cooldown
    uses (highest per-use value first), then the Royal Authority proc
    overwrite, then the kill-cut Confiteor chain. No ProbeItems."""
    causes: list[RootCause] = list(_cooldown_drift_causes(ctx))
    ow = _proc_overwrite_cause(ctx)
    if ow is not None:
        causes.append(ow)
    cut = _chain_cut_cause(ctx)
    if cut is not None:
        causes.append(cut)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
