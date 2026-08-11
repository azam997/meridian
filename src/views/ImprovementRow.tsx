// The Potential-Improvements card rows, shared by the full-fight improvements
// panel (DashboardView) and the per-phase breakdown (PhasePanel). Extracted so
// both consume the identical card treatment (severity, icon, located jump,
// children dropdown) without a DashboardView ↔ PhasePanel import cycle.

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

/** Icon-first cell for an improvement row (the MitPlanBoard language): the real
 *  ability icon when the card resolves one, else the kind/category Lucide glyph
 *  in the severity-tinted box. Severity itself is carried by the row's left
 *  edge stripe (CSS), so the icon cell is free for identification. */
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
  return (
    <div className="sev">
      {createElement(kindGlyph(im.kind), { size: size >= 24 ? 14 : 12 })}
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
      className={`finding child ${isNote ? 'info' : severityFor(im.lostPotency)}${located ? '' : ' static'}`}
      {...interactive}
    >
      <ImpIcon im={im} meta={meta} size={22} />
      <div>
        <div className="title">{im.summary}</div>
      </div>
      <div className="cost">
        {isNote ? (
          <div className="delta mut" style={{ fontSize: 11 }}>note</div>
        ) : (
          <div className="delta">−{fmtNum(Math.round(im.lostPotency))}p</div>
        )}
      </div>
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
}: {
  im: Improvement;
  meta: Record<number, AbilityMetaJson>;
  onJump: JumpToTime;
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
        className={`finding ${isNote ? 'info' : severityFor(im.lostPotency)}${activate ? '' : ' static'}${isNote ? ' note' : ''}`}
        {...interactive}
      >
        <ImpIcon im={im} meta={meta} size={24} />
        <div>
          <div className="title">
            {expandable && (
              <ChevronRight size={13} className={`chev${open ? ' open' : ''}`} />
            )}
            {!expandable && located && (
              <span className="time">{fmtClock(jumpTime)}</span>
            )}
            {im.summary}
            <span className="kind-pill">{kindLabel(im.kind)}</span>
          </div>
          {expandable ? (
            <div className="desc">
              {hasChildren
                ? `${children.length} item${children.length === 1 ? '' : 's'}`
                : "what's in here"}{' '}
              — click to {open ? 'collapse' : 'expand'}
            </div>
          ) : located ? (
            <div className="desc">click to view on the timeline</div>
          ) : null}
        </div>
        <div className="cost">
          {isNote ? (
            <div className="delta mut" style={{ fontSize: 11 }}>note</div>
          ) : (
            <div className="delta">−{fmtNum(Math.round(im.lostPotency))}p</div>
          )}
        </div>
      </div>
      {expandable && open && (
        <div className="finding-children">
          {isResidual && (
            <div className="child-note">
              Recoverable potency the sim measured but couldn't tie to a single
              cast — looser GCD pacing than the optimal line, slightly weaker
              ability or filler choices, and small ordering differences spread
              across the fight.
              {hasChildren
                ? ' The located pieces we could pin are listed below — individual estimates that may not sum to the total, since the rest is spread thinly across many GCDs.'
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
