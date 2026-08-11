// Small persisted UI booleans (panel expand/collapse states the user sets
// once and expects to stick). Single localStorage key, whole-object
// read/patch — the accent.ts idiom, not persist.ts (which is "last run
// selection" and only written at selection-change points).

const KEY = 'fflogs.efficiency.analyzer.uiprefs.v1';

export type UiPrefs = {
  /** Dashboard reference strip: false = one-line strip, true = the full
   *  "Efficiency vs references" card with percentile/rank + distro chart. */
  refStripExpanded: boolean;
  /** Potential-improvements zero-priced notes row expanded to its rows. */
  openerNotesExpanded: boolean;
};

const DEFAULTS: UiPrefs = {
  refStripExpanded: false,
  openerNotesExpanded: false,
};

export const loadUiPrefs = (): UiPrefs => {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<UiPrefs>;
    return {
      refStripExpanded: !!parsed.refStripExpanded,
      openerNotesExpanded: !!parsed.openerNotesExpanded,
    };
  } catch {
    return DEFAULTS;
  }
};

export const saveUiPrefs = (patch: Partial<UiPrefs>): void => {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...loadUiPrefs(), ...patch }));
  } catch {
    /* ignore */
  }
};
