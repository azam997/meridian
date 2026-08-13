"""Ninja deep-advice pack (`Job.advice_probes`).

RootCause candidates for the cascade re-attribution, all deterministic ledger
walks over the delivered cast stream (the MCH registry pattern):

* **Cooldown drift that cost an end-of-fight use** — Ten Chi Jin / Kunai's
  Bane / Bunshin / Kassatsu / Dokumori / Meisui / Dream Within a Dream, each
  checked against the sim's own counts with a downtime-forgiven drift ledger
  (gaps spanning death windows are skipped — deaths are priced by their own
  card). Kunai's Bane gets its own prescription: its real failure mode is a
  missing Shadow Walker, not a lazy button.
* **Ninki overflow marking a delayed spender** — the gauge ledger mirrors the
  sim's economy exactly, including the Bunshin shadow's +5 per mirrored
  weaponskill (5 mirrors per Bunshin, inside the 30s stack window), so
  overcap the generic gauge model under-counts is measured here. Only
  overflow BEYOND what the sim's own line spills counts: NIN pools Ninki for
  the burst by design, and the ideal rotation eats small overcaps doing it.
* **The shared mudra-charge pool sitting at two charges** — regen stopped;
  the economy the sim spends down ahead of caps (Chi/Jin-opened sequences
  drain the same pool, per `data.charge_sharing`). Capped time inside
  downtime and death windows is cut out of the stretches entirely, so only
  time the player could have spent through is counted or located.
* **Ninki stranded at the kill** — a full spender dead in the gauge at fight
  end, guarded by a minimum SPENDABLE hold (downtime and death time removed)
  so a gauge that only just crossed 50, or one the player died holding,
  stays silent.

No ProbeItems: NIN ships no bespoke card kinds, and the shared missed-cast /
residual probes already cover the generic cards. `measured_p` stays 0.0 — the
orchestrator prices each cause from its cascade segment's unexplained loss.

ALL user-facing copy lives in `TEXT` / `GAUGE_TEXT` below — improving the
feedback wording is a data edit here, never a logic change. `GAUGE_TEXT` is an
allowlist: sim-state fields without an entry (combo_step, mudra_goal,
tcj_step…) never surface in evidence lines.
"""
from __future__ import annotations

from jobs._core import ability_metadata
from jobs._core.advice import (
    AdviceContext, AdvicePack, EvidenceRow, GaugeText, ProbeItem, RootCause,
)
from jobs.ninja import data as nd

# Cooldowns the drift ledger walks (all single-charge). TEN's shared mudra
# pool is deliberately absent — charge pacing is economy-driven (banked for
# Suiton-before-Kunai's-Bane and downtime edges, per data.DRIFT_EXCLUSIONS)
# and has its own capped-pool ledger below.
_DRIFT_CDS: tuple[int, ...] = (
    nd.TEN_CHI_JIN, nd.KUNAIS_BANE, nd.BUNSHIN, nd.KASSATSU, nd.DOKUMORI,
    nd.MEISUI, nd.DREAM_WITHIN_A_DREAM,
)

# First-mudra ids that drain the shared 2-charge pool. The charged family
# (Ten/Chi/Jin) logs on the FIRST mudra of a paid sequence — Chi/Jin-opened
# sequences (an AoE Katon opens Chi) spend the same pool, which is exactly
# what data.JOB_DATA.charge_sharing encodes; the free family (Kassatsu /
# in-sequence ids) spends nothing.
_CHARGED_MUDRAS: frozenset[int] = frozenset({nd.TEN, nd.CHI, nd.JIN})

_NINKI_OVERCAP_MIN = 25        # overflow PAST the sim's own line before a card is worth it
_NINKI_OVERFLOW_MARK = 5       # the first overflow this big locates the card
_STRANDED_NINKI_MIN = 50       # a full spender died in the gauge
_STRANDED_HOLD_MIN_S = 5.0     # the gauge must have sat >= 50 this long, spendable
_CHARGE_CAP_MIN_S = 20.0       # >= one full charge of regen lost
_CHARGE_STRETCH_MARK_S = 5.0   # the first capped stretch this long locates the card
_BUNSHIN_MIRROR_NINKI = 5      # per mirrored weaponskill (simulator.apply_cast)
_BUNSHIN_WINDOW_S = 30.0       # 5 stacks / 30s (data.BUNSHIN_STATUS_ID): stacks
                               # left over at expiry mirror nothing, so the
                               # delivered ledger must not keep crediting them
_MUDRA_REGEN_S = nd.COOLDOWNS[nd.TEN][0]
_MUDRA_CHARGE_MAX = float(nd.COOLDOWNS[nd.TEN][1])


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
        # Kunai's Bane needs Shadow Walker, so its drift advice names the
        # Suiton feed instead of the button.
        "prescription_kb": ("Keep a Suiton banked so Shadow Walker is up "
                            "when Kunai's Bane returns. Biggest drift at "
                            "{when}, {worst:.1f}s late; the drift adds up "
                            "until a use (~{value}p) is lost."),
        "count_v": "{player} / {ideal}",
        "count_note": "casts vs the sim's line",
        "idle_v": "{drift:.0f}s",
        "idle_note": "≈ {recasts:.1f} full recasts of idle time",
    },
    "ninki_overcap": {
        "summary": "Ninki built past the cap, {total:.0f} wasted",
        "prescription": ("Use excess Ninki right away here. First overcap "
                         "at {when}; each held Bhavacakra slides the rest "
                         "until one stops fitting."),
        "worst_v": "{amount:.0f} Ninki",
        "worst_note": "wasted at {when}, the most consequential overcap",
        "total_v": "{total:.0f} Ninki",
        "total_note": ("~{value:.0f}p of spender value across {count} "
                       "overcap{plural}"),
    },
    "charge_cap": {
        "summary": ("Mudra charges sat full for {secs:.0f}s, {charges:.1f} "
                    "charge{plural} of regen lost"),
        # "was sitting full at" (not "first sat full at"): the located time is
        # the first CAPPED-AND-SPENDABLE instant, which is the re-engage when
        # the pool topped out during downtime.
        "prescription": ("Spend a Raiton here when both charges are up. The "
                         "pool was sitting full at {when}."),
        "cap_v": "{secs:.0f}s",
        "cap_note": "time at two charges across {count} stretch{plural}",
        "lost_v": "{charges:.1f} charge{plural}",
        "lost_note": "~{value:.0f}p of ninjutsu value left in the pool",
    },
    "ninki_stranded": {
        "summary": "{ninki:.0f} Ninki left at the kill",
        "prescription": "A last Bhavacakra fits before the end (~{value:.0f}p).",
        "ninki_v": "{ninki:.0f} unspent",
        "ninki_note": "last Ninki gain at {when} with no spender after",
    },
}

# Gauge glossary for the player-vs-ideal state delta (ALLOWLIST: fields not
# listed here never render). Keys are exact public scalar fields of
# `simulator.SimState`. Rows read `LABEL  {delta} over ideal  note`.
GAUGE_TEXT: dict[str, GaugeText] = {
    "ninki": GaugeText(
        label="Ninki", short="NKI",
        over_note="a Bhavacakra was ready",
        under_note=None,     # running lean on Ninki is not a mistake by itself
        min_delta=20.0),
    "kazematoi": GaugeText(
        label="Kazematoi", short="KAZ",
        over_note="Aeolian Edge bonuses sat unused",
        under_note=None,
        min_delta=2.0),
    "raiju": GaugeText(
        label="Raiju Ready", short="RAI",
        over_note="a Fleeting Raiju sat unused",
        under_note=None,
        min_delta=1.0),
}


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def _name(aid: int) -> str:
    m = ability_metadata.get_metadata(aid)
    return m.name if m else f"action {aid}"


def _overlap_s(a: float, b: float,
               windows: list[tuple[float, float]]) -> float:
    """Total overlap of [a, b] with the given windows."""
    return sum(max(0.0, min(b, we) - max(a, ws)) for ws, we in windows or [])


def _subtract(seg: tuple[float, float],
              windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """`seg` minus every window, as ordered non-empty pieces. Used to cut
    downtime and death out of a measured stretch BEFORE it is totalled or
    located, so a card can never point at a moment with no boss to hit."""
    pieces = [seg]
    for ws, we in windows or []:
        nxt: list[tuple[float, float]] = []
        for s, e in pieces:
            if we <= s or ws >= e:
                nxt.append((s, e))
                continue
            if s < ws:
                nxt.append((s, ws))
            if we < e:
                nxt.append((we, e))
        pieces = nxt
    return [(s, e) for s, e in pieces if e > s]


def _drift_causes(ctx: AdviceContext) -> list[RootCause]:
    """A cooldown the sim fit more of than the player cast, with the drift
    ledger that shows where the use was lost. Gaps are downtime-forgiven
    (a 60s cooldown spanning an untargetable stretch drifts unavoidably) and
    gaps touching a death window are skipped entirely (the death card owns
    that loss)."""
    ideal_counts: dict[int, int] = {}
    for _t, a in ctx.idealized:
        ideal_counts[a] = ideal_counts.get(a, 0) + 1
    out: list[tuple[float, RootCause]] = []
    for cd in _DRIFT_CDS:
        recast, _ch = nd.COOLDOWNS[cd]
        times = sorted(t for t, a in ctx.norm_casts if a == cd and t >= 0)
        player_n = len(times)
        deficit = ideal_counts.get(cd, 0) - player_n
        if deficit < 1 or len(times) < 2:
            continue
        drift_total = 0.0
        worst = (0.0, times[0])                  # (drift_s, gap start)
        for a, b in zip(times, times[1:]):
            if _overlap_s(a, b, ctx.death_windows) > 0.0:
                continue
            over = (b - a) - recast - _overlap_s(a, b, ctx.downtime_windows)
            if over > 0:
                drift_total += over
                if over > worst[0]:
                    worst = (over, a)
        if drift_total < recast * 0.5:
            continue
        name = _name(cd)
        value = deficit * nd.COOLDOWN_VALUE_P.get(cd, 0)
        t = TEXT["cd_drift"]
        rx_key = "prescription_kb" if cd == nd.KUNAIS_BANE else "prescription"
        out.append((float(value), RootCause(
            kind="cascade_lost_use", ability_id=cd, ability_name=name,
            time_sec=round(worst[1], 1), measured_p=0.0,
            summary=t["summary"].format(
                name=name, drift=drift_total, deficit=deficit,
                plural="s" if deficit != 1 else ""),
            prescription=t[rx_key].format(
                name=name, when=_mmss(worst[1]), worst=worst[0],
                value=nd.COOLDOWN_VALUE_P.get(cd, 0)),
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


def _walk_ninki(casts: list[tuple[float, int]]
                ) -> tuple[list[tuple[float, float]], int,
                           float | None, float | None]:
    """The Ninki gauge over the delivered stream, mirroring the sim's own
    economy (`simulator.apply_cast`): table generators + the Bunshin shadow's
    +5 per mirrored weaponskill (5 mirrors armed per Bunshin cast, mirror
    Ninki lands before the weaponskill's own grant), spenders 50 apiece
    floored at 0. Mirrors expire with the 30s stack window (the sim has no
    reason to model that — its line always spends them inside 10s — but a
    real stream can strand them across a downtime wall, and crediting
    phantom Ninki would invent overcap). Returns (overflow events, final
    Ninki, last gain time, the time the final >=50 plateau began or None)."""
    ninki = 0
    mirrors = 0
    mirror_until = float("-inf")
    overflows: list[tuple[float, float]] = []    # (t, overflowed amount)
    last_gen_t: float | None = None
    cross50_t: float | None = None

    def _add(t: float, amount: int) -> None:
        nonlocal ninki, cross50_t, last_gen_t
        was = ninki
        ninki += amount
        if ninki > nd.NINKI_CAP:
            overflows.append((t, float(ninki - nd.NINKI_CAP)))
            ninki = nd.NINKI_CAP
        if was < _STRANDED_NINKI_MIN <= ninki:
            cross50_t = t
        last_gen_t = t

    for t, a in sorted(casts, key=lambda c: c[0]):     # stable time sort
        if t < 0:
            continue
        if t > mirror_until:
            mirrors = 0
        if mirrors > 0 and a in nd.BUNSHIN_MIRRORED_IDS:
            mirrors -= 1
            _add(t, _BUNSHIN_MIRROR_NINKI)
        gain = nd.NINKI_GENERATORS.get(a, 0)
        if gain:
            _add(t, gain)
        spend = nd.NINKI_SPENDERS.get(a, 0)
        if spend:
            ninki = max(0, ninki - spend)
            if ninki < _STRANDED_NINKI_MIN:
                cross50_t = None
        if a == nd.BUNSHIN:
            mirrors = nd.BUNSHIN_STACKS
            mirror_until = t + _BUNSHIN_WINDOW_S
    return overflows, ninki, last_gen_t, cross50_t


def _ninki_overcap_cause(ctx: AdviceContext) -> RootCause | None:
    """Ledger walk of the Ninki gauge: overflow marks a spender fired later
    than the gauge allowed — the delay compounds into every later Bhavacakra
    (or Zesho Meppo under Higi).

    Measured against the SIM'S OWN spill, not against zero. NIN pools Ninki
    into the burst deliberately (`pick_ogcd` holds spenders to NINKI_POOL_MAX
    outside the Kunai's Bane window), so the idealized line itself overflows a
    little on a long fight; charging the player for that would card a clean
    stream. Only the excess above the ideal line's own overflow counts, and
    the excess is the number the copy reports."""
    overflows, _final, _last, _cross = _walk_ninki(ctx.norm_casts)
    total = sum(v for _t, v in overflows)
    ideal_overflows, _if, _il, _ic = _walk_ninki(list(ctx.idealized))
    excess = total - sum(v for _t, v in ideal_overflows)
    if excess < _NINKI_OVERCAP_MIN or not overflows:
        return None
    first = next((t for t, v in overflows if v >= _NINKI_OVERFLOW_MARK),
                 overflows[0][0])
    worst_t, worst_v = max(overflows, key=lambda o: (o[1], -o[0]))
    t = TEXT["ninki_overcap"]
    return RootCause(
        kind="cascade_burst", ability_id=nd.BHAVACAKRA,
        ability_name=_name(nd.BHAVACAKRA),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(total=excess),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="Worst",
                v=t["worst_v"].format(amount=worst_v),
                note=t["worst_note"].format(when=_mmss(worst_t))),
            EvidenceRow(
                k="Total",
                v=t["total_v"].format(total=excess),
                note=t["total_note"].format(
                    value=excess * nd.NINKI_VALUE_P_PER_UNIT,
                    count=len(overflows),
                    plural="s" if len(overflows) != 1 else "")),
        ],
        resources=[GAUGE_TEXT["ninki"]])


def _charge_cap_cause(ctx: AdviceContext) -> RootCause | None:
    """Time the shared mudra-charge pool sat at two charges with regen
    stopped. The pool is seeded at the sim's own post-prepull level when the
    stream shows a pre-pull mudra sequence, at the full pool otherwise; the
    charged first-mudra family (Ten/Chi/Jin) spends one charge each. Downtime
    and death are CUT OUT of the stretches before anything is totalled or
    located, so the card never counts (or points at) a stretch with no boss to
    spend the charge on."""
    from jobs.ninja.simulator import OPENER_CHARGES
    end = float(ctx.fight_duration_s)
    casts = sorted(ctx.norm_casts, key=lambda c: c[0])
    prepull_mudra = any(t < 0 and a in nd.MUDRA_IDS for t, a in casts)
    pool = float(OPENER_CHARGES) if prepull_mudra else _MUDRA_CHARGE_MAX
    spends = [t for t, a in casts
              if 0 <= t <= end and a in _CHARGED_MUDRAS]
    stretches: list[tuple[float, float]] = []    # capped [start, end)
    last_t = 0.0
    for i, t in enumerate(spends + [end]):
        if t > last_t:
            if pool >= _MUDRA_CHARGE_MAX:
                stretches.append((last_t, t))
            else:
                t_cap = last_t + (_MUDRA_CHARGE_MAX - pool) * _MUDRA_REGEN_S
                if t_cap < t:
                    stretches.append((t_cap, t))
                    pool = _MUDRA_CHARGE_MAX
                else:
                    pool += (t - last_t) / _MUDRA_REGEN_S
        if i < len(spends):
            pool = max(0.0, pool - 1.0)
        last_t = max(last_t, t)

    forgiven = list(ctx.downtime_windows or []) + list(ctx.death_windows or [])
    counted: list[tuple[float, float]] = []
    for s, e in stretches:
        counted.extend(_subtract((s, e), forgiven))

    total = sum(e - s for s, e in counted)
    if total < _CHARGE_CAP_MIN_S or not counted:
        return None
    first = next((s for s, e in counted
                  if e - s >= _CHARGE_STRETCH_MARK_S),
                 counted[0][0])
    charges_lost = total / _MUDRA_REGEN_S
    plural = "s" if round(charges_lost, 1) != 1.0 else ""
    t = TEXT["charge_cap"]
    return RootCause(
        kind="cascade_burst", ability_id=nd.RAITON,
        ability_name=_name(nd.RAITON),
        time_sec=round(first, 1), measured_p=0.0,
        summary=t["summary"].format(secs=total, charges=charges_lost,
                                    plural=plural),
        prescription=t["prescription"].format(when=_mmss(first)),
        evidence=[
            EvidenceRow(
                k="At cap",
                v=t["cap_v"].format(secs=total),
                note=t["cap_note"].format(
                    count=len(counted),
                    plural="es" if len(counted) != 1 else "")),
            EvidenceRow(
                k="Lost",
                v=t["lost_v"].format(charges=charges_lost, plural=plural),
                note=t["lost_note"].format(
                    value=charges_lost * nd.MUDRA_CHARGE_VALUE_P)),
        ])


def _ninki_stranded_cause(ctx: AdviceContext) -> RootCause | None:
    """Ninki that died in the gauge: a full spender (>= 50) left unspent at
    fight end, provided the gauge sat there long enough that a weave was
    realistically SPENDABLE. A gauge that only crossed 50 in the final GCDs
    stays silent, and so does one the player was dead or targetless for
    (death is priced by its own card; downtime has no target to hit)."""
    _overflows, final, last_gen_t, cross50_t = _walk_ninki(ctx.norm_casts)
    if final < _STRANDED_NINKI_MIN or last_gen_t is None or cross50_t is None:
        return None
    end = float(ctx.fight_duration_s)
    forgiven = list(ctx.downtime_windows or []) + list(ctx.death_windows or [])
    spendable = ((end - cross50_t)
                 - _overlap_s(cross50_t, end, forgiven))
    if spendable < _STRANDED_HOLD_MIN_S:
        return None
    t = TEXT["ninki_stranded"]
    return RootCause(
        kind="cascade_lost_use", ability_id=nd.BHAVACAKRA,
        ability_name=_name(nd.BHAVACAKRA),
        time_sec=round(last_gen_t, 1), measured_p=0.0,
        summary=t["summary"].format(ninki=final),
        prescription=t["prescription"].format(
            value=final * nd.NINKI_VALUE_P_PER_UNIT),
        evidence=[EvidenceRow(
            k="Ninki",
            v=t["ninki_v"].format(ninki=final),
            note=t["ninki_note"].format(when=_mmss(last_gen_t)))],
        resources=[GAUGE_TEXT["ninki"]])


def advice_probes(ctx: AdviceContext, cards: list[dict], progress=None
                  ) -> tuple[list[ProbeItem], list[RootCause]]:
    """NIN probe set. Deterministic; RootCause order is the priority order the
    orchestrator's first-in-segment-wins matching consumes: lost cooldown uses
    (highest total value first), then the Ninki-overcap spender delay, then
    the capped mudra pool, then the stranded Ninki."""
    causes: list[RootCause] = list(_drift_causes(ctx))
    for cause in (_ninki_overcap_cause(ctx), _charge_cap_cause(ctx),
                  _ninki_stranded_cause(ctx)):
        if cause is not None:
            causes.append(cause)
    return [], causes


# The registered pack: probes + the gauge glossary the orchestrator's
# state-delta evidence reads (see jobs/_core/advice.py).
PACK = AdvicePack(probes=advice_probes, gauge_text=GAUGE_TEXT)
