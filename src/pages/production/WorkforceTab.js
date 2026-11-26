import React, { useEffect, useState } from 'react';
import { Badge, Card, DataTable } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { ErrorBox, Kpi, KV, Loading, StatusBadge } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { money, num, pct } from '../../utils';
import { Eff, scopeLines } from './shared';

const ROLES = ['All roles', 'Sewer', 'Filler', 'Bagger', 'Packaging operator', 'Supervisor', 'Maintenance technician', 'Warehouse associate', 'Forklift operator'];

export default function WorkforceTab({ factory, date, shift, highlight }) {
  const { meta, sim } = useApp();
  const [lineId, setLineId] = useState('');
  const [shiftSel, setShiftSel] = useState(shift || '');
  const [role, setRole] = useState('All roles');
  const [belowOnly, setBelowOnly] = useState(false);
  useEffect(() => { setShiftSel(shift || ''); }, [shift]);
  const { data, loading, error, reload } = useApi('/api/production/workforce',
    { factory, date, shift: shiftSel || undefined, line_id: lineId || undefined }, { every: 3 });
  const lines = scopeLines(meta, factory);

  if (loading && !data) return <Loading rows={6} />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  const rules = data.shift_rules;
  let emps = data.employees;
  if (role !== 'All roles') emps = emps.filter((e) => e.role === role);
  if (belowOnly) emps = emps.filter((e) => e.below_benchmark);
  if (highlight) emps = [...emps].sort((a, b) => (a.id === highlight ? -1 : b.id === highlight ? 1 : 0));
  const idleRows = [...data.lines].filter((l) => l.idle_hours > 0).slice(0, 10);
  const lineName = lineId ? lines.find((l) => String(l.id) === String(lineId))?.name : null;
  const currentShift = sim?.sim_time && sim.sim_time.slice(11, 16) >= '14:30' ? 2 : 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <select className="select select-sm" value={lineId} onChange={(e) => setLineId(e.target.value)} aria-label="Line">
          <option value="">All lines</option>
          {lines.map((l) => <option key={l.id} value={l.id}>{l.name}{factory === 'ALL' ? ` · ${meta.factories.find((f) => f.id === l.factory_id)?.code}` : ''}</option>)}
        </select>
        <select className="select select-sm" value={shiftSel} onChange={(e) => setShiftSel(e.target.value)} aria-label="Shift">
          <option value="">Both shifts</option>
          <option value="1">Shift 1{currentShift === 1 ? ' (active)' : ''}</option>
          <option value="2">Shift 2{currentShift === 2 ? ' (active)' : ''} · +${rules.shift2_differential.toFixed(2)}/h</option>
        </select>
        <span className="text-[11px] text-fg-faint">{lineName ? `${lineName} breakdown` : 'Scope: all lines'} · activity per employee ID from production events (no facial recognition)</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3">
        <Kpi label="Workers required" value={num(data.workers_required)} sub={lineId ? 'Line crew: filler, 2 sewers, bagger per shift' : 'Standard crew for scope'} />
        <Kpi label="Workers deployed" value={num(data.workers_deployed)} tone={data.workers_deployed < data.workers_required ? 'warning' : 'success'}
          sub={`${data.absent} absent / on leave${data.source === 'shift summaries' ? ' · from the closed shift records' : ''}`} />
        <Kpi label="Output per worker" value={num(data.output_per_worker, 1)} unit="pillows"
          sub={`${num(data.pillows)} pillows · ${data.worker_hours ? `${num(data.pillows / data.worker_hours, 1)} per worker-hour` : 'no worker-hours yet'}`} />
        <Kpi label="Idle time" value={num(data.idle_hours, 1)} unit="h" tone={data.idle_hours > 4 ? 'warning' : undefined} sub={`≈ ${num(data.idle_fte, 1)} FTE of a shift`} />
        <Kpi label="Labour utilization" value={pct(data.labour_utilization_pct)} tone={data.labour_utilization_pct < 90 ? 'warning' : 'success'} sub="Productive ÷ available time" />
        <Kpi label="Labour cost / paid hour" value={money(data.labour_cost_per_hour, 2)}
          sub={`${Object.keys(data.deployed_by_shift || {}).length > 1 ? 'Average crew cost per paid hour across shifts' : 'Crew cost per paid hour'} · ${money(data.labour_cost)} over ${num(data.paid_hours, 1)} paid h`} />
        <Kpi label="Labour cost / pillow" value={data.labour_cost_per_pillow != null ? `$${data.labour_cost_per_pillow.toFixed(3)}` : '—'} sub="Incl. shift-2 differential" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card title="Line workforce breakdown" subtitle="Click a line to filter employees" padded={false} className="xl:col-span-2">
          <DataTable rowKey="line_id" rows={data.lines} onRowClick={(r) => setLineId(String(r.line_id))} emptyText="No line crews in scope." columns={[
            { key: 'line', header: 'Line', strong: true, render: (r) => <span>{r.line} <span className="text-fg-faint font-normal">{r.factory}</span></span> },
            { key: 'deployed', header: 'Deployed / req.', render: (r) => <span className={r.deployed < r.required ? 'text-warning font-semibold' : ''}>{r.deployed} / {r.required}</span> },
            { key: 'pillows', header: 'Pillows', render: (r) => num(r.pillows) },
            { key: 'output_per_worker', header: 'Per worker', render: (r) => num(r.output_per_worker, 1) },
            { key: 'idle_hours', header: 'Idle', render: (r) => <span className={r.idle_hours > 1 ? 'text-warning font-semibold' : ''}>{num(r.idle_hours, 1)} h</span> },
            { key: 'labour_cost', header: 'Labour cost', render: (r) => money(r.labour_cost) },
            { key: 'cost_per_pillow', header: '$/pillow', render: (r) => (r.cost_per_pillow != null ? `$${r.cost_per_pillow.toFixed(3)}` : '—') },
          ]} />
        </Card>
        <div className="space-y-4">
          <Card title="Shift rules applied">
            <KV cols={2} items={[
              ['Scheduled presence', `${rules.scheduled_presence_h} h`], ['Unpaid lunch', `${rules.unpaid_lunch_min} min`],
              ['Paid breaks', rules.paid_breaks], ['Productive time', `${rules.productive_min} min`],
              ['Paid time', `${rules.paid_min} min`], ['Shift 2 differential', `+$${rules.shift2_differential.toFixed(2)}/h`],
            ]} />
          </Card>
          {idleRows.length > 0 && (
            <Card title="Idle worker-hours by line">
              <MiniChart type="bar" horizontal height={Math.max(120, idleRows.length * 24)} data={{
                labels: idleRows.map((r) => (factory === 'ALL' ? `${r.line} ${r.factory}` : r.line)),
                datasets: [{ label: 'Idle h', data: idleRows.map((r) => r.idle_hours), color: 'warning' }],
              }} />
            </Card>
          )}
        </div>
      </div>

      <Card title="Employee allocation" subtitle={`${emps.length} employees${lineName ? ` on ${lineName}` : ''}`} padded={false}
        actions={(
          <div className="flex items-center gap-2">
            <select className="select select-sm" value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role">
              {ROLES.map((r) => <option key={r}>{r}</option>)}
            </select>
            <label className="flex items-center gap-1.5 text-[11px] text-fg-muted">
              <input type="checkbox" checked={belowOnly} onChange={(e) => setBelowOnly(e.target.checked)} />
              Below benchmark ({data.below_benchmark.length})
            </label>
          </div>
        )}>
        <div className="max-h-[520px] overflow-y-auto">
          <DataTable rowKey="id" rows={emps} emptyText="No employees match." columns={[
            { key: 'code', header: 'Employee ID', mono: true, render: (e) => <span className={e.id === highlight ? 'text-primary font-bold' : ''}>{e.code}</span> },
            { key: 'name', header: 'Name', strong: true },
            { key: 'role', header: 'Role' },
            { key: 'line', header: 'Line' },
            { key: 'shift', header: 'Shift' },
            { key: 'employment_type', header: 'Type' },
            { key: 'hourly_wage', header: 'Wage', render: (e) => <span>${e.hourly_wage.toFixed(2)}{e.differential > 0 && <span className="block text-[10px] text-fg-faint">incl. +${e.differential.toFixed(2)} diff.</span>}</span> },
            { key: 'attendance', header: 'Attendance', render: (e) => <StatusBadge status={e.attendance} tone={e.attendance === 'Scheduled' || e.attendance === 'Not recorded' ? 'neutral' : undefined} /> },
            { key: 'units', header: 'Contribution', render: (e) => (e.units ? `${num(e.units)} pcs` : '—') },
            { key: 'productive_min', header: 'Productive', render: (e) => `${num(e.productive_min)} min` },
            { key: 'idle_min', header: 'Idle', render: (e) => `${num(e.idle_min)} min` },
            { key: 'productivity_pct', header: 'Productivity', render: (e) => (e.productivity_pct == null ? '—' : (
              <span className="flex items-center gap-1.5"><Eff value={e.productivity_pct} good={90} warn={80} />{e.below_benchmark && <Badge tone="danger">BELOW</Badge>}</span>)) },
            { key: 'labour_cost', header: 'Cost', render: (e) => money(e.labour_cost, 2) },
          ]} />
        </div>
      </Card>
    </div>
  );
}
