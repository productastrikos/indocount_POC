import React, { useState } from 'react';
import { Badge, Button, Card, DataTable, Drawer, Icon, Modal } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { axisOptions } from '../../components/charts/chartTheme';
import { Empty, ErrorBox, KV, Loading, StatusBadge } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { post } from '../../api/client';
import { dateTime, dayTime, healthTone, hhmm, num, titleCase } from '../../utils';
import { HealthBar, RISK_TONE, Sub } from './shared';

const METRICS = ['vibration', 'temperature', 'rpm', 'pressure', 'power'];
const SHORT = { rpm: 'RPM', temperature: 'Temp', vibration: 'Vibration', pressure: 'Pressure', power: 'Power' };

function telemetryScales() {
  const sc = axisOptions({ grid: true });
  return { x: { ...sc.x, ticks: { ...sc.x.ticks, maxTicksLimit: 6, maxRotation: 0 } }, y: { ...sc.y, beginAtZero: false } };
}

function defaultStart(rec, simTime) {
  if (!simTime) return '';
  if (rec?.window_start) return rec.window_start.slice(0, 16);
  if (rec?.suggested_window?.startsWith('Non-production')) return `${simTime.slice(0, 10)}T23:00`;
  return simTime.slice(0, 16);
}

/** Create a maintenance work order from a recommendation or a machine. */
export function WorkOrderModal({ machine, rec, onClose, onDone }) {
  const { act, sim } = useApp();
  const [form, setForm] = useState({
    wo_type: rec ? 'Predictive' : 'Preventive',
    description: rec ? `${rec.recommended_action} — ${rec.anomaly}` : `Inspect ${machine.name}`,
    scheduled_start: defaultStart(rec, sim?.sim_time),
    duration_min: 60,
  });
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const wo = await act(() => post('/api/production/maintenance/work-orders', {
        machine_id: machine.id, wo_type: form.wo_type, description: form.description,
        scheduled_start: form.scheduled_start || null, duration_min: Number(form.duration_min) || 60,
      }), (r) => `${r.number} scheduled for ${r.machine}`);
      onDone?.(wo);
      onClose();
    } catch (e) { /* toast shown */ } finally { setBusy(false); }
  };
  return (
    <Modal eyebrow="Maintenance" title={`Work order · ${machine.code} ${machine.name}`} width={480} onClose={onClose}
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button icon="wrench" disabled={busy} onClick={submit}>{busy ? 'Creating…' : 'Acknowledge & create'}</Button></>}>
      <div className="space-y-3">
        {rec && (
          <div className="tile tone-warning text-xs">
            <b>{rec.risk} risk</b> · health {num(rec.health_score)} · median time to failure (P50) {num(rec.est_hours_to_maintenance)} operating h
            <div className="text-fg-muted mt-1">Suggested window: {rec.suggested_window}</div>
          </div>
        )}
        <label className="block"><span className="label-caps field-label">Type</span>
          <select className="select" value={form.wo_type} onChange={(e) => setForm({ ...form, wo_type: e.target.value })}>
            {['Predictive', 'Preventive', 'Corrective'].map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label className="block"><span className="label-caps field-label">Description</span>
          <textarea className="textarea" rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block"><span className="label-caps field-label">Scheduled start (sim time)</span>
            <input className="input" type="datetime-local" value={form.scheduled_start} onChange={(e) => setForm({ ...form, scheduled_start: e.target.value })} />
          </label>
          <label className="block"><span className="label-caps field-label">Duration (min)</span>
            <input className="input" type="number" min={15} step={15} value={form.duration_min} onChange={(e) => setForm({ ...form, duration_min: e.target.value })} />
          </label>
        </div>
        <p className="text-[11px] text-fg-faint">Starting the work order puts the machine and its line into maintenance; production estimates are recalculated.</p>
      </div>
    </Modal>
  );
}

function WoActions({ wo, onDone }) {
  const { act, can } = useApp();
  const [busy, setBusy] = useState(false);
  const allowed = can('maintenance.write');
  const run = async (action, msg) => {
    setBusy(true);
    try { await act(() => post(`/api/production/maintenance/work-orders/${wo.id}/${action}`), msg); onDone?.(); } catch (e) { /* toast */ } finally { setBusy(false); }
  };
  if (wo.status === 'Completed') return <span className="text-[11px] text-fg-faint">Done {dayTime(wo.completed_at)}</span>;
  return (
    <div className="flex gap-1.5" onClick={(e) => e.stopPropagation()}>
      {wo.status !== 'In Progress' && (
        <Button size="sm" variant="secondary" disabled={!allowed || busy} onClick={() => run('start', `${wo.number} started — ${wo.machine} in maintenance`)}>Start</Button>
      )}
      {wo.status === 'In Progress' && (
        <Button size="sm" disabled={!allowed || busy} onClick={() => run('complete', `${wo.number} completed — ${wo.machine} restored`)}>Complete</Button>
      )}
    </div>
  );
}

function woColumns(onDone) {
  return [
    { key: 'number', header: 'Work order', strong: true },
    { key: 'machine', header: 'Machine', render: (w) => <span>{w.machine} <span className="text-fg-faint">{w.machine_name}</span><span className="block text-[10px] text-fg-faint">{w.line} · {w.factory}</span></span> },
    { key: 'type', header: 'Type' },
    { key: 'priority', header: 'Priority', render: (w) => <Badge tone={w.priority === 'Critical' || w.priority === 'High' ? 'danger' : 'warning'}>{w.priority}</Badge> },
    { key: 'window', header: 'Window', render: (w) => (w.status === 'In Progress' ? `Until ${dayTime(w.expected_completion)}` : dayTime(w.scheduled_start)) },
    { key: 'status', header: 'Status', render: (w) => <StatusBadge status={w.status} /> },
    { key: 'actions', header: '', render: (w) => <WoActions wo={w} onDone={onDone} /> },
  ];
}

export default function MachinesTab({ factory, onOpen }) {
  const { can } = useApp();
  const maint = useApi('/api/production/maintenance', { factory }, { every: 2 });
  const machines = useApi('/api/production/machines', { factory }, { every: 3 });
  const [wo, setWo] = useState(null);
  const reloadAll = () => { maint.reload(); machines.reload(); };
  const recs = maint.data?.recommendations || [];
  const list = machines.data || [];
  const counts = list.reduce((a, m) => { a[m.health] = (a[m.health] || 0) + 1; return a; }, {});
  const down = list.filter((m) => m.status === 'Breakdown' || m.status === 'Maintenance');

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[['Healthy (85–100)', counts.Healthy || 0, 'success'], ['Warning (65–84)', counts.Warning || 0, 'warning'],
          ['Critical (<65)', counts.Critical || 0, 'danger'], ['Breakdown / maintenance', down.length, down.length ? 'danger' : 'success']].map(([l, v, t]) => (
            <div key={l} className="tile"><p className="label-caps">{l}</p><p className="text-2xl font-bold" style={{ color: `var(--color-${t})` }}>{v}</p></div>
        ))}
      </div>

      <Card title={<span className="flex items-center gap-2">Predictive maintenance recommendations</span>}
        subtitle={maint.data?.disclaimer} padded={false}>
        {maint.loading && !maint.data && <div className="p-4"><Loading /></div>}
        {maint.data && recs.length === 0 && <Empty>No machine currently needs maintenance.</Empty>}
        <div className="divide-y divide-line">
          {recs.map((r) => (
            <div key={r.machine_id} className="px-4 py-3 flex gap-3 items-start flex-wrap">
              <div className="w-14 text-center shrink-0">
                <p className="text-xl font-bold" style={{ color: `var(--color-${healthTone(r.health_score)})` }}>{num(r.health_score)}</p>
                <p className="label-caps">health</p>
              </div>
              <div className="flex-1 min-w-[240px]">
                <div className="flex items-center gap-2 flex-wrap">
                  <button type="button" className="text-[13px] font-bold text-fg hover:text-primary" onClick={() => onOpen(r.machine_id)}>{r.machine} · {r.name}</button>
                  <span className="text-[11px] text-fg-faint">{r.line} · {r.factory}</span>
                  <Badge tone={RISK_TONE[r.risk]}>{r.risk.toUpperCase()} RISK</Badge>
                </div>
                <p className="text-[11.5px] text-fg-muted mt-0.5">{r.anomaly}</p>
                <p className="text-[11.5px] text-fg mt-0.5"><b>Action:</b> {r.recommended_action} · <b>Window:</b> {r.suggested_window}</p>
              </div>
              <div className="text-right shrink-0">
                <p className="label-caps" title="P50 of the remaining-useful-life distribution">Median time to failure (P50)</p>
                <p className="text-sm font-bold text-fg">{num(r.est_hours_to_maintenance)} op h</p>
                {r.has_work_order
                  ? <Badge tone="info">WORK ORDER OPEN</Badge>
                  : <Button size="sm" icon="wrench" className="mt-1" disabled={!can('maintenance.write')}
                      onClick={() => setWo({ machine: { id: r.machine_id, code: r.machine, name: r.name }, rec: r })}>Create work order</Button>}
              </div>
            </div>
          ))}
        </div>
      </Card>

      {maint.data?.work_orders?.length > 0 && (
        <Card title="Maintenance work orders" padded={false}>
          <DataTable rowKey="id" rows={maint.data.work_orders} onRowClick={(w) => onOpen(w.machine_id)} columns={woColumns(reloadAll)} />
        </Card>
      )}

      <Card title="Machine condition" subtitle="Sorted by health score; current reading / baseline" padded={false}>
        {machines.loading && !machines.data && <div className="p-4"><Loading /></div>}
        <DataTable rowKey="id" rows={list} onRowClick={(m) => onOpen(m.id)} columns={[
          { key: 'code', header: 'Machine', strong: true, render: (m) => <span>{m.code}<span className="block text-[10px] text-fg-faint font-normal">{m.name}</span></span> },
          { key: 'line', header: 'Line', render: (m) => <span>{m.line}<span className="block text-[10px] text-fg-faint">{m.factory} · {m.stage}</span></span> },
          { key: 'status', header: 'Status', render: (m) => <StatusBadge status={m.status} /> },
          { key: 'health', header: 'Health', render: (m) => <HealthBar score={m.health_score} /> },
          ...['rpm', 'temperature', 'vibration', 'pressure', 'power'].map((k) => ({
            key: k, header: SHORT[k],
            render: (m) => {
              const dev = m.baseline[k] ? (m[k] / m.baseline[k] - 1) * 100 : 0;
              const warn = k === 'vibration' ? dev > 25 : Math.abs(dev) > 6;
              return <span className={`tabular-nums ${warn && m.status === 'Running' ? 'text-danger font-semibold' : ''}`}>{m[k]}<span className="text-fg-faint font-normal"> / {m.baseline[k]}</span></span>;
            },
          })),
          { key: 'hours_since_maintenance', header: 'Since service', render: (m) => `${num(m.hours_since_maintenance)} h` },
        ]} />
      </Card>
      {wo && <WorkOrderModal machine={wo.machine} rec={wo.rec} onClose={() => setWo(null)} onDone={reloadAll} />}
    </div>
  );
}

export function MachineDrawer({ id, onClose }) {
  const { can } = useApp();
  const { data, loading, error, reload } = useApi(`/api/production/machines/${id}`, {}, { every: 3 });
  const [wo, setWo] = useState(false);
  const mc = data?.machine;
  const tel = data?.telemetry || [];
  const labels = tel.map((t, i) => (i === 0 || t.ts.slice(0, 10) !== tel[i - 1].ts.slice(0, 10) ? dayTime(t.ts) : hhmm(t.ts)));
  const pen = data ? Object.entries(data.health_components.penalties) : [];
  return (
    <Drawer eyebrow="Machine health" title={mc ? `${mc.code} · ${mc.name}` : 'Loading…'} width={760} onClose={onClose}
      headerExtra={mc && <><Badge tone={healthTone(mc.health_score)}>{mc.health.toUpperCase()} {num(mc.health_score)}</Badge><StatusBadge status={mc.status} /></>}
      footer={mc && (
        <>
          <span className="text-[10.5px] text-fg-faint mr-auto">{data.disclaimer}</span>
          <Button size="sm" icon="wrench" disabled={!can('maintenance.write')} onClick={() => setWo(true)}>Create work order</Button>
        </>
      )}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <>
          <KV cols={4} items={[
            ['Line', `${mc.line} · ${mc.factory}`], ['Stage', mc.stage], ['Operating hours', num(mc.operating_hours)],
            ['Since service', `${num(mc.hours_since_maintenance)} h`],
          ]} />
          {mc.unavailable_until && <p className="tile tone-danger text-xs mt-3">Unavailable until {dateTime(mc.unavailable_until)} — line availability reduced; order estimates recalculated.</p>}

          {data.prediction && (
            <div className="tile tone-warning mt-4">
              <div className="flex items-center gap-2 flex-wrap">
                <Icon name="alert" size={14} />
                <span className="text-xs font-bold text-fg">Maintenance likely required</span>
                <Badge tone={RISK_TONE[data.prediction.risk]}>{data.prediction.risk.toUpperCase()} RISK</Badge>
              </div>
              <div className="grid grid-cols-3 gap-3 mt-2 text-[11.5px]">
                <div><p className="label-caps">Identified anomaly</p><p className="text-fg">{data.prediction.anomaly}</p></div>
                <div><p className="label-caps" title="P50 of the remaining-useful-life distribution">Median time to failure (P50)</p><p className="text-fg font-semibold">{num(data.prediction.est_hours_to_maintenance)} operating h</p>
                  <p className="label-caps mt-1">Suggested window</p><p className="text-fg">{data.prediction.suggested_window}</p></div>
                <div><p className="label-caps">Recommended action</p><p className="text-fg">{data.prediction.recommended_action}</p></div>
              </div>
            </div>
          )}

          <Sub>Telemetry vs baseline (10-min resolution)</Sub>
          {tel.length === 0 ? <Empty>No telemetry recorded.</Empty> : (
            <div className="grid sm:grid-cols-2 gap-3">
              {METRICS.map((k) => (
                <div key={k} className="tile">
                  <div className="flex justify-between items-baseline mb-1">
                    <p className="label-caps">{titleCase(k)} ({data.units[k]})</p>
                    <span className="text-[11px] text-fg-muted">now <b className="text-fg">{mc[k]}</b> · base {mc.baseline[k]}</span>
                  </div>
                  <MiniChart type="line" height={120}
                    thresholds={k === 'vibration' ? [{ value: data.thresholds.vibration_alarm, tone: 'danger' }] : undefined}
                    options={{ scales: telemetryScales() }}
                    data={{
                      labels,
                      datasets: [
                        { label: titleCase(k), data: tel.map((t) => t[k]), color: k === 'vibration' ? 'danger' : 'primary', fill: false, borderWidth: 1.6 },
                        { label: 'Baseline', data: tel.map(() => mc.baseline[k]), color: 'chart-8', fill: false, borderDash: [5, 4], borderWidth: 1.2 },
                      ],
                    }} />
                </div>
              ))}
              <div className="tile">
                <p className="label-caps mb-1">Health score breakdown</p>
                <p className="text-[11px] text-fg-muted mb-1">100 − penalties = <b className="text-fg">{num(data.health_components.total, 1)}</b> (85+ healthy, 65–84 warning, &lt;65 critical)</p>
                {pen.length ? (
                  <MiniChart type="bar" horizontal height={110} data={{
                    labels: pen.map(([k]) => titleCase(k)),
                    datasets: [{ label: 'Penalty points', data: pen.map(([, v]) => v), color: 'warning' }],
                  }} />
                ) : <p className="text-xs text-success">No penalties.</p>}
              </div>
            </div>
          )}

          <Sub>Anomaly detection</Sub>
          {data.anomalies.length === 0 ? <p className="text-xs text-success">No abnormal operating pattern detected.</p> : (
            <ul className="space-y-1.5">
              {data.anomalies.map((a) => (
                <li key={a.metric} className="tile tone-danger text-[11.5px] flex justify-between gap-2">
                  <span className="text-fg">{a.message}</span>
                  <span className="text-fg-faint whitespace-nowrap">{a.severity}× tolerance</span>
                </li>
              ))}
            </ul>
          )}

          <Sub>Work orders</Sub>
          {data.work_orders.length === 0 ? <p className="text-xs text-fg-faint">No work orders for this machine.</p>
            : <DataTable rowKey="id" rows={data.work_orders} columns={woColumns(reload)} />}

          <Sub>Maintenance history</Sub>
          <ul className="space-y-1">
            {data.maintenance_history.map((h, i) => (
              <li key={i} className="text-[11px] flex gap-2"><span className="font-mono text-fg-faint whitespace-nowrap">{dateTime(h.date)}</span><span className="text-fg">{h.type}</span><span className="text-fg-muted truncate">{h.notes}</span></li>
            ))}
          </ul>
        </>
      )}
      {wo && mc && <WorkOrderModal machine={mc} rec={data.prediction} onClose={() => setWo(false)} onDone={reload} />}
    </Drawer>
  );
}
