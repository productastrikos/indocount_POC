import React from 'react';
import { Badge, Card, DataTable } from '../../components/ui';
import ChartCard, { MiniChart } from '../../components/charts/ChartCard';
import { ErrorBox, Loading } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { num } from '../../utils';
import { Eff } from './shared';

const DT_COLORS = ['danger', 'info', 'warning', 'chart-5', 'chart-8'];

export default function MonitoringTab({ factory, date, shift, downtime, onOpenLine }) {
  const { data, loading, error, reload } = useApi('/api/production/hourly', { factory, date, shift }, { every: 1 });
  if (loading && !data) return <Loading rows={6} />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  const chart = data.chart;
  const lines = data.lines;
  const totals = chart.reduce((a, h) => ({ p: a.p + (h.actual != null ? h.planned : 0), a: a.a + (h.actual || 0) }), { p: 0, a: 0 });
  const dtEntries = Object.entries(downtime || {});
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <ChartCard className="xl:col-span-2" title="Hourly production — planned vs actual" type="bar" legend height={250}
          subtitle={`${num(totals.a)} produced vs ${num(totals.p)} planned in completed hours · updates live with the simulation`}
          data={{
            labels: chart.map((h) => h.hour),
            datasets: [
              { label: 'Planned', data: chart.map((h) => h.planned), color: 'chart-8' },
              { label: 'Actual', data: chart.map((h) => h.actual),
                color: chart.map((h) => (h.actual == null ? 'primary' : h.actual >= h.planned * 0.95 ? 'success' : h.actual >= h.planned * 0.85 ? 'warning' : 'danger')) },
            ],
          }} />
        <Card title="Downtime by cause" subtitle="Minutes of productive time lost (selected scope)">
          {dtEntries.length ? (
            <MiniChart type="bar" horizontal height={220} data={{
              labels: dtEntries.map(([c]) => c),
              datasets: [{ label: 'Minutes', data: dtEntries.map(([, v]) => v), color: DT_COLORS }],
            }} />
          ) : <p className="text-xs text-fg-faint">No downtime.</p>}
        </Card>
      </div>
      <Card title="Line performance" subtitle="Actual vs target to date; lines below 88% of target are highlighted" padded={false}>
        <DataTable rowKey="line_id" rows={lines} onRowClick={(r) => onOpenLine(r.line_id)} emptyText="No production recorded for this date." columns={[
          { key: 'line', header: 'Line', strong: true, render: (r) => <span>{r.line} <span className="text-fg-faint font-normal">{r.factory}</span></span> },
          { key: 'actual', header: 'Actual', render: (r) => num(r.actual) },
          { key: 'target', header: 'Target to date', render: (r) => num(r.target) },
          { key: 'gap', header: 'Gap', render: (r) => <span className={r.actual < r.target ? 'text-danger' : 'text-success'}>{r.actual - r.target >= 0 ? '+' : ''}{num(r.actual - r.target)}</span> },
          { key: 'efficiency_pct', header: 'Efficiency', render: (r) => <Eff value={r.efficiency_pct} /> },
          { key: 'downtime_min', header: 'Downtime', render: (r) => `${num(r.downtime_min)} min` },
          { key: 'flag', header: '', render: (r) => (r.underperforming ? <Badge tone="danger" dot>UNDERPERFORMING</Badge> : <Badge tone="success" dot plain>ON PACE</Badge>) },
        ]} />
      </Card>
    </div>
  );
}
