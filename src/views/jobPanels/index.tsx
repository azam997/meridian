// Renders the active job's dashboard panels below the shared sections. The
// job → panels mapping lives in the frontend job registry (src/jobs/{job}.ts);
// DashboardView calls `<JobPanels job=... />` and adding a job is a registry
// entry there, not a change here.

import type { AnalysisResult } from '../../sidecar/contract';
import { getJobProfile } from '../../jobs';
import { CastCountPanel } from './CastCountPanel';

type Props = {
  job: string;
  analysis: AnalysisResult;
};

export const JobPanels = ({ job, analysis }: Props) => {
  const { panels, castCountPanels = [] } = getJobProfile(job);
  if (panels.length === 0 && castCountPanels.length === 0) return null;
  return (
    <>
      {panels.map((Panel, i) => (
        <Panel key={i} analysis={analysis} />
      ))}
      {castCountPanels.map((spec, i) => (
        <CastCountPanel key={`cc${i}`} analysis={analysis} spec={spec} />
      ))}
    </>
  );
};
