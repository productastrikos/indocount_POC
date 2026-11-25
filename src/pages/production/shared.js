import React from 'react';
import { Chart as ChartJS, LineController, BarController } from 'chart.js';
import { ProgressBar } from '../../components/ui';
import { resolveColor } from '../../components/charts/chartTheme';
import { effTone, healthTone, num } from '../../utils';

// Mixed bar + line charts need both controllers registered up front.
ChartJS.register(LineController, BarController);

export const LINE_TONE = { Running: 'success', Idle: 'warning', Changeover: 'info', Breakdown: 'danger', Maintenance: 'warning' };
export const STAGE_TONE = { Normal: 'success', Constrained: 'warning', Bottleneck: 'danger', Down: 'danger' };
export const RISK_TONE = { High: 'danger', Medium: 'warning', Low: 'info', None: 'success' };

/** Line dataset for use inside a bar chart (mixed chart). */
export function lineSeries(label, data, color = 'chart-4', extra = {}) {
  return { type: 'line', label, data, color, borderColor: resolveColor(color), borderWidth: 2, pointRadius: 2, tension: 0.3, fill: false, ...extra };
}

/** Compact health score with bar. */
export function HealthBar({ score, width = 90 }) {
  const tone = healthTone(score);
  return (
    <div className="flex items-center gap-2" style={{ minWidth: width }}>
      <ProgressBar value={score} max={100} tone={tone} className="flex-1" />
      <span className="text-[11px] font-semibold tabular-nums" style={{ color: `var(--color-${tone})` }}>{num(score, 0)}</span>
    </div>
  );
}

/** Efficiency % coloured by threshold. */
export function Eff({ value, good = 95, warn = 85 }) {
  if (value === null || value === undefined) return <span className="text-fg-faint">—</span>;
  return <span className="font-semibold tabular-nums" style={{ color: `var(--color-${effTone(value, good, warn)})` }}>{value.toFixed(1)}%</span>;
}

/** Section heading used inside drawers. */
export function Sub({ children, right }) {
  return (
    <div className="flex items-center justify-between mt-5 mb-2">
      <p className="label-caps">{children}</p>
      {right}
    </div>
  );
}

/** Lines in the current scope, from metadata. */
export function scopeLines(meta, factoryCode) {
  const f = meta.factories.find((x) => x.code === factoryCode);
  return meta.lines.filter((l) => !f || l.factory_id === f.id);
}
