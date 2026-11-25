import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Badge, Button, Card, DataTable, Drawer, Icon, Modal, ProgressBar, Tabs } from '../components/ui';
import { FilterChips } from '../components/ui/Tabs';
import { MiniChart } from '../components/charts/ChartCard';
import {
  BarRow, Empty, ErrorBox, InsightsCard, Kpi, KV, Loading, PageTitle, SimTag, StageAlerts, StatusBadge,
} from '../components/common';
import { useApi } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { patch, post } from '../api/client';
import { dateShort, dateTime, dayTime, duration, hhmm, money, num, parseTs, pct } from '../utils';
import CctvWall from '../components/cctv/CctvWall';

const TABS = [
  { value: 'orders', label: 'Customer orders' },
  { value: 'schedule', label: 'Truck & dock schedule' },
  { value: 'risk', label: 'Shipment risk' },
  { value: 'detention', label: 'Detention costs' },
  { value: 'cctv', label: 'Dispatch lane CCTV' },
];
const ORDER_STATUSES = ['Not Ready', 'Production Pending', 'Quality Pending', 'Inventory Pending', 'Picking Pending',
  'Ready for Dispatch', 'Dispatched', 'Delivered'];
const ACTION_PERM = { prioritize: 'production.write', reallocate: 'production.write', expedite_picking: 'warehouse.write', reschedule: 'dispatch.write' };
const ACTION_ICON = { prioritize: 'trendUp', reallocate: 'route', expedite_picking: 'box', reschedule: 'calendar' };
const DONE = ['Dispatched', 'Delivered'];

function parseFocus(search) {
  const f = new URLSearchParams(search).get('focus');
  if (!f) return null;
  const [type, ...rest] = f.split(':');
  const raw = rest.join(':');
  return { key: f, type, id: raw !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw };
}

/* The API reports dispatched/delivered readiness as fractions (1.0); open orders as percent. */
function readyPct(order, value) {
  if (value === null || value === undefined) return 0;
  return DONE.includes(order.status) && value <= 1 ? value * 100 : value;
}

function readinessTone(o) {
  if (o.at_risk) return 'danger';
  if (o.status === 'Ready for Dispatch' || DONE.includes(o.status)) return 'success';
  return o.readiness_pct >= 75 ? 'info' : 'warning';
}

const isoLocal = (s) => (s ? s.slice(0, 16) : '');
const z2 = (n) => String(n).padStart(2, '0');
const localIso = (d) => `${d.getFullYear()}-${z2(d.getMonth() + 1)}-${z2(d.getDate())}T${z2(d.getHours())}:${z2(d.getMinutes())}`;

function useRiskAction() {
  const { act, can } = useApp();
  const [busy, setBusy] = useState(null);
  const run = async (orderId, action, newTime) => {
    setBusy(action);
    try {
      return await act(() => post(`/api/dispatch/risk/${orderId}/action`, { action, new_time: newTime || undefined }),
        (r) => `${r.message} · projected ready ${dayTime(r.ready_eta)}${r.risk?.at_risk ? ' (still at risk)' : ''}`);
    } catch (e) { return null; } finally { setBusy(null); }
  };
  const allowed = (action) => can(ACTION_PERM[action] || 'dispatch.write');
  return { run, busy, allowed };
}

function RecButtons({ orderId, recs, compact = false }) {
  const { run, busy, allowed } = useRiskAction();
  if (!recs?.length) return null;
  return (
    <div className={compact ? 'flex flex-wrap gap-1.5' : 'space-y-1.5'}>
      {recs.map((r) => (
        compact ? (
          <Button key={r.action} size="sm" variant={r.action === 'reschedule' ? 'secondary' : 'soft'} icon={ACTION_ICON[r.action]}
            disabled={!!busy || !allowed(r.action)} title={allowed(r.action) ? r.detail : 'Your role cannot perform this action'}
            onClick={() => run(orderId, r.action, r.new_time)}>
            {busy === r.action ? 'Applying…' : r.label}
          </Button>
        ) : (
          <button key={r.action} type="button" disabled={!!busy || !allowed(r.action)} onClick={() => run(orderId, r.action, r.new_time)}
            className="tile w-full text-left flex items-start gap-2.5 hover:border-primary disabled:opacity-50 disabled:cursor-not-allowed">
            <Icon name={ACTION_ICON[r.action] || 'arrowRight'} size={15} className="text-primary mt-0.5" />
            <span className="min-w-0 flex-1">
              <span className="block text-xs font-semibold text-fg">{busy === r.action ? 'Applying…' : r.label}</span>
              <span className="block text-[10.5px] text-fg-faint leading-snug">{allowed(r.action) ? r.detail : 'Your role cannot perform this action'}</span>
            </span>
          </button>
        )
      ))}
    </div>
  );
}

/* ─── Truck lifecycle ────────────────────────────────────────────────── */
function TruckActions({ truck, picked, onDone, onEdit }) {
  const { act, can } = useApp();
  const [qty, setQty] = useState(picked ?? 0);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setQty(picked ?? 0); }, [picked]);
  const allowed = can('dispatch.write');
  const step = async (action, body, msg) => {
    setBusy(true);
    try {
      const r = await act(() => post(`/api/dispatch/appointments/${truck.id}/${action}`, body), msg);
      onDone?.(r);
    } catch (e) { /* toasted */ } finally { setBusy(false); }
  };
  const dis = busy || !allowed;
  const title = allowed ? undefined : 'Requires dispatch permissions';
  if (truck.status === 'Departed') {
    return <p className="text-[11px] text-fg-muted">Departed {dayTime(truck.departed_at)} with {num(truck.loaded_qty)} pillows{truck.detention_charge ? ` · detention ${money(truck.detention_charge)} (illustrative)` : ''}.</p>;
  }
  if (truck.status === 'Cancelled') return <p className="text-[11px] text-fg-faint">Appointment cancelled.</p>;
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {truck.status === 'Scheduled' && (
        <>
          <Button size="sm" icon="truck" disabled={dis} title={title} onClick={() => step('arrive', undefined, `${truck.ref} checked in at ${truck.dock}`)}>Check in (arrived)</Button>
          {onEdit && <Button size="sm" variant="secondary" icon="calendar" disabled={dis} title={title} onClick={onEdit}>Reschedule</Button>}
          <Button size="sm" variant="ghost" disabled={dis} title={title} onClick={() => step('cancel', undefined, `${truck.ref} cancelled`)}>Cancel</Button>
        </>
      )}
      {truck.status === 'Arrived' && (
        <Button size="sm" icon="play" disabled={dis || !picked} title={!picked ? 'Nothing picked yet for this order' : title}
          onClick={() => step('start-loading', undefined, `Loading started on ${truck.ref}`)}>Start loading</Button>
      )}
      {truck.status === 'Loading' && (
        <>
          <input type="number" className="input select-sm" style={{ width: 96 }} min={0} max={picked || undefined} value={qty}
            aria-label="Loaded quantity" onChange={(e) => setQty(e.target.value)} />
          <Button size="sm" icon="check" disabled={dis} title={title}
            onClick={() => step('confirm-loaded', { loaded_qty: Number(qty) }, `${num(qty)} pillows confirmed loaded on ${truck.ref}`)}>Confirm loaded qty</Button>
        </>
      )}
      {(truck.status === 'Loaded' || truck.status === 'Loading') && (
        <Button size="sm" variant={truck.status === 'Loaded' ? 'primary' : 'secondary'} icon="arrowRight" disabled={dis} title={title}
          onClick={() => step('depart', undefined, `${truck.ref} departed — order dispatched`)}>Confirm departure</Button>
      )}
    </div>
  );
}

/* ─── Appointment modal (create / edit / view) ───────────────────────── */
function AppointmentModal({ mode, appt, factory, simTime, onClose, onSaved }) {
  const { act, can } = useApp();
  const orders = useApi(mode === 'create' ? '/api/dispatch/orders' : null, { factory, view: 'active' }, { live: false });
  const open = (orders.data || []).filter((o) => !DONE.includes(o.status));
  const defaultTime = useMemo(() => {
    const d = parseTs(simTime) || new Date();
    d.setMinutes(0, 0, 0);
    d.setHours(d.getHours() + 2);
    return localIso(d);
  }, [simTime]);
  const [form, setForm] = useState(() => (mode === 'create'
    ? { order_id: '', scheduled_arrival: defaultTime, dock: 'D1', expected_loading_min: 75, carrier: '' }
    : { scheduled_arrival: isoLocal(appt.scheduled_arrival), dock: appt.dock, expected_loading_min: appt.expected_loading_min }));
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const ord = useApi(mode !== 'create' && appt?.order_id ? `/api/dispatch/orders/${appt.order_id}` : null, {}, { every: 2 });
  const live = (mode !== 'create' && ord.data?.trucks.find((t) => t.id === appt.id)) || appt;
  const picked = ord.data?.order.picked;
  const editable = mode === 'create' || live?.status === 'Scheduled';

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = mode === 'create'
        ? await act(() => post('/api/dispatch/appointments', { ...form, order_id: Number(form.order_id), expected_loading_min: Number(form.expected_loading_min), carrier: form.carrier || undefined }),
          (r) => `${r.ref} booked at ${r.dock}, ${dayTime(r.scheduled_arrival)}`)
        : await act(() => patch(`/api/dispatch/appointments/${appt.id}`, { scheduled_arrival: form.scheduled_arrival, dock: form.dock, expected_loading_min: Number(form.expected_loading_min) }),
          (r) => `${r.ref} now ${dayTime(r.scheduled_arrival)} at ${r.dock}${r.dock !== form.dock ? ' (moved to a free dock)' : ''}`);
      onSaved?.(res);
      onClose();
    } catch (e) {
      setError(e.message);
    } finally { setBusy(false); }
  };

  const title = mode === 'create' ? 'New truck appointment' : `${appt.ref} · ${appt.order || ''}`;
  return (
    <Modal title={title} eyebrow="Truck & dock scheduling" width={520} onClose={onClose}
      subtitle={mode === 'create' ? 'Dock conflicts are checked against expected loading windows' : `${appt.customer || ''} · ${appt.carrier || ''} · ${appt.factory}`}
      footer={editable ? <>
        <Button variant="ghost" onClick={onClose}>Close</Button>
        <Button disabled={busy || !can('dispatch.write') || (mode === 'create' && !form.order_id)}
          title={can('dispatch.write') ? undefined : 'Requires dispatch permissions'} onClick={save}>
          {busy ? 'Saving…' : mode === 'create' ? 'Book appointment' : 'Save changes'}
        </Button>
      </> : <Button variant="secondary" onClick={onClose}>Close</Button>}>
      <div className="space-y-3">
        {mode !== 'create' && (
          <div className="flex items-center gap-2 flex-wrap">
            <StatusBadge status={live.status} />
            {ord.data?.order.at_risk && <Badge tone="danger" dot>AT RISK</Badge>}
            {ord.data && <span className="text-[11px] text-fg-muted">Order {ord.data.order.status} · {pct(readyPct(ord.data.order, ord.data.order.readiness_pct), 0)} ready · {num(picked)} of {num(ord.data.order.qty)} picked</span>}
          </div>
        )}
        {mode === 'create' && (
          <label className="block"><span className="label-caps field-label">Customer order</span>
            <select className="select" value={form.order_id} onChange={(e) => setForm({ ...form, order_id: e.target.value })}>
              <option value="">Select an open order…</option>
              {open.map((o) => (
                <option key={o.id} value={o.id}>{o.number} · {o.customer} · {num(o.qty)} {o.sku} · {o.factory}{o.truck ? ` (has ${o.truck.ref})` : ''}</option>
              ))}
            </select>
          </label>
        )}
        {editable ? (
          <div className="grid grid-cols-2 gap-3">
            <label className="block col-span-2"><span className="label-caps field-label">Arrival date & time</span>
              <input type="datetime-local" className="input" value={form.scheduled_arrival} onChange={(e) => setForm({ ...form, scheduled_arrival: e.target.value })} /></label>
            <label className="block"><span className="label-caps field-label">Dock</span>
              <select className="select" value={form.dock} onChange={(e) => setForm({ ...form, dock: e.target.value })}>
                {['D1', 'D2', 'D3', 'D4'].map((d) => <option key={d}>{d}</option>)}
              </select></label>
            <label className="block"><span className="label-caps field-label">Expected loading (min)</span>
              <input type="number" min={15} max={480} className="input" value={form.expected_loading_min}
                onChange={(e) => setForm({ ...form, expected_loading_min: e.target.value })} /></label>
            {mode === 'create' && (
              <label className="block col-span-2"><span className="label-caps field-label">Carrier (optional)</span>
                <input className="input" placeholder="e.g. J.B. Hunt" value={form.carrier} onChange={(e) => setForm({ ...form, carrier: e.target.value })} /></label>
            )}
          </div>
        ) : (
          <KV cols={2} items={[
            ['Dock', live.dock], ['Scheduled arrival', dateTime(live.scheduled_arrival)], ['Actual arrival', dateTime(live.actual_arrival)],
            ['Loading start', dateTime(live.loading_start)], ['Departed', dateTime(live.departed_at)], ['Loaded qty', num(live.loaded_qty)],
            ['Waiting', duration(live.wait_min)], ['Detention (est.)', live.detention_charge ? money(live.detention_charge) : '—'],
          ]} />
        )}
        {error && <div className="tile tone-danger text-xs flex gap-2"><Icon name="alert" size={14} />{error}</div>}
        {mode !== 'create' && (
          <div className="pt-1 border-t">
            <p className="label-caps mt-3 mb-2">Truck lifecycle</p>
            <TruckActions truck={live} picked={picked} onDone={(r) => { ord.reload(); onSaved?.(r); }} />
            {live.status === 'Arrived' && !picked && <p className="text-[10.5px] text-fg-faint mt-1.5">Loading can start once the order has picked stock.</p>}
          </div>
        )}
      </div>
    </Modal>
  );
}

/* ─── Order drawer ───────────────────────────────────────────────────── */
function OrderDrawer({ id, onClose, onEditTruck }) {
  const { act, can } = useApp();
  const { data, loading, error, reload } = useApi(`/api/dispatch/orders/${id}`, {}, { every: 2 });
  const o = data?.order;
  return (
    <Drawer eyebrow="Customer order" title={o ? `${o.number} · ${o.customer}` : 'Order'} width={640} onClose={onClose}
      subtitle={o ? `${num(o.qty)} × ${o.sku} · ${o.factory} · priority ${o.priority}` : undefined}
      headerExtra={o && <><StatusBadge status={o.status} />{o.at_risk && <Badge tone="danger" dot>AT RISK</Badge>}</>}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={5} />}
      {o && (
        <div className="space-y-5">
          <KV cols={3} items={[
            ['Shipping window', `${dateShort(o.ship_window_start)} – ${dateShort(o.ship_window_end)}`],
            ['Scheduled dispatch', dayTime(o.scheduled_dispatch)], ['Projected ready', o.ready_eta ? dayTime(o.ready_eta) : DONE.includes(o.status) ? '—' : 'Now'],
            ['Produced', num(o.production_completed)], ['Allocated', num(o.available_inventory)], ['Picked', num(o.picked)],
          ]} />
          <div>
            <div className="flex items-center justify-between mb-2">
              <p className="label-caps">Shipment readiness</p>
              <span className="text-sm font-bold" style={{ color: `var(--color-${readinessTone(o)})` }}>{pct(readyPct(o, o.readiness_pct), 0)}</span>
            </div>
            <div className="space-y-2.5">
              {data.stages.map((s) => {
                const v = readyPct(o, s.pct);
                return <BarRow key={s.key} label={s.label} value={v} max={100} display={pct(v, 0)}
                  tone={v >= 99.9 ? 'success' : o.blocking === s.key ? 'danger' : 'warning'} />;
              })}
            </div>
            <p className="text-[10.5px] text-fg-faint mt-2">Ready only when production, quality, finished-goods receipt, allocation and picking are all complete.</p>
          </div>
          {(o.at_risk || o.risk_level === 'Low') && o.truck && (
            <div className={`tile tone-${o.at_risk ? 'danger' : 'warning'}`}>
              <p className="text-xs font-bold text-fg flex items-center gap-2"><Icon name="alert" size={14} />
                {o.at_risk ? `Truck ${o.truck.ref} arrives ${dayTime(o.truck.arrival)} — order projected ready ${dayTime(o.ready_eta)} (${duration(-o.slack_min)} late)`
                  : `Tight: only ${duration(o.slack_min)} slack before ${o.truck.ref}`}
              </p>
              {o.risk_reasons.length > 0 && <ul className="mt-1.5 space-y-0.5">{o.risk_reasons.map((r) => <li key={r} className="text-[11px] text-fg-muted">• {r}</li>)}</ul>}
              {o.est_detention > 0 && <p className="text-[11px] text-fg mt-1.5">Estimated detention {money(o.est_detention)} <span className="text-fg-faint">(illustrative)</span></p>}
            </div>
          )}
          {data.recommendations.length > 0 && (
            <div>
              <p className="label-caps mb-2">Recommended actions</p>
              <RecButtons orderId={o.id} recs={data.recommendations} />
            </div>
          )}
          <div>
            <p className="label-caps mb-2">Truck appointment</p>
            {data.trucks.length === 0 ? <p className="text-xs text-fg-faint">No truck booked yet — one is booked automatically 30 h before dispatch, or create one in the Truck & dock schedule.</p> : (
              <div className="space-y-2">
                {data.trucks.map((t) => (
                  <div key={t.id} className="tile">
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <Icon name="truck" size={14} className="text-primary" />
                      <span className="text-xs font-bold text-fg">{t.ref}</span>
                      <span className="text-[11px] text-fg-muted">{t.carrier} · dock {t.dock} · {dayTime(t.scheduled_arrival)} · {t.expected_loading_min} min loading</span>
                      <span className="ml-auto"><StatusBadge status={t.status} /></span>
                    </div>
                    {t.status === 'Arrived' && <p className="text-[11px] text-warning mb-1.5">Waiting {duration(t.wait_min)} at the dock{t.delay_reason ? ` — ${t.delay_reason}` : ''}</p>}
                    <TruckActions truck={t} picked={o.picked} onDone={reload} onEdit={() => onEditTruck(t)} />
                  </div>
                ))}
              </div>
            )}
            {o.status === 'Dispatched' && (
              <div className="tile tone-info mt-2 flex items-center gap-3">
                <span className="text-xs text-fg flex-1">Dispatched {dayTime(o.dispatched_at)} — awaiting delivery confirmation.</span>
                <Button size="sm" icon="check" disabled={!can('dispatch.write')}
                  onClick={() => act(() => post(`/api/dispatch/orders/${o.id}/deliver`), `Delivery of ${o.number} confirmed`).then(reload).catch(() => {})}>
                  Confirm delivery</Button>
              </div>
            )}
            {o.status === 'Delivered' && <p className="text-xs text-success mt-2">Delivered {dayTime(o.delivered_at)}.</p>}
          </div>
          <div>
            <p className="label-caps mb-2">Production orders</p>
            {data.production_orders.length === 0 ? <p className="text-xs text-fg-faint">Served from existing stock.</p> : (
              <DataTable rows={data.production_orders} columns={[
                { key: 'number', header: 'PO', strong: true },
                { key: 'line', header: 'Line' },
                { key: 'completed_qty', header: 'Done', render: (p) => `${num(p.completed_qty)} / ${num(p.required_qty)}` },
                { key: 'estimated_completion', header: 'Est. completion', render: (p) => (p.status === 'Completed' ? dayTime(p.completed_at) : dayTime(p.estimated_completion)) },
                { key: 'status', header: 'Status', render: (p) => <StatusBadge status={p.status} /> },
              ]} />
            )}
          </div>
          <div>
            <p className="label-caps mb-2">Finished-goods allocations</p>
            {data.allocations.length === 0 ? <p className="text-xs text-fg-faint">Nothing allocated yet.</p> : (
              <div className="max-h-56 overflow-y-auto">
                <DataTable rows={data.allocations} columns={[
                  { key: 'pallet', header: 'Pallet', mono: true },
                  { key: 'location', header: 'Location' },
                  { key: 'cartons', header: 'Cartons' },
                  { key: 'pillows', header: 'Pillows', render: (a) => num(a.pillows) },
                  { key: 'status', header: 'Status', render: (a) => <StatusBadge status={a.status} /> },
                ]} />
              </div>
            )}
          </div>
          <div>
            <p className="label-caps mb-2">Order history</p>
            {data.events.length === 0 ? <p className="text-xs text-fg-faint">No events.</p> : (
              <ul className="space-y-1.5">
                {data.events.map((e, i) => (
                  <li key={i} className="flex gap-2 text-[11px]">
                    <span className="font-mono text-fg-faint whitespace-nowrap">{dayTime(e.ts)}</span>
                    <span className="text-fg-muted">{e.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}

/* ─── Orders tab ─────────────────────────────────────────────────────── */
function OrdersTab({ factory, customer, onOpen }) {
  const [view, setView] = useState('active');
  const [status, setStatus] = useState('');
  const { data, loading, error, reload } = useApi('/api/dispatch/orders', { factory, customer: customer || undefined, view }, { every: 2 });
  const rows = (data || []).filter((o) => !status || o.status === status);
  const counts = (data || []).reduce((acc, o) => ({ ...acc, [o.status]: (acc[o.status] || 0) + 1 }), {});
  return (
    <Card title="Customer orders" subtitle="Production → quality → finished goods → allocation → picking → dispatch" padded={false}
      actions={
        <div className="flex items-center gap-2 flex-wrap">
          <FilterChips options={[{ value: 'active', label: 'Active' }, { value: 'at_risk', label: 'At risk' }, { value: 'all', label: 'All' }]} value={view} onChange={setView} />
          <select className="select select-sm" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="">All statuses</option>
            {ORDER_STATUSES.map((s) => <option key={s} value={s}>{s}{counts[s] ? ` (${counts[s]})` : ''}</option>)}
          </select>
        </div>
      }>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <div className="p-4"><Loading rows={5} /></div>}
      {data && (
        <DataTable rows={rows} onRowClick={(o) => onOpen(o.id)} emptyText="No orders match this filter."
          columns={[
            { key: 'number', header: 'Order', render: (o) => (
              <span className="flex flex-col">
                <b className="text-fg">{o.number}</b>
                <span className="text-[10px] text-fg-faint">{o.factory} · {o.priority}</span>
              </span>
            ) },
            { key: 'customer', header: 'Customer', strong: true },
            { key: 'sku', header: 'SKU', mono: true },
            { key: 'qty', header: 'Ordered', render: (o) => num(o.qty) },
            { key: 'production_completed', header: 'Produced', render: (o) => (
              <span className="flex flex-col"><span>{num(o.production_completed)}</span>
                <span className="text-[10px] text-fg-faint">{pct((o.production_completed / Math.max(1, o.qty)) * 100, 0)}</span></span>
            ) },
            { key: 'available_inventory', header: 'Available inv.', render: (o) => num(o.available_inventory) },
            { key: 'pending_qty', header: 'Pending', render: (o) => (o.pending_qty ? num(o.pending_qty) : '—') },
            { key: 'window', header: 'Shipping window', render: (o) => <span className="whitespace-nowrap">{dateShort(o.ship_window_start)} – {dateShort(o.ship_window_end)}</span> },
            { key: 'scheduled_dispatch', header: 'Scheduled dispatch', render: (o) => (
              <span className="flex flex-col whitespace-nowrap"><span>{dayTime(o.scheduled_dispatch)}</span>
                {o.truck && <span className="text-[10px] text-fg-faint">{o.truck.ref} · {o.truck.dock}</span>}</span>
            ) },
            { key: 'status', header: 'Status · readiness', render: (o) => (
              <div className="min-w-[150px]">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <StatusBadge status={o.status} />
                  {o.at_risk && <Badge tone="danger">AT RISK</Badge>}
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <ProgressBar value={readyPct(o, o.readiness_pct)} max={100} tone={readinessTone(o)} className="flex-1" />
                  <span className="text-[10px] font-semibold text-fg tabular-nums w-9 text-right">{pct(readyPct(o, o.readiness_pct), 0)}</span>
                </div>
              </div>
            ) },
          ]} />
      )}
    </Card>
  );
}

/* ─── Truck schedule (timeline) ──────────────────────────────────────── */
const PX_PER_HOUR = 44;

function blockTone(a, freeMin) {
  if (a.status === 'Departed') return 'neutral';
  if (a.status === 'Loading' || a.status === 'Loaded') return 'info';
  if (a.status === 'Arrived') return a.wait_min > freeMin ? 'danger' : 'warning';
  return a.at_risk ? 'danger' : 'primary';
}

function Timeline({ data, factory, simTime, freeMin, onSelect, highlight }) {
  const scroller = useRef(null);
  const start = parseTs(data.start);
  const hours = data.days * 24;
  const width = hours * PX_PER_HOUR;
  const appts = data.appointments.filter((a) => a.status !== 'Cancelled');
  const factories = factory === 'ALL' ? [...new Set(appts.map((a) => a.factory))].sort() : [factory];
  const rows = (factories.length ? factories : [factory]).flatMap((f) => data.docks.map((d) => ({ key: `${f}-${d}`, factory: f, dock: d })));
  const now = parseTs(simTime);
  const nowX = now ? ((now - start) / 3600000) * PX_PER_HOUR : null;
  const x = (s) => ((parseTs(s) - start) / 3600000) * PX_PER_HOUR;

  useEffect(() => {
    if (scroller.current && nowX !== null) scroller.current.scrollLeft = Math.max(0, nowX - 240);
  }, [data.start]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex border rounded-lg overflow-hidden">
      <div className="shrink-0 border-r" style={{ width: 92, background: 'var(--color-surface)' }}>
        <div style={{ height: 30 }} className="border-b" />
        {rows.map((r) => (
          <div key={r.key} className="flex items-center px-2 border-b text-[11px] font-semibold text-fg" style={{ height: 40 }}>
            {factory === 'ALL' ? <span className="text-fg-faint mr-1">{r.factory}</span> : null}{r.dock}
          </div>
        ))}
      </div>
      <div ref={scroller} className="overflow-x-auto flex-1">
        <div className="relative" style={{ width, height: 30 + rows.length * 40 }}>
          {Array.from({ length: hours + 1 }).map((_, h) => (
            <div key={h} className="absolute top-0 bottom-0" style={{ left: h * PX_PER_HOUR, borderLeft: `1px solid var(--color-${h % 24 === 0 ? 'border-strong' : 'border'})` }}>
              {h % 2 === 0 && h < hours && (
                <span className="absolute text-[9.5px] text-fg-faint whitespace-nowrap" style={{ top: 8, left: 3 }}>
                  {h % 24 === 0 ? <b className="text-fg">{dateShort(localIso(new Date(start.getTime() + h * 3600000)))}</b> : `${z2(h % 24)}:00`}
                </span>
              )}
            </div>
          ))}
          {rows.map((r, i) => <div key={r.key} className="absolute left-0 right-0 border-b" style={{ top: 30 + (i + 1) * 40 - 1 }} />)}
          {nowX !== null && nowX >= 0 && nowX <= width && (
            <div className="absolute top-0 bottom-0 z-10" style={{ left: nowX, borderLeft: '2px solid var(--color-danger)' }}>
              <span className="absolute text-[9px] font-bold text-white px-1 rounded" style={{ top: 0, left: -18, background: 'var(--color-danger)' }}>NOW</span>
            </div>
          )}
          {appts.map((a) => {
            const ri = rows.findIndex((r) => r.dock === a.dock && r.factory === a.factory);
            if (ri < 0) return null;
            const startIso = a.status === 'Arrived' ? a.actual_arrival : (a.loading_start || a.scheduled_arrival);
            const mins = a.status === 'Arrived' ? Math.max(a.expected_loading_min, a.wait_min || 0) : (a.expected_loading_min || 60);
            const left = x(startIso);
            const w = Math.max(18, (mins / 60) * PX_PER_HOUR);
            if (left + w < 0 || left > width) return null;
            const tone = blockTone(a, freeMin);
            const hl = highlight === a.id;
            return (
              <button key={a.id} type="button" onClick={() => onSelect(a)}
                title={`${a.ref} · ${a.order || ''} · ${a.customer || ''}\n${a.status} · arrival ${dayTime(a.scheduled_arrival)} · ${a.expected_loading_min} min loading${a.at_risk ? '\nAT RISK' : ''}`}
                className={`absolute rounded-md px-1.5 text-left overflow-hidden tone-${tone === 'primary' ? 'primary' : tone}`}
                style={{ left, width: w, top: 30 + ri * 40 + 5, height: 30, borderWidth: hl ? 2 : 1, borderStyle: 'solid',
                  borderColor: hl ? 'var(--color-text)' : undefined, zIndex: hl ? 5 : 1 }}>
                <span className="block text-[10px] font-bold text-fg truncate leading-tight mt-0.5">{a.ref}</span>
                <span className="block text-[9.5px] text-fg-muted truncate leading-tight">{a.order} · {hhmm(a.scheduled_arrival)}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ScheduleTab({ factory, simTime, freeMin, focusTruck, onOpenOrder }) {
  const { can } = useApp();
  const today = simTime?.slice(0, 10);
  const [day, setDay] = useState(today || '');
  const { data, loading, error, reload } = useApi('/api/dispatch/appointments', { factory, date: day || today, days: 2 }, { every: 2 });
  const [modal, setModal] = useState(null);
  const opened = useRef(null);

  useEffect(() => {
    if (!focusTruck || !data || opened.current === focusTruck) return;
    const a = data.appointments.find((x) => x.id === focusTruck);
    if (a) { opened.current = focusTruck; setModal({ mode: a.status === 'Scheduled' ? 'edit' : 'view', appt: a }); }
  }, [focusTruck, data]);

  const appts = (data?.appointments || []).filter((a) => a.status !== 'Cancelled');
  return (
    <div className="space-y-4">
      <Card title="Dock timeline" subtitle="Docks D1–D4 · appointment blocks span the expected loading window"
        actions={
          <div className="flex items-center gap-2">
            <input type="date" className="input select-sm" style={{ width: 132 }} value={day || today || ''} onChange={(e) => setDay(e.target.value)} aria-label="Schedule date" />
            <Button size="sm" variant="ghost" onClick={() => setDay(today)}>Today</Button>
            <Button size="sm" icon="plus" disabled={!can('dispatch.write')} title={can('dispatch.write') ? undefined : 'Requires dispatch permissions'}
              onClick={() => setModal({ mode: 'create' })}>New appointment</Button>
          </div>
        }>
        <ErrorBox error={error} onRetry={reload} />
        {loading && !data && <Loading rows={4} height={36} />}
        {data && <Timeline data={data} factory={factory} simTime={simTime} freeMin={freeMin} highlight={focusTruck}
          onSelect={(a) => setModal({ mode: a.status === 'Scheduled' ? 'edit' : 'view', appt: a })} />}
        <div className="flex flex-wrap gap-3 mt-3 text-[10.5px] text-fg-muted">
          {[['primary', 'Scheduled'], ['danger', 'At risk / waiting beyond free time'], ['warning', 'Arrived, waiting'], ['info', 'Loading'], ['neutral', 'Departed']].map(([t, l]) => (
            <span key={l} className="flex items-center gap-1.5"><span className={`w-3 h-3 rounded tone-${t}`} style={{ borderWidth: 1 }} />{l}</span>
          ))}
        </div>
      </Card>
      <Card title="Appointments" subtitle={`${appts.length} in the 48-hour window`} padded={false}>
        <DataTable rows={appts} onRowClick={(a) => setModal({ mode: a.status === 'Scheduled' ? 'edit' : 'view', appt: a })} emptyText="No appointments in this window."
          columns={[
            { key: 'ref', header: 'Truck', strong: true, render: (a) => <span className="flex flex-col"><b className="text-fg">{a.ref}</b><span className="text-[10px] text-fg-faint">{a.carrier}</span></span> },
            { key: 'customer', header: 'Customer' },
            { key: 'order', header: 'Order', render: (a) => (a.order_id
              ? <button type="button" className="link-btn" onClick={(e) => { e.stopPropagation(); onOpenOrder(a.order_id); }}>{a.order}</button> : '—') },
            { key: 'scheduled_arrival', header: 'Arrival', render: (a) => (
              <span className="flex flex-col whitespace-nowrap"><span>{dayTime(a.scheduled_arrival)}</span>
                {a.actual_arrival && <span className="text-[10px] text-fg-faint">actual {hhmm(a.actual_arrival)}</span>}</span>
            ) },
            { key: 'dock', header: 'Dock', render: (a) => `${factory === 'ALL' ? `${a.factory} ` : ''}${a.dock}` },
            { key: 'expected_loading_min', header: 'Loading', render: (a) => `${a.expected_loading_min} min` },
            { key: 'readiness', header: 'Readiness', render: (a) => (a.readiness_status ? (
              <span className="flex items-center gap-1.5"><StatusBadge status={a.readiness_status} dot={false} />{a.at_risk && <Badge tone="danger">AT RISK</Badge>}</span>
            ) : '—') },
            { key: 'status', header: 'Loading status', render: (a) => (
              <span className="flex flex-col"><StatusBadge status={a.status} />
                {a.status === 'Arrived' && <span className="text-[10px] text-warning mt-0.5">waiting {duration(a.wait_min)}</span>}</span>
            ) },
          ]} />
      </Card>
      {modal && (
        <AppointmentModal mode={modal.mode} appt={modal.appt} factory={factory} simTime={simTime}
          onClose={() => setModal(null)} onSaved={() => reload()} />
      )}
    </div>
  );
}

/* ─── Shipment risk ──────────────────────────────────────────────────── */
function RiskTab({ factory, customer, onOpen }) {
  const { data, loading, error, reload } = useApi('/api/dispatch/risk', { factory }, { every: 2 });
  const rows = (data || []).filter((o) => !customer || String(o.customer_id) === String(customer));
  return (
    <div className="space-y-3">
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={3} height={80} />}
      {data && rows.length === 0 && (
        <Card><Empty>No truck appointment is currently at risk — every scheduled order is projected ready before its truck.</Empty></Card>
      )}
      {rows.map((o) => {
        const late = o.slack_min != null ? -o.slack_min : 0;
        const tone = o.at_risk ? (o.risk_level === 'High' ? 'danger' : 'warning') : 'info';
        return (
          <div key={o.id} className="card" style={{ borderColor: `var(--color-${tone}-border)` }}>
            <div className="card-header">
              <div className="flex-1 min-w-0">
                <button type="button" className="card-title hover:underline text-left" onClick={() => onOpen(o.id)}>{o.number} · {o.customer}</button>
                <p className="card-subtitle">{num(o.qty)} × {o.sku} · {o.factory} · {o.status} · readiness {pct(o.readiness_pct, 0)}</p>
              </div>
              <Badge tone={tone} dot>{o.at_risk ? `${o.risk_level.toUpperCase()} RISK` : 'TIGHT'}</Badge>
            </div>
            <div className="card-body grid grid-cols-1 lg:grid-cols-5 gap-4">
              <div className="lg:col-span-3 space-y-3">
                <div className="grid grid-cols-3 gap-2">
                  <div className="tile"><p className="label-caps">Truck arrives</p><p className="text-lg font-bold text-fg">{dayTime(o.truck?.arrival)}</p>
                    <p className="text-[10px] text-fg-faint">{o.truck?.ref} · dock {o.truck?.dock} · {o.truck?.status}</p></div>
                  <div className={`tile tone-${tone}`}><p className="label-caps">Projected ready</p><p className="text-lg font-bold text-fg">{dayTime(o.ready_eta)}</p>
                    <p className="text-[10px] text-fg-muted">{o.at_risk ? `${duration(late)} after arrival` : `${duration(o.slack_min)} slack`}</p></div>
                  <div className="tile"><p className="label-caps">Est. detention</p><p className="text-lg font-bold" style={{ color: o.est_detention ? 'var(--color-danger)' : 'var(--color-text)' }}>{money(o.est_detention)}</p>
                    <p className="text-[10px] text-fg-faint">illustrative estimate</p></div>
                </div>
                {o.risk_reasons.length > 0 && (
                  <ul className="space-y-0.5">{o.risk_reasons.map((r) => <li key={r} className="text-[11px] text-fg-muted">• {r}</li>)}</ul>
                )}
              </div>
              <div className="lg:col-span-2">
                <p className="label-caps mb-2">Recommendations</p>
                {o.recommendations.length ? <RecButtons orderId={o.id} recs={o.recommendations} /> : <p className="text-xs text-fg-faint">No action needed yet.</p>}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ─── Detention ──────────────────────────────────────────────────────── */
function DetentionTab({ factory, onOpen }) {
  const [days, setDays] = useState(7);
  const { data, loading, error, reload } = useApi('/api/dispatch/detention', { factory, days }, { every: 3 });
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (loading && !data) return <Loading rows={4} height={50} />;
  if (!data) return null;
  const free = data.free_minutes;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="tile tone-warning text-[11px] flex items-center gap-2 flex-1 min-w-[280px]">
          <Icon name="info" size={14} />
          <span><b>Estimated / illustrative.</b> {data.note} Configurable charge {money(data.charge_per_truck)} per qualifying truck after {num(free)} free minutes.</span>
        </div>
        <FilterChips label="Period" options={[{ value: 1, label: 'Today' }, { value: 7, label: '7 days' }, { value: 30, label: '30 days' }]} value={days} onChange={setDays} />
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
        <Kpi label="Incurred (estimated)" icon="dollar" value={money(data.incurred_estimate)} tone={data.incurred_estimate ? 'danger' : 'success'}
          sub={`From simulated arrival & loading events · ${data.period_days} d`} />
        <Kpi label="Projected (upcoming)" icon="clock" value={money(data.projected_estimate)} tone={data.projected_estimate ? 'warning' : 'success'}
          sub={`${data.projected.length} at-risk appointment(s), not yet incurred`} />
        <Kpi label="Delayed trucks" icon="truck" value={num(data.delayed_trucks)} sub={`of ${num(data.trucks_arrived)} arrivals waited > ${num(free)} min`} />
        <Kpi label="Average wait" icon="hourglass" value={duration(data.avg_wait_min)} sub="Arrival to loading start" />
        <Kpi label="Charge per truck" icon="settings" value={money(data.charge_per_truck)} sub={`after ${num(free)} free minutes`} />
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card title="By customer" subtitle="Estimated detention $">
          {data.by_customer.length ? <MiniChart type="bar" horizontal height={Math.max(140, data.by_customer.length * 34)}
            data={{ labels: data.by_customer.map((x) => x.name), datasets: [{ label: '$', data: data.by_customer.map((x) => x.value), color: 'warning' }] }} />
            : <Empty>No qualifying delays.</Empty>}
        </Card>
        <Card title="By factory" subtitle="Estimated detention $">
          {data.by_factory.length ? <MiniChart type="bar" height={160}
            data={{ labels: data.by_factory.map((x) => x.name), datasets: [{ label: '$', data: data.by_factory.map((x) => x.value), color: 'chart-5' }] }} />
            : <Empty>No qualifying delays.</Empty>}
        </Card>
        <Card title="Main reasons for delays" subtitle="Qualifying trucks by cause">
          {data.reasons.length ? <MiniChart type="doughnut" height={160} legend
            data={{ labels: data.reasons.map((x) => x.name), datasets: [{ data: data.reasons.map((x) => x.value) }] }} />
            : <Empty>No qualifying delays.</Empty>}
        </Card>
      </div>
      {data.projected.length > 0 && (
        <Card title="Projected charges for upcoming appointments" subtitle="Estimated from projected readiness — avoidable by acting now" padded={false}>
          <DataTable rows={data.projected} rowKey="truck" columns={[
            { key: 'order', header: 'Order', strong: true },
            { key: 'truck', header: 'Truck' },
            { key: 'customer', header: 'Customer' },
            { key: 'arrival', header: 'Arrival', render: (r) => dayTime(r.arrival) },
            { key: 'projected_wait_min', header: 'Projected wait', render: (r) => duration(r.projected_wait_min) },
            { key: 'charge', header: 'Est. charge', render: (r) => <b className="text-warning">{money(r.charge)}</b> },
          ]} />
        </Card>
      )}
      <Card title="Truck arrivals" subtitle={`Last ${data.period_days} day(s) · waiting beyond ${num(free)} min qualifies`} padded={false}>
        <DataTable rows={data.trucks} onRowClick={(t) => t.order_id && onOpen(t.order_id)} emptyText="No arrivals in this period."
          columns={[
            { key: 'ref', header: 'Truck', strong: true },
            { key: 'factory', header: 'Factory' },
            { key: 'customer', header: 'Customer' },
            { key: 'order', header: 'Order' },
            { key: 'actual_arrival', header: 'Arrived', render: (t) => dayTime(t.actual_arrival) },
            { key: 'loading_start', header: 'Loading start', render: (t) => (t.loading_start ? dayTime(t.loading_start) : '—') },
            { key: 'wait_min', header: 'Wait', render: (t) => <span style={{ color: t.wait_min > free ? 'var(--color-danger)' : undefined, fontWeight: t.wait_min > free ? 700 : 400 }}>{duration(t.wait_min)}</span> },
            { key: 'status', header: 'Status', render: (t) => <StatusBadge status={t.status} /> },
            { key: 'charge', header: 'Charge (est.)', render: (t) => {
              const live = t.status === 'Arrived' && t.wait_min > free;
              const v = t.detention_charge || (live ? data.charge_per_truck : 0);
              return v ? <span className="text-danger font-semibold">{money(v)}{live ? ' · accruing' : ''}</span> : '—';
            } },
            { key: 'delay_reason', header: 'Reason', render: (t) => t.delay_reason || '—' },
          ]} />
      </Card>
    </div>
  );
}

/* ─── Page ───────────────────────────────────────────────────────────── */
export default function Dispatch() {
  const { filters, sim, meta, act } = useApp();
  const location = useLocation();
  const [tab, setTab] = useState('orders');
  const [orderId, setOrderId] = useState(null);
  const [focusTruck, setFocusTruck] = useState(null);
  const [editTruck, setEditTruck] = useState(null);
  const applied = useRef(null);
  const factory = filters.factory;
  const { data: s, error, reload } = useApi('/api/dispatch/summary', { factory }, { every: 2 });
  const freeMin = meta?.rules?.detention_free_min ?? 120;

  useEffect(() => {
    const f = parseFocus(location.search);
    if (!f || applied.current === f.key) return;
    applied.current = f.key;
    if (f.type === 'order' && f.id) { setTab('orders'); setOrderId(f.id); }
    else if (f.type === 'truck') { setTab('schedule'); setFocusTruck(f.id || null); }
    else if (f.type === 'detention') setTab('detention');
  }, [location.search]);

  const onInsight = (action) => {
    if (action.type === 'risk_action') {
      act(() => post(`/api/dispatch/risk/${action.payload.order_id}/action`, { action: action.payload.action }),
        (r) => `${r.message} · projected ready ${dayTime(r.ready_eta)}`).catch(() => {});
    } else if (action.type === 'expedite_picking') {
      act(() => post('/api/dispatch/risk/' + action.payload.order_id + '/action', { action: 'expedite_picking' }), 'Picking expedited').catch(() => {});
    }
  };

  const det = s?.detention;
  return (
    <div className="space-y-4">
      <PageTitle title="Dispatch & Delivery" subtitle="Every customer order shipped within its window — readiness, docks, trucks and detention exposure">
        {s?.at_risk > 0 && <Badge tone="danger" dot>{s.at_risk} AT RISK</Badge>}
        <SimTag />
      </PageTitle>
      <StageAlerts stage="dispatch" />
      <ErrorBox error={error} onRetry={reload} />
      {!s && !error && <Loading rows={2} height={60} />}
      {s && (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
          <Kpi label="Open orders" icon="clipboard" value={num(s.open_orders)} sub={`${num(s.due_48h)} due within 48 h`} onClick={() => setTab('orders')} />
          <Kpi label="Ready for dispatch" icon="check" value={`${s.ready}/${s.due_48h}`} unit="due 48 h"
            tone={s.due_48h && s.ready === s.due_48h ? 'success' : 'warning'} sub={`${pct(s.due_48h ? (100 * s.ready) / s.due_48h : 100, 0)} of near-term orders ready`} />
          <Kpi label="At risk" icon="alert" value={num(s.at_risk)} tone={s.at_risk ? 'danger' : 'success'}
            sub={s.at_risk ? 'Will not be ready when the truck arrives' : 'All appointments on track'} active={tab === 'risk'} onClick={() => setTab('risk')} />
          <Kpi label="Trucks today" icon="truck" value={num(s.trucks_today)} tone={s.trucks_waiting ? 'warning' : undefined}
            sub={`${s.trucks_departed_today} departed · ${s.trucks_waiting} waiting at dock`} active={tab === 'schedule'} onClick={() => setTab('schedule')} />
          <Kpi label="Dispatched today" icon="route" value={num(s.dispatched_today)} sub="Orders loaded and departed" />
          <Kpi label="Est. detention" icon="dollar" value={money(det.incurred_estimate + det.projected_estimate)}
            tone={det.incurred_estimate + det.projected_estimate ? 'warning' : 'success'}
            sub={`${money(det.incurred_estimate)} incurred (7 d) + ${money(det.projected_estimate)} projected · illustrative`}
            active={tab === 'detention'} onClick={() => setTab('detention')} />
        </div>
      )}
      <Tabs variant="underline" tabs={TABS} value={tab} onChange={setTab} />
      <div className="grid grid-cols-1 2xl:grid-cols-4 gap-4 items-start">
        <div className="2xl:col-span-3 min-w-0">
          {tab === 'orders' && <OrdersTab factory={factory} customer={filters.customer} onOpen={setOrderId} />}
          {tab === 'schedule' && <ScheduleTab factory={factory} simTime={sim?.sim_time} freeMin={freeMin} focusTruck={focusTruck} onOpenOrder={setOrderId} />}
          {tab === 'risk' && <RiskTab factory={factory} customer={filters.customer} onOpen={setOrderId} />}
          {tab === 'detention' && <DetentionTab factory={factory} onOpen={setOrderId} />}
          {tab === 'cctv' && (
            <Card title="Dispatch lane CCTV" subtitle="Cartons verified by label OCR before loading · people tracked with RFID identity">
              <CctvWall factory={factory} filter={(f) => f.camera.endsWith('-06') || f.camera.endsWith('-04') || f.camera.endsWith('-05')} cols={3} />
            </Card>
          )}
        </div>
        <InsightsCard page="dispatch" limit={5} onAction={onInsight} />
      </div>
      {orderId && <OrderDrawer id={orderId} onClose={() => setOrderId(null)} onEditTruck={(t) => setEditTruck(t)} />}
      {editTruck && (
        <AppointmentModal mode={editTruck.status === 'Scheduled' ? 'edit' : 'view'} appt={{ ...editTruck, at_risk: false }} factory={factory}
          simTime={sim?.sim_time} onClose={() => setEditTruck(null)} onSaved={() => {}} />
      )}
    </div>
  );
}

