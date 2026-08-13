"""Samurai deep-advice pack (`Job.advice_probes`).

The SAM probe set, following the registry pattern `sidecar/advice.py`'s
docstring promised (the MCH pack is the reference implementation). One half
only — SAM ships no `ProbeItem` card enrichments (it has no fixed-duration
catch window like Wildfire to re-place); everything here is a `RootCause`
candidate for the cascade re-attribution, each a deterministic ledger walk
over the delivered cast stream grounded in `jobs/samurai/data.py`:

* **Cooldown drift** — Ikishoten / Meikyo Shisui / Hissatsu: Senei (the only
  recast-gated SAM buttons, `sd.COOLDOWNS`) that the sim fit more uses of,
  with the gap-over-recast ledger showing where the use was lost.
* **Kenki overcap** — the gauge ledger (generators/spenders from data.py)
  overflowing the 100 cap marks a Hissatsu: Shinten held too long.
* **Sen pacing** — full Sen sets that sat waiting past the sim's next-swing
  cadence until a Midare Setsugekka (and its Kaeshi) fell off the fight.
* **Stranded at the kill** — a full Sen set or a spendable Kenki pile dead in
  the gauge at fight end.

Their `measured_p` stays 0 — the orchestrator prices each from its cascade
segment's unexplained loss. Death windows reset the Kenki/Sen ledgers (the
game wipes both gauges on death) and drift gaps overlapping a death stay
uncounted, so death-caused stretches are never blamed here (deaths carry
their own card). No-enemy-targetable time is discounted out of every gap a
target-needing button is measured against, so pressing the instant the boss
returns reads clean.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is
an allowlist: sim-state fields without an entry (sen_mask, combo_step, the
Kaeshi flags…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.samurai import data as sd

# Sen bits, local mirror of the simulator's bitmask (the values are arbitrary;
# only "three distinct types" matters to these ledgers).
_GETSU, _KA, _SETSU = 1, 2, 4
_ALL_SEN = _GETSU | _KA | _SETSU
# Sen granted per ender cast (ST + AoE enders), mirroring `apply_cast`.
_SEN_GAIN: dict[int, int] = {
    sd.GEKKO: _GETSU, sd.KASHA: _KA, sd.YUKIKAZE: _SETSU,
    sd.MANGETSU: _GETSU, sd.OKA: _KA,
}
# 3-Sen-equivalent conversions counted against the sim's line (the 2-Sen Goken
# pair included so an AoE window's conversions never read as a lost Midare).
_IAI_CONVERSIONS: frozenset[int] = frozenset({
    sd.MIDARE_SETSUGEKKA, sd.TENDO_SETSUGEKKA, sd.TENKA_GOKEN, sd.TENDO_GOKEN,
})
# Everything that clears the Sen ledger, mirroring the sim's `apply_cast`
# (Higanbana zeroes the mask there too — conservative for these ledgers).
_IAI_CLEARS: frozenset[int] = _IAI_CONVERSIONS | frozenset({sd.HIGANBANA})
# Hagakure dumps the whole Sen set into Kenki. The sim never casts it, so it
# clears the ledger SILENTLY (no wait recorded): the Sen went somewhere, and a
# deliberate pre-downtime dump must never read as a stranded set.
_SEN_DUMPS: frozenset[int] = frozenset({sd.HAGAKURE})

_KENKI_OVERCAP_MIN = 25      # one Hissatsu: Shinten of Kenki before a card is worth it
_STRANDED_KENKI_MIN = 50     # two Shinten dead in the gauge at the kill
_STRAND_TAIL_S = 5.0         # the pile must predate the kill by this to be spendable
_SEN_HOLD_GRACE_S = 5.0      # ~2 GCDs: a pending Kaeshi/Ogi legitimately goes first
_SEN_HOLD_MIN_S = 6.5        # ~3 GCDs of accumulated waiting before a card is worth it

# One lost Midare Setsugekka conversion = the Iaijutsu + its Kaeshi replay,
# both guaranteed crits (derived from the data tables, never hardcoded).
_MIDARE_PAIR_P: float = (
    (sd.POTENCIES[sd.MIDARE_SETSUGEKKA] + sd.POTENCIES[sd.KAESHI_SETSUGEKKA])
    * sd.GUARANTEED_CRIT_MULT)


# --- User-facing copy (data, not code) --------------------------------------

# Copy rules (user-approved 2026-08-10): plain punctuation only (no em
# dashes), no generic filler, and hold advice scoped to the measured stretch
# ("right away here") so holding for buffs elsewhere stays legitimate. Run
# new dialogue copy by the user before shipping it.
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
    "kenki_overcap": {
        "summary": "Kenki built past the 100 cap, {total:.0f} wasted",
        "prescription": ("Use excess Kenki right away here. First overcap "
                         "at {when}; a Hissatsu: Shinten in the next weave "
                         "slot keeps the gauge under the cap."),
        "worst_v": "{amount:.0f} Kenki",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} Kenki",
        "total_note": ("~{value:.0f}p of Hissatsu: Shinten value across "
                       "{count} overcap{plural}"),
    },
    "sen_pacing": {
        "summary": ("Full Sen sat waiting {hold:.0f}s in total, {deficit} "
                    "Iaijutsu lost"),
        "prescription": ("Spend the third Sen on Midare Setsugekka right away "
                         "here. Longest wait at {when}; the waits add up "
                         "until a Midare and its Kaeshi (~{value:.0f}p) no "
                         "longer fit."),
        "count_v": "{player} / {ideal}",
        "count_note": "Iaijutsu conversions vs the sim's line",
        "hold_v": "{hold:.0f}s",
        "hold_note": "waiting at full Sen beyond the sim's next swing",
    },
    "stranded_sen": {
        "summary": ("Three Sen left at the kill, a Midare Setsugekka never "
                    "cast"),
        "prescription": ("Spend the third Sen on Midare Setsugekka before "
                         "the kill (~{value:.0f}p with its Kaeshi "
                         "follow-up)."),
        "sen_v": "3 Sen unspent",
        "sen_note": "the set completed at {when} with no Iaijutsu after",
    },
    "stranded_kenki": {
        "summary": "{kenki:.0f} Kenki left at the kill",
        "prescription": ("Spend the last Kenki on Hissatsu: Shinten before "
                         "the kill (~{value:.0f}p)."),
        "kenki_v": "{kenki:.0f} unspent",
        "kenki_note": ("the gauge stayed at {floor}+ from {when} through "
                       "the kill"),
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Keys are exact public scalar fields of
# `jobs.samurai.simulator.SimState`. Rows read `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "kenki": GaugeText(
        label="Kenki", short="KEN",
        over_note="a Hissatsu spender was ready",
        under_note=None,     # running lean on Kenki is not a mistake by itself
        min_delta=20.0),
    "meditation": GaugeText(
        label="Meditation", short="MED",
        over_note="a Shoha sat unspent",
        under_note=None,
        # 3 = a WHOLE Shoha ahead of the ideal line (the gauge caps at 3), so
        # the note is only ever attached when one genuinely sat ready.
        min_delta=3.0),
    "meikyo_stacks": GaugeText(
        label="Meikyo stacks", short="MKY",
        over_note="Meikyo Shisui enders sat unused",
        under_note=None,
        min_delta=2.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _ordered(ctx: AdviceContext) -> list[tuple[float, int]]:
    """In-fight casts, STABLE time-only sort (same-timestamp order kept)."""
    return sorted(((float(t), int(a)) for t, a in ctx.norm_casts if t >= 0),
                  key=lambda c: c[0])


def _death_starts(ctx: AdviceContext) -> list[float]:
    return sorted(float(ds) for ds, _de in (ctx.death_windows or []))


def _overlaps_death(a: float, b: float, ctx: AdviceContext) -> bool:
    return any(float(ds) < b and float(de) > a
               for ds, de in (ctx.death_windows or []))


def _downtime_overlap(a: float, b: float, ctx: AdviceContext) -> float:
    """Seconds of [a, b] with no enemy targetable. A button that needs a target
    cannot be pressed there, so that time is discounted out of every gap these
    ledgers measure — pressing the instant the boss returns must read clean."""
    total = 0.0
    for ws, we in (ctx.downtime_windows or []):
        lo, hi = max(a, float(ws)), min(b, float(we))
        if hi > lo:
            total += hi - lo
    return total


def _ideal_counts(ctx: AdviceContext) -> dict[int, int]:
    out: dict[int, int] = {}
    for _t, a in ctx.idealized:
        out[a] = out.get(a, 0) + 1
    return out


# --- Ledger walks (shared by the gauge causes) -------------------------------

def _kenki_walk(ctx: AdviceContext):
    """Kenki ledger over the delivered stream, using data.py's generator /
    spender tables (KENKI_SPENDERS plus the sim-unmodeled Kyuten / Guren /
    Gyoten / Yaten costs — a missed debit would run the ledger hot on clean
    AoE or movement play). Tengentsu procs and Meditate ticks are invisible
    in the cast stream, so income runs LEAN — overcap and stranding are
    under-, never over-stated. Deaths reset the gauge, as the game does.
    Returns (overflows [(t, amount)], end_kenki, held_since, last_gen_t):
    `held_since` is the start of the unbroken stretch the gauge has sat at
    >= _STRANDED_KENKI_MIN through fight end (None if it dipped)."""
    kenki = 0
    overflows: list[tuple[float, float]] = []
    last_gen_t = 0.0
    held_since: float | None = None
    deaths = iter(_death_starts(ctx))
    next_death = next(deaths, None)
    for t, a in _ordered(ctx):
        while next_death is not None and next_death <= t:
            kenki = 0
            held_since = None
            next_death = next(deaths, None)
        gain = sd.KENKI_GENERATORS.get(a, 0)
        if gain:
            kenki += gain
            last_gen_t = t
            if kenki > sd.KENKI_CAP:
                overflows.append((t, float(kenki - sd.KENKI_CAP)))
                kenki = sd.KENKI_CAP
        cost = (sd.KENKI_SPENDERS.get(a, 0)
                or sd.KENKI_SPENDERS_UNMODELED.get(a, 0))
        if cost:
            kenki = max(0, kenki - cost)
        if kenki >= _STRANDED_KENKI_MIN:
            if held_since is None:
                held_since = t
        else:
            held_since = None
    # A death after the last cast still wipes the gauge before the kill.
    while next_death is not None and next_death <= ctx.fight_duration_s:
        kenki = 0
        held_since = None
        next_death = next(deaths, None)
    return overflows, kenki, held_since, last_gen_t


def _sen_walk(ctx: AdviceContext):
    """Sen ledger over the delivered stream (enders grant by TYPE, Iaijutsu
    clears — mirroring the sim's `apply_cast`). Deaths and a Hagakure dump
    reset the mask and silently close any open hold; the wait an Iaijutsu does
    close is net of no-enemy-targetable time (a set dumped the instant the boss
    returns reads clean). Returns (holds, player_iai, full_since): `holds` is
    [(excess_s, full_start)] for holds CLOSED by an Iaijutsu whose wait
    exceeded the grace; `player_iai` counts 3-Sen-equivalent conversions;
    `full_since` is the still-open full-set completion time at fight end."""
    mask = 0
    full_since: float | None = None
    holds: list[tuple[float, float]] = []
    player_iai = 0
    deaths = iter(_death_starts(ctx))
    next_death = next(deaths, None)
    for t, a in _ordered(ctx):
        while next_death is not None and next_death <= t:
            mask = 0
            full_since = None
            next_death = next(deaths, None)
        if a in _SEN_DUMPS:          # Hagakure: the set went to Kenki, not waste
            mask = 0
            full_since = None
            continue
        if a in _IAI_CONVERSIONS:
            player_iai += 1
        if a in _IAI_CLEARS:
            if full_since is not None:
                excess = ((t - full_since) - _SEN_HOLD_GRACE_S
                          - _downtime_overlap(full_since, t, ctx))
                if excess > 0:
                    holds.append((excess, full_since))
                full_since = None
            mask = 0
            continue
        g = _SEN_GAIN.get(a)
        if g:
            mask |= g
            if mask == _ALL_SEN and full_since is None:
                full_since = t
    while next_death is not None and next_death <= ctx.fight_duration_s:
        mask = 0
        full_since = None
        next_death = next(deaths, None)
    return holds, player_iai, full_since


# --- RootCause producers -----------------------------------------------------
# Each returns (ordering value, cause) — the value (per-use potency worth) is
# consumed by `advice_probes`' descending sort, which is the orchestrator's
# segment-match priority order.

def _cd_drift_causes(ctx: AdviceContext) -> list[tuple[float, RootCause]]:
    """A recast-gated button (Ikishoten / Meikyo Shisui / Hissatsu: Senei —
    the whole of sd.COOLDOWNS) the sim fit more uses of than the player cast,
    with the gap-over-recast ledger showing where the use was lost. Meikyo's
    2-charge pool uses the same consecutive-gap proxy the MCH Drill ledger
    does; the deficit gate (the sim genuinely fit more) is the guard. Guren
    SHARES Senei's 60s recast, so a Guren cast counts as a Senei use (the
    MCH Drill/Bioblaster quirk — otherwise AoE fights read as fake Senei
    drift). Gaps overlapping a death window stay uncounted, and for the
    buttons that need a live target (the damage oGCDs, by POTENCIES) the
    no-enemy-targetable time inside a gap is discounted — Ikishoten and Meikyo
    Shisui are self-buffs a player presses through downtime, so their gaps
    count in full."""
    ideal_counts = _ideal_counts(ctx)
    out: list[tuple[float, RootCause]] = []
    for aid in sorted(sd.COOLDOWNS):
        recast, _ch = sd.COOLDOWNS[aid]
        needs_target = sd.POTENCIES.get(aid, 0) > 0
        consume_ids = ({aid, sd.HISSATSU_GUREN}
                       if aid == sd.HISSATSU_SENEI else {aid})
        times = sorted(t for t, a in _ordered(ctx) if a in consume_ids)
        player_n = len(times)
        deficit = ideal_counts.get(aid, 0) - player_n
        if deficit < 1 or player_n < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a_t, b_t in zip(times, times[1:]):
            if _overlaps_death(a_t, b_t, ctx):
                continue
            over = (b_t - a_t) - recast
            if needs_target:
                over -= _downtime_overlap(a_t, b_t, ctx)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a_t)
        if drift_total < recast * 0.5:
            continue
        name = _name(aid)
        per_use = sd.COOLDOWN_VALUE_P.get(aid, 0)
        t = TEXT["cd_drift"]
        out.append((float(deficit * per_use), RootCause(
            kind="cascade_lost_use", ability_id=aid, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t["prescription"].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=per_use),
            evidence=[
                EvidenceRow(
                    k=name,
                    v=t["count_v"].format(player=player_n,
                                          ideal=ideal_counts.get(aid, 0)),
                    note=t["count_note"]),
                EvidenceRow(
                    k="Idle",
                    v=t["idle_v"].format(drift=drift_total),
                    note=t["idle_note"].format(
                        recasts=drift_total / recast)),
            ])))
    return out


def _kenki_overcap_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Ledger walk of the Kenki gauge over the delivered stream: overflow
    above the 100 cap marks a Hissatsu: Shinten held too long. The ledger
    runs lean (no Tengentsu/Meditate income), so it never over-states."""
    overflows, _end, _held, _last = _kenki_walk(ctx)
    total = sum(v for _t, v in overflows)
    if total < _KENKI_OVERCAP_MIN or not overflows:
        return None
    first = next((t for t, v in overflows if v >= 5), overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["kenki_overcap"]
    value = total * sd.KENKI_VALUE_P_PER_UNIT
    return (float(value), RootCause(
        kind="cascade_burst", ability_id=sd.HISSATSU_SHINTEN,
        ability_name=_name(sd.HISSATSU_SHINTEN),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(total=total),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(amount=worst_v),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(total=total),
                note=t["total_note"].format(
                    value=value,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["kenki"]]))


def _sen_pacing_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Full Sen sets that sat waiting past the sim's next-swing cadence until
    an Iaijutsu conversion fell off the fight. Gated on BOTH a real conversion
    deficit vs the sim's line and enough accumulated waiting to matter, so a
    clean stream (or a short hold for a buff window) stays silent."""
    holds, player_iai, _open = _sen_walk(ctx)
    ideal_counts = _ideal_counts(ctx)
    ideal_iai = sum(ideal_counts.get(a, 0) for a in sorted(_IAI_CONVERSIONS))
    deficit = ideal_iai - player_iai
    if deficit < 1 or not holds:
        return None
    hold_total = sum(x for x, _s in holds)
    if hold_total < _SEN_HOLD_MIN_S:
        return None
    _worst_x, worst_start = max(holds, key=lambda h: (h[0], -h[1]))
    t = TEXT["sen_pacing"]
    value = deficit * _MIDARE_PAIR_P
    return (float(value), RootCause(
        kind="cascade_lost_use", ability_id=sd.MIDARE_SETSUGEKKA,
        ability_name=_name(sd.MIDARE_SETSUGEKKA),
        time_sec=round(worst_start, 1), measured_p=0.0,
        summary=t["summary"].format(hold=hold_total, deficit=deficit),
        prescription=t["prescription"].format(
            when=_mmss(worst_start), value=_MIDARE_PAIR_P),
        evidence=[
            EvidenceRow(
                k="Iaijutsu",
                v=t["count_v"].format(player=player_iai, ideal=ideal_iai),
                note=t["count_note"]),
            EvidenceRow(
                k="Waiting",
                v=t["hold_v"].format(hold=hold_total),
                note=t["hold_note"]),
        ]))


def _stranded_cause(ctx: AdviceContext) -> tuple[float, RootCause] | None:
    """Resources dead in the gauge at the kill: a full Sen set (a whole
    Midare Setsugekka and Kaeshi uncast) or a spendable Kenki pile. Both need
    _STRAND_TAIL_S of TARGETABLE time between the pile and the kill — a set
    completed on the final swing (or into a closing untargetable stretch) had
    no slot to spend in and stays silent."""
    dur = float(ctx.fight_duration_s)
    _ovf, end_kenki, held_since, last_gen_t = _kenki_walk(ctx)
    _holds, _iai, full_since = _sen_walk(ctx)

    def _spendable_tail(since: float) -> float:
        return (dur - since) - _downtime_overlap(since, dur, ctx)

    sen_stranded = (full_since is not None
                    and _spendable_tail(full_since) >= _STRAND_TAIL_S)
    kenki_stranded = (end_kenki >= _STRANDED_KENKI_MIN
                      and held_since is not None
                      and _spendable_tail(held_since) >= _STRAND_TAIL_S)
    if not sen_stranded and not kenki_stranded:
        return None
    ts = TEXT["stranded_sen"]
    tk = TEXT["stranded_kenki"]
    kenki_value = end_kenki * sd.KENKI_VALUE_P_PER_UNIT
    kenki_row = EvidenceRow(
        k="Kenki",
        v=tk["kenki_v"].format(kenki=float(end_kenki)),
        note=tk["kenki_note"].format(floor=_STRANDED_KENKI_MIN,
                                     when=_mmss(held_since or 0.0)))
    if sen_stranded:
        value = _MIDARE_PAIR_P + (kenki_value if kenki_stranded else 0.0)
        evidence = [EvidenceRow(
            k="Sen",
            v=ts["sen_v"],
            note=ts["sen_note"].format(when=_mmss(full_since)))]
        if kenki_stranded:
            evidence.append(kenki_row)
        return (float(value), RootCause(
            kind="cascade_lost_use", ability_id=sd.MIDARE_SETSUGEKKA,
            ability_name=_name(sd.MIDARE_SETSUGEKKA),
            time_sec=round(full_since, 1), measured_p=0.0,
            summary=ts["summary"],
            prescription=ts["prescription"].format(value=_MIDARE_PAIR_P),
            evidence=evidence,
            resources=[GAUGE_TEXT["kenki"]] if kenki_stranded else []))
    return (float(kenki_value), RootCause(
        kind="cascade_lost_use", ability_id=sd.HISSATSU_SHINTEN,
        ability_name=_name(sd.HISSATSU_SHINTEN),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=tk["summary"].format(kenki=float(end_kenki)),
        prescription=tk["prescription"].format(value=kenki_value),
        evidence=[kenki_row],
        resources=[GAUGE_TEXT["kenki"]]))


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """SAM probe set. Deterministic; no ProbeItems (causes only). RootCause
    order is the priority order the orchestrator's first-in-segment-wins
    matching consumes: descending per-use potency worth, stable-tied on
    ability id then time."""
    scored: list[tuple[float, RootCause]] = []
    scored.extend(_cd_drift_causes(ctx))
    for maybe in (_kenki_overcap_cause(ctx), _sen_pacing_cause(ctx),
                  _stranded_cause(ctx)):
        if maybe is not None:
            scored.append(maybe)
    scored.sort(key=lambda r: (-r[0], r[1].ability_id, r[1].time_sec))
    return [], [c for _v, c in scored]


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
