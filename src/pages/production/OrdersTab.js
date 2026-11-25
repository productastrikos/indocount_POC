import React, { useState } from 'react';
import { Badge, Button, DataTable, Drawer, FilterChips, Icon, ProgressBar } from '../../components/ui';
import { BarRow, Empty, ErrorBox, KV, Loading, StatusBadge } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { post } from '../../api/client';
import { dayTime, num, pct, statusTone } from '../../utils';
import { RISK_TONE, Sub } from './shared';

const STATUS_FILTERS = ['All', 'In Progress', 'Delayed', 'Ready', 'Planned', 'Completed'];

export default function OrdersTab({ orders, onOpen }) {
  const [status, setStatus] = useState('All');
  if (!orders) return <Loading rows={6} />;
  const rows = status === 'All' ? orders : orders.filter((o) => o.status === status);
  const counts = Object.fromEntries(STATUS_FILTERS.map((s) => [s, s === 'All' ? orders.length : orders.filter((o) => o.status === s).length]));
  return (
    <div className="card overflow-hidden">
      <div className="px-4 py-3 border-b flex items-center gap-3 flex-wrap">
        <FilterChips label="Status" value={status} onChange={setStatus}
          options={STATUS_FILTERS.map((s) => ({ value: s, label: `${s} (${counts[s]})` }))} />
        <span className="text-[11px] text-fg-faint ml-auto">Each production order is linked to a customer order and its raw materials</span>
      </div>
      <DataTable rowKey="id" rows={rows} onRowClick={(o) => onOpen(o.id)} emptyText="No production orders in this view." columns={[
        { key: 'number', header: 'Prod. order', strong: true, render: (o) => (
          <span className="flex items-center gap-1.5">{o.number}{o.material_risk && <span title={o.risk_reason}><Icon name="layers" size={13} className="text-danger" /></span>}</span>) },
        { key: 'customer', header: 'Customer', render: (o) => <span>{o.customer}<span className="block text-[10px] text-fg-faint">{o.order || '—'}</span></span> },
        { key: 'sku', header: 'SKU', render: (o) => <span><span className="font-mono text-[11px] text-fg">{o.sku}</span><span className="block text-[10px] text-fg-faint truncate max-w-[180px]">{o.description}</span></span> },
        { key: 'required_qty', header: 'Required', render: (o) => num(o.required_qty) },
        { key: 'completed_qty', header: 'Completed', render: (o) => (
          <div style={{ minWidth: 110 }}>
            <div className="flex justify-between text-[11px]"><span className="tabular-nums">{num(o.completed_qty)}</span><span className="text-fg-faint">{pct(o.progress_pct, 0)}</span></div>
            <ProgressBar value={o.completed_qty} max={o.required_qty} tone={o.status === 'Delayed' ? 'danger' : 'primary'} />
          </div>) },
        { key: 'remaining_qty', header: 'Remaining', render: (o) => num(o.remaining_qty) },
        { key: 'start_date', header: 'Start', render: (o) => dayTime(o.start_date) },
        { key: 'required_completion', header: 'Required by', render: (o) => dayTime(o.required_completion) },
        { key: 'estimated_completion', header: 'Est. completion', render: (o) => (
          <span className={o.late_hours > 0 && o.status !== 'Completed' ? 'text-danger font-semibold' : ''}>
            {o.status === 'Completed' ? dayTime(o.completed_at) : dayTime(o.estimated_completion)}
            {o.late_hours > 0 && o.status !== 'Completed' && <span className="block text-[10px]">+{num(o.late_hours, 1)} h late</span>}
          </span>) },
        { key: 'priority', header: 'Priority', render: (o) => <Badge tone={o.priority === 'High' ? 'danger' : o.priority === 'Medium' ? 'warning' : 'neutral'}>{o.priority}</Badge> },
        { key: 'line', header: 'Line', render: (o) => o.line ? <span>{o.line}<span className="block text-[10px] text-fg-faint">{o.factory} · #{(o.sequence ?? 0) + 1}</span></span> : '—' },
        { key: 'status', header: 'Status', render: (o) => <StatusBadge status={o.status} /> },
      ]} />
    </div>
  );
}

export function OrderDrawer({ id, onClose, onOpenLine }) {
  const { act, can, meta } = useApp();
  const { data, loading, error, reload } = useApi(`/api/production/orders/${id}`, {}, { every: 2 });
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const po = data?.po;
  const open = po && !['Completed'].includes(po.status);
  const allowed = can('production.write');
  const lines = po ? meta.lines.filter((l) => l.factory_id === po.factory_id) : [];

  const run = async (fn, msg) => {
    setBusy(true);
    try { await act(fn, msg); reload(); } catch (e) { /* toast shown */ } finally { setBusy(false); }
  };

  const co = data?.customer_order;
  return (
    <Drawer eyebrow="Production order" title={po ? `${po.number} · ${po.sku}` : 'Loading…'} width={680} onClose={onClose}
      headerExtra={po && <StatusBadge status={po.status} />}
      footer={po && (
        <>
          <span className="text-[10.5px] text-fg-faint mr-auto">{allowed ? 'Changes re-estimate completion and shipment risk immediately.' : 'Your role cannot change production orders.'}</span>
          <select className="select select-sm" value={target} onChange={(e) => setTarget(e.target.value)} disabled={!open || !allowed}
            aria-label="Target line">
            <option value="">Best available line</option>
            {lines.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
          <Button size="sm" variant="secondary" icon="route" disabled={!open || !allowed || busy}
            onClick={() => run(() => post(`/api/production/orders/${id}/reallocate`, { line_id: target ? Number(target) : null }),
              (r) => `${r.number} reallocated to ${r.line}`)}>Reallocate</Button>
          <Button size="sm" icon="trendUp" disabled={!open || !allowed || busy}
            onClick={() => run(() => post(`/api/production/orders/${id}/prioritize`), (r) => `${r.number} moved to the front of ${r.line}`)}>Prioritize</Button>
        </>
      )}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <>
          <KV cols={3} items={[
            ['Customer', po.customer], ['Customer order', po.order || '—'], ['Priority', po.priority],
            ['Line', po.line ? <button type="button" className="link-btn text-[13px]" onClick={() => onOpenLine(po.line_id)}>{po.line}</button> : '—'],
            ['Queue position', data.queue_position ? `${data.queue_position} of ${data.queue_length}` : '—'], ['Started', dayTime(po.started_at)],
            ['Required by', dayTime(po.required_completion)],
            ['Est. completion', <span className={po.late_hours > 0 ? 'text-danger' : 'text-success'}>{po.status === 'Completed' ? dayTime(po.completed_at) : dayTime(po.estimated_completion)}</span>],
            ['Schedule', po.status === 'Completed' ? 'Completed' : po.late_hours > 0 ? `${num(po.late_hours, 1)} h late` : `${num(-(po.late_hours || 0), 1)} h ahead`],
          ]} />
          {po.material_risk && <p className="tile tone-danger text-xs mt-3 flex gap-2"><Icon name="layers" size={14} />Material risk: {po.risk_reason}</p>}

          <Sub>Manufacturing progress · {num(po.completed_qty)} of {num(po.required_qty)} pillows</Sub>
          <div className="space-y-2.5">
            {data.progress.map((s) => (
              <BarRow key={s.stage} label={s.stage} value={s.qty} max={po.required_qty} display={num(s.qty)}
                tone={s.stage.startsWith('Blocked') ? 'danger' : s.stage.startsWith('Stored') ? 'success' : 'primary'} />
            ))}
          </div>
          {Object.keys(data.cartons).length > 0 && (
            <p className="text-[11px] text-fg-muted mt-2">Cartons: {Object.entries(data.cartons).map(([k, v]) => `${k} ${v}`).join(' · ')}</p>
          )}

          <Sub>Raw materials for the remaining {num(po.remaining_qty)} pillows</Sub>
          <DataTable rowKey="material_id" rows={data.materials} columns={[
            { key: 'code', header: 'Material', strong: true, render: (b) => <span>{b.code}<span className="block text-[10px] text-fg-faint font-normal">{b.name}</span></span> },
            { key: 'role', header: 'Use' },
            { key: 'per_pillow', header: 'Per pillow', render: (b) => `${b.per_pillow} ${b.uom}` },
            { key: 'required', header: 'Required', render: (b) => `${num(b.required, 1)} ${b.uom}` },
            { key: 'available', header: 'Available', render: (b) => `${num(b.available)} ${b.uom}` },
            { key: 'sufficient', header: '', render: (b) => <Badge tone={b.sufficient ? 'success' : 'danger'}>{b.sufficient ? 'OK' : 'SHORT'}</Badge> },
          ]} />

          <Sub>Linked customer order</Sub>
          {co ? (
            <div className="tile">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-bold text-fg">{co.number}</span>
                <span className="text-xs text-fg-muted">{co.customer} · {num(co.qty)} pillows · ships {dayTime(co.scheduled_dispatch)}</span>
                {co.readiness && <StatusBadge status={co.readiness.status} tone={statusTone(co.readiness.status)} />}
                {co.risk?.at_risk && <Badge tone={RISK_TONE[co.risk.level]}>AT RISK · {co.risk.level}</Badge>}
              </div>
              {co.readiness && (
                <div className="grid grid-cols-5 gap-2 mt-3">
                  {Object.entries(co.readiness.ratios).map(([k, v]) => (
                    <div key={k}>
                      <p className="label-caps capitalize">{k}</p>
                      <ProgressBar value={v} tone={v >= 99.9 ? 'success' : 'warning'} />
                      <p className="text-[11px] font-semibold text-fg mt-0.5">{pct(v, 0)}</p>
                    </div>
                  ))}
                </div>
              )}
              {co.risk?.reasons?.length > 0 && <p className="text-[11px] text-danger mt-2">{co.risk.reasons.join(' · ')}</p>}
            </div>
          ) : <Empty>Make-to-stock order (no customer order linked).</Empty>}

          <Sub>Order history</Sub>
          {data.events.length === 0 ? <p className="text-xs text-fg-faint">No events recorded for this order yet.</p> : (
            <ul className="space-y-1">
              {data.events.map((e, i) => (
                <li key={i} className="text-[11px] flex gap-2"><span className="font-mono text-fg-faint whitespace-nowrap">{dayTime(e.ts)}</span><span className="text-fg-muted">{e.message}</span></li>
              ))}
            </ul>
          )}
        </>
      )}
    </Drawer>
  );
}
