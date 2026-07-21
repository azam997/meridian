type Props = {
  data: number[];
  w?: number;
  h?: number;
  className?: string;
};

export const Spark = ({ data, w = 64, h = 22, className }: Props) => {
  if (data.length === 0) return null;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const norm = data.map((d) => (d - min) / (max - min || 1));
  const step = w / (data.length - 1);
  const path = norm.map((n, i) => `${i ? 'L' : 'M'} ${i * step} ${h - n * h}`).join(' ');
  return (
    <svg width={w} height={h} className={className} viewBox={`0 0 ${w} ${h}`} fill="none">
      <path
        d={path}
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity="0.6"
      />
      <circle
        cx={(data.length - 1) * step}
        cy={h - norm[norm.length - 1] * h}
        r="2"
        fill="currentColor"
      />
    </svg>
  );
};
