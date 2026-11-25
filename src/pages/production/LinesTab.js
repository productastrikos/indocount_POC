import React from 'react';
import { Badge, DataTable, Drawer, Icon } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { Empty, ErrorBox, KV, Loading, StatusBadge } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { dayTime, effTone, hhmm, num, pct } from '../../utils';
import { Eff, HealthBar, LINE_TONE, Sub, lineSeries } from './shared';

function LineTile({ ln, onOpen }) {
  const tone = LINE_TONE[ln.status] || 'neutral';
  return (
    <button type="button" onClick={() => onOpen(ln.id)}
      className="card card-hoverable text-left p-3 flex flex-col gap-1.5 min-w-0"
      style={{ borderLeft: `3px solid var(--color-${tone})` }}>
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[13px] font-bold text-fg truncate flex-1">{ln.name}</span>
        <StatusBadge status={ln.status} tone={tone} />
      </div>
      <div className="text-[11px] text-fg-muted truncate">
        {ln.customer ? <>{ln.customer} · <span className="font-mono">{ln.sku}</span></> : 'No active order'}
        {ln.po && <span className="text-fg-faint"> · {ln.po}</span>}
      </div>
      {ln.status_reason && <div className="text-[10.5px] truncate" style={{ color: `var(--color-${tone})` }}>{ln.status_reason}</div>}
      <div className="grid grid-cols-3 gap-1 mt-0.5">
        <div><p className="label-caps">Actual</p><p className="text-sm font-bold text-fg tabular-nums">{num(ln.actual)}</p></div>
        <div><p className="label-caps">Target</p><p className="text-sm font-semibold text-fg-muted tabular-nums">{num(ln.target_today)}</p></div>
        <div><p className="label-caps">Eff.</p><p className="text-sm"><Eff value={ln.efficiency_pct} /></p></div>
        <div><p className="label-caps">Per hour</p><p className="text-[12px] font-semibold text-fg tabular-nums">{num(ln.output_per_hour)}<span className="text-fg-faint font-normal">/{num(ln.rated_per_hour)}</span></p></div>
        <div><p className="label-caps">Downtime</p><p className="text-[12px] font-semibold text-fg tabular-nums">{num(ln.downtime_min)} min</p></div>
        <div><p className="label-caps">Shift</p><p className="text-[12px] font-semibold text-fg">{ln.active_shift || '—'}</p></div>
      </div>
      {ln.constraint && (
        <div className="text-[10.5px] text-warning flex items-center gap-1"><Icon name="alert" size={11} />Constraint: {ln.constraint} (−{ln.constraint_loss_pct}%)</div>
      )}
    </button>
  );
}

export default function LinesTab({ lines, factories, onOpen }) {
  if (!lines) return <Loading rows={4} height={90} />;
  const groups = factories.filter((f) => lines.some((l) => l.factory === f.code));
  return (
    <div className="space-y-4">
      {groups.map((f) => {
        const rows = lines.filter((l) => l.factory === f.code);
        const running = rows.filter((l) => l.status === 'Running').length;
        return (
          <div key={f.code}>
            {groups.length > 1 && (
              <p className="label-caps mb-2">{f.name}, {f.state} · {running}/{rows.length} running</p>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {rows.map((ln) => <LineTile key={ln.id} ln={ln} onOpen={onOpen} />)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function LineDrawer({ id, onClose, onOpenMachine, onOpenOrder }) {
  const { data, loading, error, reload } = useApi(`/api/production/lines/${id}`, {}, { every: 2 });
  const ln = data?.line;
  const wf = data?.workforce;
  const present = (wf?.employees || []).filter((e) => e.attendance === 'Present' || e.attendance === 'Late');
  return (
    <Drawer eyebrow="Production line" title={ln ? `${ln.name} · ${ln.factory}` : 'Loading…'} width={720} onClose={onClose}
      headerExtra={ln && <StatusBadge status={ln.status} tone={LINE_TONE[ln.status]} />}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <>
          <KV cols={4} items={[
            ['Status', ln.status_reason ? `${ln.status} — ${ln.status_reason}` : ln.status], ['Type', ln.line_type],
            ['Planned rate', `${num(ln.planned_rate)}/h`], ['Effective rate', `${num(ln.effective_rate)}/h`],
          ]} />
          {ln.perf_modifier < 1 && <p className="tile tone-warning text-xs mt-3">Running at {pct(ln.perf_modifier * 100, 0)} of standard speed.</p>}

          <Sub>Hourly output today vs target and yesterday</Sub>
          {data.hourly.length ? (
            <MiniChart type="bar" height={190} legend data={{
              labels: data.hourly.map((h) => h.hour),
              datasets: [
                { label: 'Target', data: data.hourly.map((h) => h.planned), color: 'chart-8' },
                { label: 'Actual', data: data.hourly.map((h) => h.actual), color: data.hourly.map((h) => effTone(h.planned ? (h.actual / h.planned) * 100 : 100)) },
                lineSeries('Yesterday', data.hourly.map((h) => h.yesterday), 'chart-5', { borderDash: [4, 3] }),
              ],
            }} />
          ) : <Empty>No production recorded today.</Empty>}

          <Sub>Machine condition</Sub>
          <DataTable rowKey="id" rows={data.machines} onRowClick={(m) => onOpenMachine(m.id)} columns={[
            { key: 'code', header: 'Machine', strong: true, render: (m) => <span>{m.code} <span className="text-fg-faint font-normal">{m.name}</span></span> },
            { key: 'stage', header: 'Stage' },
            { key: 'status', header: 'Status', render: (m) => <StatusBadge status={m.status} /> },
            { key: 'health', header: 'Health', render: (m) => <HealthBar score={m.health_score} /> },
            { key: 'vib', header: 'Vibration', render: (m) => <span className="tabular-nums">{m.vibration} <span className="text-fg-faint">/ {m.baseline.vibration}</span></span> },
            { key: 'temp', header: 'Temp °C', render: (m) => <span className="tabular-nums">{m.temperature} <span className="text-fg-faint">/ {m.baseline.temperature}</span></span> },
          ]} />

          <Sub>Workforce · current shift</Sub>
          <KV cols={4} items={[
            ['Deployed / required', `${wf.workers_deployed} / ${wf.workers_required}`], ['Utilization', pct(wf.labour_utilization_pct)],
            ['Idle', `${num(wf.idle_hours, 1)} h`], ['Cost / pillow', wf.labour_cost_per_pillow != null ? `$${wf.labour_cost_per_pillow.toFixed(3)}` : '—'],
          ]} />
          <div className="mt-2">
            <DataTable rowKey="id" rows={present} emptyText="No one on shift." columns={[
              { key: 'code', header: 'ID', mono: true },
              { key: 'name', header: 'Name', strong: true },
              { key: 'role', header: 'Role' },
              { key: 'units', header: 'Units', render: (e) => num(e.units) },
              { key: 'productivity_pct', header: 'Productivity', render: (e) => (e.productivity_pct == null ? '—' : (
                <span className="flex items-center gap-1.5"><Eff value={e.productivity_pct} good={90} warn={80} />{e.below_benchmark && <Badge tone="danger">LOW</Badge>}</span>)) },
              { key: 'idle_min', header: 'Idle', render: (e) => `${num(e.idle_min)} min` },
            ]} />
          </div>

          <Sub>Production order queue</Sub>
          <DataTable rowKey="id" rows={data.queue} emptyText="No orders queued." onRowClick={(p) => onOpenOrder(p.id)} columns={[
            { key: 'number', header: 'Order', strong: true },
            { key: 'sku', header: 'SKU', mono: true },
            { key: 'customer', header: 'Customer' },
            { key: 'remaining_qty', header: 'Remaining', render: (p) => num(p.remaining_qty) },
            { key: 'estimated_completion', header: 'Est. completion', render: (p) => dayTime(p.estimated_completion) },
            { key: 'status', header: 'Status', render: (p) => <StatusBadge status={p.status} /> },
          ]} />

          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <Sub>Downtime events today</Sub>
              {data.downtime.length === 0 ? <Empty>No downtime today.</Empty> : (
                <ul className="space-y-1.5">
                  {data.downtime.map((d, i) => (
                    <li key={i} className="tile text-[11px] flex gap-2">
                      <span className="font-mono text-fg-faint">{hhmm(d.start)}</span>
                      <span className="flex-1 min-w-0"><b className="text-fg">{d.category}</b> · <span className="text-fg-muted">{d.reason}</span></span>
                      <span className="font-semibold text-fg tabular-nums">{num(d.minutes, 1)} min</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <Sub>Recent changeovers</Sub>
              {data.changeovers.length === 0 ? <Empty>No changeovers recorded.</Empty> : (
                <ul className="space-y-1.5">
                  {data.changeovers.map((c) => (
                    <li key={c.id} className={`tile text-[11px] ${c.flagged ? 'tone-warning' : ''}`}>
                      <div className="flex justify-between gap-2"><span className="font-mono text-fg">{c.prev_sku} → {c.new_sku}</span><span className="text-fg-faint">{dayTime(c.start)}</span></div>
                      <div className="text-fg-muted">{c.in_progress ? 'In progress' : `${num(c.duration_min, 1)} min (std ${c.standard_min})`}{c.lost_min > 0 && <b className="text-warning"> · {num(c.lost_min, 1)} min lost</b>}</div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </Drawer>
  );
}
