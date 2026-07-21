// Startup splash — the Meridian wordmark over a solid backdrop, covering the
// window while the sidecar spawns and answers the auth handshake. Fades out
// as soon as boot completes, or at the 2s cap, whichever comes first; a short
// floor lets the fade-in land so an instant (mock/dev) boot doesn't strobe.

import { useEffect, useState } from 'react';

const MIN_VISIBLE_MS = 400;
const MAX_VISIBLE_MS = 2000;
const FADE_MS = 250; // keep in sync with .splash's opacity transition

export const Splash = ({ ready }: { ready: boolean }) => {
  const [minElapsed, setMinElapsed] = useState(false);
  const [capElapsed, setCapElapsed] = useState(false);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    const min = setTimeout(() => setMinElapsed(true), MIN_VISIBLE_MS);
    const cap = setTimeout(() => setCapElapsed(true), MAX_VISIBLE_MS);
    return () => {
      clearTimeout(min);
      clearTimeout(cap);
    };
  }, []);

  const fading = capElapsed || (ready && minElapsed);

  useEffect(() => {
    if (!fading) return;
    const t = setTimeout(() => setGone(true), FADE_MS);
    return () => clearTimeout(t);
  }, [fading]);

  if (gone) return null;
  return (
    <div className={`splash${fading ? ' out' : ''}`} data-tauri-drag-region>
      <img
        src="/meridian-wordmark.png"
        alt="FFXIV Meridian — Efficiency Analyzer"
        draggable={false}
      />
    </div>
  );
};
