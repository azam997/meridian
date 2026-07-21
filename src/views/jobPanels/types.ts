// Per-job dashboard panel registry.
//
// Each registered panel is a React component that takes the full
// AnalysisResult and decides whether to render (typically gated on the
// presence of a job-specific aspectStates key). The DashboardView walks
// the registered list for the active job and renders each in order;
// jobs without registered panels (e.g. SAM today) render nothing extra
// below the shared headline / findings / drift sections.

import type { ComponentType } from 'react';
import type { AnalysisResult, AspectStateJson } from '../../sidecar/contract';

export type JobPanelProps = {
  analysis: AnalysisResult;
};

export type JobPanel = ComponentType<JobPanelProps>;

/** Typed accessor for a job's aspect state by key. Returns undefined when the
 *  aspect wasn't produced for this pull, so a panel can early-return null. The
 *  caller names the concrete state shape (e.g. `aspectState<QueenState>(a,
 *  'Queen')`); the cast is centralized here instead of repeated per panel. */
export const aspectState = <T extends AspectStateJson>(
  analysis: AnalysisResult,
  name: string,
): T | undefined => analysis.aspectStates[name] as T | undefined;
