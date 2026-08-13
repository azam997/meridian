import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { AbilityIcon } from '../components/AbilityIcon';
import { jobColor, jobIcon } from '../components/jobs';
import type { MitLibraryAction, MitLibraryResult } from '../sidecar/contract';

/** The custom-plan editor's ability palette: one collapsible group per party
 *  slot, each ability an HTML5-draggable chip. Rendered OUTSIDE the board's
 *  `.mpb-scroll` (its sticky columns clip anything inside). The drag payload
 *  rides dataTransfer as JSON so a drop works even if React state lags. */

type Props = {
  library: MitLibraryResult;
  onDragStart: (slot: string, job: string, action: MitLibraryAction) => void;
  onDragEnd: () => void;
};

const chipStat = (a: MitLibraryAction): string => {
  const mit = Math.round((a.mitAll + Math.max(a.mitPhys, a.mitMagic)) * 100);
  if (mit > 0) return `${mit}%`;
  if (a.tier === 'invuln') return 'invuln';
  if (a.shieldPotency > 0 || a.shieldPctMaxhp > 0) return 'shield';
  if (a.healPotency > 0) return 'heal';
  if (a.healMult > 0) return `+${Math.round(a.healMult * 100)}% heals`;
  if (a.healFlatPotency > 0) return 'heal rider';
  return '';
};

const chipTip = (a: MitLibraryAction, enablerName?: string): string => {
  const bits = [`${a.cooldownSec}s cooldown${a.charges > 1 ? ` (${a.charges} charges)` : ''}`];
  if (a.durationSec > 0) bits.push(`${a.durationSec}s effect`);
  if (a.isGcd) bits.push('costs a GCD');
  const lines = [`${a.name}: ${bits.join(', ')}`];
  if (a.stackGroup) {
    lines.push(`Does not stack with other ${a.stackGroup.replace('_', ' ')} debuffs on one hit.`);
  }
  if (a.resource) lines.push(`Spends 1 ${a.resource}.`);
  if (a.healFlatPotency > 0) {
    lines.push(`Adds ${Math.round(a.healFlatPotency)} potency to qualifying `
      + `GCD heals cast during its ${a.durationSec}s window. Credited only `
      + 'when the plan casts one there.');
  }
  if (a.requiresActionId != null) {
    lines.push(`Needs ${enablerName ?? 'its enabler'} within `
      + `${Math.round(a.requiresWithinSec)}s before it.`);
  }
  if (a.tier === 'invuln') lines.push('Placeable on tank busters only.');
  return lines.join('\n');
};

export const MitPalette = ({ library, onDragStart, onDragEnd }: Props) => {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [draggingKey, setDraggingKey] = useState<string | null>(null);

  const toggle = (slot: string) =>
    setCollapsed((c) => {
      const n = new Set(c);
      if (n.has(slot)) n.delete(slot);
      else n.add(slot);
      return n;
    });

  return (
    <div className="mp-palette">
      {library.slots.map((s) => {
        const icon = jobIcon(s.job);
        const open = !collapsed.has(s.slot);
        return (
          <div key={s.slot} className="mp-pal-group">
            <button
              className="mp-pal-head"
              aria-expanded={open}
              onClick={() => toggle(s.slot)}
              style={{ borderLeftColor: jobColor(s.job) }}
            >
              {icon && <img src={icon} alt="" width={16} height={16} draggable={false} />}
              <span className="mono">{s.slot}</span>
              <span className="mp-pal-job">{s.job}</span>
              <ChevronDown size={11}
                           className={'mp-pal-chev' + (open ? ' open' : '')} />
            </button>
            {open && (
              <div className="mp-pal-chips">
                {s.actions.map((a) => {
                  const key = `${s.slot}|${a.actionId}`;
                  const meta = library.abilityMeta[a.actionId];
                  const stat = chipStat(a);
                  const enabler = a.requiresActionId != null
                    ? library.abilityMeta[a.requiresActionId]?.name
                      ?? s.actions.find(
                        (x) => x.actionId === a.requiresActionId)?.name
                    : undefined;
                  return (
                    <span
                      key={key}
                      className={'mp-pal-chip' + (draggingKey === key ? ' dragging' : '')}
                      draggable
                      title={chipTip(a, enabler)}
                      onDragStart={(e) => {
                        e.dataTransfer.setData('text/plain', JSON.stringify(
                          { slot: s.slot, job: s.job, actionId: a.actionId }));
                        e.dataTransfer.effectAllowed = 'copy';
                        setDraggingKey(key);
                        onDragStart(s.slot, s.job, a);
                      }}
                      onDragEnd={() => {
                        setDraggingKey(null);
                        onDragEnd();
                      }}
                    >
                      <AbilityIcon kind={a.isGcd ? 'gcd1' : 'ogcd1'} glyph={a.name}
                                   name={meta?.name} iconPath={meta?.iconPath}
                                   size={18} />
                      {a.name}
                      {stat && <span className="mut">{stat}</span>}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
      <div className="mp-pal-foot mut">
        Drag an ability onto a damage row below. Rows grey out while its
        cooldown is spent. Amplifiers like Zoe and Recitation are credited
        onto your shields automatically.
      </div>
    </div>
  );
};
