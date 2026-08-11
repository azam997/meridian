// Accent color: user-pickable highlight color. Persisted in localStorage,
// applied by overriding the --accent / --accent-soft / --accent-line
// custom properties on :root.

const KEY = 'fflogs.efficiency.analyzer.accent.v1';

export const DEFAULT_ACCENT = '#f97316'; // Meridian brand orange (the logo's)

export const ACCENT_OPTIONS: { value: string; label: string }[] = [
  { value: '#f97316', label: 'Meridian orange' },
  { value: '#f5a524', label: 'Amber' },
  { value: '#a78bfa', label: 'Violet' },
  { value: '#5eead4', label: 'Teal' },
  { value: '#60a5fa', label: 'Sky' },
  { value: '#f472b6', label: 'Pink' },
  { value: '#ef4f4f', label: 'Red' },
  { value: '#84cc16', label: 'Lime' },
];

export const loadAccent = (): string => {
  try {
    return localStorage.getItem(KEY) || DEFAULT_ACCENT;
  } catch {
    return DEFAULT_ACCENT;
  }
};

export const saveAccent = (hex: string): void => {
  try {
    localStorage.setItem(KEY, hex);
  } catch {
    /* ignore */
  }
};

/** Apply the accent color (and derived soft / line variants) to :root.
 *  Soft = 0x26 alpha ≈ 0.15; line = 0x59 alpha ≈ 0.35 (matches the
 *  prototype's tweak panel behavior). */
export const applyAccent = (hex: string): void => {
  const r = document.documentElement;
  r.style.setProperty('--accent', hex);
  r.style.setProperty('--accent-soft', hex + '26');
  r.style.setProperty('--accent-line', hex + '59');
};
