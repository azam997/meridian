import { useEffect, useRef, useState } from 'react';
import { DownloadCloud, HardDrive, LogOut, Palette, ShieldCheck, ZoomIn } from 'lucide-react';
import { ACCENT_OPTIONS } from '../state/accent';
import { ZOOM_OPTIONS } from '../state/zoom';
import { checkForUpdate, installUpdate, useUpdater } from '../state/updater';
import { APP_VERSION } from '../data/changelog';
import { sidecar } from '../sidecar';
import { CACHE_CAP_DEFAULT_MB, CACHE_CAP_TICKS_MB } from '../sidecar/contract';
import type { AuthStatus } from '../sidecar/contract';

/** Nearest slider notch for a persisted cap (hand-edited configs may hold
 *  values between notches; the backend only clamps to the min/max range). */
const nearestCapTick = (mb: number): number => {
  let best = 0;
  CACHE_CAP_TICKS_MB.forEach((t, i) => {
    if (Math.abs(t - mb) < Math.abs(CACHE_CAP_TICKS_MB[best] - mb)) best = i;
  });
  return best;
};

type Props = {
  accent: string;
  setAccent: (hex: string) => void;
  zoom: number;
  setZoom: (z: number) => void;
  /** Report the cache's new on-disk size upward after a cap change evicts —
   *  App owns the status-bar "Cache: N MB" stat. */
  onCacheChanged: (totalBytes: number) => void;
  /** Current FFLogs auth (null while the sidecar is still being asked). */
  auth: AuthStatus | null;
  /** Report a mode change (sign-out) upward — App owns the auth state and
   *  re-opens the sign-in gate when the result is mode 'none'. */
  onAuthChanged: (a: AuthStatus) => void;
  /** Open the (dismissible) sign-in modal — offered in client-credentials
   *  mode so a dev config doesn't lock the user out of the normal sign-in. */
  onRequestSignIn: () => void;
};

export const SettingsView = ({
  accent,
  setAccent,
  zoom,
  setZoom,
  onCacheChanged,
  auth,
  onAuthChanged,
  onRequestSignIn,
}: Props) => {
  const [custom, setCustom] = useState(accent);
  const [signingOut, setSigningOut] = useState(false);
  const updater = useUpdater();
  const [checking, setChecking] = useState(false);

  // Cache size cap: null until the sidecar reports the saved value. Dragging
  // updates locally; the commit (persist + evict) is debounced so a drag
  // doesn't write config.json per tick.
  const [capMb, setCapMb] = useState<number | null>(null);
  const capCommit = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    let dead = false;
    void sidecar
      .cacheStats()
      .then((s) => {
        if (!dead) setCapMb(s.capMb);
      })
      .catch(() => {});
    return () => {
      dead = true;
      if (capCommit.current) clearTimeout(capCommit.current);
    };
  }, []);
  const changeCap = (mb: number) => {
    setCapMb(mb);
    if (capCommit.current) clearTimeout(capCommit.current);
    capCommit.current = setTimeout(() => {
      void sidecar
        .setCacheCap(mb)
        .then((s) => {
          setCapMb(s.capMb);
          onCacheChanged(s.totalBytes);
        })
        .catch(() => {});
    }, 400);
  };

  const recheck = async () => {
    setChecking(true);
    try {
      await checkForUpdate();
    } finally {
      setChecking(false);
    }
  };

  const signOut = async () => {
    setSigningOut(true);
    try {
      onAuthChanged(await sidecar.fflogsLogout());
    } catch (e) {
      console.error('[fflogsLogout]', e);
    } finally {
      setSigningOut(false);
    }
  };

  const setBoth = (hex: string) => {
    setCustom(hex);
    setAccent(hex);
  };

  return (
    <div className="content narrow">
      <div className="hero">
        <h1>Settings</h1>
        <p>Tune the look and behavior of the app. Saved locally.</p>
      </div>

      <div className="card">
        <div className="card-head">
          <Palette size={14} />
          <h2>Accent color</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            highlight color across the UI
          </span>
        </div>
        <div className="card-body">
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))',
              gap: 8,
            }}
          >
            {ACCENT_OPTIONS.map((opt) => {
              const active = opt.value.toLowerCase() === accent.toLowerCase();
              return (
                <button
                  key={opt.value}
                  className={'btn ' + (active ? 'primary' : '')}
                  style={{ justifyContent: 'flex-start' }}
                  onClick={() => setBoth(opt.value)}
                >
                  <span
                    style={{
                      width: 18,
                      height: 18,
                      borderRadius: 4,
                      background: opt.value,
                      display: 'inline-block',
                      boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.25)',
                      flexShrink: 0,
                    }}
                  />
                  {opt.label}
                </button>
              );
            })}
          </div>

          <div className="row" style={{ marginTop: 18, gap: 10 }}>
            <label className="field-label" style={{ margin: 0 }}>
              Custom
            </label>
            <input
              type="color"
              value={custom}
              onChange={(e) => setBoth(e.target.value)}
              style={{
                width: 44,
                height: 32,
                border: '1px solid var(--border)',
                borderRadius: 8,
                background: 'var(--bg-2)',
                padding: 2,
                cursor: 'pointer',
              }}
            />
            <span className="mono mut" style={{ fontSize: 12 }}>
              {custom}
            </span>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <ZoomIn size={14} />
          <h2>UI scale</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            scales the entire interface
          </span>
        </div>
        <div className="card-body">
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(96px, 1fr))',
              gap: 8,
            }}
          >
            {ZOOM_OPTIONS.map((opt) => {
              const active = Math.abs(opt.value - zoom) < 0.001;
              return (
                <button
                  key={opt.value}
                  className={'btn ' + (active ? 'primary' : '')}
                  onClick={() => setZoom(opt.value)}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <HardDrive size={14} />
          <h2>Cache limit</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            disk space for downloaded log data
          </span>
        </div>
        <div className="card-body">
          <div className="row" style={{ gap: 14, alignItems: 'center' }}>
            {/* The slider walks the notch list by index (5 MB pitch below
                30 MB, 10 MB above), so drag and arrow keys snap to notches. */}
            <div style={{ flex: 1, position: 'relative' }}>
              <input
                type="range"
                className="slider"
                min={0}
                max={CACHE_CAP_TICKS_MB.length - 1}
                step={1}
                value={nearestCapTick(capMb ?? CACHE_CAP_DEFAULT_MB)}
                disabled={capMb === null}
                onChange={(e) => changeCap(CACHE_CAP_TICKS_MB[Number(e.target.value)])}
                aria-label="Cache size limit"
                aria-valuetext={`${capMb ?? CACHE_CAP_DEFAULT_MB} MB`}
                style={{ width: '100%', display: 'block' }}
              />
              <div className="slider-ticks" aria-hidden="true">
                {CACHE_CAP_TICKS_MB.map((t, i) => {
                  const p = i / (CACHE_CAP_TICKS_MB.length - 1);
                  // Align each mark with the 16px thumb's center of travel.
                  return (
                    <span
                      key={t}
                      className="slider-tick"
                      style={{ left: `calc(${p * 100}% + ${8 - p * 16}px)` }}
                    />
                  );
                })}
              </div>
            </div>
            <span className="num" style={{ width: 64, textAlign: 'right' }}>
              {capMb === null ? '…' : `${capMb} MB`}
            </span>
          </div>
          <p className="mut" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
            Oldest data is removed first once the limit is reached, and
            re-downloaded if needed again. A lower limit means more re-downloading
            when revisiting older encounters or other jobs.
          </p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <ShieldCheck size={14} />
          <h2>FF Logs account</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            how the analyzer reads logs
          </span>
        </div>
        <div className="card-body">
          {auth === null && (
            <p className="mut" style={{ fontSize: 13, margin: 0 }}>Checking sign-in…</p>
          )}
          {auth?.mode === 'user' && (
            <div className="row" style={{ gap: 12, alignItems: 'center' }}>
              <p className="mut" style={{ fontSize: 13, margin: 0, flex: 1 }}>
                Signed in as{' '}
                <span style={{ color: 'var(--text-2)', fontWeight: 600 }}>
                  {auth.userName || 'your FF Logs account'}
                </span>
                . Log data is fetched with your own account&apos;s access.
              </p>
              <button className="btn" onClick={signOut} disabled={signingOut}>
                <LogOut size={13} style={{ marginRight: 6 }} />
                {signingOut ? 'Signing out…' : 'Sign out'}
              </button>
            </div>
          )}
          {auth?.mode === 'client_credentials' && (
            <div className="row" style={{ gap: 12, alignItems: 'center' }}>
              <p className="mut" style={{ fontSize: 13, margin: 0, flex: 1 }}>
                {import.meta.env.DEV ? (
                  <>
                    Using developer API credentials from{' '}
                    <span className="mono">~/.fflogs_efficiency_analyzer/config.json</span>{' '}
                    (<span className="mono">client_id</span>/<span className="mono">client_secret</span>).
                  </>
                ) : (
                  'Not signed in — using a local API configuration.'
                )}
              </p>
              <button className="btn" onClick={onRequestSignIn}>
                Sign in with FF Logs
              </button>
            </div>
          )}
          {auth?.mode === 'none' && (
            <p className="mut" style={{ fontSize: 13, margin: 0 }}>
              Not signed in — the sign-in prompt will appear automatically.
            </p>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <DownloadCloud size={14} />
          <h2>Updates</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            downloaded from GitHub Releases
          </span>
        </div>
        <div className="card-body">
          <div className="row" style={{ gap: 12, alignItems: 'center' }}>
            <p className="mut" style={{ fontSize: 13, margin: 0, flex: 1 }}>
              {`Version ${APP_VERSION} installed. `}
              {updater.phase === 'available' && `Version ${updater.version} is available.`}
              {updater.phase === 'downloading' &&
                `Downloading update…${updater.progressPct != null ? ` ${Math.round(updater.progressPct)}%` : ''}`}
              {updater.phase === 'installing' && 'Installing — the app will restart.'}
              {updater.phase === 'error' && `Update failed: ${updater.error}`}
              {updater.phase === 'idle' &&
                (updater.checked ? 'You are on the latest version.' : 'Updates are checked on launch.')}
            </p>
            {updater.phase === 'available' ? (
              <button className="btn primary" onClick={() => void installUpdate()}>
                Install update
              </button>
            ) : (
              <button
                className="btn"
                onClick={() => void recheck()}
                disabled={checking || updater.phase === 'downloading' || updater.phase === 'installing'}
              >
                {checking ? 'Checking…' : 'Check for updates'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
