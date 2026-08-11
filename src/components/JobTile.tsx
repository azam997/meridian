// A tinted square job icon: the XIVAPI framed icon over the 3-letter
// abbreviation, on a job-colored wash. If the icon fails to load (XIVAPI
// outage, offline), the broken <img alt=""> renders nothing in WebView2 and
// the abbreviation shows through — no state, no onError.

import { jobAbbr, jobColor, jobIcon } from './jobs';

type Props = {
  job: string;
  /** Outer square in px; the icon insets `iconInset` on each side. */
  size: number;
  iconInset?: number;
};

export const JobTile = ({ job, size, iconInset = 3 }: Props) => {
  const color = jobColor(job);
  const icon = jobIcon(job);
  return (
    <span
      className="jicon"
      style={{
        width: size,
        height: size,
        // Hex-alpha suffixes ≈ the mock's 0.18 wash / 0.42 line.
        background: color + '2E',
        borderColor: color + '6B',
      }}
    >
      <span
        className="jicon-abbr mono"
        style={{ color, fontSize: Math.max(8, Math.round(size * 0.28)) }}
      >
        {jobAbbr(job)}
      </span>
      {icon && (
        <img
          className="jicon-img"
          src={icon}
          alt=""
          draggable={false}
          style={{ padding: iconInset }}
        />
      )}
    </span>
  );
};
