// One renderer for a release's notes, shared by the "What's new" popup and the
// Version history tab so the two can never drift apart.
//
// `**bold**` is the only inline markup the changelog supports — deliberately
// not a markdown dependency (the app ships offline and keeps its deps thin).

import type { ReactNode } from 'react';

import type { Release } from '../data/changelog';

/** Split on **bold** spans: with a capturing group, split() puts the captures
 *  at the odd indices. */
const inline = (text: string): ReactNode[] =>
  text
    .split(/\*\*(.+?)\*\*/g)
    .map((part, i) => (i % 2 === 1 ? <strong key={i}>{part}</strong> : part));

export const ReleaseNotes = ({ release }: { release: Release }) => (
  <div className="rel-notes">
    <p className="lead">{inline(release.summary)}</p>
    {release.sections?.map((s, i) => (
      <div className="rel-section" key={i}>
        {s.heading && <h3>{s.heading}</h3>}
        {s.body && <p>{inline(s.body)}</p>}
        {s.items && s.items.length > 0 && (
          <ul>
            {s.items.map((item, j) => (
              <li key={j}>{inline(item)}</li>
            ))}
          </ul>
        )}
      </div>
    ))}
  </div>
);
