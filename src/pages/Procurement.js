import React, { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Badge, Button, Card, DataTable, Drawer, FilterChips, Icon, Modal, ProgressBar, Tabs } from '../components/ui';
import ChartCard from '../components/charts/ChartCard';
import {
  Empty, ErrorBox, InsightsCard, Kpi, KpiRow, KV, Loading, PageTitle, StageAlerts, StatusBadge,
} from '../components/common';
import { useApi, useFocus } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { patch, post } from '../api/client';
import { dateShort, dateTime, dayTime, linkTo, money, num, parseTs, pct } from '../utils';

const TABS = [
  { value: 'pos', label: 'Purchase orders' },
  { value: 'shipments', label: 'Inbound shipments' },
  { value: 'materials', label: 'Material availability' },
  { value: 'suppliers', label: 'Supplier performance' },
];
const PO_VIEWS = [
  { value: '', label: 'All' }, { value: 'open', label: 'Open' }, { value: 'delayed', label: 'Delayed' }, { value: 'Received', label: 'Received' },
];
const SHIP_VIEWS = [
  { value: '', label: 'Active & recent' }, { value: 'expected', label: 'Expected inbound' }, { value: 'pending', label: 'Pending receipt' },
  { value: 'delayed', label: 'Delayed' }, { value: 'received_today', label: 'Received (24 h)' },
];
const MAT_VIEWS = [{ value: '', label: 'All materials' }, { value: 'short', label: 'Shortages (critical + low)' }];
const STATUS_TONE = { Critical: 'danger', Low: 'warning', OK: 'success', 'No demand': 'neutral' };

const qtyFmt = (v, uom) => `${num(v)} ${uom || ''}`.trim();

function isoAddDays(iso, days) {
  const d = parseTs(iso) || new Date();
  d.setTime(d.getTime() + days * 86400000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function DaysCover({ row }) {
  if (row.days_cover === null || row.days_cover === undefined) return <span className="text-fg-faint">—</span>;
  const tone = STATUS_TONE[row.status] || 'neutral';
  return (
    <div className="min-w-[110px]">
      <div className="flex justify-between text-[11px] mb-0.5">
        <b style={{ color: `var(--color-${tone})` }}>{row.days_cover} d</b>
        <span className="text-fg-faint">min {row.reorder_days} d</span>
      </div>
      <ProgressBar value={Math.min(row.days_cover, 14)} max={14} tone={tone} />
    </div>
  );
}

/* ─── Create purchase order ───────────────────────────────────────────── */
function CreatePOModal({ initial, onClose }) {
  const { meta, sim, act, can, selectedFactory } = useApp();
  const firstMat = initial?.material_id || meta.materials[0]?.id;
  const [form, setForm] = useState(() => ({
    factory_id: initial?.factory_id || selectedFactory?.id || meta.factories[0]?.id,
    material_id: firstMat,
    supplier_id: initial?.supplier_id || '',
    qty: initial?.qty ? String(Math.round(initial.qty)) : '',
    expected: '',
    notes: initial?.notes || '',
  }));
  const [busy, setBusy] = useState(false);
  const mat = meta.materials.find((x) => x.id === Number(form.material_id));
  const suppliers = meta.suppliers.filter((s) => mat && (s.materials || []).includes(mat.code));
  const sup = meta.suppliers.find((s) => s.id === Number(form.supplier_id));

  useEffect(() => {
    if (!suppliers.find((s) => s.id === Number(form.supplier_id))) {
      setForm((f) => ({ ...f, supplier_id: suppliers[0]?.id || '' }));
    }
  }, [form.material_id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!sup) return;
    const days = initial?.expected_days && Number(form.supplier_id) === initial?.supplier_id ? initial.expected_days : sup.lead_time_days;
    setForm((f) => ({ ...f, expected: isoAddDays(sim?.sim_time, days) }));
  }, [form.supplier_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const valid = form.factory_id && form.material_id && form.supplier_id && Number(form.qty) > 0 && form.expected;

  const submit = async () => {
    setBusy(true);
    try {
      await act(() => post('/api/procurement/purchase-orders', {
        factory_id: Number(form.factory_id), supplier_id: Number(form.supplier_id), material_id: Number(form.material_id),
        qty: Number(form.qty), expected_date: form.expected, notes: form.notes || null,
      }), (r) => `${r.number} created: ${num(r.ordered_qty)} ${r.uom} ${r.sku} from ${r.supplier}`);
      onClose();
    } catch (e) { /* toast shown */ } finally { setBusy(false); }
  };

  return (
    <Modal title="Create purchase order" eyebrow="Procurement" subtitle="Creates the PO and a scheduled inbound shipment (simulated ERP)"
      width={520} onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button icon="plus" disabled={!valid || busy || !can('procurement.write')} onClick={submit}>{busy ? 'Creating…' : 'Create PO'}</Button>
      </>}>
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="label-caps field-label">Factory</span>
          <select className="select" value={form.factory_id} onChange={set('factory_id')}>
            {meta.factories.map((f) => <option key={f.id} value={f.id}>{f.name}, {f.state}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="label-caps field-label">Material</span>
          <select className="select" value={form.material_id} onChange={set('material_id')}>
            {meta.materials.map((x) => <option key={x.id} value={x.id}>{x.code} · {x.name}</option>)}
          </select>
        </label>
        <label className="block col-span-2">
          <span className="label-caps field-label">Supplier</span>
          <select className="select" value={form.supplier_id} onChange={set('supplier_id')}>
            {suppliers.length === 0 && <option value="">No supplier for this material</option>}
            {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name} · lead time {s.lead_time_days} d</option>)}
          </select>
        </label>
        <label className="block">
          <span className="label-caps field-label">Quantity ({mat?.uom})</span>
          <input className="input" type="number" min="1" value={form.qty} onChange={set('qty')} />
        </label>
        <label className="block">
          <span className="label-caps field-label">Expected delivery</span>
          <input className="input" type="datetime-local" value={form.expected} onChange={set('expected')} />
        </label>
        <label className="block col-span-2">
          <span className="label-caps field-label">Notes</span>
          <textarea className="textarea" rows={2} value={form.notes} onChange={set('notes')} placeholder="Optional" />
        </label>
      </div>
      {initial?.source && <p className="tile tone-accent text-[11px] mt-3"><Icon name="sparkles" size={12} className="inline mr-1" />Prefilled from AI recommendation: {initial.source}</p>}
      {!can('procurement.write') && <p className="text-[11px] text-warning mt-3">Your role cannot create purchase orders.</p>}
    </Modal>
  );
}

/* ─── Purchase order drawer ───────────────────────────────────────────── */
function PODrawer({ id, onClose }) {
  const { act, can } = useApp();
  const navigate = useNavigate();
  const { data, loading, error } = useApi(`/api/procurement/purchase-orders/${id}`);
  const [edit, setEdit] = useState(null);
  const po = data?.po;

  const save = async () => {
    const body = {};
    if (edit.expected && edit.expected !== po.expected_date) body.expected_date = edit.expected;
    if (edit.qty && Number(edit.qty) !== po.ordered_qty) body.ordered_qty = Number(edit.qty);
    if (edit.notes !== (data.notes || '')) body.notes = edit.notes;
    if (!Object.keys(body).length) { setEdit(null); return; }
    try {
      await act(() => patch(`/api/procurement/purchase-orders/${id}`, body), `${po.number} updated`);
      setEdit(null);
    } catch (e) { /* toast shown */ }
  };
  const cancelPo = async () => {
    if (!window.confirm(`Cancel ${po.number}? Pending quantity will no longer be expected.`)) return;
    try { await act(() => patch(`/api/procurement/purchase-orders/${id}`, { status: 'Cancelled' }), `${po.number} cancelled`); } catch (e) { /* toast */ }
  };

  const editable = po && !['Received', 'Cancelled'].includes(po.status) && can('procurement.write');
  return (
    <Drawer eyebrow="Purchase order" title={po ? `${po.number} · ${po.sku}` : 'Purchase order'} width={600} onClose={onClose}
      subtitle={po ? `${po.material} · ${po.supplier}` : undefined}
      headerExtra={po && <StatusBadge status={po.status} />}
      footer={po && (edit
        ? <><Button variant="ghost" onClick={() => setEdit(null)}>Discard</Button><Button icon="check" onClick={save}>Save changes</Button></>
        : <>
          <span className="text-[10px] text-fg-faint mr-auto">Created {dateTime(po.order_date)}</span>
          <Button variant="ghost" disabled={!editable} onClick={cancelPo}>Cancel PO</Button>
          <Button variant="secondary" icon="edit" disabled={!editable}
            onClick={() => setEdit({ expected: po.expected_date, qty: String(po.ordered_qty), notes: data.notes || '' })}>Update</Button>
        </>)}>
      <ErrorBox error={error} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <div className="space-y-5">
          <div>
            <KV cols={3} items={[
              ['Factory', po.factory], ['Ordered', qtyFmt(po.ordered_qty, po.uom)], ['Received', qtyFmt(po.received_qty, po.uom)],
              ['Pending', qtyFmt(po.pending_qty, po.uom)], ['Damaged', qtyFmt(po.damaged_qty, po.uom)], ['Value', money(po.value)],
              ['Order date', dateTime(po.order_date)], ['Expected', dateTime(po.expected_date)], ['Unit price', money(po.unit_price, 2)],
            ]} />
            <div className="mt-3"><ProgressBar value={po.received_qty} max={po.ordered_qty} tone={po.status === 'Delayed' ? 'danger' : 'success'} /></div>
          </div>
          {edit && (
            <div className="tile space-y-3">
              <p className="label-caps">Update purchase order</p>
              <div className="grid grid-cols-2 gap-3">
                <label className="block"><span className="label-caps field-label">Expected delivery</span>
                  <input className="input" type="datetime-local" value={edit.expected} onChange={(e) => setEdit({ ...edit, expected: e.target.value })} /></label>
                <label className="block"><span className="label-caps field-label">Ordered qty ({po.uom})</span>
                  <input className="input" type="number" value={edit.qty} onChange={(e) => setEdit({ ...edit, qty: e.target.value })} /></label>
              </div>
              <label className="block"><span className="label-caps field-label">Notes</span>
                <textarea className="textarea" rows={2} value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} /></label>
            </div>
          )}
          {data.notes && !edit && <p className="tile text-xs text-fg-muted">{data.notes}</p>}
          <div>
            <p className="label-caps mb-2">Supplier</p>
            <div className="tile">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold text-fg">{data.supplier.name}</p>
                  <p className="text-[11px] text-fg-faint">{data.supplier.location} · lead time {data.supplier.lead_time_days} days · {data.supplier.contact}</p>
                </div>
                {data.supplier.performance && (
                  <Badge tone={data.supplier.performance.on_time_pct >= 85 ? 'success' : data.supplier.performance.on_time_pct >= 70 ? 'warning' : 'danger'}>
                    {pct(data.supplier.performance.on_time_pct, 0)} ON TIME
                  </Badge>
                )}
              </div>
              {data.supplier.performance && (
                <p className="text-[11px] text-fg-muted mt-1.5">
                  {data.supplier.performance.late} of {data.supplier.performance.deliveries} recent deliveries late
                  (avg {data.supplier.performance.avg_late_days} days) · {data.supplier.performance.open_delayed} open shipment(s) delayed now
                </p>
              )}
            </div>
          </div>
          <div>
            <p className="label-caps mb-2">Delivery status</p>
            {data.shipments.length === 0 && <p className="text-xs text-fg-faint">No shipments.</p>}
            <div className="space-y-2">
              {data.shipments.map((s) => (
                <div key={s.id} className={`tile ${s.delayed ? 'tone-danger' : ''}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-fg">{s.ref} · {s.bol_number} · {s.carrier}</span>
                    <StatusBadge status={s.status} />
                  </div>
                  <p className="text-[11px] text-fg-muted mt-1">
                    {qtyFmt(s.qty, s.uom)} · expected {dateTime(s.expected_arrival)}
                    {s.delayed && <> (was {dateTime(s.original_expected_arrival)})</>}
                    {s.actual_arrival && <> · arrived {dateTime(s.actual_arrival)} at {s.dock}</>}
                  </p>
                  {s.delay_reason && <p className="text-[11px] text-danger mt-0.5">{s.delay_reason}</p>}
                </div>
              ))}
            </div>
          </div>
          <div>
            <p className="label-caps mb-2">Material receipts</p>
            {data.receipts.length === 0 ? <p className="text-xs text-fg-faint">No goods receipts yet.</p> : (
              <div className="tile p-0 overflow-x-auto">
                <table className="table">
                  <thead><tr><th>Receipt</th><th>Received</th><th>Damaged</th><th>Shortage</th><th>Accepted</th><th>Confirmed</th></tr></thead>
                  <tbody>{data.receipts.map((r) => (
                    <tr key={r.number}>
                      <td className="cell-strong">{r.number}</td><td>{num(r.received_qty)}</td><td>{num(r.damaged_qty)}</td>
                      <td className={r.shortage_qty ? 'text-danger font-semibold' : ''}>{num(r.shortage_qty)}</td><td>{num(r.accepted_qty)}</td>
                      <td>{dateTime(r.confirmed_at)} · {r.confirmed_by}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
            {data.discrepancies.length > 0 && (
              <div className="flex flex-wrap gap-2 mt-2">
                {data.discrepancies.map((d) => (
                  <Badge key={d.id} tone={d.kind === 'Shortage' ? 'danger' : 'warning'}>{d.kind} {num(d.qty)} {po.uom} · {d.status}</Badge>
                ))}
              </div>
            )}
          </div>
          {data.affected_production_orders.length > 0 && (
            <div>
              <p className="label-caps mb-2">Production orders using this material ({po.factory})</p>
              <div className="space-y-1.5">
                {data.affected_production_orders.map((p) => (
                  <button key={p.id} type="button" className="tile w-full text-left flex items-center gap-2 hover:opacity-90"
                    onClick={() => navigate(linkTo('production', 'production_order', p.id))}>
                    <span className="text-xs font-semibold text-fg">{p.number}</span>
                    <span className="text-[11px] text-fg-muted">{p.sku} · est. {dayTime(p.estimated_completion)}</span>
                    {p.material_risk && <Badge tone="danger">MATERIAL RISK</Badge>}
                    <span className="ml-auto"><StatusBadge status={p.status} /></span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <div>
            <p className="label-caps mb-2">Order history</p>
            <ol className="space-y-2 border-l-2 border-line pl-3">
              {[...data.history].reverse().map((h, i) => (
                <li key={i} className="text-xs">
                  <span className="text-fg-faint tabular-nums">{dateTime(h.ts)}</span>
                  <span className="text-fg ml-2">{h.event}</span>
                  <span className="text-fg-faint ml-1">· {h.by}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </Drawer>
  );
}

/* ─── Mark arrived ────────────────────────────────────────────────────── */
function ArriveModal({ ship, onClose }) {
  const { act } = useApp();
  const [dock, setDock] = useState(ship.dock || 'R1');
  const go = async () => {
    try {
      await act(() => post(`/api/procurement/shipments/${ship.id}/arrive`, { dock }), `${ship.ref} checked in at receiving dock ${dock}`);
      onClose();
    } catch (e) { /* toast */ }
  };
  return (
    <Modal title={`Mark ${ship.ref} arrived`} eyebrow="Inbound" width={400} onClose={onClose}
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button icon="truck" onClick={go}>Confirm arrival</Button></>}>
      <p className="text-xs text-fg-muted mb-3">{ship.supplier} · {qtyFmt(ship.qty, ship.uom)} {ship.sku} · BOL {ship.bol_number}</p>
      <span className="label-caps field-label">Receiving dock</span>
      <div className="tabs">
        {['R1', 'R2'].map((d) => <button key={d} type="button" className={`tab ${dock === d ? 'is-active' : ''}`} onClick={() => setDock(d)}>{d}</button>)}
      </div>
    </Modal>
  );
}

/* ─── Goods receipt workflow ─────────────────────────────────────────── */
function GoodsReceiptModal({ initial, onClose }) {
  const { act, can } = useApp();
  const [ship, setShip] = useState(initial);
  const [received, setReceived] = useState(initial.scanned_qty ? String(initial.scanned_qty) : '');
  const [damaged, setDamaged] = useState(initial.scanned_damaged ? String(initial.scanned_damaged) : '0');
  const [custom, setCustom] = useState('');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const allowed = can('receiving.write');
  const expected = ship.qty;
  const rec = Number(received) || 0;
  const dmg = Number(damaged) || 0;
  const shortage = Math.max(0, expected - rec);
  const over = Math.max(0, rec - expected);
  const accepted = Math.max(0, rec - dmg);

  const run = async (fn) => {
    setBusy(true);
    try { return await fn(); } catch (e) { return null; } finally { setBusy(false); }
  };
  const start = () => run(async () => {
    const s = await act(() => post(`/api/procurement/shipments/${ship.id}/receipt/start`), `Receiving started for ${ship.ref}`);
    setShip(s);
  });
  const scan = (qty, dmgQty = 0) => run(async () => {
    const s = await act(() => post(`/api/procurement/shipments/${ship.id}/receipt/scan`, { qty, damaged: dmgQty }));
    setShip(s);
    setReceived(String(s.scanned_qty));
    setDamaged(String(s.scanned_damaged));
  });
  const confirm = () => run(async () => {
    const r = await act(() => post(`/api/procurement/shipments/${ship.id}/receipt/confirm`, { received_qty: rec, damaged_qty: dmg }),
      (x) => `${x.receipt.number} confirmed: ${num(x.receipt.accepted_qty)} ${ship.uom} added to ${ship.sku} stock`);
    setResult(r);
    setShip(r.shipment);
  });

  const scanned = ship.scanned_qty || 0;
  return (
    <Modal title={`Goods receipt · ${ship.ref}`} eyebrow={`${ship.po} · ${ship.supplier}`} width={620} onClose={onClose}
      subtitle={`${ship.sku} ${ship.material} · BOL ${ship.bol_number} · dock ${ship.dock || '—'}`}
      footer={result
        ? <Button onClick={onClose}>Done</Button>
        : <>
          <span className="text-[10px] text-fg-faint mr-auto">Inventory is updated only when the receipt is confirmed.</span>
          <Button variant="ghost" onClick={onClose}>Close</Button>
          <Button icon="check" disabled={!allowed || busy || rec <= 0 || dmg > rec || !['Arrived', 'Receiving'].includes(ship.status)} onClick={confirm}>
            Confirm goods receipt
          </Button>
        </>}>
      {result ? (
        <div className="space-y-3">
          <div className={`tile ${result.receipt.shortage_qty || result.receipt.damaged_qty ? 'tone-warning' : 'tone-success'}`}>
            <p className="text-sm font-bold text-fg">{result.receipt.number} confirmed — {result.shipment.status}</p>
            <p className="text-xs text-fg-muted mt-1">
              Expected {qtyFmt(result.receipt.expected_qty, ship.uom)} · received {qtyFmt(result.receipt.received_qty, ship.uom)}
              {result.receipt.shortage_qty > 0 && <> · <b className="text-danger">shortage {qtyFmt(result.receipt.shortage_qty, ship.uom)} recorded</b></>}
              {result.receipt.damaged_qty > 0 && <> · damaged {qtyFmt(result.receipt.damaged_qty, ship.uom)} quarantined</>}
            </p>
          </div>
          <KV cols={3} items={[['Accepted into stock', qtyFmt(result.receipt.accepted_qty, ship.uom)],
            [`${ship.sku} on hand now`, qtyFmt(result.stock_on_hand, ship.uom)], ['Shipment status', result.shipment.status]]} />
          <p className="text-[11px] text-fg-faint">The material is now available to production; schedule projections and shortage alerts have been recalculated.</p>
        </div>
      ) : (
        <div className="space-y-4">
          <KV cols={4} items={[['Ordered on shipment', qtyFmt(expected, ship.uom)], ['Scanned so far', qtyFmt(scanned, ship.uom)],
            ['Damaged scanned', qtyFmt(ship.scanned_damaged || 0, ship.uom)], ['Status', <StatusBadge key="s" status={ship.status} />]]} />
          {ship.status === 'Arrived' && (
            <div className="tile flex items-center gap-3">
              <Icon name="scan" size={18} className="text-primary" />
              <p className="text-xs text-fg-muted flex-1">Start the receipt to scan pallets/bales with the (simulated) handheld scanner, or enter the counted quantity directly.</p>
              <Button size="sm" disabled={!allowed || busy} onClick={start}>Start receipt</Button>
            </div>
          )}
          {ship.status === 'Receiving' && (
            <div className="tile space-y-2">
              <p className="label-caps flex items-center gap-1.5"><Icon name="scan" size={12} />Simulated scanner</p>
              <div className="flex flex-wrap gap-1.5">
                <Button size="sm" variant="soft" disabled={busy} onClick={() => scan(Math.round(expected * 0.25))}>+25% (one lot)</Button>
                <Button size="sm" variant="soft" disabled={busy || scanned >= expected} onClick={() => scan(Math.max(0, expected - scanned))}>Scan remaining</Button>
                <Button size="sm" variant="soft" disabled={busy || scanned >= expected - 200} onClick={() => scan(Math.max(0, expected - 200 - scanned))}>Scan remaining −200 (short-ship)</Button>
                <Button size="sm" variant="ghost" disabled={busy || scanned <= 0} onClick={() => scan(-scanned, -(ship.scanned_damaged || 0))}>Clear scans</Button>
              </div>
              <div className="flex gap-1.5">
                <input className="input" style={{ maxWidth: 140 }} type="number" placeholder={`Qty ${ship.uom}`} value={custom} onChange={(e) => setCustom(e.target.value)} />
                <Button size="sm" variant="secondary" disabled={busy || !Number(custom)} onClick={() => { scan(Number(custom)); setCustom(''); }}>Add scan</Button>
                <Button size="sm" variant="secondary" disabled={busy || !Number(custom)} onClick={() => { scan(0, Number(custom)); setCustom(''); }}>Record as damaged</Button>
              </div>
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <label className="block"><span className="label-caps field-label">Received quantity ({ship.uom})</span>
              <input className="input" type="number" min="0" value={received} onChange={(e) => setReceived(e.target.value)} /></label>
            <label className="block"><span className="label-caps field-label">Of which damaged ({ship.uom})</span>
              <input className="input" type="number" min="0" value={damaged} onChange={(e) => setDamaged(e.target.value)} /></label>
          </div>
          <div className={`tile ${shortage > 0 || dmg > 0 ? 'tone-warning' : rec > 0 ? 'tone-success' : ''}`}>
            <p className="label-caps mb-1">Receipt check</p>
            <div className="grid grid-cols-4 gap-2 text-xs">
              <div><p className="text-fg-faint">Ordered</p><p className="font-bold text-fg">{num(expected)}</p></div>
              <div><p className="text-fg-faint">Received</p><p className="font-bold text-fg">{num(rec)}</p></div>
              <div><p className="text-fg-faint">{over > 0 ? 'Over-delivery' : 'Shortage'}</p>
                <p className="font-bold" style={{ color: `var(--color-${shortage > 0 ? 'danger' : over > 0 ? 'warning' : 'success'})` }}>{num(over > 0 ? over : shortage)}</p></div>
              <div><p className="text-fg-faint">Accepted to stock</p><p className="font-bold text-fg">{num(accepted)}</p></div>
            </div>
            {shortage > 0 && rec > 0 && <p className="text-[11px] text-danger mt-1.5">A shortage record of {qtyFmt(shortage, ship.uom)} will be raised against {ship.po}.</p>}
            {dmg > rec && <p className="text-[11px] text-danger mt-1.5">Damaged quantity cannot exceed the received quantity.</p>}
          </div>
          {!allowed && <p className="text-[11px] text-warning">Your role cannot post goods receipts.</p>}
        </div>
      )}
    </Modal>
  );
}

/* ─── Material drawer ─────────────────────────────────────────────────── */
function MaterialDrawer({ id, onClose, onCreatePo }) {
  const { filters } = useApp();
  const navigate = useNavigate();
  const { data, loading, error } = useApi(`/api/procurement/materials/${id}`, { factory: filters.factory });
  const mat = data?.material;
  const worst = data?.stock?.slice().sort((a, b) => (a.days_cover ?? 999) - (b.days_cover ?? 999))[0];
  return (
    <Drawer eyebrow="Raw material" title={mat ? `${mat.code} · ${mat.name}` : 'Material'} width={620} onClose={onClose}
      subtitle={mat ? `${mat.category} · ${mat.uom} · ${money(mat.unit_cost, 2)}/${mat.uom} · used by ${data.skus.join(', ') || '—'}` : undefined}
      headerExtra={worst && <StatusBadge status={worst.status} tone={STATUS_TONE[worst.status]} />}
      footer={mat && <Button icon="plus" onClick={() => onCreatePo({ material_id: mat.id, factory_id: worst?.factory_id, source: `${mat.code} coverage ${worst?.days_cover ?? '—'} days` })}>Create PO</Button>}>
      <ErrorBox error={error} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <div className="space-y-5">
          <div>
            <p className="label-caps mb-2">Stock by factory</p>
            <div className="tile p-0 overflow-x-auto">
              <table className="table">
                <thead><tr><th>Factory</th><th>Available</th><th>Reserved</th><th>Incoming</th><th>Use / day</th><th>Days cover</th><th>Next delivery</th></tr></thead>
                <tbody>{data.stock.map((s) => (
                  <tr key={s.id}>
                    <td className="cell-strong">{s.factory}</td><td>{num(s.available)}</td><td>{num(s.reserved)}</td><td>{num(s.incoming)}</td>
                    <td>{num(s.consumption_per_day)}</td><td><DaysCover row={s} /></td><td>{dayTime(s.next_delivery)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <p className="text-[11px] text-fg-faint mt-1.5">Days of stock = available usable quantity ÷ average daily consumption. Alert when below the reorder threshold.</p>
          </div>
          <div>
            <p className="label-caps mb-2">Consuming production orders</p>
            {data.consumers.length === 0 ? <p className="text-xs text-fg-faint">No open production orders use this material.</p> : (
              <div className="space-y-1.5">
                {data.consumers.map((p) => (
                  <button key={p.id} type="button" className="tile w-full text-left flex items-center gap-2 flex-wrap hover:opacity-90"
                    onClick={() => navigate(linkTo('production', 'production_order', p.id))}>
                    <span className="text-xs font-semibold text-fg">{p.number}</span>
                    <span className="text-[11px] text-fg-muted">{p.sku} · {p.line || 'unassigned'} · {num(p.remaining)} pillows left · est. {dayTime(p.estimated_completion)}</span>
                    {p.material_risk && <Badge tone="danger">MATERIAL RISK</Badge>}
                    <span className="ml-auto"><StatusBadge status={p.status} /></span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <div>
            <p className="label-caps mb-2">Purchase orders</p>
            <div className="tile p-0 overflow-x-auto">
              <table className="table">
                <thead><tr><th>PO</th><th>Factory</th><th>Supplier</th><th>Ordered</th><th>Pending</th><th>Expected</th><th>Status</th></tr></thead>
                <tbody>{data.purchase_orders.map((p) => (
                  <tr key={p.id}>
                    <td className="cell-strong">{p.number}</td><td>{p.factory}</td><td>{p.supplier}</td><td>{num(p.ordered_qty)}</td>
                    <td>{num(p.pending_qty)}</td><td>{dateShort(p.expected_date)}</td><td><StatusBadge status={p.status} /></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </div>
          <div>
            <p className="label-caps mb-2">Approved suppliers</p>
            <div className="flex flex-wrap gap-2">{data.suppliers.map((s) => <Badge key={s.id} tone="neutral">{s.name} · {s.lead_time_days} d lead time</Badge>)}</div>
          </div>
        </div>
      )}
    </Drawer>
  );
}

/* ─── Supplier drawer ─────────────────────────────────────────────────── */
function SupplierDrawer({ supplier, onClose, onOpenPo }) {
  const { filters } = useApp();
  const { data, loading } = useApi('/api/procurement/purchase-orders', { factory: filters.factory, supplier_id: supplier.supplier_id });
  return (
    <Drawer eyebrow="Supplier" title={supplier.supplier} width={560} onClose={onClose}
      headerExtra={<Badge tone={supplier.on_time_pct >= 85 ? 'success' : supplier.on_time_pct >= 70 ? 'warning' : 'danger'}>{pct(supplier.on_time_pct, 0)} ON TIME</Badge>}>
      <KV cols={3} items={[['Deliveries', supplier.deliveries], ['Late', supplier.late], ['Avg lateness', `${supplier.avg_late_days} d`],
        ['Delayed now', supplier.open_delayed], ['Quoted lead time', `${supplier.lead_time_days} d`]]} />
      <p className="label-caps mt-5 mb-2">Purchase orders</p>
      {loading && !data && <Loading />}
      {data && (
        <DataTable rows={data} onRowClick={(r) => onOpenPo(r.id)} emptyText="No purchase orders."
          columns={[
            { key: 'number', header: 'PO', strong: true }, { key: 'factory', header: 'Factory' }, { key: 'sku', header: 'Material' },
            { key: 'ordered_qty', header: 'Ordered', render: (r) => qtyFmt(r.ordered_qty, r.uom) },
            { key: 'expected_date', header: 'Expected', render: (r) => dateShort(r.expected_date) },
            { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
          ]} />
      )}
    </Drawer>
  );
}

/* ─── Page ────────────────────────────────────────────────────────────── */
export default function Procurement() {
  const { filters, can } = useApp();
  const location = useLocation();
  const focus = useFocus();
  const [tab, setTab] = useState('pos');
  const [poView, setPoView] = useState('');
  const [shipView, setShipView] = useState('');
  const [matView, setMatView] = useState('');
  const [poId, setPoId] = useState(null);
  const [materialId, setMaterialId] = useState(null);
  const [supplier, setSupplier] = useState(null);
  const [createPo, setCreatePo] = useState(null);
  const [arrive, setArrive] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [pendingFocus, setPendingFocus] = useState(null);

  const f = { factory: filters.factory };
  const summary = useApi('/api/procurement/summary', f, { every: 2 });
  const poParams = { ...f, ...(poView === 'Received' ? { status: 'Received' } : poView ? { view: poView } : {}) };
  const pos = useApi(tab === 'pos' ? '/api/procurement/purchase-orders' : null, poParams, { every: 3 });
  const ships = useApi(tab === 'shipments' || pendingFocus?.type === 'shipment' ? '/api/procurement/shipments' : null, { ...f, view: shipView || undefined }, { every: 2 });
  const mats = useApi(tab === 'materials' ? '/api/procurement/materials' : null, f, { every: 3 });
  const sups = useApi(tab === 'suppliers' || pendingFocus?.type === 'supplier' ? '/api/procurement/suppliers' : null, f, { every: 6 });

  // ?focus=type:id from alerts, insights and other pages
  const fType = focus?.type;
  const fId = focus?.id;
  useEffect(() => {
    if (!fType) return;
    if (fType === 'material') { setTab('materials'); setMaterialId(fId); }
    else if (fType === 'purchase_order') { setTab('pos'); setPoId(fId); }
    else if (fType === 'supplier') { setTab('suppliers'); setPendingFocus({ type: 'supplier', id: fId }); }
    else if (fType === 'shipment') { setTab('shipments'); setShipView(''); setPendingFocus({ type: 'shipment', id: fId }); }
  }, [fType, fId, location.key]);

  useEffect(() => {
    if (pendingFocus?.type === 'shipment' && ships.data) {
      const s = ships.data.find((x) => x.id === pendingFocus.id);
      if (s) {
        if (['Arrived', 'Receiving'].includes(s.status)) setReceipt(s);
        else setPoId(s.po_id);
      }
      setPendingFocus(null);
    }
    if (pendingFocus?.type === 'supplier' && sups.data) {
      const s = sups.data.find((x) => x.supplier_id === pendingFocus.id);
      if (s) setSupplier(s);
      setPendingFocus(null);
    }
  }, [pendingFocus, ships.data, sups.data]);

  const s = summary.data;
  const kpiGo = (t, setter, v) => { setTab(t); setter(v); };
  const matRows = useMemo(() => (mats.data || []).filter((r) => matView !== 'short' || ['Critical', 'Low'].includes(r.status)), [mats.data, matView]);
  const showFactory = filters.factory === 'ALL';

  const onInsight = (action) => {
    if (action.type === 'create_po') {
      const p = action.payload;
      setCreatePo({ factory_id: p.factory_id, material_id: p.material_id, supplier_id: p.supplier_id, qty: p.qty,
        expected_days: p.expected_days, source: action.label });
    }
  };

  return (
    <div className="space-y-4">
      <PageTitle title="Procurement & Inbound" subtitle="Ordered → expected → arrived → received → available for production">
        <Button icon="plus" disabled={!can('procurement.write')} onClick={() => setCreatePo({})}>Create PO</Button>
      </PageTitle>
      <StageAlerts stage="procurement" />
      <ErrorBox error={summary.error} onRetry={summary.reload} />
      {!s && summary.loading && <Loading rows={2} height={60} />}
      {s && (
        <KpiRow cols={6}>
          <Kpi label="Open purchase orders" icon="clipboard" value={num(s.open_pos)} sub={`${money(s.open_po_value)} on order`}
            active={tab === 'pos' && poView === 'open'} onClick={() => kpiGo('pos', setPoView, 'open')} />
          <Kpi label="Expected inbound" icon="truck" value={num(s.expected_inbound)} sub="Shipments due within 7 days"
            active={tab === 'shipments' && shipView === 'expected'} onClick={() => kpiGo('shipments', setShipView, 'expected')} />
          <Kpi label="Goods received today" icon="check" value={num(s.received_today)} tone={s.received_today ? 'success' : undefined}
            sub="Confirmed goods receipts" active={tab === 'shipments' && shipView === 'received_today'}
            onClick={() => kpiGo('shipments', setShipView, 'received_today')} />
          <Kpi label="Pending goods receipts" icon="hourglass" value={num(s.pending_receipts)} tone={s.pending_receipts ? 'warning' : undefined}
            sub="Arrived, not yet received" active={tab === 'shipments' && shipView === 'pending'} onClick={() => kpiGo('shipments', setShipView, 'pending')} />
          <Kpi label="Material shortages" icon="layers" value={num(s.shortages)} tone={s.critical ? 'danger' : s.shortages ? 'warning' : 'success'}
            sub={`${s.critical} critical below 1.5 days`} active={tab === 'materials' && matView === 'short'} onClick={() => kpiGo('materials', setMatView, 'short')} />
          <Kpi label="Delayed deliveries" icon="clock" value={num(s.delayed)} tone={s.delayed ? 'danger' : 'success'}
            sub="Supplier ETA moved out" active={tab === 'shipments' && shipView === 'delayed'} onClick={() => kpiGo('shipments', setShipView, 'delayed')} />
        </KpiRow>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-4 gap-4 items-start">
        <Card className="xl:col-span-3" padded={false}>
          <div className="px-4 pt-1"><Tabs tabs={TABS} value={tab} onChange={setTab} variant="underline" /></div>

          {tab === 'pos' && (
            <div>
              <div className="px-4 py-3"><FilterChips label="Show" options={PO_VIEWS} value={poView} onChange={setPoView} /></div>
              <ErrorBox error={pos.error} />
              {pos.loading && !pos.data ? <div className="p-4"><Loading /></div> : (
                <DataTable rows={pos.data || []} onRowClick={(r) => setPoId(r.id)} emptyText="No purchase orders for this filter."
                  columns={[
                    { key: 'number', header: 'PO number', strong: true },
                    ...(showFactory ? [{ key: 'factory', header: 'Factory' }] : []),
                    { key: 'supplier', header: 'Supplier' },
                    { key: 'material', header: 'Material', render: (r) => <span className="truncate block max-w-[180px]" title={r.material}>{r.material}</span> },
                    { key: 'sku', header: 'SKU', mono: true },
                    { key: 'ordered_qty', header: 'Ordered', render: (r) => qtyFmt(r.ordered_qty, r.uom) },
                    { key: 'received_qty', header: 'Received', render: (r) => num(r.received_qty) },
                    { key: 'pending_qty', header: 'Pending', render: (r) => <span className={r.pending_qty ? 'text-fg font-semibold' : ''}>{num(r.pending_qty)}</span> },
                    { key: 'order_date', header: 'Ordered on', render: (r) => dateShort(r.order_date) },
                    { key: 'expected_date', header: 'Expected', render: (r) => dateTime(r.expected_date) },
                    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
                  ]} />
              )}
            </div>
          )}

          {tab === 'shipments' && (
            <div>
              <div className="px-4 py-3"><FilterChips label="Show" options={SHIP_VIEWS} value={shipView} onChange={setShipView} /></div>
              <ErrorBox error={ships.error} />
              {ships.loading && !ships.data ? <div className="p-4"><Loading /></div> : (
                <DataTable rows={ships.data || []} onRowClick={(r) => setPoId(r.po_id)} emptyText="No shipments for this filter."
                  columns={[
                    { key: 'ref', header: 'Shipment', strong: true, render: (r) => <span>{r.ref}<span className="block text-[10px] text-fg-faint font-normal">{r.carrier}</span></span> },
                    { key: 'bol_number', header: 'Bill of lading', mono: true },
                    ...(showFactory ? [{ key: 'factory', header: 'Factory' }] : []),
                    { key: 'supplier', header: 'Supplier' },
                    { key: 'sku', header: 'Material', render: (r) => <span><b className="text-fg">{r.sku}</b> · {qtyFmt(r.qty, r.uom)}</span> },
                    { key: 'expected_arrival', header: 'Expected', render: (r) => (
                      <span className={r.delayed ? 'text-danger font-semibold' : ''}>{dayTime(r.expected_arrival)}
                        {r.delayed && <span className="block text-[10px] font-normal" title={r.delay_reason}>was {dayTime(r.original_expected_arrival)}</span>}
                      </span>) },
                    { key: 'actual_arrival', header: 'Arrived', render: (r) => dayTime(r.actual_arrival) },
                    { key: 'dock', header: 'Dock', render: (r) => r.dock || '—' },
                    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
                    { key: 'actions', header: '', render: (r) => (
                      <span className="flex gap-1.5 justify-end" onClick={(e) => e.stopPropagation()}>
                        {['Scheduled', 'In Transit'].includes(r.status) && (
                          <Button size="sm" variant="secondary" disabled={!can('receiving.write')} onClick={() => setArrive(r)}>Mark arrived</Button>
                        )}
                        {['Arrived', 'Receiving'].includes(r.status) && (
                          <Button size="sm" icon="scan" disabled={!can('receiving.write')} onClick={() => setReceipt(r)}>Receive</Button>
                        )}
                      </span>) },
                  ]} />
              )}
            </div>
          )}

          {tab === 'materials' && (
            <div>
              <div className="px-4 py-3"><FilterChips label="Show" options={MAT_VIEWS} value={matView} onChange={setMatView} /></div>
              <ErrorBox error={mats.error} />
              {mats.loading && !mats.data ? <div className="p-4"><Loading /></div> : (
                <DataTable rows={matRows} onRowClick={(r) => setMaterialId(r.material_id)} emptyText="No materials below threshold."
                  columns={[
                    { key: 'code', header: 'Material', render: (r) => <span><b className="text-fg">{r.code}</b><span className="block text-[10px] text-fg-faint">{r.name}</span></span> },
                    ...(showFactory ? [{ key: 'factory', header: 'Factory' }] : []),
                    { key: 'available', header: 'Available', render: (r) => qtyFmt(r.available, r.uom) },
                    { key: 'reserved', header: 'Reserved', render: (r) => num(r.reserved) },
                    { key: 'incoming', header: 'Incoming', render: (r) => num(r.incoming) },
                    { key: 'consumption_per_day', header: 'Use / day', render: (r) => num(r.consumption_per_day) },
                    { key: 'days_cover', header: 'Days of stock', render: (r) => <DaysCover row={r} /> },
                    { key: 'reorder_point', header: 'Reorder point', render: (r) => num(r.reorder_point) },
                    { key: 'next_delivery', header: 'Next delivery', render: (r) => dayTime(r.next_delivery) },
                    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} tone={STATUS_TONE[r.status]} /> },
                  ]} />
              )}
            </div>
          )}

          {tab === 'suppliers' && (
            <div className="p-4 space-y-4">
              {sups.loading && !sups.data && <Loading />}
              {sups.data && sups.data.length === 0 && <Empty>No supplier history.</Empty>}
              {sups.data && sups.data.length > 0 && (
                <>
                  <ChartCard title="On-time delivery by supplier" subtitle="Receipts vs original due date; open delayed shipments count against reliability"
                    type="bar" horizontal height={Math.max(160, sups.data.length * 30)} yMax={100}
                    data={{ labels: sups.data.map((r) => r.supplier), datasets: [{ label: 'On-time %', data: sups.data.map((r) => r.on_time_pct),
                      color: sups.data.map((r) => (r.on_time_pct >= 85 ? 'success' : r.on_time_pct >= 70 ? 'warning' : 'danger')) }] }} />
                  <DataTable rows={sups.data} rowKey="supplier_id" onRowClick={setSupplier}
                    columns={[
                      { key: 'supplier', header: 'Supplier', strong: true },
                      { key: 'deliveries', header: 'Deliveries' },
                      { key: 'late', header: 'Late' },
                      { key: 'on_time_pct', header: 'On time', render: (r) => <span style={{ color: `var(--color-${r.on_time_pct >= 85 ? 'success' : r.on_time_pct >= 70 ? 'warning' : 'danger'})` }} className="font-semibold">{pct(r.on_time_pct, 0)}</span> },
                      { key: 'avg_late_days', header: 'Avg late', render: (r) => (r.late ? `${r.avg_late_days} d` : '—') },
                      { key: 'open_delayed', header: 'Delayed now', render: (r) => (r.open_delayed ? <Badge tone="danger">{r.open_delayed}</Badge> : '0') },
                      { key: 'lead_time_days', header: 'Lead time', render: (r) => `${r.lead_time_days} d` },
                    ]} />
                </>
              )}
            </div>
          )}
        </Card>
        <div className="space-y-4">
          <InsightsCard page="procurement" limit={6} onAction={onInsight} />
        </div>
      </div>

      {poId && <PODrawer id={poId} onClose={() => setPoId(null)} />}
      {materialId && <MaterialDrawer id={materialId} onClose={() => setMaterialId(null)} onCreatePo={(init) => { setMaterialId(null); setCreatePo(init); }} />}
      {supplier && <SupplierDrawer supplier={supplier} onClose={() => setSupplier(null)} onOpenPo={(id) => { setSupplier(null); setPoId(id); }} />}
      {createPo && <CreatePOModal initial={createPo} onClose={() => setCreatePo(null)} />}
      {arrive && <ArriveModal ship={arrive} onClose={() => setArrive(null)} />}
      {receipt && <GoodsReceiptModal initial={receipt} onClose={() => setReceipt(null)} />}
    </div>
  );
}

