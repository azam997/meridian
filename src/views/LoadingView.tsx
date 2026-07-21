import type { ProgressTask } from '../sidecar/contract';

type Props = {
  label?: string;
  progressPct?: number;
  /** When present (during the parallel reference-log download phase),
   *  the loader renders one mini-bar per task so users can see the
   *  6-way ThreadPoolExecutor working. */
  tasks?: ProgressTask[];
  /** Pipeline steps for the secondary per-step checklist (runAnalysis). `step` is
   *  the active index — steps before it are done, after it queued; `step ===
   *  steps.length` means all finished. Each row reuses the `.task` styling, so its
   *  mini-bar reads as that step's progress (empty → running shimmer → full). */
  step?: number;
  steps?: string[];
};

const STATE_LABEL: Record<ProgressTask['state'], string> = {
  pending: 'queued',
  in_flight: 'downloading',
  done: 'done',
  failed: 'failed',
};

type StepState = 'done' | 'in_flight' | 'pending';
const STEP_STATE_LABEL: Record<StepState, string> = {
  done: 'done',
  in_flight: 'running',
  pending: 'queued',
};

export const LoadingView = ({ label, progressPct, tasks, step, steps }: Props) => {
  // Cap at 95% while in-flight per the README; only show 100% on completion.
  const width = Math.min(95, progressPct ?? 60);
  const hasTasks = tasks && tasks.length > 0;
  const hasSteps = steps !== undefined && steps.length > 0 && step !== undefined;
  const inFlight = tasks?.filter((t) => t.state === 'in_flight').length ?? 0;
  const refsDone = tasks?.filter((t) => t.state === 'done' || t.state === 'failed').length ?? 0;
  // Honest phase tag: the backend's staged labels tell us whether we're
  // downloading (tasks / generic), serving warm references from cache, or
  // running the (necessary, local, no-network) simulator passes.
  const lower = (label ?? '').toLowerCase();
  const phaseTag = hasTasks
    ? `FFLogs · ${inFlight} concurrent request${inFlight === 1 ? '' : 's'}`
    : /cache/.test(lower)
      ? 'references · cached ✓'
      : /model|compar|check|finaliz|scor|consensus|ceiling|rotation|dashboard|ideal/.test(lower)
        ? 'simulating · local'
        : 'FFLogs · network';
  const stepStateAt = (i: number): StepState =>
    i < step! ? 'done' : i === step! ? 'in_flight' : 'pending';
  return (
    <div className="content">
      <div className={`loader${hasTasks || hasSteps ? ' wide' : ''}`}>
        <img className="pulse" src="/meridian.svg" alt="" draggable={false} />
        <div className="label">{label || 'Working…'}</div>
        <span className="net-tag">
          <span className="dot" />
          {phaseTag}
        </span>
        <div className="bar main-bar">
          <div className="fill" style={{ width: `${width}%` }} />
        </div>
        {hasSteps && (
          <div className="tasks steps">
            {steps!.map((name, i) => {
              const st = stepStateAt(i);
              // On the active step, surface the ref-download count inline so the
              // checklist summarizes the per-log bars rendered below it.
              const detail =
                st === 'in_flight' && hasTasks ? ` · ${refsDone}/${tasks!.length}` : '';
              return (
                <div key={i} className={`task ${st}`}>
                  <span className="glyph" />
                  <span className="name" title={name}>{name}</span>
                  <span className={`state ${st}`}>{STEP_STATE_LABEL[st]}{detail}</span>
                  <div className="mini-bar">
                    <div className="mini-fill" />
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {hasTasks && (
          <div className="tasks">
            {tasks!.map((t, i) => (
              <div key={i} className={`task ${t.state}`}>
                <span className="glyph" />
                <span className="name" title={t.label}>{t.label}</span>
                <span className={`state ${t.state}`}>{STATE_LABEL[t.state]}</span>
                <div className="mini-bar">
                  <div className="mini-fill" />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
