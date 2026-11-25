import React, { Suspense, lazy, useCallback, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Badge, Button, Card, Drawer, Icon, ProgressBar } from '../components/ui';
import ChartCard, { MiniChart } from '../components/charts/ChartCard';
import { BarRow, ErrorBox, InsightsCard, Kpi, KV, Loading, PageTitle } from '../components/common';
import { useApi } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { compact, dateShort, dayTime, effTone, linkTo, money, num, pct, severityTone } from '../utils';
import { NetworkMiniMap, STATUS_COLOR } from './twin/UsNetworkMap';
import { SiapAdvisoriesCard } from '../components/common/SiapAdvisories';

const STAGE_TONE = { Normal: 'success', Constrained: 'warning', Bottleneck: 'danger', Down: 'danger' };

function KpiGrid({ k, onOpen, navigate }) {
  const p = k.production;
  const prodTone = p.on_track ? 'success' : p.achievement_pct >= 0 && p.actual >= p.planned_to_date * 0.85 ? 'warning' : 'danger';
  const wf = k.workforce;
  const inv = k.inventory;
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
      <Kpi label="Production output" icon="factory" value={num(p.actual)} unit="pillows" tone={prodTone}
        sub={<>Target {num(p.target)} · <b>{pct(p.achievement_pct)}</b> achieved</>}
        footer={<div className="mt-1"><ProgressBar value={p.actual} max={p.target} tone={prodTone} />
          <p className="text-[10px] mt-1" style={{ color: `var(--color-${prodTone})` }}>{p.status} · plan to date {num(p.planned_to_date)}</p></div>}
        onClick={() => onOpen('production')} />
      <Kpi label="Production efficiency" icon="gauge" value={pct(k.efficiency.pct)} tone={effTone(k.efficiency.pct)}
        delta={{ value: k.efficiency.delta, good: k.efficiency.delta >= 0, unit: ' pts' }}
        sub={`Actual ÷ expected output for available time · vs ${k.efficiency.previous_label} ${pct(k.efficiency.previous_shift_pct)}`}
        onClick={() => onOpen('efficiency')} />
      <Kpi label="OEE" icon="analytics" value={pct(k.oee.oee_pct)} tone={effTone(k.oee.oee_pct, 85, 70)}
        sub={`A ${pct(k.oee.availability_pct, 0)} × P ${pct(k.oee.performance_pct, 0)} × Q ${pct(k.oee.quality_pct, 0)}`}
        onClick={() => onOpen('oee')} />
      <Kpi label="Machine downtime" icon="wrench" value={num(k.downtime.total_min)} unit="min"
        tone={k.downtime.by_category['Mechanical breakdown'] > 0 ? 'danger' : undefined}
        sub={Object.entries(k.downtime.by_category).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 2)
          .map(([c, v]) => `${c} ${num(v)}`).join(' · ') || 'No downtime recorded'}
        onClick={() => onOpen('downtime')} />
      <Kpi label="Workforce utilization" icon="users" value={pct(wf.utilization_pct)}
        tone={wf.deployed < wf.required ? 'warning' : undefined}
        sub={<>{wf.deployed} deployed / {wf.required} required · idle {num(wf.idle_hours, 1)} h ({num(wf.idle_fte, 1)} FTE)</>}
        delta={wf.previous_output_per_worker ? { value: wf.output_per_worker_hour - wf.previous_output_per_worker, good: wf.output_per_worker_hour >= wf.previous_output_per_worker, unit: '/h' } : null}
        onClick={() => onOpen('workforce')} />
      <Kpi label="Labour cost per pillow" icon="dollar" value={k.labour_cost.per_pillow != null ? `$${k.labour_cost.per_pillow.toFixed(3)}` : '—'}
        delta={k.labour_cost.previous_per_pillow ? { value: (k.labour_cost.per_pillow - k.labour_cost.previous_per_pillow) * 100, good: k.labour_cost.per_pillow <= k.labour_cost.previous_per_pillow, unit: '¢' } : null}
        sub={`${money(k.labour_cost.total)} paid labour today · shift 2 +$${k.labour_cost.shift2_differential.toFixed(2)}/h`}
        onClick={() => onOpen('labour')} />
      <Kpi label="Packaging accuracy" icon="shield" value={pct(k.packaging.accuracy_pct, 2)} tone={effTone(k.packaging.accuracy_pct, 99.5, 98.5)}
        sub={<>{num(k.packaging.failed)} failed of {num(k.packaging.inspected)} cartons{k.packaging.blocked ? <> · <b className="text-danger">{k.packaging.blocked} blocked</b></> : ''}</>}
        onClick={() => navigate('/quality')} />
      <Kpi label="Inventory availability" icon="box" value={compact(inv.fg_available)} unit="pillows FG"
        tone={inv.materials_critical ? 'danger' : inv.materials_low ? 'warning' : undefined}
        sub={<>{inv.materials_critical} critical · {inv.materials_low} low raw materials
          {inv.lines_stopped_for_material ? <> · <b className="text-danger">{inv.lines_stopped_for_material} lines stopped</b></> : ''}
          {inv.orders_with_material_risk ? ` · ${inv.orders_with_material_risk} orders at material risk` : ''}</>}
        onClick={() => onOpen('inventory')} />
      <Kpi label="Shipment readiness" icon="truck" value={`${k.shipment.ready}/${k.shipment.scheduled}`} unit="orders ready"
        tone={k.shipment.at_risk ? 'danger' : 'success'}
        sub={<>Due within 48 h · {pct(k.shipment.pct, 0)} ready{k.shipment.at_risk ? <> · <b className="text-danger">{k.shipment.at_risk} at risk</b></> : ''}</>}
        onClick={() => navigate('/dispatch')} />
      <Kpi label="Potential detention" icon="clock" value={money(k.detention.projected + k.detention.incurred)}
        tone={k.detention.projected + k.detention.incurred > 0 ? 'warning' : 'success'}
        sub={`Est. ${money(k.detention.projected)} upcoming + ${money(k.detention.incurred)} today (illustrative $${k.detention.charge_per_truck}/truck)`}
        onClick={() => navigate(linkTo('dispatch', 'detention', ''))} />
    </div>
  );
}

function KpiDrawer({ kind, data, onClose }) {
  const k = data.kpis;
  const cmp = data.comparison;
  const titles = {
    production: 'Production output', efficiency: 'Production efficiency', oee: 'Overall Equipment Effectiveness',
    downtime: 'Machine downtime', workforce: 'Workforce utilization', labour: 'Labour cost per pillow', inventory: 'Inventory availability',
  };
  let body = null;
  if (kind === 'production' || kind === 'efficiency') {
    body = (
      <>
        <KV cols={3} items={[['Actual', num(k.production.actual)], ['Plan to date', num(k.production.planned_to_date)], ['Day target', num(k.production.target)],
          ['Efficiency', pct(k.efficiency.pct)], [k.efficiency.previous_label, pct(k.efficiency.previous_shift_pct)], ['Status', k.production.status]]} />
        <p className="label-caps mt-4 mb-2">Hourly actual vs target</p>
        <MiniChart type="bar" height={200} legend data={{
          labels: data.hourly.map((h) => h.hour),
          datasets: [
            { label: 'Target', data: data.hourly.map((h) => h.planned), color: 'chart-8' },
            { label: 'Actual', data: data.hourly.map((h) => h.actual), color: 'primary' },
          ],
        }} />
        <p className="label-caps mt-4 mb-2">By factory</p>
        <div className="space-y-2.5">
          {cmp.map((f) => <BarRow key={f.code} label={f.name} value={f.output} max={f.target} display={`${num(f.output)} / ${num(f.target)} · ${pct(f.efficiency_pct)} eff.`} tone={effTone(f.efficiency_pct)} />)}
        </div>
        <p className="text-[11px] text-fg-faint mt-4">Efficiency = actual output ÷ expected output for the productive time elapsed (planned rate × productive minutes, excluding lunch and breaks).</p>
      </>
    );
  } else if (kind === 'oee') {
    const o = k.oee;
    body = (
      <>
        <div className="grid grid-cols-4 gap-2">
          {[['Availability', o.availability_pct, 'Run time ÷ planned production time'], ['Performance', o.performance_pct, 'Actual ÷ (rated rate × run time)'],
            ['Quality', o.quality_pct, 'First-pass good cartons ÷ inspected'], ['OEE', o.oee_pct, 'A × P × Q']].map(([l, v, d]) => (
              <div key={l} className="tile"><p className="label-caps">{l}</p><p className="text-xl font-bold" style={{ color: `var(--color-${effTone(v, 90, 75)})` }}>{pct(v)}</p><p className="text-[10px] text-fg-faint mt-1 leading-snug">{d}</p></div>
          ))}
        </div>
        <p className="text-xs text-fg-muted mt-4">OEE = {pct(o.availability_pct)} × {pct(o.performance_pct)} × {pct(o.quality_pct)} = <b className="text-fg">{pct(o.oee_pct)}</b></p>
      </>
    );
  } else if (kind === 'downtime') {
    const cats = Object.entries(k.downtime.by_category);
    body = (
      <>
        <p className="text-sm text-fg mb-3"><b>{num(k.downtime.total_min)} min</b> of productive time lost in the selected period.</p>
        <MiniChart type="bar" horizontal height={200} data={{ labels: cats.map(([c]) => c), datasets: [{ label: 'Minutes', data: cats.map(([, v]) => v), color: ['danger', 'info', 'warning', 'chart-5', 'chart-8'] }] }} />
        <div className="grid grid-cols-3 gap-2 mt-4">
          {cmp.map((f) => <div key={f.code} className="tile"><p className="label-caps">{f.name}</p><p className="text-lg font-bold text-fg">{num(f.downtime_min)} min</p></div>)}
        </div>
      </>
    );
  } else if (kind === 'workforce' || kind === 'labour') {
    const w = k.workforce;
    body = (
      <>
        <KV cols={3} items={[['Required', w.required], ['Deployed', w.deployed], ['Absent', w.absent], ['Utilization', pct(w.utilization_pct)],
          ['Idle', `${num(w.idle_hours, 1)} h`], ['Output / worker-hour', num(w.output_per_worker_hour, 1)],
          ['Labour cost', money(k.labour_cost.total)], ['Cost per pillow', k.labour_cost.per_pillow != null ? `$${k.labour_cost.per_pillow.toFixed(3)}` : '—'],
          ['Below benchmark', w.below_benchmark]]} />
        <p className="label-caps mt-4 mb-2">Labour cost per pillow by factory</p>
        <MiniChart type="bar" height={170} data={{ labels: cmp.map((f) => f.name), datasets: [{ label: '$ / pillow', data: cmp.map((f) => f.labour_cost_per_pillow) }] }} />
        <p className="text-[11px] text-fg-faint mt-3">Paid time = 8.5 h presence − 30 min unpaid lunch (two 10-min breaks paid). Second shift includes the +${k.labour_cost.shift2_differential.toFixed(2)}/h differential.</p>
      </>
    );
  } else if (kind === 'inventory') {
    const i = k.inventory;
    body = (
      <>
        <KV cols={3} items={[['Finished goods', num(i.fg_total)], ['Available', num(i.fg_available)], ['Reserved', num(i.fg_reserved)],
          ['Critical materials', i.materials_critical], ['Low materials', i.materials_low], ['Lines stopped', i.lines_stopped_for_material]]} />
        {i.lowest && (
          <div className="tile tone-warning mt-4 text-xs">
            Lowest cover: <b>{i.lowest.code}</b> {i.lowest.name} at {i.lowest.factory} — {i.lowest.days_cover} days ({num(i.lowest.available)} {i.lowest.uom})
          </div>
        )}
      </>
    );
  }
  return <Drawer eyebrow="KPI detail" title={titles[kind]} width={560} onClose={onClose}>{body}</Drawer>;
}

function Comparison({ rows }) {
  const metrics = [
    ['Production output', (r) => r.output, (r) => num(r.output), 'primary', (r) => r.target],
    ['Efficiency', (r) => r.efficiency_pct, (r) => pct(r.efficiency_pct), (r) => effTone(r.efficiency_pct), () => 110],
    ['Downtime (min)', (r) => r.downtime_min, (r) => num(r.downtime_min), 'warning', (rs) => Math.max(...rs.map((x) => x.downtime_min), 1)],
    ['Labour cost / pillow', (r) => r.labour_cost_per_pillow || 0, (r) => (r.labour_cost_per_pillow != null ? `$${r.labour_cost_per_pillow.toFixed(3)}` : '—'), 'chart-5', (rs) => Math.max(...rs.map((x) => x.labour_cost_per_pillow || 0), 0.01)],
    ['Packaging accuracy', (r) => r.packaging_accuracy_pct, (r) => pct(r.packaging_accuracy_pct, 2), (r) => effTone(r.packaging_accuracy_pct, 99.5, 98.5), () => 100],
    ['Shipment readiness', (r) => r.shipment_readiness_pct, (r) => pct(r.shipment_readiness_pct, 0), (r) => effTone(r.shipment_readiness_pct, 80, 50), () => 100],
  ];
  return (
    <Card title="Factory performance comparison" subtitle="Today, all three plants">
      <div className="space-y-3.5">
        {metrics.map(([label, val, disp, tone, max]) => (
          <div key={label}>
            <p className="label-caps mb-1.5">{label}</p>
            <div className="grid grid-cols-3 gap-3">
              {rows.map((r) => (
                <BarRow key={r.code} label={r.code} value={val(r)} display={disp(r)}
                  max={typeof max === 'function' ? (max.length ? max(label === 'Production output' ? r : rows) : max()) : max}
                  tone={typeof tone === 'function' ? tone(r) : tone} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Lifecycle({ stages }) {
  const navigate = useNavigate();
  return (
    <Card title="Lifecycle stage health" subtitle="Procurement → production stages → quality → warehouse → dispatch">
      <div className="flex flex-wrap items-stretch gap-1.5">
        {stages.map((s, i) => (
          <React.Fragment key={s.stage}>
            <button type="button" onClick={() => navigate(`/${s.page}`)} title={s.detail}
              className={`tile tone-${STAGE_TONE[s.status] || 'neutral'} text-left px-2.5 py-2 hover:opacity-90`} style={{ minWidth: 112, flex: '1 1 112px' }}>
              <span className="block text-[11px] font-bold text-fg leading-tight">{s.stage}</span>
              <span className="block text-[10px] font-bold mt-0.5" style={{ color: `var(--color-${STAGE_TONE[s.status] || 'neutral'})` }}>{s.status.toUpperCase()}</span>
              <span className="block text-[10px] text-fg-muted leading-snug mt-0.5 line-clamp-2">{s.detail}</span>
            </button>
            {i < stages.length - 1 && <span className="self-center text-fg-faint hidden md:inline"><Icon name="chevronRight" size={12} /></span>}
          </React.Fragment>
        ))}
      </div>
    </Card>
  );
}

function Attention({ items }) {
  const navigate = useNavigate();
  return (
    <Card title={<span className="flex items-center gap-2"><Icon name="alert" size={14} className="text-danger" />Requires attention</span>}
      subtitle="Highest-impact open issues, ranked automatically" padded={false}>
      {items.length === 0 && <p className="text-xs text-fg-faint p-4">No open issues.</p>}
      <div className="divide-y divide-line max-h-[420px] overflow-y-auto">
        {items.map((a) => (
          <button key={a.id} type="button" className="w-full text-left px-4 py-2.5 hover:bg-surface-raised flex gap-2.5"
            onClick={() => navigate(linkTo(a.page, a.entity_type, a.entity_id))}>
            <span className="w-1 rounded-full shrink-0" style={{ background: `var(--color-${severityTone(a.severity)})` }} />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5 flex-wrap">
                <Badge tone={severityTone(a.severity)}>{a.severity.toUpperCase()}</Badge>
                <span className="text-[10px] text-fg-faint">{a.factory} · {a.stage}</span>
              </span>
              <span className="block text-[12.5px] font-semibold text-fg mt-1 leading-snug">{a.title}</span>
              <span className="block text-[11px] text-fg-muted leading-snug line-clamp-2">{a.description}</span>
              <span className="block text-[11px] text-primary mt-0.5 leading-snug">→ {a.recommended_action}</span>
            </span>
          </button>
        ))}
      </div>
    </Card>
  );
}

// The 3D twin (three.js) loads on demand when the explorer is opened.
const TwinExplorer = lazy(() => import('./twin/TwinExplorer'));

/** Entry point to the digital twin: live mini map of the three plants. */
function TwinCard({ onOpen }) {
  const { data } = useApi('/api/twin/network', {}, { every: 3 });
  const fs = data?.factories || [];
  const onFloor = fs.reduce((a, f) => a + f.badges.on_floor, 0);
  const away = fs.reduce((a, f) => a + f.badges.away, 0);
  return (
    <Card
      title={<span className="flex items-center gap-2"><Icon name="globe" size={15} className="text-primary" />Digital twin · US manufacturing network</span>}
      subtitle="Live plant status and loads on the road. Open a plant for its top-view twin with every station, pillow count and RFID-tracked operator."
      actions={<Button size="sm" icon="map" onClick={() => onOpen('us')}>Open digital twin</Button>}>
      {!data && <Loading rows={4} height={40} />}
      {data && (
        <div className="grid grid-cols-1 md:grid-cols-5 gap-4 items-center">
          <div className="md:col-span-3 cursor-pointer" onClick={() => onOpen('us')} role="button" tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && onOpen('us')}>
            <NetworkMiniMap data={data} onOpen={onOpen} />
          </div>
          <div className="md:col-span-2 space-y-2">
            {fs.map((f) => (
              <button key={f.code} type="button" onClick={() => onOpen(f.code)}
                className="tile w-full text-left hover:opacity-90" style={{ borderLeft: `3px solid ${STATUS_COLOR[f.status]}` }}>
                <span className="flex items-center gap-2 text-[12px] font-bold text-fg">
                  {f.name}, {f.state}<Badge tone={{ Healthy: 'success', Watch: 'warning', Critical: 'danger' }[f.status]} className="ml-auto">{f.status.toUpperCase()}</Badge>
                </span>
                <span className="block text-[10.5px] text-fg-muted mt-0.5">
                  {num(f.output)} / {num(f.target)} pillows · {f.lines_running}/{f.lines_total} lines · {pct(f.rfid_availability_pct, 0)} at station
                </span>
                <ProgressBar value={f.output} max={f.target} className="mt-1.5" tone={f.status === 'Critical' ? 'warning' : 'primary'} />
              </button>
            ))}
            <p className="text-[10.5px] text-fg-faint flex items-center gap-1.5">
              <Icon name="users" size={12} />{onFloor} RFID badges on the floor · {away} away from station · {data.lanes.length} trucks in transit
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}

export default function Overview() {
  const { filters, meta } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const [drawer, setDrawer] = useState(null);
  const twin = new URLSearchParams(location.search).get('twin');
  const openTwin = useCallback((t) => navigate({ pathname: '/', search: `?twin=${t}` }), [navigate]);
  const closeTwin = useCallback(() => navigate({ pathname: '/', search: '' }), [navigate]);
  const params = { factory: filters.factory, date: filters.date || undefined, shift: filters.shift || undefined, customer: filters.customer || undefined };
  // the overview pauses its own polling while the full-screen twin is open
  const { data, loading, error, reload } = useApi(twin ? null : '/api/overview', params, { every: 2 });

  const scope = filters.factory === 'ALL' ? 'All factories' : meta.factories.find((f) => f.code === filters.factory)?.name;
  return (
    <div className="space-y-4">
      <PageTitle title="Executive Overview"
        subtitle={`${scope} · ${data ? dateShort(data.date) : ''}${filters.shift ? ` · Shift ${filters.shift}` : ' · both shifts'}${filters.customer ? ` · ${meta.customers.find((c) => String(c.id) === String(filters.customer))?.name}` : ''}`}>
        {data && <Badge tone={data.kpis.production.on_track ? 'success' : 'danger'} dot>{data.kpis.production.status.toUpperCase()}</Badge>}
        {data && <span className="text-[11px] text-fg-faint">Updated {dayTime(data.sim_time)}</span>}
      </PageTitle>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={6} height={60} />}
      {data && (
        <>
          <KpiGrid k={data.kpis} onOpen={setDrawer} navigate={navigate} />
          {/* xl: two independent columns, so each card keeps its content height instead of
              stretching to a taller neighbour (the S!AP list). Below xl the column wrappers
              dissolve (display: contents) and `order` keeps the original stacking order. */}
          <div className="flex flex-col gap-4 xl:grid xl:grid-cols-3 xl:items-start">
            <div className="contents xl:flex xl:flex-col xl:gap-4 xl:col-span-2 xl:min-w-0">
              <div className="order-1 xl:order-none min-w-0"><TwinCard onOpen={openTwin} /></div>
              <div className="order-3 xl:order-none min-w-0">
                <ChartCard title="Production performance — hourly actual vs target" type="bar" legend height={260}
                  subtitle="Pillows completed each hour; target follows the shift calendar (breaks and lunch excluded)"
                  data={{
                    labels: data.hourly.map((h) => h.hour),
                    datasets: [
                      { label: 'Target', data: data.hourly.map((h) => h.planned), color: 'chart-8' },
                      { label: 'Actual', data: data.hourly.map((h) => h.actual), color: data.hourly.map((h) => (
                        h.actual == null || h.hour.slice(0, 2) === data.sim_time.slice(11, 13) ? 'info'   // hour in progress
                          : h.actual >= h.planned * 0.95 ? 'success' : h.actual >= h.planned * 0.85 ? 'warning' : 'danger')) },
                    ],
                  }} />
              </div>
              <div className="order-5 xl:order-none min-w-0"><Lifecycle stages={data.lifecycle} /></div>
              <div className="order-6 xl:order-none min-w-0"><Comparison rows={data.comparison} /></div>
              <div className="order-7 xl:order-none min-w-0"><InsightsCard page="overview" limit={5} title="Operational recommendations" /></div>
            </div>
            <div className="contents xl:flex xl:flex-col xl:gap-4 xl:min-w-0">
              <div className="order-2 xl:order-none min-w-0">
                <SiapAdvisoriesCard factory={filters.factory} limit={4} compact title="S!AP advisories"
                  subtitle="Machine-repair predictions, quality risk and RFID workforce patterns · illustrative models on simulated data" />
              </div>
              <div className="order-4 xl:order-none min-w-0"><Attention items={data.attention} /></div>
            </div>
          </div>
          {drawer && <KpiDrawer kind={drawer} data={data} onClose={() => setDrawer(null)} />}
        </>
      )}
      {twin && (
        <Suspense fallback={<div className="fixed inset-0 z-[1200] grid place-items-center" style={{ background: '#050a14' }}><Loading rows={3} /></div>}>
          <TwinExplorer target={twin} onNavigate={openTwin} onClose={closeTwin} />
        </Suspense>
      )}
    </div>
  );
}

