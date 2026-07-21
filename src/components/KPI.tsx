import { ArrowDown, ArrowUp, HelpCircle } from 'lucide-react';
import { Spark } from './Spark';

export type KPIProps = {
  label: string;
  value: string;
  unit?: string;
  tone?: 'accent' | 'good' | 'warn' | '';
  hint?: string;
  delta?: { dir: 'up' | 'down'; text: string };
  sparkData?: number[];
};

export const KPI = ({ label, value, unit, tone, hint, delta, sparkData }: KPIProps) => (
  <div className={`kpi ${tone || ''}`}>
    <div className="label">
      {label}
      {hint && (
        <span className="mut-2" title={hint}>
          <HelpCircle size={11} />
        </span>
      )}
    </div>
    <div className="value">
      {value}
      {unit && <span className="unit">{unit}</span>}
    </div>
    {delta && (
      <div className={`delta ${delta.dir}`}>
        {delta.dir === 'up' ? <ArrowUp size={11} /> : <ArrowDown size={11} />}
        {delta.text}
      </div>
    )}
    {sparkData && <Spark data={sparkData} className="spark" />}
  </div>
);
