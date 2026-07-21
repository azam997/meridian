// Change-character modal (opened from the sidebar character tile). Thin
// chrome around the shared CharacterSelect flow, plus the logged-in overview
// (quick-switch alternates) and the success beat. The Encounter page embeds
// CharacterSelect directly for the no-character-yet case — shared flow, one
// implementation.

import { useState } from 'react';
import { ArrowLeftRight, Check, LogOut, X } from 'lucide-react';
import { CharacterAvatar } from './CharacterAvatar';
import { CharacterSelect, type CharacterPicked } from './CharacterSelect';
import type { Region, PersistedCharacter } from '../state/appState';

// Re-exported so App.tsx keeps one import site for the picked shape.
export type { CharacterPicked };

type Props = {
  open: boolean;
  /** Pre-fill from current state when the user is changing characters. */
  initialName?: string;
  initialServer?: string;
  initialRegion?: Region;
  initialDataCenter?: string;
  initialAvatarUrl?: string;
  /** Claimed-character list from the last FFLogs fetch, persisted by App.
   *  The logged-in panel renders alternates as a quick-switch picker. */
  fflogsCharacters?: PersistedCharacter[];
  /** When true, the backdrop / close button are hidden — used on first
   *  launch where the user has no saved character and must pick one. */
  required?: boolean;
  onClose: () => void;
  onPicked: (c: CharacterPicked) => void;
  /** Clears the saved character identity and returns the picker to its
   *  "no character" state. Called from the logged-in entry view. */
  onLogout: () => void;
};

type Stage =
  /** Entry state when the user already has a saved character — they can
   *  switch (open the select flow) or clear the character. */
  | { kind: 'logged_in' }
  | { kind: 'select'; autoAdvance: boolean }
  /** Brief celebration screen — auto-advances to onPicked after a beat. */
  | { kind: 'success'; characterName: string };

export const CharacterPickerModal = (props: Props) => {
  const {
    open, required, onClose, onPicked, onLogout,
    initialName, initialServer, initialRegion, initialDataCenter,
    initialAvatarUrl, fflogsCharacters,
  } = props;
  // Opening the modal while a character is already loaded lands on the
  // logged-in overview; a fresh open goes straight to selection (with the
  // single-character auto-advance, since nothing is loaded yet).
  const [stage, setStage] = useState<Stage>(() =>
    initialName ? { kind: 'logged_in' } : { kind: 'select', autoAdvance: true }
  );

  if (!open) return null;

  // Celebration beat before handing off — gives the user a moment to
  // register "yes, this is me" before the modal disappears.
  const acceptWithSuccess = (c: CharacterPicked) => {
    setStage({ kind: 'success', characterName: c.name });
    window.setTimeout(() => onPicked(c), 1200);
  };

  // Quick-switch to an alternate from the persisted list — a fast utility
  // action, no celebration.
  const switchToPersisted = (p: PersistedCharacter) => {
    onPicked({ ...p, logsCount: 0, fflogsCharacters });
  };

  return (
    // Closing without a character is allowed — the app gates its nav so the
    // user can't break things, and the sidebar character tile is the way
    // back to re-open the picker.
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>
            {stage.kind === 'logged_in'
              ? 'Your character'
              : stage.kind === 'success'
                ? 'Character picked'
                : required
                  ? 'Welcome — pick your character'
                  : 'Pick a character'}
          </h2>
          {stage.kind !== 'success' && (
            <button className="btn ghost sm" onClick={onClose} title="Close">
              <X size={14} />
            </button>
          )}
        </div>

        <div className="modal-body">
          {stage.kind === 'logged_in' && (
            <LoggedInPanel
              name={initialName!}
              server={initialServer}
              region={initialRegion}
              dataCenter={initialDataCenter}
              avatarUrl={initialAvatarUrl}
              fflogsCharacters={fflogsCharacters}
              activeLodestoneId={
                fflogsCharacters?.find((c) => c.name === initialName)?.lodestoneId
              }
              onSwitchPersisted={switchToPersisted}
              // No auto-advance from here: a single-character list would
              // otherwise instantly re-pick the character being switched
              // away from / cleared.
              onSwitch={() => setStage({ kind: 'select', autoAdvance: false })}
              onLogout={() => {
                onLogout();
                setStage({ kind: 'select', autoAdvance: false });
              }}
            />
          )}

          {stage.kind === 'select' && (
            <CharacterSelect
              autoAdvanceSingle={stage.autoAdvance}
              initialServer={initialServer}
              initialRegion={initialRegion}
              onPicked={acceptWithSuccess}
            />
          )}

          {stage.kind === 'success' && (
            <SuccessPanel characterName={stage.characterName} />
          )}
        </div>
      </div>
    </div>
  );
};

const LoggedInPanel = ({
  name,
  server,
  region,
  dataCenter,
  avatarUrl,
  fflogsCharacters,
  activeLodestoneId,
  onSwitchPersisted,
  onSwitch,
  onLogout,
}: {
  name: string;
  server?: string;
  region?: Region;
  dataCenter?: string;
  avatarUrl?: string;
  fflogsCharacters?: PersistedCharacter[];
  activeLodestoneId?: number;
  onSwitchPersisted: (p: PersistedCharacter) => void;
  onSwitch: () => void;
  onLogout: () => void;
}) => {
  const alternates = (fflogsCharacters ?? []).filter(
    (c) => c.lodestoneId !== activeLodestoneId,
  );
  const hasAlternates = alternates.length > 0;
  return (
    <>
      <div className="logged-in-card">
        <CharacterAvatar name={name} avatarUrl={avatarUrl} size={44} />
        <div className="logged-in-info">
          <div className="logged-in-name">{name}</div>
          <div className="logged-in-meta">
            {server}
            {dataCenter ? ` · ${dataCenter}` : ''}
            {region ? ` · ${region}` : ''}
          </div>
        </div>
      </div>

      {hasAlternates && (
        <>
          <div
            className="field-label"
            style={{ marginTop: 16, marginBottom: 6 }}
          >
            Switch active character
          </div>
          <div className="char-list">
            {alternates.map((c) => (
              <button
                key={c.lodestoneId}
                className="char-list-item"
                onClick={() => onSwitchPersisted(c)}
              >
                <CharacterAvatar name={c.name} avatarUrl={c.avatarUrl} size={40} />
                <div className="char-list-item-info">
                  <div className="char-list-item-name">{c.name}</div>
                  <div className="char-list-item-meta">
                    {c.server}
                    {c.dataCenter ? ` · ${c.dataCenter}` : ''}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </>
      )}

      <div className="row" style={{ marginTop: 14, gap: 10 }}>
        <button className="btn primary" onClick={onSwitch}>
          <ArrowLeftRight size={14} />
          Switch character
        </button>
        <button className="btn ghost" onClick={onLogout}>
          <LogOut size={14} />
          Clear character
        </button>
      </div>
    </>
  );
};

const SuccessPanel = ({ characterName }: { characterName: string }) => (
  <div className="success-panel">
    <div className="success-check">
      <Check size={32} strokeWidth={3} />
    </div>
    <div className="success-title">Welcome, {characterName}!</div>
    <div className="success-sub mut">Loading your pulls…</div>
  </div>
);
