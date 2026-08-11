// Version history — every shipped release, newest first. Standalone view: no
// character, no analysis, no sidecar call. The notes are bundled with the build
// (src/data/changelog.json), so this works offline and always matches the
// version you're running.

import { ReleaseNotes } from '../components/ReleaseNotes';
import { APP_VERSION, CHANGELOG, fmtReleaseDate } from '../data/changelog';

export const VersionHistoryView = () => (
  <div className="content narrow">
    <div className="hero">
      <h1>Version history</h1>
      <p>
        What changed in each Meridian release, newest first. You are running
        v{APP_VERSION}.
      </p>
    </div>

    {CHANGELOG.map((r) => (
      <div className="card" key={r.version} style={{ marginTop: 14 }}>
        <div className="card-head">
          <h2>v{r.version}</h2>
          {r.version === APP_VERSION && <span className="tag accent">Current</span>}
          <span className="sub" style={{ marginLeft: 'auto' }}>
            {fmtReleaseDate(r.date)}
          </span>
        </div>
        <div className="card-body">
          <ReleaseNotes release={r} />
        </div>
      </div>
    ))}
  </div>
);
