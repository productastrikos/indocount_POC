import React, { useState } from 'react';
import { Badge, Card, DataTable, FilterChips } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { Empty, ErrorBox, Kpi, Loading } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { dayTime, num } from '../../utils';

export default function ChangeoversTab({ factory, onOpenLine }) {
  const [days, setDays] = useState(2);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const { data, loading, error, reload } = useApi('/api/production/changeovers', { factory, days }, { every: 3 });
  if (loading && !data) return <Loading rows={5} />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  const events = flaggedOnly ? data.events.filter((e) => e.flagged) : data.events;
  const top = data.top_lines.filter((t) => t.lost_min > 0);
  const durs = data.events.filter((e) => e.duration_min);
  const avg = durs.length ? durs.reduce((a, e) => a + e.duration_min, 0) / durs.length : 0;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <FilterChips label="Period" value={days} onChange={setDays} options={[{ value: 1, label: 'Today' }, { value: 2, label: 'Today + yesterday' }, { value: 7, label: '7 days' }]} />
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Kpi label="Standard duration" value={`${num(data.standard_min, 1)} min`} sub="Per customer SKU changeover" />
        <Kpi label="Changeovers" value={num(data.events.length)} sub={`Average ${num(avg, 1)} min`} />
        <Kpi label="Over standard" value={num(data.flagged)} tone={data.flagged ? 'warning' : 'success'} sub="Flagged events" onClick={() => setFlaggedOnly((f) => !f)} active={flaggedOnly} />
        <Kpi label="Time lost" value={`${num(data.total_lost_min, 1)} min`} tone={data.total_lost_min > 15 ? 'danger' : undefined} sub="Beyond the standard" />
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card title="Lines losing the most time to changeovers" subtitle="Minutes beyond standard">
          {top.length ? (
            <MiniChart type="bar" horizontal height={Math.max(140, top.length * 26)} data={{
              labels: top.map((t) => (factory === 'ALL' ? `${t.line} ${t.factory}` : t.line)),
              datasets: [{ label: 'Minutes lost', data: top.map((t) => t.lost_min), color: top.map((t, i) => (i === 0 ? 'danger' : 'warning')) }],
            }} />
          ) : <Empty>No time lost to changeovers.</Empty>}
        </Card>
        <Card title="Changeover log" subtitle={flaggedOnly ? 'Showing only changeovers over standard' : 'Every SKU change on every line'} padded={false} className="xl:col-span-2">
          <div className="max-h-[460px] overflow-y-auto">
            <DataTable rowKey="id" rows={events} onRowClick={(e) => onOpenLine(e.line_id)} emptyText="No changeovers in this period." columns={[
              { key: 'line', header: 'Line', strong: true, render: (e) => <span>{e.line} <span className="text-fg-faint font-normal">{e.factory}</span></span> },
              { key: 'skus', header: 'Previous → new SKU', render: (e) => <span className="font-mono text-[11px] text-fg">{e.prev_sku || '—'} → {e.new_sku}</span> },
              { key: 'customer', header: 'Customer' },
              { key: 'start', header: 'Start', render: (e) => dayTime(e.start) },
              { key: 'end', header: 'Completed', render: (e) => (e.in_progress ? <Badge tone="info" dot>IN PROGRESS</Badge> : dayTime(e.end)) },
              { key: 'duration_min', header: 'Duration', render: (e) => (e.duration_min != null ? `${num(e.duration_min, 1)} min` : '—') },
              { key: 'lost_min', header: 'Time lost', render: (e) => (e.lost_min > 0 ? <b className="text-warning">{num(e.lost_min, 1)} min</b> : '—') },
              { key: 'flagged', header: '', render: (e) => (e.flagged ? <Badge tone="warning">OVER STANDARD</Badge> : e.in_progress ? null : <Badge tone="success" plain>OK</Badge>) },
            ]} />
          </div>
        </Card>
      </div>
    </div>
  );
}
