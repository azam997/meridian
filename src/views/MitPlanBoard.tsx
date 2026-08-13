import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Crosshair, Droplet, HeartCrack, Layers, Shield, Waves,
  type LucideIcon,
} from 'lucide-react';
import { AbilityIcon } from '../components/AbilityIcon';
import { jobColor, jobIcon } from '../components/jobs';
import { fmtClock } from '../format';
import { KIND_LABEL, SCHOOL_LABEL, fmtK, roleLine } from './mitPlanShared';
import type {
  MitAssignment, MitLibraryAction, MitLibraryHealOption, MitMechanic,
  MitPlanResult, RoleAmounts,
} from '../sidecar/contract';

/** What a drag carries through dataTransfer: a palette chip (no `from`), or a
 *  placed bar being MOVED (`from` = its current mechanic id). */
type DragPayload = { slot: string; job: string; actionId: number; from?: string };

/** A healer slot's authorable AoE GCD heals (the detail card's incrementer). */
type HealerHealOptions = {
  slot: string;
  job: string;
  options: (MitLibraryHealOption & { iconPath?: string })[];
};
type DraftHealRow = { job: string; actionId: number; count: number };

/** Edit-mode callbacks/state — all optional, `{result}`-only callers are the
 *  read-only board unchanged. */
type EditProps = {
  editable?: boolean;
  /** The chip/bar currently being dragged (drop strips render only then). */
  dragAction?: { slot: string; job: string; action: MitLibraryAction } | null;
  /** Row indexes where that chip cannot land (cooldown/pool/hpSet preview). */
  blockedRows?: Set<number>;
  /** A drop landed on a row — a placement, or a move when payload.from set. */
  onDropAction?: (mechanicId: string, payload: DragPayload) => void;
  onRemove?: (mechanicId: string, job: string, actionId: number) => void;
  /** A placed bar started/finished dragging (the parent mirrors this into its
   *  dragAction state so the strips + availability preview light up). */
  onCastDragStart?: (from: string, slot: string, job: string,
                     actionId: number) => void;
  onCastDragEnd?: () => void;
  /** The healers' authorable AoE GCD heals + the current heal draft — the
   *  detail card renders a per-gap +/- incrementer from these. */
  healOptions?: HealerHealOptions[];
  healDraft?: Record<string, DraftHealRow[]>;
  onHealDelta?: (mechanicId: string, job: string, actionId: number,
                 delta: number) => void;
};

/** Row-per-mechanic vertical plan board: compact icon-first mechanic rows on
 *  the left, one column per party slot on the right, every planned cast drawn
 *  ONCE as a vertical capsule spanning the mechanic rows it covers (originals
 *  + carryovers reconstruct coverage — no extra wire data). Clicking a row
 *  expands the detailed card inline, directly below that row. */

const ROW_H = 46;
const BAR_W = 26;
const LANE_GAP = 4;
// Reserved band at the right of the healer column for the ×N top-up chips so
// they never overlap the mitigation bars or spill into the next column.
const CHIP_BAND_W = 54;

// Boss-ability icons are often absent from XIVAPI for the newest tier — fall
// back to a kind glyph so rows still read at a glance.
const KIND_GLYPH: Record<MitMechanic['kind'], LucideIcon> = {
  raidwide: Waves, tankbuster: Shield, bleed: Droplet,
  multiHit: Layers, other: Crosshair, hpSet: HeartCrack,
};

type BoardCast = {
  key: string;
  slot: string;
  job: string;
  actionId: number;
  name: string;
  castAtSec: number;
  durationSec: number;
  isSuggestion: boolean;
  isGcd: boolean;
  mitPct: number;
  shieldAmount: number;
  /** Row indexes (into the mechanics array) this cast covers. */
  rows: number[];
  /** The row whose mechanic OWNS the cast (its non-carryover copy). NOT
   *  always rows[0]: a cast's duration can blanket an EARLIER mechanic (a
   *  bleed ticking through the lead window), and move/remove must target the
   *  owner or the draft entry is missed and the cast duplicates. */
  ownerRow: number;
  coveredNames: string[];
  lane: number;
};

type GcdChip = { row: number; name: string; actionId: number; count: number };

function deriveCasts(result: MitPlanResult): {
  casts: BoardCast[];
  lanesBySlot: Record<string, number>;
  chips: GcdChip[];
} {
  const byKey = new Map<string, BoardCast>();
  result.mechanics.forEach((m, row) => {
    for (const a of m.assignments) {
      const key = `${a.slot}|${a.actionId}|${a.castAtSec.toFixed(1)}`;
      let c = byKey.get(key);
      if (!c) {
        c = {
          key, slot: a.slot, job: a.job, actionId: a.actionId, name: a.name,
          castAtSec: a.castAtSec, durationSec: a.durationSec,
          isSuggestion: a.isSuggestion, isGcd: a.isGcd,
          mitPct: 0, shieldAmount: 0, rows: [], ownerRow: row,
          coveredNames: [], lane: 0,
        };
        byKey.set(key, c);
      }
      if (!a.isCarryover) {
        c.mitPct = a.mitPct;
        c.shieldAmount = a.shieldAmount;
        c.isSuggestion = a.isSuggestion;
        c.ownerRow = row;
      }
      if (!c.rows.includes(row)) {
        c.rows.push(row);
        c.coveredNames.push(`${fmtClock(m.timeSec)} ${m.name}`);
      }
    }
  });
  const casts = [...byKey.values()];
  for (const c of casts) c.rows.sort((a, b) => a - b);
  casts.sort((a, b) => a.rows[0] - b.rows[0] || a.castAtSec - b.castAtSec
    || a.actionId - b.actionId);

  // Greedy sub-lane packing per slot column (side-by-side when overlapping).
  const lanesBySlot: Record<string, number> = {};
  const laneEnds: Record<string, number[]> = {};
  for (const c of casts) {
    const ends = (laneEnds[c.slot] ??= []);
    const start = c.rows[0];
    const end = c.rows[c.rows.length - 1];
    let lane = ends.findIndex((e) => e < start);
    if (lane === -1) {
      lane = ends.length;
      ends.push(end);
    } else {
      ends[lane] = end;
    }
    c.lane = lane;
    lanesBySlot[c.slot] = Math.max(lanesBySlot[c.slot] ?? 1, lane + 1);
  }

  const chips: GcdChip[] = [];
  result.mechanics.forEach((m, row) => {
    for (const g of m.gcdHeals) {
      chips.push({ row, name: g.name, actionId: g.actionId, count: g.count });
    }
  });
  return { casts, lanesBySlot, chips };
}

const castTip = (c: BoardCast): string => {
  const extras: string[] = [];
  if (c.mitPct > 0) extras.push(`${Math.round(c.mitPct * 100)}% mit`);
  if (c.shieldAmount > 0) extras.push(`${fmtK(c.shieldAmount)} shield`);
  if (c.isGcd) extras.push('costs a GCD');
  if (c.isSuggestion) extras.push('suggested — the player owns this button');
  return [
    `${fmtClock(c.castAtSec)}  ${c.name} (${c.slot} ${c.job})`,
    extras.join(' · '),
    `covers: ${c.coveredNames.join(', ')}`,
  ].filter(Boolean).join('\n');
};

// --- Detail card (opens inline under the clicked row) ------------------------

const HpBar = ({ label, hp, max }: { label: string; hp: number; max: number }) => {
  const frac = max > 0 ? Math.max(0, Math.min(1, hp / max)) : 0;
  const tone = frac >= 0.25 ? 'ok' : frac >= 0.05 ? 'warn' : 'bad';
  return (
    <div className="mp-hp" title={`${label}: ${fmtK(hp)} / ${fmtK(max)} HP after this mechanic`}>
      <span className="mp-hp-lbl">{label}</span>
      <div className="mp-hp-track">
        <div className={`mp-hp-fill ${tone}`} style={{ width: `${frac * 100}%` }} />
      </div>
    </div>
  );
};

export const MechanicDetail = ({ m, result, onRemove, healOptions, healDraft,
                                 onHealDelta }: {
  m: MitMechanic; result: MitPlanResult;
  /** Edit mode: render a remove control on user-placed (non-carryover,
   *  non-suggestion) assignment chips. */
  onRemove?: (mechanicId: string, job: string, actionId: number) => void;
  /** Edit mode: the per-gap GCD-heal incrementer (heals are plan content —
   *  authored here, never auto-inserted for a user plan). */
  healOptions?: HealerHealOptions[];
  healDraft?: Record<string, DraftHealRow[]>;
  onHealDelta?: (mechanicId: string, job: string, actionId: number,
                 delta: number) => void;
}) => {
  const hasRole = (r: keyof RoleAmounts) => (m.unmitigated[r] ?? 0) > 0;
  return (
    <div className={`mp-card ${m.status}`}>
      <div className="mp-card-head">
        <span className="mp-time">{fmtClock(m.timeSec)}</span>
        <span className="mp-name">{m.name}</span>
        <span className={`mp-badge kind-${m.kind}`}>{KIND_LABEL[m.kind]}</span>
        {m.school !== 'unknown' && (
          <span className={`mp-badge school-${m.school}`}>{SCHOOL_LABEL[m.school]}</span>
        )}
        {m.hits.length > 1 && <span className="mp-badge">{m.hits.length} hits</span>}
        <span className={`mp-status ${m.status}`}>{m.status}</span>
      </div>
      {m.kind !== 'hpSet' && (
        <div className="mp-card-dmg mut">
          <span title="Median unmitigated damage per person hit (tank / healer / DPS), from the top logs">
            Unmitigated {roleLine(m.unmitigated)}
          </span>
          <span title="Damage expected after this plan's mitigation and shields">
            → planned {roleLine(m.predicted)}
          </span>
          {m.observedMitPct > 0 && (
            <span title="Average mitigation observed across the top logs (context)">
              top logs mit ~{Math.round(m.observedMitPct * 100)}%
            </span>
          )}
        </div>
      )}
      {(m.assignments.length > 0 || m.gcdHeals.length > 0) && (
        <div className="mp-assigns">
          {m.assignments.map((a: MitAssignment, i: number) => {
            const meta = result.abilityMeta[a.actionId];
            const extras: string[] = [];
            if (a.mitPct > 0) extras.push(`${Math.round(a.mitPct * 100)}%`);
            if (a.shieldAmount > 0) extras.push(`${fmtK(a.shieldAmount)} shield`);
            if (a.healAmount > 0) extras.push(`${fmtK(a.healAmount)} heal`);
            const cls = a.isCarryover ? ' carry' : a.isSuggestion ? ' suggest' : '';
            const tip =
              `${fmtClock(a.castAtSec)} · ${a.slot} ${a.job} — ${a.name}` +
              (extras.length ? ` (${extras.join(', ')})` : '') +
              (a.isCarryover ? ' · still active from an earlier mechanic' : '') +
              (a.isSuggestion ? ' · suggested personal' : '') +
              (a.isGcd ? ' · costs a GCD' : '');
            return (
              <span key={i} className={`mp-chip${cls}`} title={tip}>
                <AbilityIcon kind="ogcd1" glyph={a.name} name={meta?.name}
                             iconPath={meta?.iconPath} size={18} />
                <span className="mp-chip-slot">{a.slot}</span>
                {a.name}
                {extras.length > 0 && <span className="mp-chip-x mut">{extras[0]}</span>}
                {onRemove && !a.isCarryover && !a.isSuggestion && (
                  <button
                    className="mp-chip-rm"
                    title={`Remove ${a.name} from this mechanic`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemove(m.id, a.job, a.actionId);
                    }}
                  >
                    ×
                  </button>
                )}
              </span>
            );
          })}
          {m.gcdHeals.map((g, i) => (
            <span key={`g${i}`} className="mp-chip gcdheal"
                  title={`${fmtClock(g.castAtSec)} · ${g.slot} ${g.job} — ${g.name} ×${g.count} to top the party up before the hit (GCD time)`}>
              <AbilityIcon kind="gcd1" glyph={g.name}
                           name={result.abilityMeta[g.actionId]?.name}
                           iconPath={result.abilityMeta[g.actionId]?.iconPath}
                           size={18} />
              <span className="mp-chip-slot">{g.slot}</span>
              {g.name} ×{g.count}
            </span>
          ))}
        </div>
      )}
      {onHealDelta && healOptions && healOptions.length > 0
        && m.kind !== 'hpSet' && (
        <div className="mp-heals-edit">
          <span className="mp-heals-lbl">Healing GCDs before this hit</span>
          <div className="mp-heal-ctls">
            {healOptions.map((h) => h.options.map((o) => {
              const count = healDraft?.[m.id]?.find(
                (x) => x.job === h.job && x.actionId === o.actionId)?.count ?? 0;
              return (
                <span key={`${h.slot}|${o.actionId}`}
                      className={'mp-heal-ctl' + (count > 0 ? ' on' : '')}
                      title={`${o.name} (${h.slot} ${h.job})`
                        + (o.target === 'single' ? ' · heals the tank only' : '')
                        + (o.gcdCostPotency > 0
                          ? ` · costs ~${Math.round(o.gcdCostPotency)} potency per cast`
                          : ' · free (resource-gated)')}>
                  <AbilityIcon kind="gcd1" glyph={o.name}
                               name={result.abilityMeta[o.actionId]?.name ?? o.name}
                               iconPath={result.abilityMeta[o.actionId]?.iconPath
                                 ?? o.iconPath}
                               size={16} />
                  <span className="mp-heal-name">{o.name}</span>
                  <span className="mp-chip-slot">{h.slot}</span>
                  {o.target === 'single' && (
                    <span className="mp-heal-tgt">tank</span>
                  )}
                  <button
                    className="mp-heal-btn"
                    disabled={count === 0}
                    onClick={(e) => {
                      e.stopPropagation();
                      onHealDelta(m.id, h.job, o.actionId, -1);
                    }}
                  >
                    −
                  </button>
                  <span className="mp-heal-n">{count}</span>
                  <button
                    className="mp-heal-btn"
                    disabled={count >= 8}
                    onClick={(e) => {
                      e.stopPropagation();
                      onHealDelta(m.id, h.job, o.actionId, 1);
                    }}
                  >
                    +
                  </button>
                </span>
              );
            }))}
          </div>
        </div>
      )}
      <div className="mp-card-foot">
        <div className="mp-hp-row">
          {hasRole('tank') && <HpBar label="T" hp={m.hpAfter.tank} max={result.roleHp.tank} />}
          {hasRole('healer') && <HpBar label="H" hp={m.hpAfter.healer} max={result.roleHp.healer} />}
          {hasRole('dps') && <HpBar label="D" hp={m.hpAfter.dps} max={result.roleHp.dps} />}
        </div>
        {m.notes.length > 0 && <div className="mp-notes mut">{m.notes.join(' ')}</div>}
      </div>
    </div>
  );
};

// --- The board ----------------------------------------------------------------

export const MitPlanBoard = ({
  result, editable, dragAction, blockedRows, onDropAction, onRemove,
  onCastDragStart, onCastDragEnd, healOptions, healDraft, onHealDelta,
}: { result: MitPlanResult } & EditProps) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [hoverRow, setHoverRow] = useState<number | null>(null);
  const [hoverCast, setHoverCast] = useState<string | null>(null);
  // Drop-strip highlight while a palette chip drags over a row.
  const [overRow, setOverRow] = useState<number | null>(null);
  // Measured height of the inline detail card, so every row/bar below the
  // expanded row shifts down by exactly the space the card needs.
  const [panelH, setPanelH] = useState(0);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const { casts, lanesBySlot, chips } = useMemo(() => deriveCasts(result), [result]);
  const mechanics = result.mechanics;

  // Edit mode: EVERY row's detail card is open, hosted inline in a widened
  // mechanic column. The bars/strips to the right position through the same
  // cumulative-offset geometry, so a spanning bar stretches across the card
  // zones and drop strips stay row-sized.
  const allOpen = !!editable;
  const CARD_EST = 150;
  const [cardHs, setCardHs] = useState<number[]>([]);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);
  useLayoutEffect(() => {
    if (!allOpen) return;
    const hs = mechanics.map((_, i) => cardRefs.current[i]?.offsetHeight ?? 0);
    setCardHs((prev) =>
      prev.length === hs.length && prev.every((v, i) => v === hs[i])
        ? prev : hs);
    // Heights change with card content: a new result, or heal-incrementer
    // edits re-wrapping the controls. The identity check guarantees
    // convergence (measure → same heights → no state change).
  }, [allOpen, mechanics, healDraft, result]);

  const expandedIdx = useMemo(
    () => (selected == null ? -1 : mechanics.findIndex((m) => m.id === selected)),
    [selected, mechanics],
  );
  useLayoutEffect(() => {
    const h = expandedIdx >= 0 && panelRef.current
      ? panelRef.current.offsetHeight + 14
      : 0;
    setPanelH((prev) => (prev === h ? prev : h));
    // healDraft: the edit card's heal incrementer content changes height
    // without a new result — re-measure or the rows below drift/overlap.
  }, [expandedIdx, selected, result, healDraft]);

  const gap = expandedIdx >= 0 && !allOpen ? panelH : 0;
  // Prefix sums of the inline card heights (all-open mode): cum[r] = total
  // card height above row r.
  const cum = useMemo(() => {
    const out = [0];
    for (let i = 0; i < mechanics.length; i++) {
      out.push(out[i] + (allOpen ? (cardHs[i] ?? CARD_EST) : 0));
    }
    return out;
  }, [mechanics.length, cardHs, allOpen]);
  const yOf = (row: number) =>
    row * ROW_H + (allOpen
      ? cum[row]
      : (expandedIdx >= 0 && row > expandedIdx ? gap : 0));
  const bodyH = mechanics.length * ROW_H
    + (allOpen ? cum[mechanics.length] : gap);

  const litRows = useMemo(() => {
    if (hoverCast == null) return new Set<number>();
    const c = casts.find((x) => x.key === hoverCast);
    return new Set<number>(c ? c.rows : []);
  }, [hoverCast, casts]);

  const chipSlot = chips.length ? 'H2' : null;
  const colWidth = (slot: string) =>
    Math.max(1, lanesBySlot[slot] ?? 1) * (BAR_W + LANE_GAP) + LANE_GAP + 2
    + (slot === chipSlot ? CHIP_BAND_W : 0);

  const selectedMech = expandedIdx >= 0 ? mechanics[expandedIdx] : null;

  return (
    <div className={'mpb' + (allOpen ? ' edit' : '')}>
      <div className="mpb-scroll">
        {/* header */}
        <div className="mpb-head" style={{ height: 40 }}>
          <div className="mpb-head-mech">Mechanic</div>
          {result.lanes.map((lane) => {
            const icon = jobIcon(lane.job);
            return (
              <div key={lane.slot} className="mpb-head-col"
                   style={{ width: colWidth(lane.slot) }} title={lane.job}>
                {icon ? (
                  <img src={icon} alt="" width={18} height={18} draggable={false} />
                ) : (
                  <span className="mp-lane-dot" style={{ background: jobColor(lane.job) }} />
                )}
                <span>{lane.slot}</span>
              </div>
            );
          })}
        </div>
        <div className="mpb-body" style={{ height: bodyH }}>
          {/* row separators + hover wash (span the full board width). In
              all-open mode the separator sits BELOW each row's inline card
              (just before the next row). */}
          {mechanics.map((_, r) => (
            <div key={`ln${r}`} className="mpb-line"
                 style={{ top: (allOpen ? yOf(r + 1) : yOf(r) + ROW_H) - 1 }} />
          ))}
          {hoverRow != null && (
            <div className="mpb-rowlight"
                 style={{ top: yOf(hoverRow), height: ROW_H }} />
          )}
          {/* mechanic rows (a spacer reserves the inline card's slot) */}
          <div className="mpb-mechcol">
            {mechanics.map((m, i) => {
              const meta = m.bossAbilityIds.length
                ? result.abilityMeta[m.bossAbilityIds[0]] : undefined;
              const lit = litRows.has(i);
              const Glyph = KIND_GLYPH[m.kind];
              return (
                <div key={m.id} style={{ display: 'contents' }}>
                  <div
                    id={`mp-mech-${m.id}`}
                    className={
                      `mpb-row ${m.status}` +
                      (m.kind === 'hpSet' ? ' hpset' : '') +
                      (lit ? ' lit' : '') +
                      (selected === m.id ? ' selected' : '')
                    }
                    style={{ height: ROW_H }}
                    onMouseEnter={() => setHoverRow(i)}
                    onMouseLeave={() => setHoverRow(null)}
                    onClick={allOpen ? undefined
                      : () => setSelected(selected === m.id ? null : m.id)}
                  >
                    <span className="mpb-row-time">{fmtClock(m.timeSec)}</span>
                    {meta?.iconPath && m.kind !== 'hpSet' ? (
                      <AbilityIcon kind="gcd1" glyph={m.name} name={meta.name}
                                   iconPath={meta.iconPath} size={24} />
                    ) : (
                      <Glyph size={m.kind === 'hpSet' ? 20 : 17}
                             className="mpb-row-glyph" />
                    )}
                    <span className="mpb-row-name" title={m.name}>{m.name}</span>
                    <span className={`mp-badge kind-${m.kind}`}>{KIND_LABEL[m.kind]}</span>
                    {m.kind === 'hpSet' && (
                      <span className="mpb-row-note mut">→ 1 HP</span>
                    )}
                    {editable && m.kind !== 'hpSet' && (() => {
                      // Live damage readout for the editor: the most-hit
                      // role's per-person damage, shrinking as mit lands.
                      // Explicitly labeled — a single HP number was tried
                      // and hides the per-class HP differences; an unlabeled
                      // pair read like an HP before/after.
                      const role = (['tank', 'healer', 'dps'] as const).reduce(
                        (b, r) => (m.unmitigated[r] > m.unmitigated[b] ? r : b),
                        'tank' as 'tank' | 'healer' | 'dps');
                      const unmit = m.unmitigated[role];
                      const planned = m.predicted[role];
                      const reduced = fmtK(planned) !== fmtK(unmit);
                      return (
                        <span
                          className="mpb-row-dmg"
                          title={'Damage dealt per person '
                            + '(unmitigated → with your mitigation):\n'
                            + `Unmitigated ${roleLine(m.unmitigated)}\n`
                            + `Planned ${roleLine(m.predicted)}\n`
                            + 'HP after this hit: '
                            + `T ${fmtK(m.hpAfter.tank)} · H ${fmtK(m.hpAfter.healer)}`
                            + ` · D ${fmtK(m.hpAfter.dps)}`}
                        >
                          {reduced ? (
                            <>
                              <span className="mpb-row-dmg-was">{fmtK(unmit)}</span>
                              <span className="mpb-row-dmg-sep">→</span>
                              {fmtK(planned)}
                            </>
                          ) : fmtK(unmit)}
                          <span className="mpb-row-dmg-lbl">damage dealt</span>
                        </span>
                      );
                    })()}
                    <span className={`mpb-dot ${m.status}`}
                          title={`${m.status}${m.kind !== 'hpSet'
                            ? ` — planned ${roleLine(m.predicted)}` : ''}`} />
                  </div>
                  {allOpen ? (
                    <div
                      className="mpb-inline-card"
                      ref={(el) => { cardRefs.current[i] = el; }}
                    >
                      <MechanicDetail m={m} result={result}
                                      onRemove={onRemove}
                                      healOptions={healOptions}
                                      healDraft={healDraft}
                                      onHealDelta={onHealDelta} />
                    </div>
                  ) : (
                    i === expandedIdx && <div style={{ height: gap }} />
                  )}
                </div>
              );
            })}
          </div>
          {/* one column per party slot */}
          {result.lanes.map((lane) => (
            <div key={lane.slot} className="mpb-col"
                 style={{ width: colWidth(lane.slot), height: bodyH }}>
              <div className="mpb-col-inner" style={{ width: colWidth(lane.slot) }}>
              {casts.filter((c) => c.slot === lane.slot).map((c) => {
                const top = yOf(c.rows[0]) + 4;
                const height =
                  yOf(c.rows[c.rows.length - 1]) - yOf(c.rows[0]) + ROW_H - 8;
                const lit = hoverCast === c.key
                  || (hoverRow != null && c.rows.includes(hoverRow));
                const movable = !!editable && !c.isSuggestion
                  && !!onCastDragStart;
                return (
                  <div
                    key={c.key}
                    className={
                      'mpb-bar' +
                      (c.isGcd ? ' gcd' : '') +
                      (lit ? ' lit' : '') +
                      (movable ? ' movable' : '')
                    }
                    style={{
                      top,
                      height,
                      left: LANE_GAP + c.lane * (BAR_W + LANE_GAP),
                      width: BAR_W,
                      background: jobColor(c.job) + '55',
                      borderColor: jobColor(c.job),
                    }}
                    title={castTip(c)}
                    draggable={movable}
                    onDragStart={movable ? (e) => {
                      // Owner mechanic, NOT rows[0] — a bar can visually start
                      // on an earlier mechanic it merely blankets (carryover).
                      const from = mechanics[c.ownerRow].id;
                      e.dataTransfer.setData('text/plain', JSON.stringify({
                        slot: c.slot, job: c.job, actionId: c.actionId, from,
                      }));
                      e.dataTransfer.effectAllowed = 'move';
                      onCastDragStart!(from, c.slot, c.job, c.actionId);
                    } : undefined}
                    onDragEnd={movable ? () => onCastDragEnd?.() : undefined}
                    onMouseEnter={() => setHoverCast(c.key)}
                    onMouseLeave={() => setHoverCast(null)}
                  >
                    <AbilityIcon
                      kind={c.isGcd ? 'gcd1' : 'ogcd1'}
                      glyph={c.name}
                      name={result.abilityMeta[c.actionId]?.name}
                      iconPath={result.abilityMeta[c.actionId]?.iconPath}
                      size={BAR_W - 4}
                    />
                    {editable && onRemove && !c.isSuggestion && (
                      <button
                        className="mpb-bar-rm"
                        title={`Remove ${c.name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onRemove(mechanics[c.ownerRow].id, c.job, c.actionId);
                        }}
                      >
                        ×
                      </button>
                    )}
                  </div>
                );
              })}
              {lane.slot === chipSlot && chips.map((g, i) => (
                <div key={`chip${i}`} className="mpb-gcdchip"
                     style={{
                       top: yOf(g.row) + ROW_H / 2 - 12,
                       left: colWidth(lane.slot) - CHIP_BAND_W + 2,
                     }}
                     title={`${g.name} ×${g.count} — top-up GCDs before this mechanic`}>
                  <AbilityIcon
                    kind="gcd1"
                    glyph={g.name}
                    name={result.abilityMeta[g.actionId]?.name}
                    iconPath={result.abilityMeta[g.actionId]?.iconPath}
                    size={16}
                  />
                  <span>×{g.count}</span>
                </div>
              ))}
              </div>
            </div>
          ))}
          {/* drop strips: rendered only during an active palette drag, one per
              row via yOf() (they follow the expanded-row gap and need no
              pointer math — element hit-testing dodges the root-zoom trap).
              z-index above the columns and the sticky mechanic column so a
              drop lands anywhere on the row; below the inline detail (5). */}
          {editable && dragAction && mechanics.map((m, r) => {
            const blocked = blockedRows?.has(r) ?? m.kind === 'hpSet';
            return (
              <div
                key={`dz${r}`}
                className={'mpb-droprow' + (blocked ? ' blocked' : '')
                  + (overRow === r && !blocked ? ' on' : '')}
                style={{ top: yOf(r), height: ROW_H }}
                onDragOver={(e) => {
                  if (blocked) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = 'copy';
                }}
                onDragEnter={() => setOverRow(r)}
                onDragLeave={() => setOverRow((o) => (o === r ? null : o))}
                onDrop={(e) => {
                  e.preventDefault();
                  setOverRow(null);
                  if (blocked) return;
                  try {
                    const p = JSON.parse(
                      e.dataTransfer.getData('text/plain')) as DragPayload;
                    if (p && typeof p.actionId === 'number' && p.job) {
                      onDropAction?.(m.id, p);
                    }
                  } catch {
                    /* not a palette chip — ignore */
                  }
                }}
              />
            );
          })}
          {/* the inline detail card, directly under the clicked row
              (read-only mode; edit mode hosts every card in the column) */}
          {!allOpen && selectedMech && (
            <div ref={panelRef} className="mpb-inline-detail"
                 style={{ top: yOf(expandedIdx) + ROW_H + 6 }}>
              <MechanicDetail m={selectedMech} result={result}
                              onRemove={editable ? onRemove : undefined}
                              healOptions={editable ? healOptions : undefined}
                              healDraft={healDraft}
                              onHealDelta={editable ? onHealDelta : undefined} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
