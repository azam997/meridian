// The Potential-Improvements card rows, shared by the full-fight improvements
// panel (DashboardView) and the per-phase breakdown (PhasePanel). Extracted so
// both consume the identical card treatment (severity, icon, located jump,
// children dropdown) without a DashboardView ↔ PhasePanel import cycle.
//
// Layout (design pass 2026-08): `.finding.imp4` rows are a 4-column grid —
// icon | fixed time column | content | cost — so times and costs scan as a
// table. Severity lives on the left stripe and the magnitude bar only; cost
// text is neutral mono. Evidence renders as a labelled k/v/note grid and the
// diffuse count table as cast-vs-sim bars, both straight off the wire.

import { createElement, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { ChevronRight } from 'lucide-react';
import { AbilityIcon } from '../components/AbilityIcon';
import { fmtClock, fmtNum } from '../format';
import type { AbilityMetaJson, Improvement } from '../sidecar/contract';
import { kindGlyph, kindLabel, severityFor } from './findings';

/** Callback shape shared by every located row: jump to a time in the Timeline,
 *  carrying the kind/ability so the target cast can be highlighted. */
export type JumpToTime = (
  timeSec: number,
  opts?: { kind?: string; abilityId?: number },
) => void;

/** Panel-level scale for the cost cell: `max` drives the magnitude bar
 *  (largest single loss = full track), `total` the under-5% dimming. */
export type CostScale = { max: number; total: number };

/** Icon-first cell for an improvement row (the MitPlanBoard language): the
 *  real ability icon when the card resolves one; else a neutral resource tile
 *  (BAT / HEAT) when the deep pass tagged the implicated gauge; else the
 *  kind/category Lucide glyph. Severity itself is carried by the row's left
 *  edge stripe + magnitude bar, so the icon cell is free for identification. */
export const ImpIcon = ({
  im,
  meta,
  size,
}: {
  im: Improvement;
  meta: Record<number, AbilityMetaJson>;
  size: number;
}) => {
  const m = im.abilityId > 0 ? meta[im.abilityId] : undefined;
  if (m) {
    return (
      <AbilityIcon
        kind={m.isOgcd ? 'ogcd1' : 'gcd1'}
        glyph={im.abilityName || (m.name ?? '')}
        name={m.name}
        iconPath={m.iconPath}
        size={size}
      />
    );
  }
  const res = im.resources?.[0];
  if (res) {
    return (
      <div className="res-tile" title={im.resources!.map((r) => r.label).join(' + ')}>
        {res.short || res.label.slice(0, 4).toUpperCase()}
      </div>
    );
  }
  return (
    <div className="sev">
      {createElement(kindGlyph(im.kind), { size: size >= 24 ? 14 : 12 })}
    </div>
  );
};

/** Fixed right-aligned mono time column ("—" for aggregate rows). */
const TimeCell = ({ located, timeSec }: { located: boolean; timeSec: number }) => (
  <div className={`ftime${located ? '' : ' none'}`}>
    {located ? fmtClock(Math.max(0, timeSec)) : '—'}
  </div>
);

/** Cost cell: 44×4 magnitude track (proportional to the panel's largest
 *  single loss, severity-coloured via the row class) + neutral mono value. */
const CostCell = ({
  im,
  scale,
}: {
  im: Improvement;
  scale?: CostScale;
}) => {
  if (im.lostPotency <= 0) {
    return (
      <div className="cost">
        <div className="delta mut" style={{ fontSize: 11 }}>note</div>
      </div>
    );
  }
  const frac = scale && scale.max > 0
    ? Math.max(0.06, Math.min(1, im.lostPotency / scale.max))
    : null;
  const dim = scale && scale.total > 0 && im.lostPotency < 0.05 * scale.total;
  return (
    <div className="cost">
      {frac != null && (
        <div className="mag-track">
          <div className="mag-fill" style={{ width: `${frac * 100}%` }} />
        </div>
      )}
      <div className={`delta${dim ? ' dim' : ''}`}>
        −{fmtNum(Math.round(im.lostPotency))}p
      </div>
    </div>
  );
};

/** Labelled evidence rows (KEY / mono value / prose note). */
const EvidenceGrid = ({ rows }: { rows: NonNullable<Improvement['evidence']> }) => (
  <div className="evi-grid">
    {rows.slice(0, 3).map((r, i) => (
      <div key={i} className="evi-row" style={{ display: 'contents' }}>
        <span className="evi-k">{r.k}</span>
        <span className="evi-v">{r.v}</span>
        <span className="evi-note">{r.note}</span>
      </div>
    ))}
  </div>
);

/** Cast-vs-sim count bars: your count as the fill, the sim's as a tick. */
const CountGapBars = ({ rows }: { rows: NonNullable<Improvement['countGaps']> }) => {
  const max = Math.max(1, ...rows.map((r) => Math.max(r.you, r.sim)));
  return (
    <div className="gap-rows">
      {rows.map((r, i) => (
        <div key={i} style={{ display: 'contents' }}>
          <span className="gap-label">{r.name}</span>
          <span className="gap-track">
            <span className="gap-fill" style={{ width: `${(r.you / max) * 100}%` }} />
            <span className="gap-tick" style={{ left: `${(r.sim / max) * 100}%` }} />
          </span>
          <span className="gap-count">{r.you} / {r.sim}</span>
        </div>
      ))}
      <div className="gap-legend">
        <span><span className="gap-legend-fill" /> you</span>
        <span><span className="gap-legend-tick" /> sim</span>
      </div>
    </div>
  );
};

/** A single (leaf) child row inside an expanded breakdown. Located children
 *  jump to the timeline; the rest are static. */
export const ChildRow = ({
  im,
  meta,
  onJump,
}: {
  im: Improvement;
  meta: Record<number, AbilityMetaJson>;
  onJump: JumpToTime;
}) => {
  // `timeSec <= 0` is the non-located sentinel — except the opener note, which
  // genuinely lives at 0:00 (see contract.ts) and jumps to the pull start.
  const located = im.timeSec > 0 || im.kind === 'opener';
  const jumpTime = Math.max(0, im.timeSec);
  const jump = () => onJump(jumpTime, { kind: im.kind, abilityId: im.abilityId });
  const isNote = im.lostPotency <= 0;
  const interactive = located
    ? {
        role: 'button',
        tabIndex: 0,
        onClick: jump,
        onKeyDown: (e: ReactKeyboardEvent) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            jump();
          }
        },
        title: `Jump to ${fmtClock(jumpTime)} in the Timeline`,
      }
    : {};
  return (
    <div
      className={`finding imp4 child ${isNote ? 'info' : severityFor(im.lostPotency)}${located ? '' : ' static'}`}
      {...interactive}
    >
      <ImpIcon im={im} meta={meta} size={22} />
      <TimeCell located={located} timeSec={im.timeSec} />
      <div>
        <div className="title">{im.summary}</div>
      </div>
      <CostCell im={im} />
    </div>
  );
};

/** One row in the unified panel. Three flavours:
 *   - aggregate cards with `children` (idle / clip totals, grouped "×N" rows,
 *     the "Other" residual) expand into a dropdown of individual, located,
 *     priced contributors — clicking the row toggles the breakdown;
 *   - leaf located items (`timeSec > 0`) jump to that time in the Timeline;
 *   - zero-priced diagnostics (`lostPotency <= 0`: missed enablers, opener
 *     ordering notes) show a muted "note" tag — they point at where to look
 *     without adding a double-counted number to the total. */
export const ImprovementRow = ({
  im,
  meta,
  onJump,
  scale,
  hidePill,
}: {
  im: Improvement;
  meta: Record<number, AbilityMetaJson>;
  onJump: JumpToTime;
  /** Panel cost scale (magnitude bar + <5% dimming). Absent → no bar. */
  scale?: CostScale;
  /** True when the row's kind pill would just restate its category header. */
  hidePill?: boolean;
}) => {
  const [open, setOpen] = useState(false);
  const children = im.children ?? [];
  const hasChildren = children.length > 0;
  // `timeSec <= 0` is the non-located sentinel — except the opener note, which
  // genuinely lives at 0:00 (see contract.ts) and jumps to the pull start.
  const located = im.timeSec > 0 || im.kind === 'opener';
  const jumpTime = Math.max(0, im.timeSec);
  const isNote = im.lostPotency <= 0;
  // The "Other" residual is always expandable — even with no located children
  // it reveals an explanation of what the diffuse remainder is.
  const isResidual = im.kind === 'residual';
  const expandable = hasChildren || isResidual;

  // Expandable cards toggle the dropdown; leaf located cards jump. (When a card
  // is expandable the breakdown is the point, so expansion wins over the jump.)
  const activate = expandable
    ? () => setOpen((o) => !o)
    : located
      ? () => onJump(jumpTime, { kind: im.kind, abilityId: im.abilityId })
      : undefined;
  const interactive = activate
    ? {
        role: 'button',
        tabIndex: 0,
        onClick: activate,
        onKeyDown: (e: ReactKeyboardEvent) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate();
          }
        },
        title: expandable
          ? open
            ? 'Collapse breakdown'
            : 'Expand breakdown'
          : `Jump to ${fmtClock(jumpTime)} in the Timeline`,
      }
    : {};

  return (
    <div className="improvement">
      <div
        className={`finding imp4 ${isNote ? 'info' : severityFor(im.lostPotency)}${activate ? '' : ' static'}${isNote ? ' note' : ''}`}
        {...interactive}
      >
        <ImpIcon im={im} meta={meta} size={24} />
        <TimeCell located={located} timeSec={im.timeSec} />
        <div>
          <div className="title">
            {expandable && (
              <ChevronRight size={13} className={`chev${open ? ' open' : ''}`} />
            )}
            {im.summary}
            {!hidePill && (
              <span className="kind-pill">{kindLabel(im.kind)}</span>
            )}
          </div>
          {im.prescription ? (
            <div className="desc">{im.prescription}</div>
          ) : expandable ? (
            <div className="desc">
              {hasChildren
                ? `${children.length} item${children.length === 1 ? '' : 's'}`
                : "what's in here"}{' '}
              — click to {open ? 'collapse' : 'expand'}
            </div>
          ) : null}
          {(im.evidence?.length ?? 0) > 0 && (
            <EvidenceGrid rows={im.evidence!} />
          )}
          {(im.countGaps?.length ?? 0) > 0 && (
            <CountGapBars rows={im.countGaps!} />
          )}
        </div>
        <CostCell im={im} scale={scale} />
      </div>
      {expandable && open && (
        <div className="finding-children">
          {isResidual && (
            <div className="child-note">
              Real, measured loss spread across many small choices: burst
              spacing, resource timing, filler picks. None is big enough to
              tie to a single cast.
              {hasChildren
                ? ' The located pieces we could pin are listed below. They are individual estimates and may not sum to the total, since the rest is spread thinly across many GCDs.'
                : ''}
            </div>
          )}
          {children.map((c, i) => (
            <ChildRow key={i} im={c} meta={meta} onJump={onJump} />
          ))}
        </div>
      )}
    </div>
  );
};
