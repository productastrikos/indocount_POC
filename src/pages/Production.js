import React, { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Badge, Card, Icon, ProgressBar, Tabs } from '../components/ui';
import { ErrorBox, InsightsCard, Kpi, Loading, PageTitle, StageAlerts, StatusBadge } from '../components/common';
import { useApi, useFocus } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { dateShort, dayTime, effTone, num, pct } from '../utils';
import LinesTab, { LineDrawer } from './production/LinesTab';
import OrdersTab, { OrderDrawer } from './production/OrdersTab';
import MonitoringTab from './production/MonitoringTab';
import MachinesTab, { MachineDrawer } from './production/MachinesTab';
import PredictiveTab from './production/PredictiveTab';
import WorkforceTab from './production/WorkforceTab';
import ChangeoversTab from './production/ChangeoversTab';
import OptimizationTab from './production/OptimizationTab';
import { STAGE_TONE } from './production/shared';
import CctvTab from './production/CctvTab';

const FactoryTwinTab = lazy(() => import('./production/FactoryTwinTab'));

const TABS = [
  { value: 'lines', label: 'Production lines' },
  { value: 'twin', label: 'Digital twin' },
  { value: 'orders', label: 'Production orders' },
  { value: 'monitoring', label: 'Live monitoring' },
  { value: 'cctv', label: 'CCTV · Vision AI' },
  { value: 'machines', label: 'Machine health' },
  { value: 'predictive', label: 'Predictive maintenance' },
  { value: 'workforce', label: 'Workforce' },
  { value: 'changeovers', label: 'Changeovers' },
  { value: 'optimization', label: 'Optimization' },
];

function Pipeline({ stages, selected, onSelect }) {
  return (
    <div className="card p-3">
      <div className="flex items-center justify-between mb-2 px-1">
        <p className="label-caps">Manufacturing process · today</p>
        <p className="text-[10.5px] text-fg-faint">Click a stage for constrained lines</p>
      </div>
      <div className="flex items-stretch gap-1 overflow-x-auto pb-1">
        {stages.map((s, i) => {
          const tone = STAGE_TONE[s.status] || 'neutral';
          const active = selected === s.stage;
          return (
            <React.Fragment key={s.stage}>
              <button type="button" onClick={() => onSelect(active ? null : s.stage)}
                className={`tile tone-${tone} text-left flex flex-col gap-1 hover:opacity-95`}
                style={{ minWidth: 138, flex: '1 1 138px', ...(active ? { boxShadow: '0 0 0 2px var(--color-primary)' } : {}) }}>
                <span className="flex items-center justify-between gap-1">
                  <span className="text-[11.5px] font-bold text-fg leading-tight">{s.stage}</span>
                  {s.bottleneck && <Icon name="alert" size={13} className="text-danger shrink-0" />}
                </span>
                <span className="text-[10px] font-bold" style={{ color: `var(--color-${tone})` }}>{s.bottleneck ? 'BOTTLENECK' : s.status.toUpperCase()}</span>
                <span className="text-[15px] font-bold text-fg tabular-nums leading-tight">{num(s.qty)} <span className="text-[10px] font-normal text-fg-faint">{s.unit}</span></span>
                <ProgressBar value={s.qty} max={s.target || 1} tone={effTone(s.output_pct)} />
                <span className="text-[10px] text-fg-muted">{pct(s.output_pct, 0)} of {num(s.target)} plan · cap. {pct(s.efficiency_pct, 0)}</span>
                <span className="text-[10px] text-fg-faint">{s.active_orders} active orders{s.constrained_lines.length ? ` · ${s.constrained_lines.length} constrained` : ''}</span>
              </button>
              {i < stages.length - 1 && <span className="self-center text-fg-faint shrink-0"><Icon name="chevronRight" size={12} /></span>}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}

function StageDetail({ stage, pipe, lines, onOpenLine }) {
  const s = pipe.stages.find((x) => x.stage === stage);
  const constrained = (lines || [])
    .map((ln) => ({ ...ln, c: pipe.per_line[ln.id] || pipe.per_line[String(ln.id)] }))
    .filter((ln) => ln.c?.binding)
    .sort((a, b) => b.c.loss_pct - a.c.loss_pct);
  const rows = s ? constrained.filter((ln) => ln.c.binding === s.stage) : constrained;
  return (
    <Card title={s ? `${s.stage} — ${s.bottleneck ? 'current bottleneck' : s.status.toLowerCase()}` : 'Line constraints'}
      subtitle={s ? `${num(s.qty)} ${s.unit} vs ${num(s.target)} planned to date · stage capacity ${pct(s.efficiency_pct)}`
        : pipe.bottleneck ? `Bottleneck: ${pipe.bottleneck}. Binding constraint per line below.` : 'No stage is constraining output by more than 8%.'}
      actions={s && <StatusBadge status={s.bottleneck ? 'Bottleneck' : s.status} tone={STAGE_TONE[s.bottleneck ? 'Bottleneck' : s.status]} />}
      padded={false}>
      {s && (s.down_lines.length > 0 || s.changeover_lines.length > 0) && (
        <div className="px-4 pt-3 flex flex-wrap gap-2">
          {s.down_lines.map((n) => <Badge key={n} tone="danger" dot>{n} stopped</Badge>)}
          {s.changeover_lines.map((n) => <Badge key={n} tone="info" dot>{n} in changeover</Badge>)}
        </div>
      )}
      {rows.length === 0 ? (
        <p className="text-xs text-fg-faint p-4">{s ? `No line is currently constrained at ${s.stage}.` : 'All lines running without a binding constraint.'}</p>
      ) : (
        <div className="divide-y divide-line">
          {rows.map((ln) => (
            <button key={ln.id} type="button" onClick={() => onOpenLine(ln.id)} className="w-full text-left px-4 py-2 hover:bg-surface-raised flex items-center gap-3">
              <span className="text-[12px] font-semibold text-fg w-40 truncate">{ln.name} <span className="text-fg-faint font-normal">{ln.factory}</span></span>
              <span className="text-[11px] text-fg-muted flex-1 truncate">{ln.c.binding} · {ln.sku || 'no order'} · {ln.status}{ln.status_reason ? ` — ${ln.status_reason}` : ''}</span>
              <span className="text-[11px] font-bold text-danger tabular-nums">−{num(ln.c.loss_pct, 0)}%</span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

export default function Production() {
  const { filters, meta } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const focus = useFocus();
  const [tab, setTab] = useState('lines');
  const [stage, setStage] = useState(null);
  const [lineId, setLineId] = useState(null);
  const [machineId, setMachineId] = useState(null);
  const [orderId, setOrderId] = useState(null);
  const [highlightEmp, setHighlightEmp] = useState(null);
  const [optFactory, setOptFactory] = useState(null);
  const [focusRun, setFocusRun] = useState(null);

  const factory = filters.factory;
  const date = filters.date || undefined;
  const shift = filters.shift || undefined;
  const summary = useApi('/api/production/summary', { factory, date, shift }, { every: 1 });
  const lines = useApi('/api/production/lines', { factory }, { every: 1 });
  const orders = useApi(tab === 'orders' ? '/api/production/orders' : null, { factory }, { every: 2 });

  const fType = focus?.type;
  const fId = focus?.id;
  useEffect(() => {
    if (!fType) return;
    if (fType === 'line') { setTab('lines'); setLineId(fId); }
    else if (fType === 'machine') { setTab('machines'); setMachineId(fId); }
    else if (fType === 'production_order') { setTab('orders'); setOrderId(fId); }
    else if (fType === 'employee') { setTab('workforce'); setHighlightEmp(fId); }
    else if (fType === 'workforce') setTab('workforce');
    else if (fType === 'stage') setStage(fId);
    else if (fType === 'optimization') { setTab('optimization'); setFocusRun(fId); }
  }, [fType, fId]);

  const clearFocus = () => { if (location.search) navigate(location.pathname, { replace: true }); };
  const close = (setter) => () => { setter(null); clearFocus(); };

  // the page's digital twin card opens the full twin tab on the same plant and stage
  const tabsRef = useRef(null);
  const [twinStart, setTwinStart] = useState(null);
  const openTwin = (plant, stageKey) => {
    setTwinStart({ plant, stage: stageKey, n: Date.now() });
    setTab('twin');
    requestAnimationFrame(() => tabsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };
  const twinCard = tab !== 'twin' && (
    <Suspense fallback={<Card title="Digital twin · pillow plant"><Loading rows={4} height={60} /></Card>}>
      <FactoryTwinTab compact factory={factory} onExpand={openTwin} onOpenLine={setLineId} onOpenMachine={setMachineId} />
    </Suspense>
  );

  const onInsightAction = (action) => {
    if (action.type === 'optimize') {
      setOptFactory(action.payload?.factory_id || null);
      setTab('optimization');
    }
  };

  const s = summary.data;
  const lineStatus = useMemo(() => (lines.data || []).reduce((a, l) => { a[l.status] = (a[l.status] || 0) + 1; return a; }, {}), [lines.data]);
  const scope = factory === 'ALL' ? 'All factories' : meta.factories.find((f) => f.code === factory)?.name;
  const factories = factory === 'ALL' ? meta.factories : meta.factories.filter((f) => f.code === factory);

  return (
    <div className="space-y-4">
      <PageTitle title="Production & Manufacturing"
        subtitle={`${scope} · ${date ? dateShort(date) : 'today'}${shift ? ` · Shift ${shift}` : ' · both shifts'} · raw material preparation through transfer to packaging`}>
        {s && <Badge tone={s.production.on_track ? 'success' : 'danger'} dot>{s.production.on_track ? 'ON TRACK' : 'BEHIND PLAN'}</Badge>}
        {s && <span className="text-[11px] text-fg-faint">Updated {dayTime(s.sim_time)}</span>}
      </PageTitle>
      <ErrorBox error={summary.error} onRetry={summary.reload} />
      {summary.loading && !s && <Loading rows={4} height={60} />}
      {s && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
            <Kpi label="Output vs target" icon="factory" value={num(s.production.actual)} unit="pillows"
              tone={s.production.on_track ? 'success' : 'danger'}
              sub={<>Target {num(s.production.target)} · {pct(s.production.achievement_pct)} · plan to date {num(s.production.planned_to_date)}</>}
              onClick={() => setTab('monitoring')} active={tab === 'monitoring'} />
            <Kpi label="Efficiency" icon="gauge" value={pct(s.production.efficiency_pct)} tone={effTone(s.production.efficiency_pct)}
              sub="Actual ÷ expected for productive time" onClick={() => setTab('monitoring')} />
            <Kpi label="OEE" icon="analytics" value={pct(s.oee.oee_pct)} tone={effTone(s.oee.oee_pct, 85, 70)}
              sub={`A ${pct(s.oee.availability_pct, 0)} × P ${pct(s.oee.performance_pct, 0)} × Q ${pct(s.oee.quality_pct, 0)}`} />
            <Kpi label="Downtime" icon="wrench" value={num(s.downtime_total)} unit="min"
              tone={s.downtime['Mechanical breakdown'] > 0 ? 'danger' : undefined}
              sub={Object.entries(s.downtime).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([c, v]) => `${c} ${num(v)}`).join(' · ') || 'None'}
              onClick={() => setTab('monitoring')} />
            <Kpi label="Lines running" icon="settings" value={`${s.lines_running}/${s.lines_total}`}
              tone={s.lines_running < s.lines_total ? 'warning' : 'success'}
              sub={Object.entries(lineStatus).filter(([k]) => k !== 'Running').map(([k, v]) => `${v} ${k.toLowerCase()}`).join(' · ') || 'All running'}
              onClick={() => setTab('lines')} active={tab === 'lines'} />
            <Kpi label="Delayed orders" icon="hourglass" value={`${s.delayed_orders}/${s.open_orders}`}
              tone={s.delayed_orders ? 'danger' : 'success'} sub="Projected past required completion"
              onClick={() => setTab('orders')} active={tab === 'orders'} />
          </div>

          <Pipeline stages={s.pipeline.stages} selected={stage} onSelect={setStage} />

          {/* items-start: each card keeps its content height; the stage alerts sit under the
              constraint list so the left column is not left empty beside the advisories. */}
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
            <div className="xl:col-span-2 space-y-4 min-w-0">
              {twinCard}
              <StageDetail stage={stage} pipe={s.pipeline} lines={lines.data} onOpenLine={setLineId} />
              <StageAlerts stage="production" limit={4} />
            </div>
            <InsightsCard page="production" limit={4} compact onAction={onInsightAction} />
          </div>
        </>
      )}

      {!s && <StageAlerts stage="production" limit={4} />}

      <div ref={tabsRef} style={{ scrollMarginTop: 72 }}>
        <Tabs tabs={TABS} value={tab} onChange={setTab} variant="underline" />
      </div>
      <div>
        {tab === 'lines' && <LinesTab lines={lines.data} factories={factories} onOpen={setLineId} />}
        {tab === 'twin' && (
          <Suspense fallback={<Loading rows={6} />}>
            <FactoryTwinTab key={twinStart?.n || 'twin'} factory={factory} initialPlant={twinStart?.plant} initialStage={twinStart?.stage}
              onOpenLine={setLineId} onOpenMachine={setMachineId} />
          </Suspense>
        )}
        {tab === 'orders' && <OrdersTab orders={orders.data} onOpen={setOrderId} />}
        {tab === 'monitoring' && <MonitoringTab factory={factory} date={date} shift={shift} downtime={s?.downtime} onOpenLine={setLineId} />}
        {tab === 'cctv' && <CctvTab factory={factory} />}
        {tab === 'machines' && <MachinesTab factory={factory} onOpen={setMachineId} />}
        {tab === 'predictive' && <PredictiveTab factory={factory} onOpenMachine={setMachineId} />}
        {tab === 'workforce' && <WorkforceTab factory={factory} date={date} shift={shift} highlight={highlightEmp} />}
        {tab === 'changeovers' && <ChangeoversTab factory={factory} onOpenLine={setLineId} />}
        {tab === 'optimization' && <OptimizationTab factory={factory} presetFactoryId={optFactory} focusRunId={focusRun} />}
      </div>

      {lineId && <LineDrawer id={lineId} onClose={close(setLineId)}
        onOpenMachine={(id) => { setLineId(null); setMachineId(id); }}
        onOpenOrder={(id) => { setLineId(null); setOrderId(id); }} />}
      {machineId && <MachineDrawer id={machineId} onClose={close(setMachineId)} />}
      {orderId && <OrderDrawer id={orderId} onClose={close(setOrderId)} onOpenLine={(id) => { setOrderId(null); setLineId(id); }} />}
    </div>
  );
}
