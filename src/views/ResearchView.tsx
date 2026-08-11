// Top Pulls — browse an encounter's top-10 ranked players for a job and load
// one into the normal analysis flow (dashboard / timeline / cast counts), with
// the ranked player as the analyzed subject.
//
// Character-independent, like the Kill Time Theorizer: the catalog drives the
// setup panel (category tabs + encounter chips, role-grouped job rail), and
// the rankings come from the list_rankings sidecar request (the same cached
// blob the "Top 10" refs warm reads, so the list and the reference lanes are
// literally the same ten players).

import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { EncounterPicker } from '../components/EncounterPicker';
import { JobTile } from '../components/JobTile';
import { groupJobsByRole } from '../components/jobs';
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

/** Median kill time (ms) of the visible rows; null when none carry one. */
const medianKillMs = (list: RankingEntry[]): number | null => {
  const ds = list
    .map((r) => r.durationMs)
    .filter((d): d is number => d != null)
    .sort((a, b) => a - b);
  if (ds.length === 0) return null;
  const mid = Math.floor(ds.length / 2);
  return ds.length % 2 ? ds[mid] : (ds[mid - 1] + ds[mid]) / 2;
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
  const [refreshing, setRefreshing] = useState(false);

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
          j && c.supportedJobs.includes(j) ? j : c.supportedJobs[0] ?? '',
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

  // Refetch past the session + disk caches. The current list stays mounted
  // (the spinning button is the feedback); the combo-key discipline discards
  // the response if the selection changed mid-flight anyway.
  const refresh = async () => {
    if (refreshing || !job || !encounterId) return;
    const key = comboKey;
    setRefreshing(true);
    try {
      const fresh = await sidecar.listRankings(job, encounterId, true);
      setRows({ key, list: fresh });
      setFetchError((fe) => (fe && fe.key === key ? null : fe));
    } catch (e) {
      setFetchError({
        key,
        msg: `Could not load rankings: ${e instanceof Error ? e.message : String(e)}`,
      });
    } finally {
      setRefreshing(false);
    }
  };

  const roleGroups = groupJobsByRole(jobs);
  const meta = [job, encounterName, 'top 10 by rdps'].filter(Boolean).join(' · ');
  const medMs = list ? medianKillMs(list) : null;
  const amounts = (list ?? [])
    .map((r) => r.amount)
    .filter((a): a is number => a != null);
  const amtMin = amounts.length ? Math.min(...amounts) : 0;
  const amtMax = amounts.length ? Math.max(...amounts) : 0;
  // Relative bar across the visible ten: the worst row still shows 42%.
  const barPct = (a: number) =>
    amtMax === amtMin ? 100 : 42 + (58 * (a - amtMin)) / (amtMax - amtMin);

  return (
    <div className="content wide">
      <div className="page-title-row">
        <div>
          <h1>Top pulls</h1>
          <p className="page-meta">{meta}</p>
        </div>
        <button
          className="btn"
          disabled={refreshing || busy || !job || !encounterId}
          onClick={() => void refresh()}
        >
          <RefreshCw size={13} className={refreshing ? 'spin' : undefined} />
          Refresh rankings
        </button>
      </div>

      <div className="setup-panel">
        <div className="setup-row">
          <span className="setup-label">Encounter</span>
          <div className="setup-controls">
            <EncounterPicker
              encounters={encounters}
              encounterId={encounterId}
              onPick={setEncounterId}
            />
          </div>
        </div>
        <div className="setup-row">
          <span className="setup-label">Job</span>
          <div className="setup-controls">
            {roleGroups.length === 0 ? (
              <span className="mut" style={{ fontSize: 12 }}>Loading jobs…</span>
            ) : (
              <div className="job-rail">
                {roleGroups.map((g) => (
                  <div className="job-rail-group" key={g.role}>
                    {g.jobs.map((j) =>
                      j === job ? (
                        <button className="job-pill" key={j} onClick={() => setJob(j)}>
                          <JobTile job={j} size={26} iconInset={2} />
                          <span>{j}</span>
                        </button>
                      ) : (
                        <button
                          className="job-cell"
                          key={j}
                          title={j}
                          onClick={() => setJob(j)}
                        >
                          <JobTile job={j} size={34} />
                        </button>
                      ),
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="rank-head">
        <span className="rank-head-title">Top 10</span>
        <span className="rank-head-sub">
          click a player to load their pull into the full analysis
        </span>
        {medMs != null && (
          <span className="rank-head-median">
            median kill {fmtClock(medMs / 1000)}
          </span>
        )}
      </div>

      <div className="rank-rows">
        {error ? (
          <div className="mut" style={{ padding: 14, fontSize: 12, color: 'var(--bad)' }}>
            {error}
          </div>
        ) : loading || list === null ? (
          Array.from({ length: 10 }, (_, i) => (
            <div className="rank-row skeleton" aria-hidden="true" key={i}>
              <span className="sk-line" style={{ width: 22 }} />
              <span>
                <span className="sk-line" style={{ width: 150, marginBottom: 7 }} />
                <span className="sk-line" style={{ width: 96 }} />
              </span>
              <span className="sk-line" />
              <span className="sk-line" style={{ height: 30, borderRadius: 8 }} />
            </div>
          ))
        ) : list.length === 0 ? (
          <div className="mut" style={{ padding: 14, fontSize: 12 }}>
            No rankings found for this job and encounter.
          </div>
        ) : (
          list.map((r) => (
            <div
              key={`${r.reportCode}:${r.fightId}:${r.rank}`}
              className={'rank-row' + (busy ? ' busy' : '')}
              role="button"
              tabIndex={0}
              onClick={() => void load(r)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void load(r);
                }
              }}
            >
              <span className="rank-num mono">#{r.rank}</span>
              <span style={{ minWidth: 0 }}>
                <span className="rank-name">{r.name}</span>
                <span className="rank-sub">
                  {[r.server, r.durationMs ? `kill ${fmtClock(r.durationMs / 1000)}` : null]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              {r.amount != null ? (
                <span className="rank-metric">
                  <span className="rank-metric-head">
                    <span>rdps</span>
                    <span className="rank-metric-val mono">{fmtNum(r.amount, 0)}</span>
                  </span>
                  <span className="rank-metric-bar">
                    <span
                      className="rank-metric-fill"
                      style={{ width: `${barPct(r.amount)}%` }}
                    />
                  </span>
                </span>
              ) : (
                <span />
              )}
              <button
                className="btn"
                disabled={busy}
                onClick={(e) => {
                  e.stopPropagation();
                  void load(r);
                }}
              >
                Analyze
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
