// Research — browse an encounter's top-10 ranked players for a job and load
// one into the normal analysis flow (dashboard / timeline / cast counts), with
// the ranked player as the analyzed subject.
//
// Character-independent, like the Kill Time Theorizer: the catalog drives the
// job/encounter pickers, and the rankings come from the new list_rankings
// sidecar request (the same cached blob the "Top 10" refs warm reads, so the
// list and the reference lanes are literally the same ten players).

import { useEffect, useState } from 'react';
import { Medal, Swords, Target, Trophy } from 'lucide-react';
import { jobColor, jobIcon, isJobPending, PENDING_JOB_TIP } from '../components/jobs';
import { fmtClock, fmtNum } from '../format';
import { sidecar } from '../sidecar';
import { refsWarmer } from '../state/refsPrefetch';
import type { Catalog, RankingEntry } from '../sidecar/contract';
import type { AppState } from '../state/appState';

type Props = {
  /** Optional defaults from the app's current selection — the page works
   *  without them (no character/analysis required). */
  defaultJob?: string;
  defaultEncounterId?: number;
  /** App.runAnalysis — loading a row hands it the ranked pull as a snapshot
   *  (report/fight + playerName) and the app flips to the loading dashboard. */
  onRunAnalysis: (snapshot: Partial<AppState>) => void;
};

export const ResearchView = ({ defaultJob, defaultEncounterId, onRunAnalysis }: Props) => {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [job, setJob] = useState<string>(defaultJob ?? '');
  const [encounterId, setEncounterId] = useState<number>(defaultEncounterId ?? 0);
  // Rankings (and their fetch error) tagged with the (job, encounter) combo that
  // produced them, so switching selections shows "loading" instead of a stale
  // list — same pattern as the theorizer's refAvg. No reset-on-change needed.
  const [rows, setRows] = useState<{ key: string; list: RankingEntry[] } | null>(null);
  const [fetchError, setFetchError] = useState<{ key: string; msg: string } | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Catalog drives the job + encounter pickers (no character needed). Once it
  // lands, snap the job/encounter to valid choices (keeping passed defaults).
  useEffect(() => {
    let alive = true;
    sidecar
      .getCatalog()
      .then((c) => {
        if (!alive) return;
        setCatalog(c);
        setJob((j) =>
          j && c.supportedJobs.includes(j) && !isJobPending(j)
            ? j
            : c.supportedJobs.find((x) => !isJobPending(x)) ?? '',
        );
        setEncounterId((e) =>
          e && c.encounters.some((x) => x.id === e) ? e : c.encounters[0]?.id ?? 0,
        );
      })
      .catch(() => setCatalogError('Could not load the job / encounter catalog.'));
    return () => {
      alive = false;
    };
  }, []);

  // Warm this (job, encounter)'s reference set ahead, silently (no popup), so
  // a Load while browsing is usually instant. The blocking path only engages
  // on Load if the warm hasn't finished by then.
  useEffect(() => {
    if (!job || !encounterId) return;
    void refsWarmer.ensureJob(job, encounterId, false);
  }, [job, encounterId]);

  // The top-10 list for the current selection. Cheap after the warm above —
  // both read the same session-cached rankings blob.
  useEffect(() => {
    if (!job || !encounterId) return;
    const key = `${job}|${encounterId}`;
    let alive = true;
    sidecar
      .listRankings(job, encounterId)
      .then((r) => {
        if (alive) setRows({ key, list: r });
      })
      .catch((e) => {
        if (alive) {
          setFetchError({
            key,
            msg: `Could not load rankings: ${e instanceof Error ? e.message : String(e)}`,
          });
        }
      });
    return () => {
      alive = false;
    };
  }, [job, encounterId]);

  const encounters = catalog?.encounters ?? [];
  const jobs = catalog?.supportedJobs ?? [];
  const encounterName = encounters.find((e) => e.id === encounterId)?.name ?? '';
  const comboKey = `${job}|${encounterId}`;
  const list = rows && rows.key === comboKey ? rows.list : null;
  const error = catalogError ?? (fetchError && fetchError.key === comboKey ? fetchError.msg : null);
  const loading = list === null && error === null;

  const load = async (r: RankingEntry) => {
    if (busy) return;
    setBusy(true);
    try {
      // Gate on warm refs (blocking popup if cold): a run that piggybacks on an
      // in-flight background warm gets no progress events of its own and can
      // trip the client's inactivity timeout.
      await refsWarmer.ensureJob(job, encounterId);
    } finally {
      setBusy(false);
    }
    onRunAnalysis({
      job,
      encounter: encounterName,
      encounterId,
      pullId: `#${r.rank} ${r.name}${r.durationMs ? ` — ${fmtClock(r.durationMs / 1000)}` : ''}`,
      pullReportCode: r.reportCode,
      pullFightId: r.fightId,
      refsBucket: 'Top 10',
      playerName: r.name,
      pullsLoaded: false,
    });
  };

  return (
    <div className="content narrow">
      <div className="card">
        <div className="card-head">
          <Trophy size={14} />
          <h2>Research</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            Study the top parses
          </span>
        </div>
        <div className="card-body">
          <p className="mut" style={{ fontSize: 12.5, margin: '0 0 14px' }}>
            Pick a job and encounter to browse its top-ranked players, then load
            one — the full analysis (dashboard, timeline, cast counts) runs on
            their pull, exactly as it does for your own. No character required.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <span className="field-label">
                <Swords size={12} /> Job
              </span>
              <div className="job-grid">
                {jobs.length === 0 ? (
                  <span className="mut" style={{ fontSize: 12 }}>Loading jobs…</span>
                ) : (
                  jobs.map((j) => {
                    const icon = jobIcon(j);
                    const pending = isJobPending(j);
                    return (
                      <button
                        key={j}
                        className={'btn job-tile ' + (job === j ? 'primary ' : '') + (pending ? 'pending' : '')}
                        disabled={pending}
                        title={pending ? PENDING_JOB_TIP : undefined}
                        onClick={() => setJob(j)}
                      >
                        {icon ? (
                          <img src={icon} alt="" width={22} height={22} draggable={false} className="job-tile-icon" />
                        ) : (
                          <span className="job-tile-icon" style={{ background: jobColor(j) }} />
                        )}
                        <span className="job-tile-label">{j}</span>
                      </button>
                    );
                  })
                )}
              </div>
            </div>
            <div>
              <span className="field-label">
                <Target size={12} /> Encounter
              </span>
              <select
                className="select"
                style={{ maxWidth: 420 }}
                value={encounterId}
                onChange={(e) => setEncounterId(Number(e.target.value))}
              >
                {encounters.length === 0 && <option value={0}>Loading…</option>}
                {encounters.map((e) => (
                  <option key={e.id} value={e.id}>{e.name}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <Medal size={14} />
          <h2>Top 10 — {job || '…'}</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            click a player to load their pull
          </span>
        </div>
        <div className="card-body" style={{ padding: 0 }}>
          {error ? (
            <div className="mut" style={{ padding: 14, fontSize: 12, color: 'var(--bad)' }}>
              {error}
            </div>
          ) : loading || list === null ? (
            <div className="mut" style={{ padding: 14, fontSize: 12 }}>
              Loading top parses…
            </div>
          ) : list.length === 0 ? (
            <div className="mut" style={{ padding: 14, fontSize: 12 }}>
              No rankings found for this job and encounter.
            </div>
          ) : (
            list.map((r) => (
              <button
                key={`${r.reportCode}:${r.fightId}:${r.rank}`}
                className="recent-pull"
                disabled={busy}
                onClick={() => void load(r)}
              >
                <span
                  className="mut"
                  style={{ width: 26, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
                >
                  #{r.rank}
                </span>
                <div style={{ flex: 1, textAlign: 'left', minWidth: 0 }}>
                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {r.name}
                  </div>
                  <div className="mut" style={{ fontSize: 11, marginTop: 2 }}>
                    {[r.server, r.durationMs ? `kill ${fmtClock(r.durationMs / 1000)}` : null]
                      .filter(Boolean)
                      .join(' · ')}
                  </div>
                </div>
                {r.amount != null && (
                  <span className="tag accent">
                    <span className="pip" />
                    {fmtNum(r.amount, 0)} rdps
                  </span>
                )}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
