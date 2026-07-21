// Submit Feedback — the user-submitted half of crash/anomaly reporting.
//
// The app makes no network connections beyond FFLogs, so "submit" means:
// the sidecar exports a diagnostics zip (event log + environment + analysis
// context), we reveal it in Explorer, and we open a prefilled GitHub
// new-issue page in the default browser for the user to attach it to.

import { useEffect, useMemo, useState } from 'react';
import {
  ExternalLink, FolderOpen, MessageSquare, PackageOpen, ScrollText,
} from 'lucide-react';

import { sidecar } from '../sidecar';
import type {
  AppEvent, FeedbackBundleResult, FeedbackCategory,
} from '../sidecar/contract';
import type { AppState } from '../state/appState';
import { getBufferedEvents } from '../log';
import { openUrl } from '../tauri/openUrl';
import { revealPath } from '../tauri/revealPath';

const ISSUES_URL = 'https://github.com/azam997/meridian-releases/issues/new';
const MAX_URL_LEN = 8000; // browsers/GitHub start dropping longer URLs

/** Seed passed by the dashboard's >100% nudge and Setup's "Report this". */
export type FeedbackPrefill = {
  category: FeedbackCategory;
  summary?: string;
};

const CATEGORIES: { id: FeedbackCategory; label: string; hint: string }[] = [
  { id: 'bug', label: 'Bug report', hint: 'something broke or looked wrong' },
  { id: 'feedback', label: 'Feedback', hint: 'ideas, requests, impressions' },
  { id: 'anomaly', label: 'Over-ceiling result',
    hint: 'an efficiency above 100%' },
];

const buildIssueUrl = (r: FeedbackBundleResult): string => {
  const make = (body: string) =>
    `${ISSUES_URL}?title=${encodeURIComponent(r.issueTitle)}` +
    `&body=${encodeURIComponent(body)}`;
  let body = r.issueBody;
  let url = make(body);
  // The backend caps the body well under this, but URL-encoding non-ASCII can
  // still blow past it — trim with a pointer at the zip, never a broken link.
  const note = '\n\n…(truncated — full details in the attached zip)';
  while (url.length > MAX_URL_LEN && body.length > 200) {
    body = body.slice(0, body.length - 500);
    url = make(body + note);
  }
  return url;
};

const fmtTime = (epochMs: number): string =>
  new Date(epochMs).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });

type Props = {
  prefill: FeedbackPrefill | null;
  state: AppState;
};

export const FeedbackView = ({ prefill, state }: Props) => {
  const [category, setCategory] =
    useState<FeedbackCategory>(prefill?.category ?? 'feedback');
  const [description, setDescription] = useState(prefill?.summary ?? '');
  const [includeContext, setIncludeContext] = useState(true);
  const [events, setEvents] = useState<AppEvent[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<FeedbackBundleResult | null>(null);

  // What rides along in the bundle's context.json (and seeds the issue body).
  const analysisContext = useMemo(() => {
    const h = state.analysis?.headline;
    if (!h) return undefined;
    return {
      job: state.job,
      encounterId: state.encounterId,
      encounterName: state.encounter,
      reportCode: state.pullReportCode,
      fightId: state.pullFightId,
      playerName: state.playerName,
      efficiencyPct: h.efficiencyPct,
      efficiencyPctLenient: h.efficiencyPctLenient,
      killTimeSec: h.killTimeSec,
      downtimeSource: h.downtimeSource,
      multiTargetCredited: h.multiTargetCredited,
      multiTargetDisclaimed: h.multiTargetDisclaimed,
      ceilingAnomaly: h.ceilingAnomaly,
    } as Record<string, unknown>;
  }, [state]);

  useEffect(() => {
    let dead = false;
    void sidecar
      .getRecentEvents(50)
      .then((r) => {
        if (!dead) setEvents([...r.events].reverse()); // newest first
      })
      .catch(() => {
        // Sidecar unreachable — fall back to the in-memory frontend tail.
        if (!dead) {
          setEvents(getBufferedEvents()
            .map((e) => ({ t: Date.now(), lv: e.level, cat: `ui.${e.cat}`,
                           msg: e.msg, data: e.data }))
            .reverse());
        }
      });
    return () => { dead = true; };
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await sidecar.exportFeedbackBundle({
        category,
        description: description.trim() || undefined,
        analysisContext: includeContext ? analysisContext : undefined,
      });
      setResult(r);
      // Best-effort reveal — the path stays visible as text either way.
      void revealPath(r.path).catch(() => {});
      const url = buildIssueUrl(r);
      void openUrl(url).catch(() => window.open(url, '_blank'));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const openIssueAgain = () => {
    if (!result) return;
    const url = buildIssueUrl(result);
    void openUrl(url).catch(() => window.open(url, '_blank'));
  };

  return (
    <div className="content narrow">
      <div className="hero">
        <h1>Submit feedback</h1>
        <p>
          Meridian never phones home — reports are sent by you, as a GitHub
          issue with a diagnostics file attached. The file holds the app's
          event log and your analysis context; never your FFLogs sign-in.
        </p>
      </div>

      <div className="card">
        <div className="card-head">
          <MessageSquare size={14} />
          <h2>Report</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            what should we look at?
          </span>
        </div>
        <div className="card-body">
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))',
              gap: 8,
            }}
          >
            {CATEGORIES.map((c) => (
              <button
                key={c.id}
                className={'btn ' + (category === c.id ? 'primary' : '')}
                style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 2 }}
                onClick={() => setCategory(c.id)}
              >
                {c.label}
                <span className="mut" style={{ fontSize: 11, fontWeight: 400 }}>
                  {c.hint}
                </span>
              </button>
            ))}
          </div>

          <label className="field-label" style={{ marginTop: 16 }}>
            What happened?
          </label>
          <textarea
            className="input"
            rows={5}
            style={{ width: '100%', resize: 'vertical' }}
            placeholder={
              category === 'anomaly'
                ? 'Anything unusual about the pull? (multi-target phases, deaths, disconnects…)'
                : 'Describe what you saw, and what you expected instead.'
            }
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />

          {analysisContext ? (
            <label
              className="mut"
              style={{ display: 'flex', gap: 8, alignItems: 'center',
                       marginTop: 12, fontSize: 12, cursor: 'pointer' }}
            >
              <input
                type="checkbox"
                checked={includeContext}
                onChange={(e) => setIncludeContext(e.target.checked)}
              />
              Include the loaded analysis ({state.job || '—'} ·{' '}
              {state.encounter || '—'} · {state.pullReportCode}#
              {state.pullFightId}
              {state.analysis?.headline.ceilingAnomaly
                ? ` · over-ceiling ${state.analysis.headline.ceilingAnomaly.maxEffPct.toFixed(2)}%`
                : ''}
              )
            </label>
          ) : (
            <p className="mut" style={{ marginTop: 12, fontSize: 12 }}>
              No analysis is loaded — the report will carry the event log and
              app environment only.
            </p>
          )}

          <div className="row" style={{ marginTop: 16, gap: 10, alignItems: 'center' }}>
            <button className="btn primary" disabled={busy} onClick={() => void submit()}>
              <PackageOpen size={14} />
              {busy ? 'Exporting…' : 'Export bundle & open GitHub issue'}
            </button>
            {error && (
              <span style={{ color: 'var(--bad)', fontSize: 12 }}>
                Export failed: {error} — try again.
              </span>
            )}
          </div>

          {result && (
            <div className="finding info static" style={{ marginTop: 14 }}>
              <div>
                <div className="title">Bundle exported — one step left</div>
                <div className="desc">
                  Attach the zip (just revealed in Explorer) to the GitHub
                  issue that opened in your browser, then submit it there.
                  <br />
                  <span className="mono" style={{ userSelect: 'all' }}>
                    {result.path}
                  </span>
                </div>
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  <button className="btn ghost sm" onClick={openIssueAgain}>
                    <ExternalLink size={12} /> Open issue again
                  </button>
                  <button
                    className="btn ghost sm"
                    onClick={() => void revealPath(result.path).catch(() => {})}
                  >
                    <FolderOpen size={12} /> Show zip
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <ScrollText size={14} />
          <h2>Recent events</h2>
          <span className="sub" style={{ marginLeft: 'auto' }}>
            the log your report will include (newest first)
          </span>
        </div>
        <div className="card-body">
          {events === null ? (
            <p className="mut" style={{ fontSize: 12 }}>Loading…</p>
          ) : events.length === 0 ? (
            <p className="mut" style={{ fontSize: 12 }}>
              Nothing logged yet this session.
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4,
                          maxHeight: 320, overflowY: 'auto' }}>
              {events.map((e, i) => {
                const highlight = e.cat === 'ceiling_anomaly' || e.lv === 'error';
                return (
                  <div
                    key={`${e.t}-${i}`}
                    className="mono"
                    style={{
                      fontSize: 11.5,
                      display: 'flex',
                      gap: 10,
                      padding: '3px 6px',
                      borderRadius: 6,
                      background: highlight ? 'var(--surface)' : 'transparent',
                      color: e.lv === 'error' ? 'var(--bad)'
                        : e.lv === 'warn' ? 'var(--warn)' : 'var(--muted)',
                    }}
                  >
                    <span className="mut-2" style={{ flexShrink: 0 }}>
                      {fmtTime(e.t)}
                    </span>
                    <span style={{ flexShrink: 0, minWidth: 110 }}>{e.cat}</span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis',
                                   whiteSpace: 'nowrap' }}>
                      {e.msg}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
