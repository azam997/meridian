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
  MitAssignment, MitMechanic, MitPlanResult, RoleAmounts,
} from '../sidecar/contract';

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
          mitPct: 0, shieldAmount: 0, rows: [], coveredNames: [], lane: 0,
        };
        byKey.set(key, c);
      }
      if (!a.isCarryover) {
        c.mitPct = a.mitPct;
        c.shieldAmount = a.shieldAmount;
        c.isSuggestion = a.isSuggestion;
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

export const MechanicDetail = ({ m, result }: { m: MitMechanic; result: MitPlanResult }) => {
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

export const MitPlanBoard = ({ result }: { result: MitPlanResult }) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [hoverRow, setHoverRow] = useState<number | null>(null);
  const [hoverCast, setHoverCast] = useState<string | null>(null);
  // Measured height of the inline detail card, so every row/bar below the
  // expanded row shifts down by exactly the space the card needs.
  const [panelH, setPanelH] = useState(0);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const { casts, lanesBySlot, chips } = useMemo(() => deriveCasts(result), [result]);
  const mechanics = result.mechanics;

  const expandedIdx = useMemo(
    () => (selected == null ? -1 : mechanics.findIndex((m) => m.id === selected)),
    [selected, mechanics],
  );
  useLayoutEffect(() => {
    const h = expandedIdx >= 0 && panelRef.current
      ? panelRef.current.offsetHeight + 14
      : 0;
    setPanelH((prev) => (prev === h ? prev : h));
  }, [expandedIdx, selected, result]);

  const gap = expandedIdx >= 0 ? panelH : 0;
  const yOf = (row: number) =>
    row * ROW_H + (expandedIdx >= 0 && row > expandedIdx ? gap : 0);
  const bodyH = mechanics.length * ROW_H + gap;

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
    <div className="mpb">
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
          {/* row separators + hover wash (span the full board width) */}
          {mechanics.map((_, r) => (
            <div key={`ln${r}`} className="mpb-line"
                 style={{ top: yOf(r) + ROW_H - 1 }} />
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
                    onClick={() => setSelected(selected === m.id ? null : m.id)}
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
                    <span className={`mpb-dot ${m.status}`}
                          title={`${m.status}${m.kind !== 'hpSet'
                            ? ` — planned ${roleLine(m.predicted)}` : ''}`} />
                  </div>
                  {i === expandedIdx && <div style={{ height: gap }} />}
                </div>
              );
            })}
          </div>
          {/* one column per party slot */}
          {result.lanes.map((lane) => (
            <div key={lane.slot} className="mpb-col"
                 style={{ width: colWidth(lane.slot), height: bodyH }}>
              {casts.filter((c) => c.slot === lane.slot).map((c) => {
                const top = yOf(c.rows[0]) + 4;
                const height =
                  yOf(c.rows[c.rows.length - 1]) - yOf(c.rows[0]) + ROW_H - 8;
                const lit = hoverCast === c.key
                  || (hoverRow != null && c.rows.includes(hoverRow));
                return (
                  <div
                    key={c.key}
                    className={
                      'mpb-bar' +
                      (c.isGcd ? ' gcd' : '') +
                      (lit ? ' lit' : '')
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
          ))}
          {/* the inline detail card, directly under the clicked row */}
          {selectedMech && (
            <div ref={panelRef} className="mpb-inline-detail"
                 style={{ top: yOf(expandedIdx) + ROW_H + 6 }}>
              <MechanicDetail m={selectedMech} result={result} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
