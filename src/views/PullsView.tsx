// The Pulls screen — the app's entry point. One job-agnostic, chronological
// list of the character's tier pulls with Analyze on every row; character,
// job, encounter and difficulty are row properties (plus filters), not wizard
// steps. Data lives in state/pullsStore.ts (session singleton) so the sidebar
// badge and the filters survive navigation.

import { useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, ScanLine, Search, Sparkles, HeartPulse } from 'lucide-react';

import { ANALYZABLE_HEALERS, isHealer, jobIcon } from '../components/jobs';
import { CharacterSelect, type CharacterPicked } from '../components/CharacterSelect';
import type { AppState, RefsBucket } from '../state/appState';
import type { PullRow } from '../sidecar/contract';
import { pullsStore, usePulls, type PullsSort } from '../state/pullsStore';
import { refsWarmer } from '../state/refsPrefetch';
import { sidecar } from '../sidecar';
import {
  encounterShortName, extractReportCode, fmtTimeOfDay, groupRowsByDate,
  parseTone, rowMetaLine, rowPullLabel,
} from './pullsFormat';

const REFS_BUCKET: RefsBucket = 'Top 10';

const rowKey = (r: PullRow): string => `${r.reportCode}:${r.fightId}`;

/** "synced 2 min ago" (re-rendered by a ticker upstream). */
const syncedAgo = (ms: number, now: number): string => {
  const min = Math.max(0, Math.round((now - ms) / 60_000));
  if (min < 1) return 'synced just now';
  if (min < 60) return `synced ${min} min ago`;
  const h = Math.round(min / 60);
  return `synced ${h} h ago`;
};

type Props = {
  state: AppState;
  setState: (next: AppState) => void;
  onRunAnalysis: (snapshot: Partial<AppState>) => void;
  onPlanMitigation: (snapshot: Partial<AppState>) => void;
  externalError?: string | null;
  clearExternalError?: () => void;
  onCharacterPicked: (c: CharacterPicked) => void;
  onReportError?: (msg: string) => void;
};

export const PullsView = ({
  state, setState, onRunAnalysis, onPlanMitigation,
  externalError, clearExternalError, onCharacterPicked, onReportError,
}: Props) => {
  const pulls = usePulls();
  const { filters } = pulls;
  const [localError, setLocalError] = useState<string | null>(null);
  const [pasteBusy, setPasteBusy] = useState(false);
  const [scanBusy, setScanBusy] = useState<number | null>(null);
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  // 30s ticker so "synced N min ago" stays honest without re-fetching.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  // Load (or reuse) this character's list. Store no-ops on the same character.
  const { lodestoneId, characterName, server } = state;
  useEffect(() => {
    if (lodestoneId && characterName) {
      pullsStore.load(lodestoneId, characterName, server);
    }
  }, [lodestoneId, characterName, server]);

  // Ctrl/⌘-K focuses the search field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const categoryOf = useMemo(() => {
    const m = new Map<number, 'savage' | 'ultimate'>();
    for (const e of pulls.encounters) m.set(e.id, e.category ?? 'savage');
    return m;
  }, [pulls.encounters]);

  const encounterName = useMemo(() => {
    const m = new Map<number, string>();
    for (const e of pulls.encounters) m.set(e.id, encounterShortName(e.name));
    return m;
  }, [pulls.encounters]);

  // --- client-side filtering + sorting ---------------------------------------

  const visible = useMemo(() => {
    const q = filters.query.trim().toLowerCase();
    const rows = pulls.rows.filter((r) => {
      if (filters.jobFilter && r.job !== filters.jobFilter) return false;
      if (filters.categoryFilter !== 'all'
          && (categoryOf.get(r.encounterId) ?? 'savage') !== filters.categoryFilter) return false;
      if (filters.outcomeFilter === 'kills' && !r.kill) return false;
      if (filters.outcomeFilter === 'wipes' && r.kill) return false;
      if (q && !extractReportCode(q)) {
        const hay = `${encounterName.get(r.encounterId) ?? ''} ${r.job}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    if (filters.sort === 'oldest') rows.sort((a, b) => a.startTimeMs - b.startTimeMs);
    else if (filters.sort === 'parse') {
      rows.sort((a, b) => (b.parsePct ?? -1) - (a.parsePct ?? -1)
        || b.startTimeMs - a.startTimeMs);
    } else rows.sort((a, b) => b.startTimeMs - a.startTimeMs);
    return rows;
  }, [pulls.rows, filters, categoryOf, encounterName]);

  const groups = useMemo(
    () => (filters.sort === 'parse'
      ? [{ label: 'Best parse first', rows: visible }]
      : groupRowsByDate(visible, now)),
    [visible, filters.sort, now]);

  // The "primary" row: explicit selection if visible, else the first row.
  const primaryKey = selectedKey && visible.some((r) => rowKey(r) === selectedKey)
    ? selectedKey
    : visible.length ? rowKey(visible[0]) : null;

  // --- warm + speculative pre-analysis on hover/focus ------------------------

  // Hover intent (~150ms) → non-blocking refs warm for the row's (job, enc).
  // This replaces the old blocking job-confirm warm: there is no confirm step
  // left to hang a popup on.
  useEffect(() => {
    if (!hoverKey) return;
    const row = pulls.rows.find((r) => rowKey(r) === hoverKey);
    if (!row) return;
    const t = setTimeout(() => {
      void refsWarmer.ensureJob(row.job, row.encounterId, false);
    }, 150);
    return () => clearTimeout(t);
  }, [hoverKey, pulls.rows]);

  // 500ms after a row becomes primary (hover or selection), fire the
  // speculative runAnalysis — fire-and-forget; the explicit click collapses
  // onto this build via the backend's _result_inflight. Gated on warm refs
  // (else the warm itself is the bottleneck) and skipped for plan-only healers.
  const specKey = hoverKey ?? primaryKey;
  useEffect(() => {
    if (!specKey) return;
    const row = pulls.rows.find((r) => rowKey(r) === specKey);
    if (!row) return;
    if (isHealer(row.job) && !ANALYZABLE_HEALERS.has(row.job)) return;
    if (!refsWarmer.isReady(row.job, row.encounterId)) return;
    const t = setTimeout(() => {
      void sidecar
        .runAnalysis(row.reportCode, row.fightId, row.job, row.encounterId, REFS_BUCKET)
        .catch(() => {});
    }, 500);
    return () => clearTimeout(t);
  }, [specKey, pulls.rows]);

  // --- actions ----------------------------------------------------------------

  const analyze = (row: PullRow) => {
    const snapshot: Partial<AppState> = {
      job: row.job,
      encounter: encounterName.get(row.encounterId) ?? '',
      encounterId: row.encounterId,
      pullId: rowPullLabel(row),
      pullReportCode: row.reportCode,
      pullFightId: row.fightId,
      refsBucket: REFS_BUCKET,
      // Clear any Research-loaded subject: this run analyzes the loaded
      // character's own pull, and a stale name would split the result-cache
      // key from the speculative pre-analysis above.
      playerName: undefined,
      pullsLoaded: pulls.rows.length > 0,
    };
    setState({ ...state, ...snapshot });
    if (isHealer(row.job)) onPlanMitigation(snapshot);
    else onRunAnalysis(snapshot);
  };

  const submitSearch = () => {
    const code = extractReportCode(filters.query);
    if (code) {
      setPasteBusy(true);
      setLocalError(null);
      pullsStore.mergePasted(code)
        .then((added) => {
          pullsStore.setFilters({ query: '' });
          if (added === 0) setLocalError('That report added no new pulls (already listed).');
        })
        .catch((e) => setLocalError(e instanceof Error ? e.message : String(e)))
        .finally(() => setPasteBusy(false));
      return;
    }
    // Plain text just filters (already live); Enter/Analyze on a non-code
    // query analyzes the primary row.
    const row = visible.find((r) => rowKey(r) === primaryKey);
    if (row) analyze(row);
  };

  const scanEncounter = (encId: number) => {
    setScanBusy(encId);
    setLocalError(null);
    pullsStore.scanEncounter(encId)
      .then((added) => {
        if (added === 0) setLocalError('No additional wipes found in recent reports.');
      })
      .catch((e) => setLocalError(e instanceof Error ? e.message : String(e)))
      .finally(() => setScanBusy(null));
  };

  // Encounters worth offering a deep wipe-scan for: visible in the current
  // category filter, not yet scanned this session. Ultimates first (prog-
  // heavy), capped at 3 buttons.
  const scanCandidates = useMemo(() => {
    const inView = new Set(visible.map((r) => r.encounterId));
    return pulls.encounters
      .filter((e) => inView.has(e.id) && !pulls.scannedEncounters.has(e.id))
      .sort((a, b) => Number(b.category === 'ultimate') - Number(a.category === 'ultimate'))
      .slice(0, 3);
  }, [visible, pulls.encounters, pulls.scannedEncounters]);

  // --- empty states -----------------------------------------------------------

  if (!state.lodestoneId) {
    return (
      <div className="content narrow">
        <div className="hero compact">
          <h1>Pick your character</h1>
          <p className="mut" style={{ margin: '4px 0 0', fontSize: 13 }}>
            Your pulls load straight from FFLogs — pick a character to begin.
          </p>
        </div>
        <div className="card">
          <div className="card-body">
            <CharacterSelect
              autoAdvanceSingle
              initialServer={state.server}
              initialRegion={state.region}
              onPicked={onCharacterPicked}
            />
          </div>
        </div>
      </div>
    );
  }

  const err = localError || pulls.error || externalError;
  const encountersWithRows = new Set(pulls.rows.map((r) => r.encounterId)).size;
  const showSkeletons = pulls.loading && pulls.rows.length === 0;

  return (
    <div className="pulls-view">
      <div className="pulls-head">
        <div className="pulls-head-inner">
          <div className="pulls-title-row">
            <div>
              <h1>Your pulls</h1>
              <p className="pulls-sub">
                {pulls.loading
                  ? (pulls.loadingStage ? `${pulls.loadingStage}…` : 'syncing…')
                  : pulls.syncedAtMs
                    ? `${pulls.rows.length} pulls · ${encountersWithRows} encounters · `
                      + `${pulls.jobs.length} jobs · ${syncedAgo(pulls.syncedAtMs, now)}`
                    : '—'}
              </p>
            </div>
            <button
              className="btn"
              onClick={() => pullsStore.refresh()}
              disabled={pulls.loading}
              title="Re-sync the list from FFLogs"
            >
              <RefreshCw size={13} className={pulls.loading ? 'spin' : undefined} />
              Refresh
            </button>
          </div>

          <div className="pulls-search-row">
            <div className="pulls-search">
              <Search size={13} className="mut" />
              <input
                ref={searchRef}
                className="pulls-search-input"
                placeholder="Search an encounter or job, or paste an FFLogs report link…"
                value={filters.query}
                onChange={(e) => pullsStore.setFilters({ query: e.target.value })}
                onKeyDown={(e) => { if (e.key === 'Enter') submitSearch(); }}
              />
              <span className="kbd-hint">Ctrl K</span>
            </div>
            <button
              className="btn primary"
              onClick={submitSearch}
              disabled={pasteBusy || (!extractReportCode(filters.query) && !primaryKey)}
            >
              {pasteBusy ? 'Adding…' : 'Analyze'}
            </button>
          </div>

          <div className="filter-pills">
            <select
              className="pill pill-select"
              value={filters.jobFilter ?? ''}
              onChange={(e) => pullsStore.setFilters({ jobFilter: e.target.value || null })}
            >
              <option value="">All jobs ({pulls.jobs.length})</option>
              {pulls.jobs.map((j) => <option key={j} value={j}>{j}</option>)}
            </select>
            <span className="pill-divider" />
            {(['all', 'savage', 'ultimate'] as const).map((c) => (
              <button
                key={c}
                className={'pill' + (filters.categoryFilter === c ? ' on' : '')}
                onClick={() => pullsStore.setFilters({ categoryFilter: c })}
              >
                {c === 'all' ? 'All' : c === 'savage' ? 'Savage' : 'Ultimate'}
              </button>
            ))}
            <span className="pill-divider" />
            {(['kills', 'wipes'] as const).map((o) => (
              <button
                key={o}
                className={'pill' + (filters.outcomeFilter === o ? ' on' : '')}
                onClick={() => pullsStore.setFilters({
                  outcomeFilter: filters.outcomeFilter === o ? 'all' : o,
                })}
              >
                {o === 'kills' ? 'Kills' : 'Wipes'}
              </button>
            ))}
            <span style={{ flex: 1 }} />
            <select
              className="pill pill-select"
              value={filters.sort}
              onChange={(e) => pullsStore.setFilters({ sort: e.target.value as PullsSort })}
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="parse">Best parse</option>
            </select>
          </div>
        </div>
      </div>

      <div className="pulls-scroll">
        <div className="pulls-list">
          {err && (
            <div className="card pulls-error">
              <div className="card-body" style={{ padding: '10px 14px', fontSize: 13, display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                <div style={{ flex: 1 }}>
                  <strong style={{ color: 'var(--bad)' }}>Error: </strong>{err}
                </div>
                {externalError && onReportError && (
                  <button className="btn ghost sm" style={{ flexShrink: 0 }}
                    onClick={() => onReportError(externalError)}
                    title="Send this error to the developer via Submit Feedback">
                    Report this
                  </button>
                )}
                <button className="btn ghost sm" style={{ flexShrink: 0 }}
                  onClick={() => { setLocalError(null); pullsStore.clearError(); clearExternalError?.(); }}
                  title="Dismiss">
                  ×
                </button>
              </div>
            </div>
          )}

          {showSkeletons && (
            <div className="pull-rows" aria-hidden="true">
              {Array.from({ length: 5 }, (_, i) => (
                <div key={i} className="pull-row skeleton">
                  <div className="sk-icon" />
                  <div style={{ flex: 1 }}>
                    <div className="sk-line" style={{ width: '38%' }} />
                    <div className="sk-line" style={{ width: '62%', marginTop: 6 }} />
                  </div>
                  <div className="sk-line" style={{ width: 110 }} />
                </div>
              ))}
            </div>
          )}

          {!showSkeletons && visible.length === 0 && (
            <div className="pulls-empty mut">
              {pulls.rows.length === 0
                ? 'No pulls found on this tier yet. Kill or wipe to a tier boss with logging on, then Refresh — or paste an FFLogs report link above.'
                : 'Nothing matches these filters.'}
            </div>
          )}

          {groups.map((g, gi) => (
            <div key={g.label}>
              <div className="pull-group-head">
                <span className="pull-group-label">{g.label}</span>
                <span className="pull-group-rule" />
                <span className="pull-group-count">
                  {g.rows.length} {g.rows.length === 1 ? 'pull' : 'pulls'}
                </span>
              </div>
              <div className="pull-rows">
                {g.rows.map((r) => {
                  const k = rowKey(r);
                  const primary = k === primaryKey;
                  const healer = isHealer(r.job);
                  const icon = jobIcon(r.job);
                  const cat = categoryOf.get(r.encounterId) ?? 'savage';
                  return (
                    <div
                      key={k}
                      className={'pull-row' + (primary ? ' selected' : '')}
                      tabIndex={0}
                      onMouseEnter={() => setHoverKey(k)}
                      onMouseLeave={() => setHoverKey((cur) => (cur === k ? null : cur))}
                      onFocus={() => { setHoverKey(k); setSelectedKey(k); }}
                      onClick={() => setSelectedKey(k)}
                      onDoubleClick={() => analyze(r)}
                      onKeyDown={(e) => { if (e.key === 'Enter') analyze(r); }}
                    >
                      {icon
                        ? <img className="pull-job-icon" src={icon} alt={r.job} width={28} height={28} draggable={false} />
                        : <span className="pull-job-icon" />}
                      <div className="pull-main">
                        <div className="pull-title">
                          <span className="pull-enc">
                            {encounterName.get(r.encounterId) ?? `Encounter ${r.encounterId}`}
                          </span>
                          <span className="tag">{cat === 'ultimate' ? 'Ultimate' : 'Savage'}</span>
                          <span className="pull-time">{fmtTimeOfDay(r.startTimeMs)}</span>
                        </div>
                        <div className="pull-meta">{rowMetaLine(r)}</div>
                      </div>
                      <div className="pull-metric">
                        {r.kill && r.parsePct != null ? (
                          <>
                            <div className="pull-metric-head">
                              <span>parse</span>
                              <span className={`pull-metric-val ${parseTone(r.parsePct)}`}>
                                {r.parsePct.toFixed(1)}%
                              </span>
                            </div>
                            <div className="parse-bar">
                              <div
                                className={`parse-bar-fill ${parseTone(r.parsePct)}`}
                                style={{ width: `${Math.min(100, Math.max(2, r.parsePct))}%` }}
                              />
                            </div>
                          </>
                        ) : r.kill ? (
                          <div className="pull-metric-head"><span>pasted kill</span></div>
                        ) : (
                          <>
                            <div className="pull-metric-head">
                              <span>phase</span>
                              <span className="pull-metric-val">
                                {(r.lastPhase ?? 0) >= 1 ? `P${r.lastPhase}` : '—'}
                              </span>
                            </div>
                            <div className="phase-bar">
                              {Array.from({ length: 5 }, (_, i) => {
                                const progressed = r.fightPercentage != null
                                  ? (100 - r.fightPercentage) / 20 : 0;
                                const fill = Math.min(1, Math.max(0, progressed - i));
                                return (
                                  <span key={i} className="phase-seg">
                                    <span className="phase-seg-fill"
                                      style={{ width: `${fill * 100}%` }} />
                                  </span>
                                );
                              })}
                            </div>
                          </>
                        )}
                      </div>
                      <button
                        className={'btn' + (primary ? ' primary' : '')}
                        style={{ flexShrink: 0 }}
                        onClick={(e) => { e.stopPropagation(); analyze(r); }}
                      >
                        {healer ? <HeartPulse size={13} /> : <Sparkles size={13} />}
                        {healer ? 'Analyze vs plan' : 'Analyze'}
                      </button>
                    </div>
                  );
                })}
              </div>

              {gi === 0 && scanCandidates.length > 0 && (
                <div className="pulls-scan-strip">
                  <ScanLine size={14} className="mut-2" />
                  <span className="mut" style={{ flex: 1, fontSize: 12 }}>
                    Wipes older than your last few reports aren't loaded yet.
                  </span>
                  {scanCandidates.map((e) => (
                    <button
                      key={e.id}
                      className="btn sm"
                      disabled={scanBusy !== null || pulls.merging}
                      onClick={() => scanEncounter(e.id)}
                    >
                      {scanBusy === e.id ? 'Scanning…' : `Scan ${encounterShortName(e.name)}`}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}

          {!showSkeletons && pulls.rows.length > 0 && (
            <div className="pulls-footer">
              <button
                className="btn"
                disabled={pulls.loading || pulls.recentLimit >= 50}
                onClick={() => {
                  if (!pullsStore.loadOlder()) {
                    setLocalError('Older wipes: scan an encounter above, or paste the report link. Older kills are already all listed.');
                  }
                }}
                title="Deepen the recent-report wipe scan (kills are always complete — they come from rankings)"
              >
                {pulls.recentLimit >= 50 ? 'All recent reports scanned' : 'Load older pulls'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
