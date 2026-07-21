// The frontend job registry. Shared analysis views call getJobProfile(job) (and
// the nonRotational* helpers) instead of hardcoding per-job constants. Adding a
// job is a registry entry here, not an edit to TimelineView / DashboardView.

import { astrologian } from './astrologian';
import { bard } from './bard';
import { blackmage } from './blackmage';
import { dancer } from './dancer';
import { darkknight } from './darkknight';
import { dragoon } from './dragoon';
import { gunbreaker } from './gunbreaker';
import { machinist } from './machinist';
import { monk } from './monk';
import { ninja } from './ninja';
import { paladin } from './paladin';
import { pictomancer } from './pictomancer';
import { reaper } from './reaper';
import { redmage } from './redmage';
import { sage } from './sage';
import { samurai } from './samurai';
import { scholar } from './scholar';
import { summoner } from './summoner';
import { viper } from './viper';
import { warrior } from './warrior';
import { whitemage } from './whitemage';
import { SHARED_NON_ROTATIONAL_NAMES, type JobProfile } from './types';

export type { JobProfile, JobPanel, JobPanelProps } from './types';

const REGISTRY: Record<string, JobProfile> = {
  [astrologian.name]: astrologian,
  [bard.name]: bard,
  [blackmage.name]: blackmage,
  [dancer.name]: dancer,
  [darkknight.name]: darkknight,
  [dragoon.name]: dragoon,
  [gunbreaker.name]: gunbreaker,
  [machinist.name]: machinist,
  [monk.name]: monk,
  [ninja.name]: ninja,
  [paladin.name]: paladin,
  [pictomancer.name]: pictomancer,
  [reaper.name]: reaper,
  [redmage.name]: redmage,
  [sage.name]: sage,
  [samurai.name]: samurai,
  [scholar.name]: scholar,
  [summoner.name]: summoner,
  [viper.name]: viper,
  [warrior.name]: warrior,
  [whitemage.name]: whitemage,
};

const EMPTY: JobProfile = { name: '', panels: [] };

/** Profile for a job, or an empty profile (no panels / no extra filters) for
 *  shared-aspects-only jobs (e.g. Samurai) and unknown jobs. */
export const getJobProfile = (job: string): JobProfile => REGISTRY[job] ?? EMPTY;

/** Cast names that never belong on a DPS timeline for `job`: the shared
 *  role-action baseline plus the job's extras. Fallback only — the primary
 *  defensive-cast filter is the backend `isDefensive` flag on abilityMeta. */
export const nonRotationalNames = (job: string): Set<string> =>
  new Set([
    ...SHARED_NON_ROTATIONAL_NAMES,
    ...(getJobProfile(job).nonRotationalNames ?? []),
  ]);
