// Category tabs + encounter chips — the shared ENCOUNTER row of the Top Pulls
// and Heal/Mit setup panels. Stateless: the active tab is derived from the
// selected encounter's category, so tab/selection can never disagree. A tab
// click selects that category's first encounter; the consumers' own effects
// (rankings refetch, plan dirty-tracking) do the rest.

import type { ReactNode } from 'react';
import type { Catalog, EncounterCategory } from '../sidecar/contract';
import { splitEncounterName } from '../views/pullsFormat';

type Props = {
  encounters: Catalog['encounters'];
  encounterId: number;
  onPick: (id: number) => void;
  /** Optional trailing hint, e.g. "1 of 1 ultimate in the catalog". */
  hint?: ReactNode;
};

// Absent on legacy payloads ⇒ savage (see EncounterCategory in contract.ts).
const catOf = (e: Catalog['encounters'][number]): EncounterCategory =>
  e.category ?? 'savage';

const CAT_LABEL: Record<EncounterCategory, string> = {
  savage: 'Savage',
  ultimate: 'Ultimate',
};

export const EncounterPicker = ({ encounters, encounterId, onPick, hint }: Props) => {
  if (encounters.length === 0) {
    return <span className="mut" style={{ fontSize: 12 }}>Loading encounters…</span>;
  }

  const cats: EncounterCategory[] = [];
  for (const e of encounters) {
    const c = catOf(e);
    if (!cats.includes(c)) cats.push(c);
  }
  const activeCat = catOf(
    encounters.find((e) => e.id === encounterId) ?? encounters[0],
  );
  const visible = encounters.filter((e) => catOf(e) === activeCat);

  return (
    <>
      <div className="seg-tabs">
        {cats.map((c) => (
          <button
            key={c}
            className={'seg-tab' + (c === activeCat ? ' on' : '')}
            onClick={() => {
              if (c === activeCat) return;
              const first = encounters.find((e) => catOf(e) === c);
              if (first) onPick(first.id);
            }}
          >
            {CAT_LABEL[c]}
          </button>
        ))}
      </div>
      <span className="enc-tabs-rule" />
      <div className="enc-chips">
        {visible.map((e) => {
          const { label, code } = splitEncounterName(e.name);
          return (
            <button
              key={e.id}
              className={'enc-chip' + (e.id === encounterId ? ' on' : '')}
              onClick={() => onPick(e.id)}
            >
              {label}
              {code && <span className="enc-chip-code mono">{code}</span>}
            </button>
          );
        })}
      </div>
      {hint && <span className="setup-hint">{hint}</span>}
    </>
  );
};
