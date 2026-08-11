"""Measure healer AMPLIFIER magnitudes from live logs (network, not pytest).

Sibling to validate_mit_values.py. That script audits the mitigation *library*
(mit % per defensive). This one MEASURES the real effect size of the healer
"amplifier" layer — the tools that buff, crit, spread, or transform another
heal/shield rather than dealing their own math. The mit planner currently
guesses these from wiki tooltips; this tool asks the logs what they actually do.

Five measurement modes, one per amplifier archetype (see AMPS):

  SHIELD_DIRECT — the amp grants a barrier the base spell does not have. There
    is no baseline to divide by; the measured barrier IS the amplifier's whole
    contribution. FFLogs emits it as an `applybuff` carrying `absorb` (the pool
    in HP) with `extraAbilityGameID` = the spell that produced it.
      * AST Neutral Sect — Aspected Benefic / Helios Conjunction gain a barrier.

  SHIELD_AMPLIFY — the amp scales an EXISTING barrier. Partition the barrier's
    `absorb` by whether the caster held the amp buff in the ~3s before the shield
    landed (it is consumed on cast), then amplified/baseline → the boost %.
      * SGE Zoe — +50% on the next healing spell's barrier (→ Eukrasian Prognosis
        II / Diagnosis).

  SHIELD_CRIT — the amp forces a guaranteed crit on the next shield GCD. A crit
    Adloquium both inflates its Galvanize pool and adds a Catalyze pool; we flag
    crits by a co-occurring Catalyze `applybuff` from the same source and compare
    crit vs non-crit Galvanize `absorb` → the crit multiplier + Catalyze value.
      * SCH Recitation.

  HEAL_MULT_SELF — the amp raises the HEALER's own healing potency for a window.
    Per healed ability, compare gross healing (amount+overheal), non-crit only,
    inside vs outside the window → the effective multiplier. Robust for un-
    transformed kits (WHM Temperance); partial where the amp also renames GCDs
    (SCH Seraphism → Accession/Manifestation only exist in-window, so only the
    surviving oGCD heals compare).
      * WHM Temperance (+20%), SCH Seraphism (+20%), SGE Philosophia (+20%).

  HEAL_MULT_RECV — the amp raises healing RECEIVED by a buffed target. Same gross
    ratio, but bucketed by the target's buff window and pooled across sources —
    noisier (small n, mixed sources), reported at low confidence.
      * SGE Krasis (+20% ST), SGE Physis II (+10%), SCH Protraction (+10% ST).

Absorb pools are converted to cure-potency-equivalent via a per-healer
HP-per-potency factor, calibrated the same way damage.py does it: the healer's
own direct heals of a known potency (median gross / potency).

Run from python/ (needs client_id/client_secret in
~/.fflogs_efficiency_analyzer/config.json, or a persisted app sign-in):

    python scripts/calibrate_amplifiers.py                 # Dancing Mad, top 6
    python scripts/calibrate_amplifiers.py --enc 105 --top 8   # M12S P2
    python scripts/calibrate_amplifiers.py --code <url|code> [--fight N]

Amplifiers whose healer never appears in the sampled comps are reported as
"not seen" — Dancing Mad's very top parses skew SCH/AST, so a couple of extra
logs (or a Savage tier with more WHM/SGE) are needed to catch Zoe / Temperance.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encounters import ALL_ENCOUNTERS  # noqa: E402
from fflogs_api import (  # noqa: E402
    AuthExpiredError, BundleStream, parse_report_url,
)
from jobs._core.ability_metadata import get_metadata  # noqa: E402
from jobs._core.buff_windows import _friendly_actor_jobs  # noqa: E402
from mitplan.damage import _HEAL_POTENCY_BY_NAME, _pick_logs  # noqa: E402
from mitplan.library import (  # noqa: E402
    HP_PER_POTENCY_DEFAULT, ROLE_MAX_HP_DEFAULT, internal_job_name,
)

# --- tunables ---------------------------------------------------------------
DEFAULT_ENC = 1085          # Dancing Mad (Ultimate) — the AST/SCH/SGE/WHM ref
DEFAULT_TOP = 6
CONSUME_LOOKBACK_S = 3.0     # a shield's cast is up to a GCD before it lands
CRIT_PAIR_S = 1.5            # Catalyze lands with the Adloquium that spawned it
MIN_SAMPLES = 3             # below this a bucket is "thin" (low confidence)
# Absorb below this fraction of role max HP is a tooltip-scale unit artifact
# (Divine Benison logs 500, Guardian's Will 1000), not an HP pool — damage.py.
ABSORB_TRUST_MIN_FRAC = 0.01

HEALER_SUBTYPES = {"WhiteMage", "Scholar", "Astrologian", "Sage"}


@dataclass(frozen=True)
class AmpSpec:
    key: str
    job: str
    mode: str                       # SHIELD_DIRECT | SHIELD_AMPLIFY | ...
    amp_status: tuple[str, ...]     # the buff that marks the amplifier window
    wiki: str
    # SHIELD_* modes: the buff name(s) that carry the produced barrier's absorb.
    barrier_status: tuple[str, ...] = ()
    # SHIELD_CRIT: the status that marks a crit shield (Catalyze).
    crit_status: str = ""


AMPS: tuple[AmpSpec, ...] = (
    AmpSpec(
        key="Neutral Sect", job="Astrologian", mode="SHIELD_DIRECT",
        amp_status=("Neutral Sect",), barrier_status=("Neutral Sect",),
        wiki="Aspected Helios/Conjunction gain a 125% barrier; Aspected "
             "Benefic 250% (of the heal potency)."),
    AmpSpec(
        key="Zoe", job="Sage", mode="SHIELD_AMPLIFY",
        amp_status=("Zoe",),
        barrier_status=("Eukrasian Prognosis", "Eukrasian Diagnosis"),
        wiki="+50% potency on the next healing spell (-> its barrier)."),
    AmpSpec(
        key="Recitation", job="Scholar", mode="SHIELD_CRIT",
        amp_status=("Recitation",), barrier_status=("Galvanize",),
        crit_status="Catalyze",
        wiki="Guarantees crit+DH on the next Adlo/Succor/Indom/Excog; the "
             "crit inflates the barrier and adds a Catalyze pool."),
    AmpSpec(
        key="Temperance", job="White Mage", mode="HEAL_MULT_SELF",
        amp_status=("Temperance",),
        wiki="+20% healing magic potency (self, 20s)."),
    AmpSpec(
        key="Seraphism", job="Scholar", mode="HEAL_MULT_SELF",
        amp_status=("Seraphism",),
        wiki="+20% healing magic potency (self, 20s) — also renames GCD heals."),
    AmpSpec(
        key="Philosophia", job="Sage", mode="HEAL_MULT_SELF",
        amp_status=("Philosophia",),
        wiki="+20% healing magic potency (self, 20s)."),
    AmpSpec(
        key="Krasis", job="Sage", mode="HEAL_MULT_RECV",
        amp_status=("Krasis",),
        wiki="+20% healing received (single target, 10s)."),
    AmpSpec(
        key="Physis II", job="Sage", mode="HEAL_MULT_RECV",
        amp_status=("Physis II",),
        wiki="+10% healing received (party, 15s over-time window)."),
    AmpSpec(
        key="Protraction", job="Scholar", mode="HEAL_MULT_RECV",
        amp_status=("Protraction",),
        wiki="+10% healing received (single target, 10s)."),
)


# --- per-amp accumulator ----------------------------------------------------
@dataclass
class Bucket:
    spec: AmpSpec
    seen_job: bool = False
    # SHIELD_DIRECT / SHIELD_AMPLIFY: {producing-action-name: [potency-equiv]}
    direct_hp: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    direct_pot: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    ampl_hp: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    base_hp: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    # SHIELD_CRIT — keyed by producing action so the crit multiplier is measured
    # against the SAME spell (Adloquium crit vs Adloquium non-crit), never across.
    crit_hp: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    noncrit_hp: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    catalyze_hp: list[float] = field(default_factory=list)
    recit_confirmed: int = 0
    # HEAL_MULT_*: {ability: {"in": [gross], "out": [gross], "mi": [mult], "mo": [mult]}}
    heal: dict[str, dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(
            lambda: {"in": [], "out": [], "mi": [], "mo": []}))
    heal_ids: dict[str, int] = field(default_factory=dict)   # ability name -> id


def _med(vals: list[float]) -> float:
    return float(statistics.median(vals)) if vals else 0.0


def _windows(buff_evs, names, target_id, status_names, fight_end_ms):
    """apply/refresh → remove intervals (ms) for `status_names` on `target_id`."""
    evs = sorted(
        (e for e in buff_evs
         if e.get("targetID") == target_id
         and names.get(e.get("abilityGameID") or 0) in status_names
         and e.get("type") in ("applybuff", "refreshbuff", "removebuff")),
        key=lambda e: e["timestamp"])
    out: list[tuple[int, int]] = []
    open_t = None
    for e in evs:
        if e["type"] in ("applybuff", "refreshbuff"):
            if open_t is None:
                open_t = e["timestamp"]
        elif open_t is not None:
            out.append((open_t, e["timestamp"]))
            open_t = None
    if open_t is not None:
        out.append((open_t, fight_end_ms))
    return out


def _in_any(t: float, windows, lookback_ms: float = 0.0) -> bool:
    return any(a - lookback_ms <= t <= b for a, b in windows)


def _hp_per_potency(heal_evs) -> float | None:
    """Median gross HP per point of cure potency, from a healer's own direct
    non-crit heals of a known potency (mirrors damage.py's calibration)."""
    ratios = []
    for ev in heal_evs:
        if ev.get("type") != "heal" or ev.get("tick") or ev.get("hitType") == 2:
            continue
        name = ev.get("_name") or ""
        pot = _HEAL_POTENCY_BY_NAME.get(name)
        if not pot:
            continue
        gross = float(ev.get("amount") or 0) + float(ev.get("overheal") or 0)
        if gross > 0:
            ratios.append(gross / pot)
    return _med(ratios) if len(ratios) >= 5 else None


def collect_log(client, code, fid, summary, buckets: dict[str, Bucket],
                warnings: list[str]) -> None:
    fight = next((f for f in (summary.get("fights") or [])
                  if f.get("id") == fid), None)
    if fight is None:
        warnings.append(f"{code}#{fid}: fight not in report")
        return
    start, end = fight["startTime"], fight["endTime"]
    names = {ab["gameID"]: ab.get("name") or ""
             for ab in (summary.get("masterData") or {}).get("abilities") or []}

    healers = [(aid, internal_job_name(sub))
               for aid, sub in _friendly_actor_jobs(summary, fight)
               if sub in HEALER_SUBTYPES]
    if not healers:
        return
    for _, job in healers:
        for b in buckets.values():
            if b.spec.job == job:
                b.seen_job = True

    # One bundle: each healer's sourced Healing (attributes heals + absorbs to a
    # caster) + one unsourced Buffs (all barrier applies + amp windows).
    streams = [BundleStream("Healing", start, end, source_id=aid)
               for aid, _ in healers]
    streams.append(BundleStream("Buffs", start, end))
    bundles = client.get_event_bundle(code, streams)
    heal_by_healer = {healers[i][0]: bundles[i] for i in range(len(healers))}
    buff_evs = bundles[-1]
    for evs in heal_by_healer.values():
        for ev in evs:
            ev["_name"] = names.get(ev.get("abilityGameID") or 0, "")
    all_heals = [ev for evs in heal_by_healer.values() for ev in evs]

    hp_per_pot = {aid: (_hp_per_potency(evs) or HP_PER_POTENCY_DEFAULT)
                  for aid, evs in heal_by_healer.items()}
    absorb_floor = ABSORB_TRUST_MIN_FRAC * min(ROLE_MAX_HP_DEFAULT.values())
    job_of = {aid: job for aid, job in healers}

    # amp self-windows per (healer, amp-key)
    self_win: dict[tuple[int, str], list] = {}
    for aid, job in healers:
        for b in buckets.values():
            if b.spec.job == job:
                self_win[(aid, b.spec.key)] = _windows(
                    buff_evs, names, aid, b.spec.amp_status, end)

    # ---- SHIELD_* : walk barrier applybuffs -------------------------------
    for ev in buff_evs:
        if ev.get("type") not in ("applybuff", "refreshbuff"):
            continue
        absorb = ev.get("absorb")
        if not absorb or float(absorb) < absorb_floor:
            continue
        nm = names.get(ev.get("abilityGameID") or 0, "")
        src = ev.get("sourceID")
        t = ev["timestamp"]
        for b in buckets.values():
            sp = b.spec
            if nm not in sp.barrier_status:
                continue
            xnm = names.get(ev.get("extraAbilityGameID") or 0,
                            f"ability {ev.get('extraAbilityGameID')}")
            hpp = hp_per_pot.get(src, HP_PER_POTENCY_DEFAULT)
            if sp.mode == "SHIELD_DIRECT":
                b.direct_hp[xnm].append(float(absorb))
                b.direct_pot[xnm].append(float(absorb) / hpp)
            elif sp.mode == "SHIELD_AMPLIFY":
                win = self_win.get((src, sp.key), [])
                if _in_any(t, win, CONSUME_LOOKBACK_S * 1000):
                    b.ampl_hp[xnm].append(float(absorb))
                else:
                    b.base_hp[xnm].append(float(absorb))
            elif sp.mode == "SHIELD_CRIT":
                # crit = a Catalyze applybuff from the same source within 1.5s
                crit = any(
                    names.get(e.get("abilityGameID") or 0) == sp.crit_status
                    and e.get("sourceID") == src
                    and abs(e["timestamp"] - t) <= CRIT_PAIR_S * 1000
                    and e.get("type") in ("applybuff", "refreshbuff")
                    for e in buff_evs)
                (b.crit_hp if crit else b.noncrit_hp)[xnm].append(float(absorb))
                if crit and _in_any(t, self_win.get((src, sp.key), []),
                                    CONSUME_LOOKBACK_S * 1000):
                    b.recit_confirmed += 1

    # Catalyze pool sizes (for Recitation's added shield)
    for b in buckets.values():
        if b.spec.mode != "SHIELD_CRIT":
            continue
        for ev in buff_evs:
            if (names.get(ev.get("abilityGameID") or 0) == b.spec.crit_status
                    and ev.get("absorb") and float(ev["absorb"]) >= absorb_floor
                    and ev.get("type") in ("applybuff", "refreshbuff")):
                b.catalyze_hp.append(float(ev["absorb"]))

    # ---- HEAL_MULT_SELF : healer's own gross heal in/out window ------------
    for b in buckets.values():
        if b.spec.mode != "HEAL_MULT_SELF":
            continue
        for aid, job in healers:
            if job != b.spec.job:
                continue
            win = self_win.get((aid, b.spec.key), [])
            if not win:
                continue
            for ev in heal_by_healer[aid]:
                if ev.get("type") != "heal" or ev.get("tick"):
                    continue
                if ev.get("hitType") == 2:      # skip crits (variance)
                    continue
                gross = float(ev.get("amount") or 0) + float(ev.get("overheal") or 0)
                if gross <= 0:
                    continue
                slot = b.heal[ev["_name"]]
                b.heal_ids.setdefault(ev["_name"], ev.get("abilityGameID") or 0)
                inside = _in_any(ev["timestamp"], win)
                slot["in" if inside else "out"].append(gross)
                m = ev.get("multiplier")
                if isinstance(m, (int, float)):
                    slot["mi" if inside else "mo"].append(float(m))

    # ---- HEAL_MULT_RECV : buffed target's gross heal-received in/out -------
    for b in buckets.values():
        if b.spec.mode != "HEAL_MULT_RECV":
            continue
        # windows keyed by the buffed TARGET (amp is applied to whoever gets it)
        tgt_win: dict[int, list] = defaultdict(list)
        for ev in buff_evs:
            if (names.get(ev.get("abilityGameID") or 0) in b.spec.amp_status
                    and ev.get("type") in ("applybuff", "refreshbuff", "removebuff")):
                tgt_win[ev.get("targetID")]  # touch to register the target
        for tgt in list(tgt_win):
            tgt_win[tgt] = _windows(buff_evs, names, tgt, b.spec.amp_status, end)
        if not tgt_win:
            continue
        for ev in all_heals:
            if ev.get("type") != "heal" or ev.get("tick") or ev.get("hitType") == 2:
                continue
            tgt = ev.get("targetID")
            win = tgt_win.get(tgt)
            gross = float(ev.get("amount") or 0) + float(ev.get("overheal") or 0)
            if gross <= 0:
                continue
            slot = b.heal[ev["_name"]]
            b.heal_ids.setdefault(ev["_name"], ev.get("abilityGameID") or 0)
            if win is not None and _in_any(ev["timestamp"], win):
                slot["in"].append(gross)
            elif win is not None:
                slot["out"].append(gross)   # same target, outside its window


# --- reporting --------------------------------------------------------------
def _fmt_ratio_table(b: Bucket, split_gcd: bool) -> list[str]:
    """Per-ability gross ratios for the HEAL_MULT modes.

    `split_gcd` (the caster-side +heal% amps): the buff scales GCD healing
    SPELLS, not oGCD abilities — so the headline is the GCD-spell median, and
    the oGCD group is reported separately (expected ~1.0, a self-check). The
    receiver-side amps set split_gcd=False (they boost ALL incoming heals)."""
    rows: list[tuple[str, float, int, int, bool]] = []
    for name, s in sorted(b.heal.items()):
        if len(s["in"]) < MIN_SAMPLES or len(s["out"]) < MIN_SAMPLES:
            continue
        gi, go = _med(s["in"]), _med(s["out"])
        if go <= 0:
            continue
        meta = get_metadata(b.heal_ids.get(name, 0))
        is_ogcd = bool(meta.is_ogcd) if meta else False
        rows.append((name, gi / go, len(s["in"]), len(s["out"]), is_ogcd))

    if not rows:
        return ["    no ability had >=3 non-crit samples both in AND out of "
                "window"]

    lines: list[str] = []
    gcd_r = [r for _, r, _, _, o in rows if not o]
    ogcd_r = [r for _, r, _, _, o in rows if o]
    if split_gcd:
        if gcd_r:
            agg = statistics.median(gcd_r)
            lines.append(f"    multiplier on GCD healing spells (median of "
                         f"{len(gcd_r)}): x{agg:.3f}  (+{(agg-1)*100:.1f}%)")
        else:
            lines.append("    no GCD healing SPELL was comparable in/out — the "
                         "amp only affects renamed GCDs (see rows)")
        if ogcd_r:
            om = statistics.median(ogcd_r)
            lines.append(f"    oGCD abilities (self-check, expect ~1.0): "
                         f"x{om:.3f}  ({len(ogcd_r)} abilities)")
    else:
        agg = statistics.median([r for _, r, _, _, _ in rows])
        lines.append(f"    multiplier on received healing (median of "
                     f"{len(rows)}): x{agg:.3f}  (+{(agg-1)*100:.1f}%)")
    for name, r, ni, no, o in rows:
        tag = "oGCD" if o else "GCD "
        lines.append(f"      [{tag}] {name:<20} in {_med(b.heal[name]['in']):>7.0f}"
                     f"(n={ni:>3}) out {_med(b.heal[name]['out']):>7.0f}(n={no:>3})"
                     f"  x{r:.3f}")
    return lines


def report(buckets: dict[str, Bucket], n_logs: int, enc_name: str) -> None:
    print(f"\n{'='*74}\nAMPLIFIER CALIBRATION — {enc_name} ({n_logs} logs)\n{'='*74}")
    for b in buckets.values():
        sp = b.spec
        print(f"\n[{sp.job}] {sp.key}  ({sp.mode})")
        print(f"    wiki: {sp.wiki}")
        if not b.seen_job:
            print(f"    -- not seen: no {sp.job} in the sampled comps --")
            continue

        if sp.mode == "SHIELD_DIRECT":
            if not b.direct_hp:
                print("    -- amp buff present but no barrier applications seen --")
            for xnm in sorted(b.direct_hp):
                hp = _med(b.direct_hp[xnm])
                pot = _med(b.direct_pot[xnm])
                n = len(b.direct_hp[xnm])
                conf = "clean" if n >= MIN_SAMPLES else "THIN"
                # % of the producing spell's own heal potency (the wiki basis),
                # when that base potency is in the mit library.
                base = _HEAL_POTENCY_BY_NAME.get(xnm)
                pct = (f"  = {pot / base * 100:.0f}% of the {int(base)}p base heal"
                       if base else "")
                print(f"      via {xnm:<20} barrier {hp:>8.0f} HP  "
                      f"~{pot:>5.0f} potency-equiv  (n={n}, {conf}){pct}")

        elif sp.mode == "SHIELD_AMPLIFY":
            actions = sorted(set(b.ampl_hp) | set(b.base_hp))
            if not actions:
                print("    -- no barriers from the amplified spell seen --")
            for xnm in actions:
                ai, bi = b.ampl_hp.get(xnm, []), b.base_hp.get(xnm, [])
                a, base = _med(ai), _med(bi)
                if ai and bi and base > 0:
                    r = a / base
                    print(f"      {xnm:<20} amplified {a:>8.0f} HP (n={len(ai)}) "
                          f"vs base {base:>8.0f} HP (n={len(bi)})  x{r:.3f} "
                          f"(+{(r-1)*100:.0f}%)")
                else:
                    print(f"      {xnm:<20} amplified n={len(ai)} base n={len(bi)}"
                          "  -- need samples on both sides --")

        elif sp.mode == "SHIELD_CRIT":
            # crit multiplier per producing action (same spell both sides)
            actions = sorted(set(b.crit_hp) | set(b.noncrit_hp))
            mults = []
            for xnm in actions:
                cr, nc = b.crit_hp.get(xnm, []), b.noncrit_hp.get(xnm, [])
                if len(cr) >= MIN_SAMPLES and len(nc) >= MIN_SAMPLES:
                    r = _med(cr) / _med(nc)
                    mults.append(r)
                    print(f"      {xnm:<20} non-crit {_med(nc):>8.0f} HP(n={len(nc):>3}) "
                          f"crit {_med(cr):>8.0f} HP(n={len(cr):>3})  x{r:.3f}")
                else:
                    print(f"      {xnm:<20} crit n={len(cr)} non-crit n={len(nc)} "
                          "-- thin --")
            if mults:
                print(f"      => barrier crit multiplier x{statistics.median(mults):.3f} "
                      f"(median across spells)")
            if b.catalyze_hp:
                print(f"      + Catalyze pool  {_med(b.catalyze_hp):>8.0f} HP "
                      f"(n={len(b.catalyze_hp)}) — the extra shield a crit adds")
            print(f"      Recitation-confirmed crit shields: {b.recit_confirmed}")

        else:  # HEAL_MULT_*
            for line in _fmt_ratio_table(b, split_gcd=(sp.mode == "HEAL_MULT_SELF")):
                print(line)


def _resolve_picks(client, args):
    if args.code:
        code, fid = parse_report_url(args.code)
        if args.fight is not None:
            fid = args.fight
        if fid is None:
            summary = client.get_report_summaries([code])[code] or {}
            kills = [f for f in (summary.get("fights") or []) if f.get("kill")]
            if not kills:
                raise RuntimeError(f"{code}: no killed Encounter fights found")
            fid = kills[0]["id"]
        return [(code, fid)]
    return _pick_logs(client, args.enc)[: args.top]


def main() -> None:
    # This tool prints a few unicode dashes/arrows; force UTF-8 so a cp1252
    # Windows console doesn't choke (the run environment default).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(
        description="Measure healer amplifier magnitudes from live FFLogs data.")
    ap.add_argument("--enc", "--encounter", type=int, default=DEFAULT_ENC,
                    dest="enc",
                    help=f"encounter id (default {DEFAULT_ENC} = Dancing Mad)")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="number of top-ranked kill logs to sample")
    ap.add_argument("--code", type=str, default=None,
                    help="analyze one report (URL or 16-char code) instead")
    ap.add_argument("--fight", type=int, default=None,
                    help="fight id within --code (default: first kill)")
    args = ap.parse_args()

    try:
        from sidecar.main import _client
        client = _client()
    except AuthExpiredError as e:
        print("CANNOT RUN — no FFLogs credentials.\n"
              f"  {e}\n"
              "  Put client_id/client_secret in "
              "~/.fflogs_efficiency_analyzer/config.json, or sign in via the "
              "app, then re-run:\n"
              "    python scripts/calibrate_amplifiers.py", file=sys.stderr)
        sys.exit(2)

    try:
        picks = _resolve_picks(client, args)
    except Exception as e:  # noqa: BLE001
        print(f"CANNOT RUN — could not locate logs: {e}", file=sys.stderr)
        sys.exit(2)
    if not picks:
        print("CANNOT RUN — no logs found for that selection.", file=sys.stderr)
        sys.exit(2)

    enc_name = (args.code or
                next((n for i, n in ALL_ENCOUNTERS if i == args.enc),
                     f"Encounter {args.enc}"))
    summaries = client.get_report_summaries([c for c, _ in picks])
    buckets = {a.key: Bucket(a) for a in AMPS}
    warnings: list[str] = []
    used = 0
    for code, fid in picks:
        summary = summaries.get(code) or {}
        try:
            collect_log(client, code, fid, summary, buckets, warnings)
            used += 1
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{code}#{fid}: {e}")

    report(buckets, used, enc_name)
    if warnings:
        print("\nwarnings:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
