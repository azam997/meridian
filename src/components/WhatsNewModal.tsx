// "What's new" popup — shown once per version, the first launch after an
// update. App owns the gating (see state/whatsNew.ts); this is presentation.
//
// Follows the CharacterPickerModal vocabulary (.modal-backdrop / .modal), and
// is the first modal to use the .modal-foot that main.css has always had.

import { Sparkles, X } from 'lucide-react';

import { fmtReleaseDate, type Release } from '../data/changelog';
import { ReleaseNotes } from './ReleaseNotes';

type Props = {
  /** Releases to show, newest first. Never empty when rendered. */
  releases: Release[];
  onClose: () => void;
  onViewHistory: () => void;
};

export const WhatsNewModal = ({ releases, onClose, onViewHistory }: Props) => {
  const multi = releases.length > 1;
  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
      <div className="modal whats-new" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>
            <Sparkles size={14} style={{ marginRight: 8, verticalAlign: '-2px' }} />
            {multi
              ? `What's new — ${releases.length} updates`
              : `What's new in v${releases[0].version}`}
          </h2>
          <button className="btn ghost sm" onClick={onClose} title="Close">
            <X size={14} />
          </button>
        </div>

        <div className="modal-body">
          {releases.map((r, i) => (
            <div key={r.version} className={i > 0 ? 'rel-stacked' : undefined}>
              {multi && (
                <div className="rel-version-head">
                  <span className="mono">v{r.version}</span>
                  <span className="mut-2">{fmtReleaseDate(r.date)}</span>
                </div>
              )}
              <ReleaseNotes release={r} />
            </div>
          ))}
        </div>

        <div className="modal-foot">
          <button className="btn ghost" onClick={onViewHistory}>
            Full history
          </button>
          <button className="btn primary" onClick={onClose}>
            Got it
          </button>
        </div>
      </div>
    </div>
  );
};
