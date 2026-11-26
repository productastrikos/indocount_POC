import React from 'react';
import { Badge, Card, DataTable } from '../../components/ui';
import ChartCard, { MiniChart } from '../../components/charts/ChartCard';
import { ErrorBox, Loading } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { num } from '../../utils';
import { Eff } from './shared';

const DT_COLORS = ['danger', 'info', 'warning', 'chart-5', 'chart-8'];
const IDLE_LABEL = 'Idle · no orders';

export default function MonitoringTab({ factory, date, shift, downtime, onOpenLine }) {
  const { data, loading, error, reload } = useApi('/api/production/hourly', { factory, date, shift }, { every: 1 });
  if (loading && !data) return <Loading rows={6} />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  const chart = data.chart;
  const lines = data.lines;
  // Plan to date = the same figure as the "Output vs target" KPI: completed hours plus the elapsed part of the hour in progress.
  const totals = data.totals || chart.reduce((a, h) => ({ planned_to_date: a.planned_to_date + (h.planned_to_date || 0), actual: a.actual + (h.actual || 0) }),
    { planned_to_date: 0, actual: 0 });
  const idle = data.idle_no_orders_min || 0;
  const dtEntries = [...Object.entries(data.downtime || downtime || {}), ...(idle > 0 ? [[IDLE_LABEL, idle]] : [])];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <ChartCard className="xl:col-span-2" title="Hourly production — planned vs actual" type="bar" legend height={250}
          subtitle={`${num(totals.actual)} produced vs ${num(totals.planned_to_date)} planned to date (the hour in progress counts its elapsed minutes) · updates live`}
          data={{
            labels: chart.map((h) => h.hour),
            datasets: [
              { label: 'Planned', data: chart.map((h) => h.planned), color: 'chart-8' },
              { label: 'Actual', data: chart.map((h) => h.actual),
                color: chart.map((h) => (h.actual == null || h.completed === false ? 'primary' : h.actual >= h.planned * 0.95 ? 'success' : h.actual >= h.planned * 0.85 ? 'warning' : 'danger')) },
            ],
          }} />
        <Card title="Downtime by cause"
          subtitle={`Line-minutes of planned production time lost (selected scope)${idle > 0 ? ` · "${IDLE_LABEL}" = line without a production order: not a machine loss, not planned time, excluded from OEE` : ''}`}>
          {dtEntries.length ? (
            <MiniChart type="bar" horizontal height={220} data={{
              labels: dtEntries.map(([c]) => c),
              datasets: [{ label: 'Minutes', data: dtEntries.map(([, v]) => v), color: dtEntries.map(([c], i) => (c === IDLE_LABEL ? 'neutral' : DT_COLORS[i % DT_COLORS.length])) }],
            }} />
          ) : <p className="text-xs text-fg-faint">No downtime.</p>}
        </Card>
      </div>
      <Card title="Line performance" subtitle="Actual vs plan to date (time without production orders is not planned); lines below 88% of plan are highlighted" padded={false}>
        <DataTable rowKey="line_id" rows={lines} onRowClick={(r) => onOpenLine(r.line_id)} emptyText="No production recorded for this date." columns={[
          { key: 'line', header: 'Line', strong: true, render: (r) => <span>{r.line} <span className="text-fg-faint font-normal">{r.factory}</span></span> },
          { key: 'actual', header: 'Actual', render: (r) => num(r.actual) },
          { key: 'target', header: 'Plan to date', render: (r) => num(r.target) },
          { key: 'gap', header: 'Gap', render: (r) => <span className={r.actual < r.target ? 'text-danger' : 'text-success'}>{r.actual - r.target >= 0 ? '+' : ''}{num(r.actual - r.target)}</span> },
          { key: 'efficiency_pct', header: 'Efficiency', render: (r) => <Eff value={r.efficiency_pct} /> },
          { key: 'downtime_min', header: 'Downtime', render: (r) => (
            <span>{num(r.downtime_min)} min{r.idle_no_orders_min > 0 && <span className="block text-[10px] text-fg-faint">+ {num(r.idle_no_orders_min)} min idle · no orders</span>}</span>
          ) },
          { key: 'flag', header: '', render: (r) => (r.underperforming ? <Badge tone="danger" dot>UNDERPERFORMING</Badge> : <Badge tone="success" dot plain>ON PACE</Badge>) },
        ]} />
      </Card>
    </div>
  );
}
