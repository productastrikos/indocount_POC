import React, { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Badge, Button, Card, DataTable, Drawer, FilterChips, Icon, Modal, ProgressBar, Tabs } from '../components/ui';
import ChartCard from '../components/charts/ChartCard';
import {
  Empty, ErrorBox, InsightsCard, Kpi, KV, Loading, PageTitle, StageAlerts, StatusBadge,
} from '../components/common';
import { useApi, useFocus } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { api, post } from '../api/client';
import { compact, dateTime, dayTime, linkTo, num, pct } from '../utils';

const TABS = [
  { value: 'layout', label: 'Warehouse layout' },
  { value: 'receiving', label: 'Finished goods receiving' },
  { value: 'allocation', label: 'Inventory allocation' },
  { value: 'picking', label: 'Picking' },
  { value: 'workload', label: 'Workload & workforce' },
];

/* ─── Layout ──────────────────────────────────────────────────────────── */
function cellStyle(loc, selected) {
  const base = { borderRadius: 3, border: '1px solid var(--color-border)', cursor: 'pointer' };
  let bg = 'var(--color-surface-raised)';
  if (loc.occupied) bg = loc.reserved ? 'var(--color-warning)' : 'var(--color-primary)';
  return { ...base, background: bg, opacity: loc.occupied && loc.kind === 'Rack' ? 0.9 : 1,
    outline: selected ? '2px solid var(--color-accent)' : 'none', outlineOffset: 1 };
}

function RackZone({ zone, selected, onPick }) {
  const bays = [...new Set(zone.locations.map((l) => l.bay))];
  return (
    <div className="tile">
      <div className="flex items-center justify-between mb-2 gap-2">
        <p className="text-xs font-bold text-fg">{zone.name}</p>
        <span className="text-[10.5px] text-fg-faint">{pct(zone.utilization_pct, 0)} occupied</span>
      </div>
      <ProgressBar value={zone.utilization_pct} tone={zone.utilization_pct > 90 ? 'danger' : zone.utilization_pct > 75 ? 'warning' : 'primary'} />
      <div className="grid grid-cols-4 gap-2 mt-3">
        {bays.map((bay) => {
          const locs = zone.locations.filter((l) => l.bay === bay);
          const racks = [...new Set(locs.map((l) => l.rack))];
          const levels = [...new Set(locs.map((l) => l.level))].sort((a, b) => b - a);
          return (
            <div key={bay}>
              <p className="text-[9.5px] text-fg-faint mb-1 text-center">Bay {String(bay).padStart(2, '0')}</p>
              <div className="grid gap-[3px]" style={{ gridTemplateColumns: `repeat(${racks.length}, minmax(0, 1fr))` }}>
                {levels.flatMap((lv) => racks.map((rk) => {
                  const loc = locs.find((l) => l.rack === rk && l.level === lv);
                  if (!loc) return <span key={`${rk}-${lv}`} />;
                  return (
                    <button key={loc.id} type="button" aria-label={loc.code} title={`${loc.code}${loc.sku ? ` · ${loc.sku}` : ' · empty'}${loc.reserved ? ' · reserved' : ''}`}
                      onClick={() => onPick(loc.id)} style={{ ...cellStyle(loc, selected === loc.id), height: 16 }} />
                  );
                }))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FloorZone({ zone, selected, onPick }) {
  return (
    <div className="tile">
      <div className="flex items-center justify-between mb-2 gap-2">
        <p className="text-xs font-bold text-fg">{zone.name}</p>
        <span className="text-[10.5px] text-fg-faint">{pct(zone.utilization_pct, 0)} occupied</span>
      </div>
      <ProgressBar value={zone.utilization_pct} tone={zone.utilization_pct > 90 ? 'danger' : zone.utilization_pct > 75 ? 'warning' : 'primary'} />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-3">
        {zone.locations.map((loc) => (
          <button key={loc.id} type="button" onClick={() => onPick(loc.id)} className="text-left rounded-md p-1.5"
            style={{ border: `1px solid ${selected === loc.id ? 'var(--color-accent)' : 'var(--color-border)'}`, background: 'var(--color-surface)' }}>
            <div className="flex justify-between text-[10px] mb-1"><span className="font-semibold text-fg">{loc.code.split('-').slice(1).join('-')}</span>
              <span className="text-fg-faint">{loc.occupied}/{loc.capacity}</span></div>
            <ProgressBar value={loc.occupied} max={loc.capacity} tone={loc.reserved ? 'warning' : 'primary'} />
            <p className="text-[9.5px] text-fg-faint mt-1 truncate">{loc.sku || 'Empty lane'}</p>
          </button>
        ))}
      </div>
    </div>
  );
}

function LocationDrawer({ id, onClose }) {
  const { act } = useApp();
  const navigate = useNavigate();
  const { data, loading, error } = useApi(`/api/warehouse/locations/${id}`);
  const loc = data?.location;
  const scanLoc = () => act(() => post('/api/warehouse/scan', { code: loc.code }), (r) => `Barcode ${loc.barcode} read: ${r.message}`).catch(() => {});
  return (
    <Drawer eyebrow="Storage location" title={loc?.code || 'Location'} width={540} onClose={onClose}
      subtitle={loc ? `Zone ${loc.zone} · bay ${loc.bay} · ${loc.kind === 'Floor' ? `lane ${loc.rack}` : `rack ${loc.rack} · level ${loc.level}`}` : undefined}
      headerExtra={loc && <Badge tone={loc.occupied ? (loc.occupied >= loc.capacity ? 'primary' : 'info') : 'neutral'}>{loc.occupied}/{loc.capacity} PALLETS</Badge>}
      footer={loc && <><span className="text-[10px] text-fg-faint mr-auto font-mono">Barcode {loc.barcode}</span>
        <Button size="sm" variant="secondary" icon="scan" onClick={scanLoc}>Simulate barcode scan</Button></>}>
      <ErrorBox error={error} />
      {loading && !data && <Loading rows={5} />}
      {data && data.pallets.length === 0 && <Empty icon="box">Empty location — available for put-away.</Empty>}
      {data && (
        <div className="space-y-3">
          {data.pallets.map((p) => (
            <div key={p.id} className="tile">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-bold text-fg">{p.code}</span>
                <StatusBadge status={p.status} />
              </div>
              <div className="mt-2">
                <KV cols={3} items={[
                  ['SKU', p.sku], ['Customer', p.customer], ['Quantity', `${num(p.cartons)} ctn · ${num(p.pillows)} pillows`],
                  ['Batch', p.batch_no], ['Production order', p.production_order], ['Date stored', dateTime(p.stored_at)],
                  ['Reserved', p.allocated_cartons ? `${p.allocated_cartons} cartons` : 'Unallocated'], ['Carton type', p.carton_type],
                  ['RFID tag', <span key="r" className="font-mono text-[11px]">{p.rfid_tag}</span>],
                ]} />
              </div>
              {p.po_id && (
                <button type="button" className="link-btn mt-2" onClick={() => navigate(linkTo('production', 'production_order', p.po_id))}>OPEN PRODUCTION ORDER</button>
              )}
            </div>
          ))}
        </div>
      )}
    </Drawer>
  );
}

/* ─── Put-away ────────────────────────────────────────────────────────── */
function PutawayModal({ pallet, onClose }) {
  const { act, can } = useApp();
  const { data: layout } = useApi('/api/warehouse/layout', { factory: pallet.factory }, { live: false });
  const [suggested, setSuggested] = useState(pallet.suggested_location || null);
  const [sku, setSku] = useState('');
  const [cartons, setCartons] = useState('');
  const [locId, setLocId] = useState(pallet.suggested_location?.id || '');
  const [busy, setBusy] = useState(false);
  const bulk = (pallet.carton_type || '').includes('Gaylord');

  useEffect(() => {
    if (suggested) return;
    api('/api/warehouse/scan', { method: 'POST', body: { code: pallet.code } })
      .then((r) => { if (r.pallet?.suggested_location) { setSuggested(r.pallet.suggested_location); setLocId((v) => v || r.pallet.suggested_location.id); } })
      .catch(() => {});
  }, [pallet.code, suggested]);

  const free = useMemo(() => (layout?.zones || []).flatMap((z) => z.locations)
    .filter((l) => l.occupied < l.capacity)
    .sort((a, b) => ((a.kind === 'Floor') === bulk ? 0 : 1) - ((b.kind === 'Floor') === bulk ? 0 : 1)), [layout, bulk]);

  useEffect(() => {
    if (!locId && !suggested && free.length) setLocId(free[0].id);
  }, [free, locId, suggested]);

  const skuOk = sku.trim().toUpperCase() === pallet.sku;
  const cartonsOk = Number(cartons) === pallet.cartons;
  const ready = skuOk && cartonsOk && locId;
  const chosen = free.find((l) => l.id === Number(locId)) || (suggested && Number(locId) === suggested.id ? suggested : null);

  const confirm = async () => {
    setBusy(true);
    try {
      await act(() => post('/api/warehouse/putaway', { pallet_id: pallet.id, location_id: Number(locId), confirmed_sku: sku.trim().toUpperCase(),
        confirmed_cartons: Number(cartons) }), (r) => `${r.code} stored at ${r.location} — finished goods inventory updated`);
      onClose();
    } catch (e) { /* toast */ } finally { setBusy(false); }
  };

  const Step = ({ n, ok, title, children }) => (
    <div className={`tile ${ok ? 'tone-success' : ''}`}>
      <p className="text-xs font-bold text-fg flex items-center gap-2 mb-2">
        <span className="w-5 h-5 rounded-full flex items-center justify-center text-[10px]"
          style={{ background: ok ? 'var(--color-success)' : 'var(--color-border-strong)', color: '#fff' }}>{ok ? '✓' : n}</span>{title}
      </p>
      {children}
    </div>
  );

  return (
    <Modal title={`Put away ${pallet.code}`} eyebrow="Finished goods receiving" width={560} onClose={onClose}
      subtitle={`${pallet.sku} · ${pallet.customer} · ${num(pallet.cartons)} cartons (${num(pallet.pillows)} pillows) · batch ${pallet.batch_no}`}
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button icon="check" disabled={!ready || busy || !can('warehouse.write')} onClick={confirm}>{busy ? 'Storing…' : 'Confirm put-away'}</Button></>}>
      <div className="space-y-3">
        {pallet.status === 'Open' && <p className="tile tone-info text-[11px]">This pallet is still open ({pallet.cartons}/{pallet.capacity} cartons). Confirming closes it and moves it to stock.</p>}
        <Step n={1} ok={skuOk} title="Verify SKU (scan carton label)">
          <div className="flex gap-2">
            <input className="input" placeholder="Scan or type SKU" value={sku} onChange={(e) => setSku(e.target.value)} />
            <Button size="sm" variant="soft" icon="scan" onClick={() => setSku(pallet.sku)}>Simulate scan</Button>
          </div>
          {sku && !skuOk && <p className="text-[11px] text-danger mt-1">SKU mismatch — pallet record shows {pallet.sku}.</p>}
        </Step>
        <Step n={2} ok={cartonsOk} title="Confirm carton quantity">
          <div className="flex gap-2">
            <input className="input" type="number" placeholder="Counted cartons" value={cartons} onChange={(e) => setCartons(e.target.value)} />
            <Button size="sm" variant="soft" onClick={() => setCartons(String(pallet.cartons))}>RFID count ({pallet.cartons})</Button>
          </div>
          {cartons && !cartonsOk && <p className="text-[11px] text-danger mt-1">Count differs from the pallet record ({pallet.cartons}). Recount before storing.</p>}
        </Step>
        <Step n={3} ok={!!locId} title="Assign warehouse location">
          {suggested && <p className="text-[11px] text-fg-muted mb-1.5">Suggested: <b className="text-fg">{suggested.code}</b> {bulk ? '(bulk floor lane for Gaylords)' : '(next to the same SKU where possible)'}</p>}
          <select className="select" value={locId} onChange={(e) => setLocId(e.target.value)}>
            {suggested && <option value={suggested.id}>{suggested.code} · suggested</option>}
            {free.filter((l) => !suggested || l.id !== suggested.id).slice(0, 80).map((l) => (
              <option key={l.id} value={l.id}>{l.code} · {l.kind === 'Floor' ? `floor lane ${l.occupied}/${l.capacity}` : 'rack slot'}{l.sku ? ` · ${l.sku}` : ''}</option>
            ))}
          </select>
          {chosen && <p className="text-[10.5px] text-fg-faint mt-1">Destination {chosen.code}</p>}
        </Step>
      </div>
    </Modal>
  );
}

/* ─── Order fulfilment drawer ─────────────────────────────────────────── */
function OrderDrawer({ id, onClose }) {
  const { act, can } = useApp();
  const navigate = useNavigate();
  const { data, loading, error } = useApi(`/api/dispatch/orders/${id}`);
  const o = data?.order;
  return (
    <Drawer eyebrow="Order fulfilment" title={o ? `${o.number} · ${o.customer}` : 'Order'} width={580} onClose={onClose}
      subtitle={o ? `${num(o.qty)} × ${o.sku} · ships ${dayTime(o.scheduled_dispatch)}${o.truck ? ` · ${o.truck.ref} at ${o.truck.dock}` : ''}` : undefined}
      headerExtra={o && <StatusBadge status={o.status} />}
      footer={o && <>
        <Button variant="ghost" icon="external" onClick={() => navigate(linkTo('dispatch', 'order', o.id))}>Open in Dispatch</Button>
        <Button variant="secondary" disabled={!can('warehouse.write') || o.status === 'Dispatched' || o.status === 'Delivered'}
          onClick={() => act(() => post('/api/warehouse/picking/expedite', { order_id: o.id }), `Picking expedited for ${o.number}`).catch(() => {})}>Expedite picking</Button>
        <Button disabled={!can('warehouse.write') || o.available_inventory >= o.qty}
          onClick={() => act(() => post('/api/warehouse/allocate', { order_id: o.id }), (r) => r.message).catch(() => {})}>Allocate stock</Button>
      </>}>
      <ErrorBox error={error} />
      {loading && !data && <Loading rows={5} />}
      {data && (
        <div className="space-y-5">
          <div>
            <div className="flex items-center justify-between mb-2"><p className="label-caps">Shipment readiness</p><b className="text-sm text-fg">{pct(o.readiness_pct, 0)}</b></div>
            <div className="space-y-2">
              {data.stages.map((s) => (
                <div key={s.key}>
                  <div className="flex justify-between text-[11px] mb-0.5"><span className="text-fg-muted">{s.label}</span><span className="font-semibold text-fg">{pct(s.pct, 0)}</span></div>
                  <ProgressBar value={s.pct} tone={s.pct >= 99.9 ? 'success' : s.key === o.blocking ? 'danger' : 'warning'} />
                </div>
              ))}
            </div>
          </div>
          <KV cols={3} items={[['Ordered', num(o.qty)], ['Allocated', num(o.available_inventory)], ['Picked', num(o.picked)]]} />
          <div>
            <p className="label-caps mb-2">Allocations & pick tasks</p>
            {data.allocations.length === 0 ? <p className="text-xs text-fg-faint">Nothing allocated yet.</p> : (
              <div className="tile p-0 overflow-x-auto">
                <table className="table">
                  <thead><tr><th>Pallet</th><th>Location</th><th>Cartons</th><th>Pillows</th><th>Status</th><th /></tr></thead>
                  <tbody>{data.allocations.map((a) => (
                    <tr key={a.id}>
                      <td className="cell-strong">{a.pallet}</td><td>{a.location}</td><td>{num(a.cartons)}</td><td>{num(a.pillows)}</td>
                      <td><StatusBadge status={a.status === 'Reserved' ? 'Picking Pending' : a.status} /></td>
                      <td>{a.status === 'Reserved' && (
                        <Button size="sm" disabled={!can('warehouse.write')}
                          onClick={() => act(() => post(`/api/warehouse/picking/${a.id}/confirm`), `Picked ${a.cartons} cartons from ${a.pallet}`).catch(() => {})}>Confirm pick</Button>
                      )}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </div>
          {data.production_orders.length > 0 && o.blocking && ['production', 'quality', 'inventory', 'allocation'].includes(o.blocking) && (
            <div>
              <p className="label-caps mb-2">Linked production (covers the shortage)</p>
              {data.production_orders.map((p) => (
                <button key={p.id} type="button" className="tile w-full text-left flex items-center gap-2 hover:opacity-90"
                  onClick={() => navigate(linkTo('production', 'production_order', p.id))}>
                  <span className="text-xs font-semibold text-fg">{p.number}</span>
                  <span className="text-[11px] text-fg-muted">{p.line} · {num(p.remaining_qty)} pillows left · est. {dayTime(p.estimated_completion)}</span>
                  <span className="ml-auto"><StatusBadge status={p.status} /></span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

/* ─── Tabs ────────────────────────────────────────────────────────────── */
function LayoutTab({ onPick, selected }) {
  const { filters } = useApp();
  const { data, loading, error } = useApi('/api/warehouse/layout', { factory: filters.factory }, { every: 3 });
  if (loading && !data) return <div className="p-4"><Loading rows={6} /></div>;
  return (
    <div className="p-4 space-y-3">
      <ErrorBox error={error} />
      {data && (
        <>
          <div className="flex items-center justify-between flex-wrap gap-2">
            <p className="text-xs text-fg-muted">
              Showing <b className="text-fg">{data.factory_name}</b> finished-goods warehouse
              {filters.factory === 'ALL' && <span className="text-fg-faint"> — select a factory in the header to view another site</span>}
            </p>
            <div className="flex items-center gap-3 text-[10.5px] text-fg-muted">
              <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm" style={{ background: 'var(--color-primary)' }} />Stored</span>
              <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm" style={{ background: 'var(--color-warning)' }} />Reserved for an order</span>
              <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm border" style={{ background: 'var(--color-surface-raised)' }} />Empty</span>
            </div>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {data.zones.filter((z) => z.zone !== 'D').map((z) => <RackZone key={z.zone} zone={z} selected={selected} onPick={onPick} />)}
          </div>
          {data.zones.filter((z) => z.zone === 'D').map((z) => <FloorZone key={z.zone} zone={z} selected={selected} onPick={onPick} />)}
          <p className="text-[10.5px] text-fg-faint">Racks: rows are levels (L4 at top), columns are racks R1–R3 within each bay. Occupancy is read from simulated barcode/RFID put-away records.</p>
        </>
      )}
    </div>
  );
}

function ReceivingTab({ onPutaway, onLocation }) {
  const { filters } = useApp();
  const navigate = useNavigate();
  const { data, loading, error } = useApi('/api/warehouse/receiving', { factory: filters.factory }, { every: 2 });
  const { data: blocked } = useApi('/api/quality/cartons', { factory: filters.factory, status: 'Blocked', limit: 3 }, { every: 5 });
  const summary = useApi('/api/warehouse/summary', { factory: filters.factory }, { every: 3 });
  const [code, setCode] = useState('');
  const [result, setResult] = useState(null);
  const [scanErr, setScanErr] = useState(null);

  const scan = async (c) => {
    const value = (c ?? code).trim();
    if (!value) return;
    setCode(value);
    setScanErr(null);
    try { setResult(await post('/api/warehouse/scan', { code: value })); } catch (e) { setResult(null); setScanErr(e.message); }
  };

  const quick = [
    ...(data?.pending || []).slice(0, 2).map((p) => p.code),
    ...(data?.recent_cartons || []).slice(0, 2).map((c) => c.carton_code),
    ...(blocked || []).slice(0, 1).map((c) => c.hold_code),
    ...(data?.building || []).slice(0, 1).map((p) => p.code),
  ].filter(Boolean);
  const disc = summary.data?.discrepancy_detail;

  return (
    <div className="p-4 space-y-4">
      <div className="tile">
        <p className="label-caps mb-2 flex items-center gap-1.5"><Icon name="scan" size={12} />Scan carton, pallet or location (simulated handheld / RFID)</p>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); scan(); }}>
          <input className="input font-mono" placeholder="CTN-… / PLT-… / HOLD-… / location code" value={code} onChange={(e) => setCode(e.target.value)} />
          <Button type="submit" icon="scan" disabled={!code.trim()}>Scan</Button>
        </form>
        {quick.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2 items-center">
            <span className="text-[10px] text-fg-faint">Try:</span>
            {quick.map((q) => <button key={q} type="button" className="chip font-mono" onClick={() => scan(q)}>{q}</button>)}
          </div>
        )}
        {scanErr && <p className="tile tone-danger text-xs mt-2">{scanErr}</p>}
        {result && (
          <div className={`tile mt-2 ${result.ok ? 'tone-success' : 'tone-danger'}`}>
            <div className="flex items-start gap-2">
              <Icon name={result.ok ? 'check' : 'alert'} size={15} className="mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-fg">{result.message}</p>
                {result.type === 'blocked_carton' && (
                  <p className="text-[11px] text-fg-muted mt-1">Failed quality inspection — this carton cannot enter approved finished-goods inventory until it is reworked or overridden in Quality.
                    <button type="button" className="link-btn ml-2" onClick={() => navigate(linkTo('quality', 'carton', result.carton.id))}>OPEN IN QUALITY</button></p>
                )}
                {result.type === 'carton' && result.carton && (
                  <p className="text-[11px] text-fg-muted mt-1">{result.carton.sku} · {result.carton.customer} · {result.carton.detected}/{result.carton.required} pillows · line {result.carton.line} · approved {dayTime(result.carton.approved_at)}</p>
                )}
              </div>
              {(result.pallet && ['Pending Put-away', 'Open'].includes(result.pallet.status)) && (
                <Button size="sm" onClick={() => onPutaway(result.pallet)}>Put away {result.pallet.code}</Button>
              )}
              {result.pallet && result.pallet.status === 'Stored' && result.pallet.location_id && (
                <Button size="sm" variant="secondary" onClick={() => onLocation(result.pallet.location_id)}>Stored at {result.pallet.location}</Button>
              )}
              {result.type === 'location' && <Button size="sm" variant="secondary" onClick={() => onLocation(result.location.id)}>View location</Button>}
            </div>
          </div>
        )}
      </div>
      {disc && (disc.override_count_cartons > 0 || disc.unlocated_pallets_over_2h > 0) && (
        <p className="tile tone-warning text-xs">Inventory discrepancies: {disc.override_count_cartons} stored cartons with supervisor-overridden counts · {disc.unlocated_pallets_over_2h} pallets unlocated for more than 2 hours.</p>
      )}
      <ErrorBox error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div>
            <p className="label-caps mb-2">Pending put-away ({data.pending.length}) — approved cartons on closed pallets</p>
            <div className="tile p-0">
              <DataTable rows={data.pending} emptyText="No pallets waiting — the put-away crew is keeping up." onRowClick={onPutaway}
                columns={[
                  { key: 'code', header: 'Pallet', strong: true },
                  ...(filters.factory === 'ALL' ? [{ key: 'factory', header: 'Factory' }] : []),
                  { key: 'sku', header: 'SKU', render: (r) => <span><b className="text-fg">{r.sku}</b><span className="block text-[10px] text-fg-faint">{r.customer}</span></span> },
                  { key: 'cartons', header: 'Cartons', render: (r) => `${r.cartons} (${num(r.pillows)} pillows)` },
                  { key: 'batch_no', header: 'Batch', mono: true },
                  { key: 'waiting_min', header: 'Waiting', render: (r) => <span className={r.waiting_min > 60 ? 'text-warning font-semibold' : ''}>{r.waiting_min ?? '—'} min</span> },
                  { key: 'suggested_location', header: 'Suggested location', render: (r) => r.suggested_location?.code || '—' },
                  { key: 'a', header: '', render: (r) => <Button size="sm" onClick={(e) => { e.stopPropagation(); onPutaway(r); }}>Put away</Button> },
                ]} />
            </div>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div>
              <p className="label-caps mb-2">Pallets being built at packing ({data.building.length})</p>
              <div className="tile p-0">
                <DataTable rows={data.building} emptyText="No open pallets."
                  columns={[
                    { key: 'code', header: 'Pallet', strong: true },
                    { key: 'sku', header: 'SKU' },
                    { key: 'cartons', header: 'Fill', render: (r) => (
                      <div className="min-w-[90px]"><span className="text-[11px]">{r.cartons}/{r.capacity}</span><ProgressBar value={r.cartons} max={r.capacity} /></div>) },
                    { key: 'a', header: '', render: (r) => <Button size="sm" variant="secondary" onClick={() => onPutaway(r)}>Close & store</Button> },
                  ]} />
              </div>
            </div>
            <div>
              <p className="label-caps mb-2">Recently approved cartons (eligible for finished goods)</p>
              <div className="tile p-0">
                <DataTable rows={data.recent_cartons.slice(0, 8)} emptyText="No approved cartons yet."
                  columns={[
                    { key: 'carton_code', header: 'Carton ID', mono: true },
                    { key: 'sku', header: 'SKU' },
                    { key: 'detected', header: 'Qty', render: (r) => `${r.detected}/${r.required}` },
                    { key: 'approved_at', header: 'Approved', render: (r) => dayTime(r.approved_at) },
                    { key: 'scan', header: '', render: (r) => <button type="button" className="link-btn" onClick={() => scan(r.carton_code)}>SCAN</button> },
                  ]} />
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function AllocationTab({ onOrder }) {
  const { filters, act, can } = useApp();
  const navigate = useNavigate();
  const [days, setDays] = useState(7);
  const { data, loading, error } = useApi('/api/warehouse/allocation', { factory: filters.factory, days }, { every: 3 });
  const allocate = (r) => act(() => post('/api/warehouse/allocate', { order_id: r.order_id }), (x) => x.message).catch(() => {});
  if (loading && !data) return <div className="p-4"><Loading rows={5} /></div>;
  const t = data?.totals;
  return (
    <div className="p-4 space-y-4">
      <ErrorBox error={error} />
      {data && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
            {[['Total stock', num(t.total)], ['Reserved', num(t.reserved)], ['Unallocated', num(t.unallocated)],
              ['Pending requirements', num(t.pending_requirements)], ['Uncovered by stock', num(t.shortage)]].map(([l, v], i) => (
                <div key={l} className={`tile ${i === 4 && t.shortage ? 'tone-warning' : ''}`}><p className="label-caps">{l}</p><p className="text-lg font-bold text-fg">{v}</p><p className="text-[10px] text-fg-faint">pillows</p></div>
            ))}
          </div>
          <div className="flex items-center justify-between flex-wrap gap-2">
            <p className="text-xs text-fg-muted">Orders scheduled to ship — reserve finished goods (FIFO, own production order first). Shortages link to the production order that will cover them.</p>
            <FilterChips label="Ship within" options={[{ value: 2, label: '2 days' }, { value: 7, label: '7 days' }, { value: 14, label: '14 days' }]} value={days} onChange={setDays} />
          </div>
          <div className="tile p-0">
            <DataTable rows={data.orders} rowKey="order_id" onRowClick={(r) => onOrder(r.order_id)} emptyText="No orders in this window."
              columns={[
                { key: 'order', header: 'Order', strong: true, render: (r) => <span>{r.order}<span className="block text-[10px] text-fg-faint font-normal">{r.customer}</span></span> },
                ...(filters.factory === 'ALL' ? [{ key: 'factory', header: 'Factory' }] : []),
                { key: 'sku', header: 'SKU', mono: true },
                { key: 'required', header: 'Required', render: (r) => num(r.required) },
                { key: 'allocated', header: 'Allocated', render: (r) => (
                  <div className="min-w-[90px]"><span className="text-[11px]">{num(r.allocated)}</span><ProgressBar value={r.allocated} max={r.required} tone={r.allocated >= r.required ? 'success' : 'warning'} /></div>) },
                { key: 'picked', header: 'Picked', render: (r) => num(r.picked) },
                { key: 'sku_unallocated', header: 'SKU unallocated', render: (r) => num(r.sku_unallocated) },
                { key: 'shortage', header: 'Shortage', render: (r) => (r.shortage ? (
                  <span><b className="text-danger">{num(r.shortage)}</b>{r.linked_po && (
                    <button type="button" className="block text-[10px] text-primary underline" onClick={(e) => { e.stopPropagation(); navigate(linkTo('production', 'production_order', r.linked_po.id)); }}>
                      {r.linked_po.number} · est. {dayTime(r.linked_po.estimated_completion)}
                    </button>)}</span>) : <span className="text-success">Covered</span>) },
                { key: 'scheduled_dispatch', header: 'Ships', render: (r) => dayTime(r.scheduled_dispatch) },
                { key: 'readiness', header: 'Readiness', render: (r) => <StatusBadge status={r.readiness.status} /> },
                { key: 'a', header: '', render: (r) => (
                  <Button size="sm" variant={r.pending_requirement && r.sku_unallocated ? 'primary' : 'secondary'}
                    disabled={!can('warehouse.write') || !r.pending_requirement || !r.sku_unallocated}
                    onClick={(e) => { e.stopPropagation(); allocate(r); }}>Allocate</Button>) },
              ]} />
          </div>
          <div>
            <p className="label-caps mb-2">Finished goods by SKU</p>
            <div className="tile p-0">
              <DataTable rows={data.stock_by_sku} rowKey="sku" emptyText="No stock."
                columns={[
                  { key: 'sku', header: 'SKU', strong: true }, { key: 'customer', header: 'Customer' },
                  ...(filters.factory === 'ALL' ? [{ key: 'factory', header: 'Factory' }] : []),
                  { key: 'stored', header: 'Stored', render: (r) => num(r.stored) }, { key: 'reserved', header: 'Reserved', render: (r) => num(r.reserved) },
                  { key: 'available', header: 'Unallocated', render: (r) => <b className="text-fg">{num(r.available)}</b> },
                  { key: 'pallets', header: 'Pallets' },
                  { key: 'locations', header: 'Locations', render: (r) => <span className="text-[10.5px]">{r.locations.join(', ')}{r.pallets > r.locations.length ? ' …' : ''}</span> },
                ]} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function PickingTab({ onOrder }) {
  const { filters, act, can } = useApp();
  const { data, loading, error } = useApi('/api/warehouse/picking', { factory: filters.factory }, { every: 2 });
  const [onlyDue, setOnlyDue] = useState('all');
  const rows = (data || []).filter((r) => onlyDue === 'all' || r.expedite || r.truck_arrived);
  const confirm = (r) => act(() => post(`/api/warehouse/picking/${r.id}/confirm`), `Picked ${r.cartons} cartons for ${r.order} from ${r.location}`).catch(() => {});
  const expedite = (r) => act(() => post('/api/warehouse/picking/expedite', { order_id: r.order_id }), `Picking expedited for ${r.order}`).catch(() => {});
  return (
    <div className="p-4 space-y-3">
      <ErrorBox error={error} />
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-xs text-fg-muted">{(data || []).length} pick tasks · {num((data || []).reduce((s, r) => s + r.cartons, 0))} cartons reserved and waiting to be picked</p>
        <FilterChips options={[{ value: 'all', label: 'All tasks' }, { value: 'urgent', label: 'Expedited / truck waiting' }]} value={onlyDue} onChange={setOnlyDue} />
      </div>
      {loading && !data ? <Loading /> : (
        <div className="tile p-0">
          <DataTable rows={rows} onRowClick={(r) => onOrder(r.order_id)} emptyText="Nothing to pick."
            columns={[
              { key: 'order', header: 'Order', strong: true, render: (r) => (
                <span>{r.order}{r.expedite && <Badge tone="danger" className="ml-1.5">EXPEDITE</Badge>}<span className="block text-[10px] text-fg-faint font-normal">{r.customer}</span></span>) },
              { key: 'sku', header: 'SKU', mono: true },
              { key: 'cartons', header: 'Quantity', render: (r) => `${num(r.cartons)} ctn · ${num(r.pillows)} pillows` },
              { key: 'location', header: 'Location', render: (r) => <span><b className="text-fg">{r.location}</b><span className="block text-[10px] text-fg-faint">{r.pallet}</span></span> },
              { key: 'required_dispatch', header: 'Required dispatch', render: (r) => dayTime(r.required_dispatch) },
              { key: 'truck', header: 'Truck', render: (r) => (r.truck ? <span>{r.truck} · {r.dock}{r.truck_arrived && <Badge tone="warning" className="ml-1.5">AT DOCK</Badge>}</span> : '—') },
              { key: 'a', header: '', render: (r) => (
                <span className="flex gap-1.5 justify-end" onClick={(e) => e.stopPropagation()}>
                  {!r.expedite && <Button size="sm" variant="ghost" disabled={!can('warehouse.write')} onClick={() => expedite(r)}>Expedite</Button>}
                  <Button size="sm" disabled={!can('warehouse.write')} onClick={() => confirm(r)}>Confirm pick</Button>
                </span>) },
            ]} />
        </div>
      )}
    </div>
  );
}

function WorkloadTab({ initialSku }) {
  const { filters } = useApp();
  const { data, loading, error } = useApi('/api/warehouse/workload', { factory: filters.factory }, { every: 3 });
  const [sku, setSku] = useState(initialSku || '');
  const [query, setQuery] = useState(initialSku || '');
  const stock = useApi(query ? '/api/warehouse/stock' : null, { factory: filters.factory, sku: query }, { every: 4 });
  useEffect(() => { if (initialSku) { setSku(initialSku); setQuery(initialSku); } }, [initialSku]);

  if (loading && !data) return <div className="p-4"><Loading rows={5} /></div>;
  const w = data;
  const drivers = w ? [
    ['Inbound shipments', w.inbound_shipments, 'shipments × 1.5 h unload & check', w.inbound_shipments * 1.5],
    ['Put-away', w.pallets_to_putaway, 'pallets (pending + next shift output) × 5 min', w.pallets_to_putaway * 0.08],
    ['Picking', w.cartons_to_pick, 'cartons ÷ 90 per hour', w.cartons_to_pick / 90],
    ['Outbound loads', w.outbound_loads, 'truck loads × 1.25 h', w.outbound_loads * 1.25],
  ] : [];
  const gap = w ? Math.round((w.required_fte - w.deployed) * 10) / 10 : 0;
  return (
    <div className="p-4 space-y-4">
      <ErrorBox error={error} />
      {w && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <div className="tile"><p className="label-caps">Required FTE</p><p className="text-xl font-bold text-fg">{num(w.required_fte, 1)}</p><p className="text-[10px] text-fg-faint">{num(w.workload_hours, 1)} workload hours ÷ 7.7 productive h</p></div>
            <div className="tile"><p className="label-caps">Deployed (this shift)</p><p className="text-xl font-bold text-fg">{w.deployed}</p><p className="text-[10px] text-fg-faint">associates & forklift operators present</p></div>
            <div className={`tile ${w.utilization_pct > 100 ? 'tone-danger' : w.utilization_pct < 60 ? 'tone-info' : 'tone-success'}`}>
              <p className="label-caps">Labour utilization</p><p className="text-xl font-bold text-fg">{pct(w.utilization_pct, 0)}</p>
              <p className="text-[10px] text-fg-faint">{gap > 0 ? `Short ${gap} FTE` : `${Math.abs(gap)} FTE available to redeploy`}</p></div>
            <div className="tile"><p className="label-caps">Today</p><p className="text-xs text-fg mt-1 leading-relaxed">
              {w.today.inbound} receipts · {w.today.putaway} put-aways<br />{w.today.picking} picks · {w.today.outbound} trucks dispatched</p></div>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="tile">
              <p className="label-caps mb-2">Workload drivers (next 10 hours)</p>
              <table className="table">
                <thead><tr><th>Activity</th><th>Volume</th><th>Basis</th><th>Hours</th></tr></thead>
                <tbody>{drivers.map(([a, v, basis, h]) => (
                  <tr key={a}><td className="cell-strong">{a}</td><td>{num(v)}</td><td className="text-[10.5px]">{basis}</td><td>{num(h, 1)}</td></tr>
                ))}</tbody>
              </table>
            </div>
            <ChartCard title="Warehouse activity today" subtitle="Events per hour from the operations log" type="bar" stacked legend height={210}
              data={{ labels: w.activity.map((a) => a.hour), datasets: [
                { label: 'Inbound', data: w.activity.map((a) => a.inbound), color: 'chart-6' },
                { label: 'Put-away', data: w.activity.map((a) => a.putaway), color: 'chart-3' },
                { label: 'Picking', data: w.activity.map((a) => a.picking), color: 'primary' },
                { label: 'Outbound', data: w.activity.map((a) => a.outbound), color: 'chart-4' },
              ] }} />
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div>
              <p className="label-caps mb-2">Warehouse crew on shift</p>
              <div className="tile p-0">
                <DataTable rows={w.crew} rowKey="code" emptyText="No crew on this shift."
                  columns={[
                    { key: 'code', header: 'ID', mono: true }, { key: 'name', header: 'Name', strong: true }, { key: 'role', header: 'Role' },
                    { key: 'attendance', header: 'Attendance', render: (r) => <StatusBadge status={r.attendance} /> },
                    { key: 'hourly_wage', header: 'Wage', render: (r) => `$${r.hourly_wage.toFixed(2)}/h` },
                  ]} />
              </div>
            </div>
            <div>
              <p className="label-caps mb-2">Where is SKU …? (stock locator)</p>
              <form className="flex gap-2 mb-2" onSubmit={(e) => { e.preventDefault(); setQuery(sku.trim().toUpperCase()); }}>
                <input className="input" placeholder="e.g. WMT-MS-Q or CST" value={sku} onChange={(e) => setSku(e.target.value)} />
                <Button type="submit" icon="search" disabled={!sku.trim()}>Find</Button>
              </form>
              {query && (
                <div className="tile p-0">
                  {stock.loading && !stock.data ? <div className="p-3"><Loading rows={2} /></div> : (
                    <DataTable rows={stock.data || []} rowKey="sku" emptyText={`No stored stock matches “${query}”.`}
                      columns={[
                        { key: 'sku', header: 'SKU', strong: true },
                        ...(filters.factory === 'ALL' ? [{ key: 'factory', header: 'Factory' }] : []),
                        { key: 'available', header: 'Unallocated', render: (r) => num(r.available) },
                        { key: 'reserved', header: 'Reserved', render: (r) => num(r.reserved) },
                        { key: 'locations', header: 'Locations', render: (r) => <span className="text-[10.5px]">{r.locations.join(', ')}{r.pallets > r.locations.length ? ` +${r.pallets - r.locations.length} more` : ''}</span> },
                      ]} />
                  )}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ─── Page ────────────────────────────────────────────────────────────── */
export default function Warehouse() {
  const { filters, meta, act } = useApp();
  const location = useLocation();
  const focus = useFocus();
  const [tab, setTab] = useState('layout');
  const [locId, setLocId] = useState(null);
  const [orderId, setOrderId] = useState(null);
  const [putaway, setPutaway] = useState(null);
  const [skuFocus, setSkuFocus] = useState('');
  const { data: s, error, reload } = useApi('/api/warehouse/summary', { factory: filters.factory }, { every: 2 });

  const fType = focus?.type;
  const fId = focus?.id;
  useEffect(() => {
    if (!fType) return;
    if (fType === 'location') { setTab('layout'); if (fId) setLocId(fId); }
    else if (fType === 'order') { setTab('allocation'); if (fId) setOrderId(fId); }
    else if (fType === 'sku') { setTab('workload'); setSkuFocus(meta.skus.find((x) => x.id === fId)?.code || String(fId || '')); }
    else if (fType === 'workforce') setTab('workload');
    else if (fType === 'warehouse') setTab('layout');
    else if (fType === 'pallet' && fId) {
      setTab('receiving');
      api('/api/warehouse/receiving', { params: { factory: filters.factory } }).then((r) => {
        const p = [...r.pending, ...r.building].find((x) => x.id === fId);
        if (p) setPutaway(p);
      }).catch(() => {});
    }
  }, [fType, fId, location.key]); // eslint-disable-line react-hooks/exhaustive-deps

  const onInsight = (action) => {
    if (action.type === 'expedite_picking') {
      act(() => post('/api/warehouse/picking/expedite', { order_id: action.payload.order_id }), 'Picking expedited').catch(() => {});
    }
  };

  return (
    <div className="space-y-4">
      <PageTitle title="Warehouse & Inventory" subtitle="Approved cartons → put-away → reserved for orders → picked for dispatch" />
      <StageAlerts stage="warehouse" />
      <ErrorBox error={error} onRetry={reload} />
      {!s && !error && <Loading rows={2} height={60} />}
      {s && (
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3">
          <Kpi label="Total finished goods" icon="box" value={compact(s.total_pillows)} unit="pillows" sub={`${num(s.total_cartons)} cartons · ${s.stored_pallets} pallets`}
            onClick={() => setTab('allocation')} active={tab === 'allocation'} />
          <Kpi label="Available stock" icon="check" value={compact(s.available_pillows)} unit="pillows" tone="success" sub="Not reserved for any order"
            onClick={() => setTab('allocation')} />
          <Kpi label="Reserved stock" icon="clipboard" value={compact(s.reserved_pillows)} unit="pillows" sub="Allocated to customer orders"
            onClick={() => setTab('allocation')} />
          <Kpi label="Pending put-away" icon="hourglass" value={num(s.pending_putaway_pallets)} unit="pallets"
            tone={s.pending_putaway_pallets >= 6 ? 'warning' : undefined} sub={`${num(s.pending_putaway_pillows)} pillows in receiving lane`}
            onClick={() => setTab('receiving')} active={tab === 'receiving'} />
          <Kpi label="Pending picking" icon="list" value={compact(s.pending_pick_pillows)} unit="pillows" sub={`${s.pending_pick_orders} orders with open pick tasks`}
            onClick={() => setTab('picking')} active={tab === 'picking'} />
          <Kpi label="Capacity utilization" icon="grid" value={pct(s.utilization_pct, 0)}
            tone={s.utilization_pct >= 90 ? 'danger' : s.utilization_pct >= 75 ? 'warning' : undefined}
            sub={`${s.stored_pallets} of ${s.capacity_pallets} pallet positions`}
            footer={<ProgressBar value={s.utilization_pct} tone={s.utilization_pct >= 90 ? 'danger' : s.utilization_pct >= 75 ? 'warning' : 'primary'} />}
            onClick={() => setTab('layout')} active={tab === 'layout'} />
          <Kpi label="Inventory discrepancies" icon="alert" value={num(s.discrepancies)} tone={s.discrepancies ? 'warning' : 'success'}
            sub={`${s.discrepancy_detail.override_count_cartons} override counts · ${s.discrepancy_detail.unlocated_pallets_over_2h} unlocated > 2 h`}
            onClick={() => setTab('receiving')} />
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-4 gap-4">
        <Card className="xl:col-span-3" padded={false}>
          <div className="px-4 pt-1"><Tabs tabs={TABS} value={tab} onChange={setTab} variant="underline" /></div>
          {tab === 'layout' && <LayoutTab onPick={setLocId} selected={locId} />}
          {tab === 'receiving' && <ReceivingTab onPutaway={setPutaway} onLocation={(id) => { setLocId(id); }} />}
          {tab === 'allocation' && <AllocationTab onOrder={setOrderId} />}
          {tab === 'picking' && <PickingTab onOrder={setOrderId} />}
          {tab === 'workload' && <WorkloadTab initialSku={skuFocus} />}
        </Card>
        <div className="space-y-4">
          <InsightsCard page="warehouse" limit={6} onAction={onInsight} />
          {s && (
            <Card title="Stock position" subtitle="Pillows in finished goods">
              <div className="space-y-2 text-xs">
                {[['Available', s.available_pillows, 'success'], ['Reserved', s.reserved_pillows, 'warning'], ['Pending put-away', s.pending_putaway_pillows, 'info']].map(([l, v, tone]) => (
                  <div key={l}>
                    <div className="flex justify-between mb-0.5"><span className="text-fg-muted">{l}</span><b className="text-fg">{num(v)}</b></div>
                    <ProgressBar value={v} max={Math.max(1, s.total_pillows + s.pending_putaway_pillows)} tone={tone} />
                  </div>
                ))}
              </div>
              <p className="text-[10.5px] text-fg-faint mt-3">Failed-quality cartons are excluded until reworked or overridden in Quality &amp; Packaging.</p>
            </Card>
          )}
        </div>
      </div>

      {locId && <LocationDrawer id={locId} onClose={() => setLocId(null)} />}
      {orderId && <OrderDrawer id={orderId} onClose={() => setOrderId(null)} />}
      {putaway && <PutawayModal pallet={putaway} onClose={() => setPutaway(null)} />}
    </div>
  );
}

