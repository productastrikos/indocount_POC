import React, { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Badge, Button, Card, DataTable, Drawer, Icon, Modal, ProgressBar, Tabs } from '../components/ui';
import { MiniChart } from '../components/charts/ChartCard';
import { axisOptions } from '../components/charts/chartTheme';
import {
  Empty, ErrorBox, InsightsCard, Kpi, KV, Loading, PageTitle, SimTag, StageAlerts, StatusBadge,
} from '../components/common';
import { useApi } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { buildUrl, post, put } from '../api/client';
import { dateShort, dateTime, dayTime, hhmm, num, pct, statusTone } from '../utils';

const FLOW_TONE = { Normal: 'success', Attention: 'warning', Blocked: 'danger' };
const RESULT_LABEL = { Correct: 'PASS', Underpacked: 'UNDERPACKED', Overpacked: 'OVERPACKED', Defect: 'DEFECT' };
const TABS = [
  { value: 'stations', label: 'Packing stations' },
  { value: 'blocked', label: 'Blocked cartons & defects' },
  { value: 'cameras', label: 'Cameras' },
  { value: 'specs', label: 'Specifications' },
  { value: 'analytics', label: 'Analytics' },
];

function parseFocus(search) {
  const f = new URLSearchParams(search).get('focus');
  if (!f) return null;
  const [type, ...rest] = f.split(':');
  const raw = rest.join(':');
  return { key: f, type, id: raw !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw };
}

function resultTone(c) {
  if (c.status === 'Blocked' || c.status === 'Rejected') return 'danger';
  if (c.status === 'Overridden') return 'warning';
  if (c.status === 'Reworked') return 'info';
  return 'success';
}

/* ─── Workflow strip ─────────────────────────────────────────────────── */
function Workflow({ stages }) {
  return (
    <Card title="Packaging workflow" subtitle="Pillows and cartons processed today at each step">
      <div className="flex flex-wrap items-stretch gap-1.5">
        {stages.map((s, i) => {
          const tone = FLOW_TONE[s.status] || 'neutral';
          return (
            <React.Fragment key={s.stage}>
              <div className={`tile tone-${tone} px-2.5 py-2`} style={{ minWidth: 120, flex: '1 1 120px' }}>
                <span className="block text-[11px] font-bold text-fg leading-tight">{s.stage}</span>
                <span className="block text-lg font-bold text-fg leading-tight mt-0.5 tabular-nums">{num(s.qty)}</span>
                <span className="flex items-center justify-between gap-1 text-[10px] mt-0.5">
                  <span className="text-fg-faint">{s.unit}</span>
                  <span className="font-bold" style={{ color: `var(--color-${tone})` }}>{s.status.toUpperCase()}</span>
                </span>
              </div>
              {i < stages.length - 1 && <span className="self-center text-fg-faint hidden md:inline"><Icon name="chevronRight" size={12} /></span>}
            </React.Fragment>
          );
        })}
      </div>
    </Card>
  );
}

/* ─── Carton actions (rework / override / reject) ───────────────────── */
function useCartonActions(onDone) {
  const { act, can } = useApp();
  const [override, setOverride] = useState(null);
  const [reject, setReject] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const run = async (fn, msg) => {
    setBusy(true);
    try {
      const res = await act(fn, msg);
      onDone?.(res);
      return res;
    } catch (e) { return null; } finally { setBusy(false); }
  };

  const rework = (c) => run(() => post(`/api/quality/cartons/${c.id}/rework`),
    (r) => `${c.hold_code || c.code} reworked and recounted → ${r.carton_code}`);

  const modals = (
    <>
      {override && (
        <Modal title={`Supervisor override · ${override.hold_code || override.code}`} eyebrow="Final approval"
          subtitle={`${override.sku} · detected ${override.detected} of ${override.required} required`}
          onClose={() => { setOverride(null); setReason(''); }}
          footer={<>
            <Button variant="ghost" onClick={() => { setOverride(null); setReason(''); }}>Cancel</Button>
            <Button variant="danger" disabled={busy || reason.trim().length < 5} onClick={async () => {
              const r = await run(() => post(`/api/quality/cartons/${override.id}/override`, { reason: reason.trim() }),
                (x) => `Override recorded; carton approved as ${x.carton_code}`);
              if (r) { setOverride(null); setReason(''); }
            }}>Approve with override</Button>
          </>}>
          <p className="text-xs text-fg-muted mb-3">The carton will be approved with its detected count ({override.detected} pillows) and enter finished goods. The reason and your user ID are recorded on the carton and defect record.</p>
          <label className="block">
            <span className="label-caps field-label">Reason (required)</span>
            <textarea className="textarea" rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Customer QA accepted short-pack for this lot (ref. email 11/24)" />
          </label>
        </Modal>
      )}
      {reject && (
        <Modal title={`Reject ${reject.hold_code || reject.code}?`} eyebrow="Quality decision" onClose={() => setReject(null)}
          footer={<>
            <Button variant="ghost" onClick={() => setReject(null)}>Cancel</Button>
            <Button variant="danger" disabled={busy} onClick={async () => {
              const r = await run(() => post(`/api/quality/cartons/${reject.id}/reject`), `${reject.hold_code} rejected; pillows re-queued for production`);
              if (r) setReject(null);
            }}>Reject carton</Button>
          </>}>
          <p className="text-xs text-fg-muted">The {reject.detected} pillows are scrapped and the quantity is added back to the production order, so its completion estimate and the customer order's shipment readiness are recalculated.</p>
        </Modal>
      )}
    </>
  );

  const buttons = (c, size = 'sm') => (
    <div className="flex gap-1.5 flex-wrap" onClick={(e) => e.stopPropagation()}>
      <Button size={size} variant="soft" icon="refresh" disabled={busy || !can('quality.write')}
        title={can('quality.write') ? 'Recount and correct the carton' : 'Requires quality permissions'} onClick={() => rework(c)}>Rework</Button>
      <Button size={size} variant="secondary" icon="shield" disabled={busy || !can('quality.override')}
        title={can('quality.override') ? 'Approve with a recorded reason' : 'Only an authorised supervisor can override'}
        onClick={() => setOverride(c)}>Override</Button>
      <Button size={size} variant="ghost" disabled={busy || !can('quality.write')} onClick={() => setReject(c)}>Reject</Button>
    </div>
  );
  return { buttons, modals };
}

/* ─── Carton drawer ──────────────────────────────────────────────────── */
function CartonDrawer({ id, onClose }) {
  const { data, loading, reload } = useApi(id ? `/api/quality/cartons/${id}` : null, {}, { every: 3 });
  const { buttons, modals } = useCartonActions(() => reload());
  const c = data?.carton;
  return (
    <Drawer eyebrow="Carton" title={c ? (c.code || `#${c.id}`) : 'Carton'} width={520} onClose={onClose}
      headerExtra={c && <Badge tone={resultTone(c)} dot>{c.status.toUpperCase()}</Badge>}>
      {loading && !data && <Loading />}
      {c && (
        <div className="space-y-4">
          {c.status === 'Blocked' && (
            <div className="tile tone-danger">
              <p className="text-sm font-bold text-fg flex items-center gap-2"><Icon name="alert" size={15} />Blocked from final approval</p>
              <p className="text-xs text-fg-muted mt-1">Detected {c.detected} pillows; specification requires {c.required}. The carton cannot enter finished goods until it is reworked or a supervisor overrides with a reason.</p>
              <div className="mt-2">{buttons(c)}</div>
            </div>
          )}
          <KV cols={3} items={[
            ['Result', RESULT_LABEL[c.result] || c.result], ['Detected / required', `${c.detected} / ${c.required}`], ['Defect', c.defect || '—'],
            ['SKU', c.sku], ['Customer', c.customer], ['Line', c.line],
            ['Carton type', c.carton_type], ['Packaging', c.packaging_type], ['Inference', `${c.source} · ${c.confidence != null ? c.confidence.toFixed(2) : '—'}`],
            ['Packed', dateTime(c.created_at)], ['Approved', c.approved_at ? dateTime(c.approved_at) : '—'], ['Approved by', c.approved_by || '—'],
            ['Carton ID', c.carton_code || 'Not issued (on hold)'], ['Hold code', c.hold_code || '—'],
            ['Pallet', data.pallet ? `${data.pallet.code} · ${data.pallet.status}${data.pallet.location ? ` @ ${data.pallet.location}` : ''}` : '—'],
          ]} />
          {c.override_reason && <div className="tile tone-warning text-xs"><b>Override reason:</b> {c.override_reason}</div>}
          <div>
            <p className="label-caps mb-2">Defect records</p>
            {data.defects.length === 0 ? <p className="text-xs text-fg-faint">None — carton passed first-time.</p> : (
              <div className="space-y-1.5">
                {data.defects.map((d, i) => (
                  <div key={i} className="tile text-xs flex items-center gap-2 flex-wrap">
                    <StatusBadge status={d.status} />
                    <b className="text-fg">{d.category}</b>
                    <span className="text-fg-muted">qty {d.qty} · {d.disposition}</span>
                    <span className="text-fg-faint ml-auto">{dayTime(d.created_at)}{d.resolved_by ? ` · by ${d.resolved_by}` : ''}</span>
                    {d.notes && <span className="w-full text-fg-muted">{d.notes}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {modals}
    </Drawer>
  );
}

/* ─── Packing stations ───────────────────────────────────────────────── */
function StationCard({ s, showFactory, highlight, onOpenCarton }) {
  const { act, can } = useApp();
  const ppc = s.spec?.pillows_per_carton || 0;
  const [detected, setDetected] = useState(ppc);
  const [last, setLast] = useState(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  useEffect(() => { setDetected(ppc); }, [ppc, s.po_id]);
  useEffect(() => { if (highlight) ref.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, [highlight]);

  const allowed = can('quality.write') || can('simulation');
  const d = Number(detected);
  const reason = !s.po_id ? 'No active production order on this line'
    : !allowed ? 'Requires quality permissions'
    : Number.isNaN(d) || d < 0 ? 'Enter a count'
    : s.queue < d ? `Only ${s.queue} pillows waiting at the station` : null;

  const pack = async () => {
    setBusy(true);
    try {
      const res = await act(() => post(`/api/quality/stations/${s.line_id}/pack`, { detected: d }),
        (r) => (r.status === 'Blocked'
          ? `${r.hold_code} BLOCKED — ${r.result}: ${r.detected}/${r.required} pillows`
          : `${r.carton_code} passed: ${r.detected}/${r.required} pillows`));
      setLast(res);
    } catch (e) { /* toasted */ } finally { setBusy(false); }
  };

  const alarm = s.blocked > 0 || last?.status === 'Blocked';
  const progress = ppc ? Math.min(100, (s.current_count / ppc) * 100) : 0;
  return (
    <div ref={ref} className="card flex flex-col"
      style={{
        borderColor: alarm ? 'var(--color-danger)' : highlight ? 'var(--color-primary)' : undefined,
        boxShadow: alarm ? '0 0 0 2px var(--color-danger-soft)' : highlight ? '0 0 0 2px var(--color-primary-soft)' : undefined,
      }}>
      <div className="card-header">
        <div className="flex-1 min-w-0">
          <p className="card-title truncate">{s.line}{showFactory ? ` · ${s.factory}` : ''}</p>
          <p className="card-subtitle truncate">{s.po ? `${s.po} · ${s.sku} · ${s.customer}` : 'No active production order'}</p>
        </div>
        <StatusBadge status={s.line_status} />
      </div>
      {alarm && (
        <div className="tone-danger px-4 py-2 text-xs font-bold flex items-center gap-2" style={{ borderBottom: '1px solid var(--color-danger-border)' }}>
          <Icon name="alert" size={14} />
          {last?.status === 'Blocked'
            ? `${last.hold_code}: ${last.result} — ${last.detected}/${last.required} pillows. Blocked from final approval.`
            : `${s.blocked} carton${s.blocked > 1 ? 's' : ''} blocked — resolve before final approval`}
        </div>
      )}
      <div className="card-body space-y-3 flex-1">
        {s.spec ? (
          <div className="grid grid-cols-2 gap-2">
            <div className="tile">
              <p className="label-caps">Spec · pillows per carton</p>
              <p className="text-2xl font-bold text-fg leading-tight">{ppc}</p>
              <p className="text-[10px] text-fg-faint truncate" title={s.spec.carton_type}>{s.spec.carton_type}</p>
            </div>
            <div className="tile">
              <p className="label-caps">Live count · next carton</p>
              <p className="text-2xl font-bold leading-tight tabular-nums" style={{ color: 'var(--color-primary)' }}>
                {s.current_count}<span className="text-sm text-fg-faint"> / {ppc}</span>
              </p>
              <ProgressBar value={progress} max={100} tone="primary" />
              <p className="text-[10px] text-fg-faint mt-1">{num(s.queue)} pillows waiting at station</p>
            </div>
            <p className="col-span-2 text-[10.5px] text-fg-muted leading-snug"><b className="text-fg">{s.spec.packaging_type}</b> · {s.spec.label_requirements}</p>
          </div>
        ) : <Empty icon="box">Line has no SKU loaded.</Empty>}
        <div className="flex items-center gap-3 text-[11px]">
          <span className="text-fg-muted">Today <b className="text-fg">{num(s.cartons_today)}</b> cartons</span>
          <span className="text-fg-muted">Accuracy <b style={{ color: `var(--color-${s.accuracy_pct >= 99.5 ? 'success' : s.accuracy_pct >= 98.5 ? 'warning' : 'danger'})` }}>{pct(s.accuracy_pct, 1)}</b></span>
          {s.blocked > 0 && <Badge tone="danger">{s.blocked} blocked</Badge>}
        </div>
        <div>
          <p className="label-caps mb-1.5">Recent cartons</p>
          {s.recent.length === 0 ? <p className="text-[11px] text-fg-faint">No cartons packed yet.</p> : (
            <div className="space-y-1">
              {s.recent.map((c) => (
                <button key={c.id} type="button" onClick={() => onOpenCarton(c.id)}
                  className="w-full flex items-center gap-2 text-[11px] px-2 py-1 rounded hover:bg-surface-raised text-left">
                  <span className="font-mono text-fg-faint truncate flex-1">{c.code}</span>
                  <span className="tabular-nums text-fg">{c.detected}/{c.required}</span>
                  <Badge tone={c.result === 'Correct' ? 'success' : 'danger'}>{RESULT_LABEL[c.result] || c.result}</Badge>
                  <span className="text-fg-faint w-10 text-right">{hhmm(c.created_at)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="panel-footer justify-between flex-wrap" style={{ padding: '10px 16px' }}>
        <span className="text-[10.5px] text-fg-faint flex items-center gap-1"><SimTag /> carton count</span>
        <div className="flex items-center gap-1.5">
          <input type="number" min={0} max={ppc * 2 || 1} className="input select-sm" style={{ width: 64 }} aria-label="Detected pillows"
            value={detected} onChange={(e) => setDetected(e.target.value)} disabled={!s.po_id || !allowed} />
          <Button size="sm" icon="box" disabled={busy || !!reason} title={reason || 'Pack one carton with this detected count'} onClick={pack}>
            {busy ? 'Packing…' : 'Simulate carton'}
          </Button>
        </div>
      </div>
    </div>
  );
}

function StationsTab({ factory, highlightLine, onOpenCarton }) {
  const { data, loading, error, reload } = useApi('/api/quality/stations', { factory });
  return (
    <div className="space-y-3">
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={4} height={80} />}
      {data && data.length === 0 && <Empty>No packing stations in scope.</Empty>}
      <div className="grid grid-cols-1 md:grid-cols-2 2xl:grid-cols-4 gap-3">
        {data?.map((s) => (
          <StationCard key={s.line_id} s={s} showFactory={factory === 'ALL'} highlight={highlightLine === s.line_id} onOpenCarton={onOpenCarton} />
        ))}
      </div>
    </div>
  );
}

/* ─── Blocked cartons & defect history ───────────────────────────────── */
function BlockedTab({ factory, category, setCategory, onOpenCarton }) {
  const blocked = useApi('/api/quality/cartons', { factory, status: 'Blocked', limit: 150 });
  const defects = useApi('/api/quality/defects', { factory, days: 2 }, { every: 2 });
  const { buttons, modals } = useCartonActions(() => { blocked.reload(); defects.reload(); });
  const [status, setStatus] = useState('');
  const cats = [...new Set((defects.data || []).map((d) => d.category))];
  const rows = (defects.data || []).filter((d) => (!category || d.category === category) && (!status || d.status === status));

  return (
    <div className="space-y-4">
      <Card title={<span className="flex items-center gap-2"><Icon name="alert" size={14} className="text-danger" />Blocked carton queue</span>}
        subtitle="Cartons that failed count validation or inspection. Final approval requires rework or a supervisor override with a reason."
        padded={false}>
        <ErrorBox error={blocked.error} onRetry={blocked.reload} />
        {blocked.loading && !blocked.data && <div className="p-4"><Loading /></div>}
        {blocked.data && (
          <DataTable
            rows={blocked.data}
            onRowClick={(c) => onOpenCarton(c.id)}
            emptyText="No blocked cartons — every carton on hold has been resolved."
            columns={[
              { key: 'code', header: 'Hold code', mono: true, render: (c) => c.hold_code || c.code },
              { key: 'line', header: 'Line', strong: true, render: (c) => (factory === 'ALL' ? `${c.line} · ${c.factory}` : c.line) },
              { key: 'sku', header: 'SKU' },
              { key: 'customer', header: 'Customer' },
              { key: 'count', header: 'Detected / spec', render: (c) => <b className="text-danger tabular-nums">{c.detected} / {c.required}</b> },
              { key: 'result', header: 'Result', render: (c) => <Badge tone="danger">{c.defect || c.result}</Badge> },
              { key: 'created_at', header: 'Packed', render: (c) => dayTime(c.created_at) },
              { key: 'actions', header: 'Resolve', render: (c) => buttons(c) },
            ]}
          />
        )}
      </Card>
      <Card title="Defect history" subtitle="Every failed inspection: time, SKU, customer, line and defect category (last 48 h)" padded={false}
        actions={
          <div className="flex items-center gap-1.5 flex-wrap">
            <select className="select select-sm" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Defect category">
              <option value="">All categories</option>
              {cats.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select className="select select-sm" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Defect status">
              <option value="">Open & resolved</option>
              <option value="Open">Open</option>
              <option value="Resolved">Resolved</option>
            </select>
          </div>
        }>
        {defects.loading && !defects.data && <div className="p-4"><Loading /></div>}
        {defects.data && (
          <div className="max-h-[460px] overflow-y-auto">
            <DataTable
              rows={rows}
              onRowClick={(d) => d.carton_id && onOpenCarton(d.carton_id)}
              emptyText="No defects recorded for this filter."
              columns={[
                { key: 'time', header: 'Time', render: (d) => dayTime(d.time) },
                { key: 'carton', header: 'Carton', mono: true },
                { key: 'sku', header: 'SKU', strong: true },
                { key: 'customer', header: 'Customer' },
                { key: 'line', header: 'Line', render: (d) => (factory === 'ALL' ? `${d.line} · ${d.factory}` : d.line) },
                { key: 'category', header: 'Category', render: (d) => <Badge tone={['Underpacked', 'Overpacked'].includes(d.category) ? 'danger' : 'warning'}>{d.category}</Badge> },
                { key: 'qty', header: 'Qty' },
                { key: 'disposition', header: 'Disposition' },
                { key: 'status', header: 'Status', render: (d) => <StatusBadge status={d.status} /> },
                { key: 'resolved_by', header: 'Resolved by', render: (d) => d.resolved_by || '—' },
              ]}
            />
          </div>
        )}
      </Card>
      {modals}
    </div>
  );
}

/* ─── Cameras ────────────────────────────────────────────────────────── */
function CameraFrame({ camId }) {
  const [src, setSrc] = useState(null);
  const [info, setInfo] = useState({ provider: null, count: null, empty: false });
  const current = useRef(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(buildUrl(`/api/quality/cameras/${camId}/frame`, { t: Date.now() }));
        if (!alive) return;
        const provider = res.headers.get('X-CV-Provider') || 'simulated';
        if (res.status === 204 || !res.ok) { setInfo({ provider, count: null, empty: true }); return; }
        const blob = await res.blob();
        if (!alive) return;
        if (!blob.size) { setInfo({ provider, count: null, empty: true }); return; }
        const url = URL.createObjectURL(blob);
        if (current.current) URL.revokeObjectURL(current.current);
        current.current = url;
        setSrc(url);
        setInfo({ provider, count: res.headers.get('X-Detected-Count'), empty: false });
      } catch (e) {
        if (alive) setInfo((i) => ({ ...i, empty: true }));
      }
    };
    setSrc(null);
    load();
    const t = setInterval(load, 1000);
    return () => {
      alive = false;
      clearInterval(t);
      if (current.current) URL.revokeObjectURL(current.current);
      current.current = null;
    };
  }, [camId]);

  return (
    <div className="relative rounded-lg overflow-hidden" style={{ background: '#16161a', aspectRatio: '16 / 9' }}>
      {src && !info.empty ? (
        <img src={src} alt="Simulated camera frame" className="w-full h-full object-contain" />
      ) : (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-center gap-2 p-6" style={{ color: '#9aa4b2' }}>
          <Icon name="camera" size={28} />
          <p className="text-sm font-semibold">{info.empty ? 'Simulated camera — no frame available' : 'Connecting to camera…'}</p>
          <p className="text-[11px] max-w-sm">
            {info.empty ? 'The vision service returned no image (OpenCV not installed on the server). Detection events below still come from the simulated inference.' : ''}
          </p>
        </div>
      )}
      <div className="absolute bottom-9 left-2 flex gap-1.5">
        <Badge tone="warning" dot>SIMULATED</Badge>
        <Badge tone="neutral">{(info.provider || 'simulated').toUpperCase()}</Badge>
      </div>
      {info.count !== null && info.count !== '' && !info.empty && (
        <div className="absolute bottom-9 right-2"><Badge tone="info">Detected {info.count}</Badge></div>
      )}
    </div>
  );
}

function CamerasTab({ factory }) {
  const { data, loading, error, reload } = useApi('/api/quality/cameras', { factory }, { every: 3 });
  const [sel, setSel] = useState(null);
  const selected = sel ?? data?.find((c) => c.station === 'Carton packing')?.id ?? data?.[0]?.id;
  const detail = useApi(selected ? `/api/quality/cameras/${selected}` : null, {}, { every: 1 });
  const stations = [...new Set((data || []).map((c) => c.station))];
  const cam = detail.data?.camera;

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <Card title="Cameras" subtitle={`${data?.length || 0} cameras · simulated feeds`} padded={false}>
        <ErrorBox error={error} onRetry={reload} />
        {loading && !data && <div className="p-4"><Loading /></div>}
        <div className="divide-y divide-line">
          {stations.map((st) => (
            <div key={st} className="px-3 py-2">
              <p className="label-caps mb-1.5">{st}</p>
              <div className="space-y-1">
                {data.filter((c) => c.station === st).map((c) => (
                  <button key={c.id} type="button" onClick={() => setSel(c.id)}
                    className={`w-full text-left rounded-md px-2.5 py-1.5 flex items-center gap-2 ${c.id === selected ? 'bg-primary-soft' : 'hover:bg-surface-raised'}`}>
                    <Icon name="camera" size={14} className={c.id === selected ? 'text-primary' : 'text-fg-faint'} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-xs font-semibold text-fg truncate">{c.code}</span>
                      <span className="block text-[10.5px] text-fg-faint truncate">{c.line}{c.sku ? ` · ${c.sku}` : ''}</span>
                    </span>
                    <span className="text-[10px] text-fg-faint tabular-nums">{num(c.inspections_today)}</span>
                    <span className="w-2 h-2 rounded-full" style={{ background: `var(--color-${statusTone(c.status)})` }} title={c.status} />
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Card>
      <div className="xl:col-span-2 space-y-4">
        <Card title={cam ? `${cam.code} · ${cam.name}` : 'Camera'} subtitle={cam ? `${cam.station} · ${cam.line}${cam.sku ? ` · ${cam.sku}` : ''}` : undefined}
          actions={cam && <StatusBadge status={cam.status} />}>
          {selected ? <CameraFrame camId={selected} /> : <Empty icon="camera">No camera selected.</Empty>}
          {cam && (
            <div className="mt-3">
              <KV cols={4} items={[
                ['Status', cam.status], ['Production line', cam.line], ['Current SKU', cam.sku || '—'],
                ['Inference', `${cam.provider}${cam.opencv_available ? ' (OpenCV)' : ''}`],
                ['Inspections today', num(cam.inspections_today)], ['Detection events', num(cam.detections_today)],
                ['Latest carton', detail.data.latest_carton?.code || '—'],
                ['Latest count', detail.data.latest_carton ? `${detail.data.latest_carton.detected}/${detail.data.latest_carton.required}` : '—'],
              ]} />
              <p className="text-[10.5px] text-fg-faint mt-3">{detail.data.note}</p>
            </div>
          )}
        </Card>
        <Card title="Detection events" subtitle="Most recent first" padded={false}>
          {detail.loading && !detail.data && <div className="p-4"><Loading /></div>}
          {detail.data && detail.data.events.length === 0 && <Empty icon="camera">No detection events yet for this camera.</Empty>}
          <div className="divide-y divide-line max-h-[320px] overflow-y-auto">
            {detail.data?.events.map((e, i) => (
              <div key={i} className="px-4 py-2 flex items-center gap-2.5 text-xs">
                <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: `var(--color-${e.severity === 'critical' ? 'danger' : 'success'})` }} />
                <span className="font-mono text-fg-faint w-12">{hhmm(e.ts)}</span>
                <span className={`flex-1 min-w-0 truncate ${e.severity === 'critical' ? 'text-danger font-semibold' : 'text-fg-muted'}`}>{e.label}</span>
                <span className="text-fg-faint">{e.type}</span>
                <span className="text-fg-faint tabular-nums w-10 text-right">{e.confidence != null ? e.confidence.toFixed(2) : ''}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ─── Specifications ─────────────────────────────────────────────────── */
function SpecsTab() {
  const { act, can } = useApp();
  const { data, loading, error, reload } = useApi('/api/quality/specs', {}, { live: false });
  const [edit, setEdit] = useState(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await act(() => put(`/api/quality/specs/${edit.id}`, {
        pillows_per_carton: Number(edit.pillows_per_carton), packaging_type: edit.packaging_type,
        carton_type: edit.carton_type, label_requirements: edit.label_requirements,
      }), `Packaging specification for ${edit.code} updated`);
      setEdit(null);
      reload();
    } catch (e) { /* toasted */ } finally { setBusy(false); }
  };
  return (
    <Card title="SKU packaging specification database" subtitle="The packing station loads these rules for the active production SKU" padded={false}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <div className="p-4"><Loading /></div>}
      {data && (
        <DataTable rows={data} columns={[
          { key: 'code', header: 'SKU', strong: true },
          { key: 'customer', header: 'Customer' },
          { key: 'description', header: 'Description' },
          { key: 'pillows_per_carton', header: 'Pillows / carton', render: (s) => <b className="text-fg">{s.pillows_per_carton}</b> },
          { key: 'packaging_type', header: 'Packaging' },
          { key: 'carton_type', header: 'Carton' },
          { key: 'label_requirements', header: 'Labels', render: (s) => <span className="text-[11px]">{s.label_requirements}</span> },
          { key: 'cartons_per_pallet', header: 'Cartons / pallet' },
          { key: 'fill', header: 'Fill', render: (s) => `${s.fill} · ${s.fill_kg} kg` },
          { key: 'edit', header: '', render: (s) => (
            <Button size="sm" variant="ghost" icon="edit" disabled={!can('quality.write')}
              title={can('quality.write') ? 'Edit specification' : 'Requires quality permissions'} onClick={() => setEdit({ ...s })}>Edit</Button>
          ) },
        ]} />
      )}
      {edit && (
        <Modal title={`Edit ${edit.code}`} eyebrow="Packaging specification" subtitle={`${edit.customer} · ${edit.description}`} width={500}
          onClose={() => setEdit(null)}
          footer={<><Button variant="ghost" onClick={() => setEdit(null)}>Cancel</Button>
            <Button disabled={busy || !(Number(edit.pillows_per_carton) >= 1)} onClick={save}>Save specification</Button></>}>
          <div className="space-y-3">
            <label className="block"><span className="label-caps field-label">Required pillows per carton</span>
              <input className="input" type="number" min={1} max={120} value={edit.pillows_per_carton}
                onChange={(e) => setEdit({ ...edit, pillows_per_carton: e.target.value })} /></label>
            <label className="block"><span className="label-caps field-label">Packaging type</span>
              <input className="input" value={edit.packaging_type} onChange={(e) => setEdit({ ...edit, packaging_type: e.target.value })} /></label>
            <label className="block"><span className="label-caps field-label">Carton type</span>
              <input className="input" value={edit.carton_type} onChange={(e) => setEdit({ ...edit, carton_type: e.target.value })} /></label>
            <label className="block"><span className="label-caps field-label">Label requirements</span>
              <textarea className="textarea" rows={2} value={edit.label_requirements} onChange={(e) => setEdit({ ...edit, label_requirements: e.target.value })} /></label>
            <p className="text-[11px] text-fg-faint">Changes apply to the next carton packed for this SKU.</p>
          </div>
        </Modal>
      )}
    </Card>
  );
}

/* ─── Analytics ──────────────────────────────────────────────────────── */
function AnalyticsTab({ factory, focusCustomer }) {
  const { data, loading, error, reload } = useApi('/api/quality/analytics', { factory }, { every: 5 });
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (loading && !data) return <Loading rows={5} height={40} />;
  if (!data) return null;
  const trend = data.accuracy_trend;
  const vals = trend.map((t) => t.accuracy_pct).filter((v) => v != null);
  const lo = vals.length ? Math.max(90, Math.floor(Math.min(...vals) - 0.5)) : 95;
  const ax = axisOptions({ grid: true });
  const lines = data.by_line.slice(0, 10);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card title="Packaging accuracy trend" subtitle="First-pass cartons ÷ inspected, daily (today live)" className="xl:col-span-2">
          <MiniChart type="line" height={230}
            thresholds={[{ value: 99.5, tone: 'success' }]}
            options={{ scales: { x: ax.x, y: { ...ax.y, beginAtZero: false, min: lo, max: 100 } } }}
            data={{ labels: trend.map((t) => (t.live ? 'Today' : dateShort(t.date))), datasets: [{ label: 'Accuracy %', data: trend.map((t) => t.accuracy_pct), color: 'primary', pointRadius: 2 }] }} />
          <p className="text-[10.5px] text-fg-faint mt-2"><span className="text-success font-bold">- - -</span> 99.5% target</p>
        </Card>
        <Card title="Defect distribution" subtitle="Last 7 days by category">
          {data.defect_distribution.length === 0 ? <Empty>No defects recorded.</Empty> : (
            <MiniChart type="doughnut" height={230} legend
              data={{ labels: data.defect_distribution.map((d) => d.category), datasets: [{ data: data.defect_distribution.map((d) => d.count) }] }} />
          )}
        </Card>
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Card title="Customer-wise packaging errors" subtitle="Share of cartons failing first-pass inspection, last 7 days">
          <MiniChart type="bar" horizontal height={Math.max(160, data.by_customer.length * 30)}
            data={{ labels: data.by_customer.map((c) => c.customer), datasets: [{ label: 'Error rate %', data: data.by_customer.map((c) => c.error_rate_pct),
              color: data.by_customer.map((c) => (focusCustomer && c.customer === focusCustomer ? 'accent' : c.error_rate_pct >= 1 ? 'danger' : c.error_rate_pct >= 0.5 ? 'warning' : 'success')) }] }} />
          <div className="mt-3">
            <DataTable rows={data.by_customer} rowKey="customer" columns={[
              { key: 'customer', header: 'Customer', strong: true },
              { key: 'inspected', header: 'Inspected', render: (c) => num(c.inspected) },
              { key: 'errors', header: 'Errors' },
              { key: 'error_rate_pct', header: 'Error rate', render: (c) => pct(c.error_rate_pct, 2) },
            ]} />
          </div>
        </Card>
        <Card title="Production line-wise defect rates" subtitle="Top lines by defect rate, last 7 days">
          <MiniChart type="bar" horizontal height={Math.max(160, lines.length * 26)}
            data={{ labels: lines.map((l) => l.line), datasets: [{ label: 'Defect rate %', data: lines.map((l) => l.defect_rate_pct),
              color: lines.map((l) => (l.defect_rate_pct >= 1 ? 'danger' : l.defect_rate_pct >= 0.5 ? 'warning' : 'success')) }] }} />
        </Card>
      </div>
    </div>
  );
}

/* ─── Page ───────────────────────────────────────────────────────────── */
export default function Quality() {
  const { filters, meta } = useApp();
  const location = useLocation();
  const [tab, setTab] = useState('stations');
  const [cartonId, setCartonId] = useState(null);
  const [highlightLine, setHighlightLine] = useState(null);
  const [category, setCategory] = useState('');
  const [focusCustomer, setFocusCustomer] = useState(null);
  const applied = useRef(null);
  const factory = filters.factory;
  const { data, loading, error, reload } = useApi('/api/quality/summary', { factory, date: filters.date || undefined }, { every: 2 });

  useEffect(() => {
    const f = parseFocus(location.search);
    if (!f || applied.current === f.key) return;
    applied.current = f.key;
    if (f.type === 'carton') { setTab('blocked'); if (f.id) setCartonId(f.id); }
    else if (f.type === 'line') { setTab('stations'); setHighlightLine(f.id); }
    else if (f.type === 'defect_category') { setTab('blocked'); setCategory(String(f.id || '')); }
    else if (f.type === 'customer') {
      setTab('analytics');
      const c = meta?.customers?.find((x) => x.id === f.id);
      setFocusCustomer(c?.name || null);
    }
  }, [location.search, meta]);

  const c = data?.counts;
  return (
    <div className="space-y-4">
      <PageTitle title="Quality & Packaging" subtitle="Right pillow count and right specification in every carton — blocked cartons never reach finished goods">
        {c?.blocked > 0 && <Badge tone="danger" dot>{c.blocked} BLOCKED</Badge>}
        <SimTag />
      </PageTitle>
      <StageAlerts stage="quality" />
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={3} height={60} />}
      {data && (
        <>
          <Workflow stages={data.workflow} />
          <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3">
            <Kpi label="Cartons inspected" icon="box" value={num(c.inspected)} sub={`${num(data.pack_queue)} pillows waiting to pack`} />
            <Kpi label="Passed first time" icon="check" value={num(c.passed_first)} tone="success" sub={`${num(c.reworked)} reworked · ${num(c.overridden)} overridden`} />
            <Kpi label="Failed inspection" icon="alert" value={num(c.failed)} tone={c.failed ? 'warning' : undefined}
              sub={Object.entries(data.defects_today).slice(0, 2).map(([k, v]) => `${k} ${v}`).join(' · ') || 'No defects today'}
              onClick={() => setTab('blocked')} />
            <Kpi label="Packaging accuracy" icon="shield" value={pct(c.accuracy_pct, 2)}
              tone={c.accuracy_pct >= 99.5 ? 'success' : c.accuracy_pct >= 98.5 ? 'warning' : 'danger'} sub="First-pass correct ÷ inspected"
              onClick={() => setTab('analytics')} />
            <Kpi label="Rework quantity" icon="refresh" value={num(data.rework_qty)} unit="pillows" sub="In reworked cartons today" />
            <Kpi label="Rejected quantity" icon="close" value={num(data.rejected_qty)} unit="pillows" tone={data.rejected_qty ? 'danger' : undefined}
              sub="Scrapped and re-queued to production" />
            <Kpi label="Blocked cartons" icon="alert" value={num(c.blocked)} tone={c.blocked ? 'danger' : 'success'}
              sub={c.blocked ? 'Awaiting rework or override' : 'None on hold'} active={tab === 'blocked'} onClick={() => setTab('blocked')} />
          </div>
        </>
      )}
      <Tabs variant="underline" tabs={TABS} value={tab} onChange={setTab} />
      {tab === 'stations' && <StationsTab factory={factory} highlightLine={highlightLine} onOpenCarton={setCartonId} />}
      {tab === 'blocked' && <BlockedTab factory={factory} category={category} setCategory={setCategory} onOpenCarton={setCartonId} />}
      {tab === 'cameras' && <CamerasTab factory={factory} />}
      {tab === 'specs' && <SpecsTab />}
      {tab === 'analytics' && (
        <div className="grid grid-cols-1 2xl:grid-cols-4 gap-4">
          <div className="2xl:col-span-3"><AnalyticsTab factory={factory} focusCustomer={focusCustomer} /></div>
          <InsightsCard page="quality" limit={5} />
        </div>
      )}
      {tab !== 'analytics' && <InsightsCard page="quality" limit={4} />}
      {cartonId && <CartonDrawer id={cartonId} onClose={() => setCartonId(null)} />}
    </div>
  );
}
