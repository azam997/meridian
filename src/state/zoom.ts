// UI zoom factor: scales the whole app via CSS `zoom` on :root. Persisted in
// localStorage. CSS zoom is a Chromium/WebKit feature — fine here since the
// app runs in a Tauri webview (or a Chromium-family browser during dev).

const KEY = 'fflogs.efficiency.analyzer.zoom.v2';

// "100%" in the UI maps to a 1.25 CSS zoom factor — the previous default
// (1.0) felt cramped, so we rebased the ladder. All labels are multiplied
// by BASE to get the underlying zoom factor.
const BASE = 1.25;

export const DEFAULT_ZOOM = BASE; // i.e. label "100%"

const lbl = (pct: number) => ({ value: (pct / 100) * BASE, label: `${pct}%` });

export const ZOOM_OPTIONS: { value: number; label: string }[] = [
  lbl(75),
  lbl(90),
  lbl(100),
  lbl(110),
  lbl(125),
  lbl(150),
];

export const loadZoom = (): number => {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULT_ZOOM;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : DEFAULT_ZOOM;
  } catch {
    return DEFAULT_ZOOM;
  }
};

export const saveZoom = (z: number): void => {
  try {
    localStorage.setItem(KEY, String(z));
  } catch {
    /* ignore */
  }
};

export const applyZoom = (z: number): void => {
  // `zoom` is non-standard but supported across Chromium/WebKit. Using a
  // cast since lib.dom.d.ts doesn't include it.
  (document.documentElement.style as CSSStyleDeclaration & { zoom: string }).zoom = String(z);
};

/** The CSS `zoom` factor currently applied to :root (what `applyZoom` last set).
 *  Needed wherever we mix pointer coordinates with CSS `left`/`top` lengths:
 *  `clientX` and `getBoundingClientRect()` are in post-zoom *visual* pixels, but
 *  a `left: Npx` set on an element inside the zoomed subtree is a *layout* length
 *  that the browser re-scales by `zoom`. Divide a visual delta by this before
 *  assigning it as `left`/`top`, or the zoom is applied twice. */
export const currentZoom = (): number => {
  const raw = (document.documentElement.style as CSSStyleDeclaration & { zoom: string }).zoom;
  const n = parseFloat(raw);
  return Number.isFinite(n) && n > 0 ? n : 1;
};
