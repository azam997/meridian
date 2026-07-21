// Blocking FFLogs sign-in modal. Shown by App whenever the sidecar reports
// auth mode 'none' — nothing in the app works without FFLogs API access, so
// this gates every view (Setup, Research, Theorizer alike). Reuses the
// .refs-modal-* overlay vocabulary from RefsLoadingModal.
//
// The sign-in itself is the sidecar's PKCE flow (each user signs into their
// own free FF Logs account in the browser — the app never sees a password
// and ships no API secret).

import { useEffect, useRef, useState } from 'react';
import { ExternalLink, LogIn } from 'lucide-react';

import { startSignIn, type SignInHandle } from '../auth/fflogsAuth';
import { openUrl } from '../tauri/openUrl';

type Phase = 'idle' | 'waiting' | 'error';

type Props = {
  /** True when we got here via a mid-session 'auth_expired' (vs first run). */
  expired?: boolean;
  onSignedIn: (userName: string) => void;
  /** Present only when the modal was opened voluntarily (e.g. from Settings
   *  while dev client-credentials still work) — renders a dismiss button.
   *  Absent on the hard gate (mode 'none'), which stays blocking. */
  onDismiss?: () => void;
};

/** Mount/unmount to show/hide (App renders it conditionally) — unmounting
 *  both abandons any in-flight attempt and resets the internal state. */
export const FFLogsSignInModal = ({ expired, onSignedIn, onDismiss }: Props) => {
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string>('');
  const [authorizeUrl, setAuthorizeUrl] = useState<string>('');
  const handleRef = useRef<SignInHandle | null>(null);

  // Abandon an in-flight attempt on unmount.
  useEffect(() => () => handleRef.current?.cancel(), []);

  const begin = () => {
    setPhase('waiting');
    setError('');
    setAuthorizeUrl('');
    handleRef.current = startSignIn({
      onWaiting: (url) => setAuthorizeUrl(url),
      onDone: (userName) => {
        handleRef.current = null;
        onSignedIn(userName);
      },
      onExpired: () => {
        handleRef.current = null;
        setPhase('error');
        setError('The sign-in timed out. Try again.');
      },
      onError: (message) => {
        handleRef.current = null;
        setPhase('error');
        setError(message);
      },
    });
  };

  const cancelWaiting = () => {
    handleRef.current?.cancel();
    handleRef.current = null;
    setAuthorizeUrl('');
    setPhase('idle');
  };

  return (
    <div className="refs-modal-overlay" role="dialog" aria-modal="true">
      <div
        className="refs-modal"
        style={{ maxWidth: 440, textAlign: 'center', padding: '36px 32px' }}
      >
        <img
          src="/meridian.svg"
          alt=""
          width={54}
          height={54}
          draggable={false}
          style={{ display: 'block', margin: '0 auto 14px' }}
        />
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>
          {expired ? 'FF Logs session expired' : 'Sign in to FF Logs'}
        </div>
        <p className="mut" style={{ fontSize: 13, marginTop: 0, marginBottom: 0 }}>
          {expired
            ? 'Your FF Logs sign-in is no longer valid — sign in again to keep analyzing.'
            : 'The analyzer reads logs through your own free FF Logs account. ' +
              'Your browser will open to authorize.'}
        </p>

        {phase === 'waiting' && (
          <div style={{ marginTop: 16 }}>
            <div className="label" style={{ fontSize: 13 }}>
              Waiting for the browser…
            </div>
            <div className="bar main-bar" style={{ marginTop: 10 }}>
              <div className="fill indeterminate" style={{ width: '40%' }} />
            </div>
            {authorizeUrl && (
              <p className="mut" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
                Browser didn’t open?{' '}
                <button
                  className="link-btn"
                  onClick={() =>
                    void openUrl(authorizeUrl).catch(() =>
                      window.open(authorizeUrl, '_blank'))
                  }
                >
                  Open the sign-in page <ExternalLink size={11} style={{ verticalAlign: -1 }} />
                </button>
              </p>
            )}
          </div>
        )}

        {phase === 'error' && error && (
          <p style={{ color: 'var(--bad)', fontSize: 12, marginTop: 12, marginBottom: 0 }}>
            {error}
          </p>
        )}

        <div className="row" style={{ justifyContent: 'center', gap: 10, marginTop: 20 }}>
          {phase !== 'waiting' ? (
            <>
              <button className="btn primary" onClick={begin}>
                <LogIn size={14} />
                {phase === 'error' ? 'Try again' : 'Sign in with FF Logs'}
              </button>
              {onDismiss && (
                <button className="btn ghost" onClick={onDismiss}>
                  Not now
                </button>
              )}
            </>
          ) : (
            <button className="btn ghost" onClick={cancelWaiting}>
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
