// Landing page — a card per tab, mirroring the sidebar's enable/disable rules.
// Pure navigation: no data fetches; the launch-time ref warm runs independently
// in App's mount effect.

import {
  BarChart3, Cog, FlaskConical, Gauge, HeartPulse, History, Layers,
  MessageSquare, Trophy, User,
} from 'lucide-react';
import type { View } from '../state/appState';

type Props = {
  characterLoaded: boolean;
  /** An analysis has completed — gates the analysis-bound cards. */
  ready: boolean;
  onNavigate: (v: View) => void;
};

type CardDef = {
  id: View;
  label: string;
  Icon: typeof User;
  desc: string;
  needs: 'character' | 'ready' | null;
};

const CARDS: CardDef[] = [
  {
    id: 'setup',
    label: 'Encounter',
    Icon: User,
    desc: 'Pick your character, job, and pull, then run the analysis.',
    needs: null,
  },
  {
    id: 'dashboard',
    label: 'Analysis',
    Icon: Gauge,
    desc: 'Efficiency, downtime, and priced improvement suggestions for the analyzed pull.',
    needs: 'ready',
  },
  {
    id: 'timeline',
    label: 'Timeline',
    Icon: BarChart3,
    desc: 'The analyzed casts against the idealized rotation lanes, second by second.',
    needs: 'ready',
  },
  {
    id: 'counts',
    label: 'Cast counts',
    Icon: Layers,
    desc: 'Cast counts compared to the reference median.',
    needs: 'ready',
  },
  {
    id: 'research',
    label: 'Research',
    Icon: Trophy,
    desc: 'Browse the top-ranked players per job and load their pulls into the analyzer.',
    needs: null,
  },
  {
    id: 'theorizer',
    label: 'Kill time theorizer',
    Icon: FlaskConical,
    desc: 'The ideal rotation and output for a hypothetical kill time.',
    needs: null,
  },
  {
    id: 'mitigation',
    label: 'Healing / Mitigation',
    Icon: HeartPulse,
    desc: 'A mitigation plan for your healer duo — every forced hit measured from top logs, invulns and party cooldowns scheduled.',
    needs: null,
  },
  {
    id: 'settings',
    label: 'Settings',
    Icon: Cog,
    desc: 'FFLogs login, accent color, and zoom.',
    needs: null,
  },
  {
    id: 'feedback',
    label: 'Submit feedback',
    Icon: MessageSquare,
    desc: 'Report a bug or share an idea — packages diagnostics into a prefilled GitHub issue.',
    needs: null,
  },
  {
    id: 'changelog',
    label: 'Version history',
    Icon: History,
    desc: 'What changed in each release, newest first.',
    needs: null,
  },
];

export const HomeView = ({ characterLoaded, ready, onNavigate }: Props) => (
  <div className="content narrow">
    <div className="hero compact">
      <img
        className="wordmark"
        src="/meridian-wordmark.png"
        alt="FFXIV Meridian — Efficiency Analyzer"
        draggable={false}
      />
      <p>
        Analyze one of your pulls, study the top parses, theorize a kill time,
        or plan your healers’ mitigation.
      </p>
    </div>
    <div className="card-grid home-grid">
      {CARDS.map(({ id, label, Icon, desc, needs }) => {
        const disabled =
          (needs === 'character' && !characterLoaded) ||
          (needs === 'ready' && (!characterLoaded || !ready));
        const hint =
          !disabled ? null
          : !characterLoaded ? 'Pick a character first'
          : 'Run an analysis first';
        return (
          <button
            key={id}
            className="card home-card"
            disabled={disabled}
            onClick={() => onNavigate(id)}
          >
            <span className="home-card-title">
              <Icon size={16} />
              {label}
            </span>
            <span className="home-card-desc">{desc}</span>
            {hint && <span className="home-card-hint">{hint}</span>}
          </button>
        );
      })}
    </div>
  </div>
);
