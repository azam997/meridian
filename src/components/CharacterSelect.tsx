// Reusable character selection flow: the characters claimed on the signed-in
// FF Logs account (via the sidecar's list_user_characters) plus a manual
// name/server search fallback. No chrome of its own — SetupView embeds it
// inline as the Encounter page's first step, and CharacterPickerModal wraps
// it for the sidebar's change-character flow.

import { useEffect, useRef, useState } from 'react';
import { ArrowLeftRight, Check, ExternalLink, Search } from 'lucide-react';
import { CharacterAvatar } from './CharacterAvatar';
import { sidecar } from '../sidecar';
import { clearUserCharactersCache, listUserCharactersCached } from '../sidecar/sessionCache';
import { toPersistedCharacter, type Region, type PersistedCharacter } from '../state/appState';
import { openUrl } from '../tauri/openUrl';

export type CharacterPicked = {
  name: string;
  server: string;
  region: Region;
  lodestoneId: number;
  logsCount: number;
  dataCenter?: string;
  avatarUrl?: string;
  /** True when the pick is "this session only" — manual searches. App.tsx
   *  uses this to bypass localStorage persistence. */
  transient?: boolean;
  /** When the pick comes from the FFLogs character list, the full list is
   *  included so App.tsx can persist it for in-app quick-switching later.
   *  Absent from manual mode picks. */
  fflogsCharacters?: PersistedCharacter[];
};

type Props = {
  /** Immediately pick when the account has exactly one claimed character
   *  (fresh-login convenience). Off for inline/refresh contexts where
   *  auto-picking would fight an explicit "clear"/"switch" action. */
  autoAdvanceSingle?: boolean;
  /** Highlight this character as the currently-loaded one (the Encounter
   *  page keeps the selector visible with the active pick marked). */
  activeLodestoneId?: number;
  /** The currently-loaded character's identity. When it isn't one of the
   *  signed-in account's characters (a manual-search pick), the account list
   *  can't reflect it, so we surface it as a "currently loaded" banner — else
   *  a manual pick looks like it silently reverted to the account character. */
  activeCharacter?: {
    lodestoneId: number;
    name: string;
    server: string;
    dataCenter?: string;
    avatarUrl?: string;
  };
  /** Prefills for the manual form (sensible defaults for an alt). */
  initialServer?: string;
  initialRegion?: Region;
  onPicked: (c: CharacterPicked) => void;
};

type Flow = 'list' | 'manual';
type ListStage =
  | { kind: 'loading' }
  | { kind: 'selecting'; characters: PersistedCharacter[] }
  /** Signed in, but no characters claimed on the FFLogs account (also the
   *  legacy client-credentials mode, which has no user to list). */
  | { kind: 'empty' }
  | { kind: 'error'; message: string };

export const CharacterSelect = ({
  autoAdvanceSingle,
  activeLodestoneId,
  activeCharacter,
  initialServer,
  initialRegion,
  onPicked,
}: Props) => {
  const [flow, setFlow] = useState<Flow>('list');
  const [stage, setStage] = useState<ListStage>({ kind: 'loading' });
  // Guards state updates from a fetch that resolves after unmount. Armed
  // inside the effect so StrictMode's mount→cleanup→mount cycle re-arms it.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const doFetch = async (autoAdvance: boolean) => {
    try {
      const r = await listUserCharactersCached();
      if (!alive.current) return;
      const list = r.characters.map(toPersistedCharacter);
      if (list.length === 0) {
        setStage({ kind: 'empty' });
      } else if (autoAdvance && list.length === 1) {
        onPicked({ ...list[0], logsCount: 0, fflogsCharacters: list });
      } else {
        setStage({ kind: 'selecting', characters: list });
      }
    } catch (e) {
      if (!alive.current) return;
      const msg = e instanceof Error ? e.message : String(e);
      setStage({ kind: 'error', message: msg });
    }
  };

  // Manual refresh: bust the session cache so a just-claimed character shows.
  const refetch = (autoAdvance: boolean) => {
    clearUserCharactersCache();
    setStage({ kind: 'loading' });
    void doFetch(autoAdvance);
  };

  // Fetch on mount. Every setStage inside doFetch fires after its await —
  // callback territory, which the set-state-in-effect rule allows but its
  // static trace can't see.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void doFetch(!!autoAdvanceSingle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (flow === 'manual') {
    return (
      <ManualSearch
        initialServer={initialServer}
        initialRegion={initialRegion}
        onPicked={onPicked}
        onBackToList={() => setFlow('list')}
      />
    );
  }

  // A manually-searched character isn't on the signed-in account, so it never
  // appears in the fetched list. Surface it explicitly so the pick doesn't look
  // like it reverted to the account character.
  const activeInList =
    stage.kind === 'selecting'
    && stage.characters.some((c) => c.lodestoneId === activeCharacter?.lodestoneId);
  const showActiveBanner = !!activeCharacter && !activeInList && flow === 'list';

  return (
    <>
      {showActiveBanner && activeCharacter && (
        <div className="char-list" style={{ marginBottom: 12 }}>
          <div className="char-list-item active">
            <CharacterAvatar name={activeCharacter.name} avatarUrl={activeCharacter.avatarUrl} size={40} />
            <div className="char-list-item-info">
              <div className="char-list-item-name">{activeCharacter.name}</div>
              <div className="char-list-item-meta">
                {activeCharacter.server}
                {activeCharacter.dataCenter ? ` · ${activeCharacter.dataCenter}` : ''}
                {' · currently loaded'}
              </div>
            </div>
            <Check size={16} style={{ color: 'var(--accent)', flexShrink: 0 }} />
          </div>
        </div>
      )}

      {stage.kind === 'loading' && (
        <p className="mut" style={{ fontSize: 13, margin: 0 }}>
          Loading your FF Logs characters…
        </p>
      )}

      {stage.kind === 'selecting' && (
        <>
          <p className="mut" style={{ fontSize: 13, marginTop: 0 }}>
            These are the characters claimed on your FF Logs account.
          </p>
          <div className="char-list">
            {stage.characters.map((c) => {
              const active = c.lodestoneId === activeLodestoneId;
              return (
                <button
                  key={c.lodestoneId}
                  className={'char-list-item' + (active ? ' active' : '')}
                  onClick={() =>
                    onPicked({ ...c, logsCount: 0, fflogsCharacters: stage.characters })
                  }
                >
                  <CharacterAvatar name={c.name} avatarUrl={c.avatarUrl} size={40} />
                  <div className="char-list-item-info">
                    <div className="char-list-item-name">{c.name}</div>
                    <div className="char-list-item-meta">
                      {c.server}
                      {c.dataCenter ? ` · ${c.dataCenter}` : ''}
                    </div>
                  </div>
                  {active && <Check size={16} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
                </button>
              );
            })}
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn ghost sm" onClick={() => setFlow('manual')}>
              <Search size={13} />
              Someone else? Search manually
            </button>
          </div>
        </>
      )}

      {stage.kind === 'empty' && (
        <>
          <p className="mut" style={{ fontSize: 13, marginTop: 0 }}>
            Your FF Logs account has no claimed characters. Claim yours on{' '}
            <button
              className="link-btn"
              onClick={() => openUrl('https://www.fflogs.com/profile').catch(() => {})}
            >
              your FF Logs profile{' '}
              <ExternalLink size={11} style={{ verticalAlign: -1 }} />
            </button>{' '}
            (then refresh here), or search for the character manually.
          </p>
          <div className="row" style={{ marginTop: 14, gap: 10 }}>
            <button className="btn primary" onClick={() => refetch(false)}>
              <ArrowLeftRight size={14} />
              Refresh
            </button>
            <button className="btn ghost" onClick={() => setFlow('manual')}>
              <Search size={14} />
              Search manually
            </button>
          </div>
        </>
      )}

      {stage.kind === 'error' && (
        <>
          <div
            className="card"
            style={{ borderColor: 'var(--bad)', background: 'var(--bad-soft)' }}
          >
            <div className="card-body" style={{ padding: '10px 14px', fontSize: 13 }}>
              <strong style={{ color: 'var(--bad)' }}>
                Couldn&apos;t load your characters:{' '}
              </strong>
              {stage.message}
            </div>
          </div>
          <div className="row" style={{ marginTop: 14, gap: 10 }}>
            <button className="btn primary" onClick={() => refetch(false)}>
              Try again
            </button>
            <button className="btn ghost" onClick={() => setFlow('manual')}>
              Search manually
            </button>
          </div>
        </>
      )}
    </>
  );
};

// ---------------------------------------------------------------------------
// Manual search — always transient (App.tsx bypasses persistence) so the
// select re-prompts on every launch when a manual character is loaded.
// ---------------------------------------------------------------------------

const ManualSearch = ({
  initialServer,
  initialRegion,
  onPicked,
  onBackToList,
}: {
  initialServer?: string;
  initialRegion?: Region;
  onPicked: (c: CharacterPicked) => void;
  onBackToList: () => void;
}) => {
  // Start the name blank rather than pre-filling the current character: this
  // form is reached to *change* characters, and a pre-filled (autofocused)
  // name invites an Enter that looks up — and re-picks — the very character
  // you're switching away from. Server/region keep their prefill as sensible
  // defaults for an alt on the same account.
  const [name, setName] = useState('');
  const [server, setServer] = useState(initialServer ?? '');
  const [region, setRegion] = useState<Region>(initialRegion ?? 'NA');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Latest field values, read inside the async lookup so a result that lands
  // after the user kept typing is dropped instead of picking a stale character
  // mid-typing (the "modal vanishes while I type" bug).
  const nameRef = useRef(name);
  const serverRef = useRef(server);
  // Mirror the latest values into the refs after commit (writing a ref during
  // render is disallowed); the async lookup only reads them post-await, by which
  // point any typing-triggered re-render has flushed this effect.
  useEffect(() => {
    nameRef.current = name;
    serverRef.current = server;
  }, [name, server]);

  const submit = async () => {
    const qName = name.trim();
    const qServer = server.trim();
    if (!qName || !qServer) {
      setError('Enter both a character name and a server.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await sidecar.lookupCharacter(qName, qServer, region, '');
      // Drop a stale lookup: if the fields changed while the request was in
      // flight, the user is no longer asking for this character.
      if (qName !== nameRef.current.trim() || qServer !== serverRef.current.trim()) {
        return;
      }
      if (r.found && r.lodestoneId) {
        onPicked({
          name: qName,
          server: qServer,
          region,
          lodestoneId: r.lodestoneId,
          logsCount: r.logsCount ?? 0,
          transient: true,
        });
      } else {
        setError(`Character "${qName}" not found on ${qServer} (${region}).`);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error('[lookupCharacter]', e);
      setError(`Lookup failed: ${msg}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="row gap-lg" style={{ flexWrap: 'wrap' }}>
        <div style={{ flex: '2 1 220px' }}>
          <label className="field-label">Character name</label>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Firstname Lastname"
            autoFocus
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
          />
        </div>
        <div style={{ flex: '1 1 140px' }}>
          <label className="field-label">Server</label>
          <input
            className="input"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="Hyperion"
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
          />
        </div>
        <div style={{ flex: '0 0 100px' }}>
          <label className="field-label">Region</label>
          <select
            className="select"
            value={region}
            onChange={(e) => setRegion(e.target.value as Region)}
          >
            <option>NA</option>
            <option>EU</option>
            <option>JP</option>
            <option>OC</option>
          </select>
        </div>
      </div>
      {error && (
        <div
          className="card"
          style={{
            marginTop: 12,
            borderColor: 'var(--bad)',
            background: 'var(--bad-soft)',
          }}
        >
          <div className="card-body" style={{ padding: '8px 12px', fontSize: 13 }}>
            <strong style={{ color: 'var(--bad)' }}>Error: </strong>
            {error}
          </div>
        </div>
      )}
      <div className="row" style={{ marginTop: 14, gap: 10 }}>
        <button className="btn ghost sm" onClick={onBackToList}>
          My FF Logs characters
        </button>
        <span style={{ flex: 1 }} />
        <button className="btn primary" onClick={submit} disabled={busy}>
          <Search size={14} />
          {busy ? 'Looking up…' : 'Look up character'}
        </button>
      </div>
    </>
  );
};
