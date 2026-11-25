import React, { useState } from 'react';
import { Chart as ChartJS, LogarithmicScale } from 'chart.js';
import { Badge, Button, Card, DataTable, Drawer, FilterChips, Icon, Modal, ProgressBar } from '../../components/ui';
import ChartCard, { MiniChart } from '../../components/charts/ChartCard';
import { axisOptions, cssVar, resolveColor, tooltipOptions, withAlpha } from '../../components/charts/chartTheme';
import { Empty, ErrorBox, Kpi, KpiRow, KV, Loading, StatusBadge } from '../../components/common';
import {
  MAINTENANCE_CATEGORIES, PredictionStrip, SEVERITY, SiapAdvisoriesCard, SiapMark,
  fmtProb, fmtRul, fmtUsd, probTone, windowLabel,
} from '../../components/common/SiapAdvisories';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { useTheme } from '../../theme/ThemeProvider';
import { post } from '../../api/client';
import { dateShort, dayTime, hhmm, loadTone, num, pct } from '../../utils';
import { HealthBar, Sub, lineSeries } from './shared';

// The risk matrix uses a log-scaled probability axis.
ChartJS.register(LogarithmicScale);

const sevTone = (s) => SEVERITY[s]?.tone || 'neutral';
const SEV_LABEL = { critical: 'Critical', warning: 'Warning', info: 'Watch' };
const LOG_TICKS = { 0.1: '0.1%', 1: '1%', 10: '10%', 100: '100%' };
const AS_IS = 'chart-2';
const PLANNED = 'chart-1';

function SevBadge({ severity, wo }) {
  if (wo) return <Badge tone="info" dot>{wo.number} {wo.status.toUpperCase()}</Badge>;
  if (!severity) return <Badge tone="success" dot plain>LOW RISK</Badge>;
  return <Badge tone={sevTone(severity)} dot>{SEV_LABEL[severity].toUpperCase()}</Badge>;
}

function Prob({ p }) {
  return <span className="font-semibold tabular-nums" style={{ color: `var(--color-${probTone(p)})` }}>{fmtProb(p)}</span>;
}

/** Schedules the predictive work order for an advisory's window. */
function ScheduleButton({ advisoryId, window: w, label = 'Schedule', size = 'sm', onDone, disabled }) {
  const { act, can } = useApp();
  const [busy, setBusy] = useState(false);
  if (!can('maintenance.write')) return null;
  const run = async (e) => {
    e?.stopPropagation();
    setBusy(true);
    try {
      await act(() => post(`/api/predictive/advisories/${encodeURIComponent(advisoryId)}/schedule`, { start: w?.start, end: w?.end }),
        (r) => `${r.number} scheduled for ${r.machine} · ${dayTime(r.scheduled_start)}–${hhmm(r.scheduled_end)}`);
      onDone?.();
    } catch (err) { /* toast shown by act() */ } finally { setBusy(false); }
  };
  return <Button size={size} variant="accent" icon="calendar" disabled={busy || disabled || !w?.start} onClick={run}>{busy ? 'Scheduling…' : label}</Button>;
}

/** P10–P50–P90 remaining-life band on a log scale (1 h … 10,000 h). */
function RulBand({ low, mid, high, tone }) {
  const pos = (h) => Math.max(0, Math.min(100, (Math.log10(Math.max(1, h || 1)) / 4) * 100));
  const a = pos(low);
  const b = pos(high);
  return (
    <div className="relative h-3.5 w-full" title={`P10 ${num(low)} · P50 ${num(mid)} · P90 ${num(high)} operating hours`}>
      <div className="absolute inset-x-0 top-1/2 h-px" style={{ background: 'var(--color-border-strong)' }} />
      <div className="absolute rounded-sm" style={{ top: 3, bottom: 3, left: `${a}%`, width: `${Math.max(1.5, b - a)}%`, background: `var(--color-${tone}-soft)`, border: `1px solid var(--color-${tone}-border)` }} />
      <div className="absolute rounded-sm" style={{ top: 0, bottom: 0, width: 3, left: `calc(${pos(mid)}% - 1.5px)`, background: `var(--color-${tone})` }} />
    </div>
  );
}

function RulAxis() {
  return (
    <div className="flex justify-between text-[9px] text-fg-faint tabular-nums mt-1">
      {['1 h', '10 h', '100 h', '1,000 h', '10,000 h'].map((t) => <span key={t}>{t}</span>)}
    </div>
  );
}

// ─── risk matrix ─────────────────────────────────────────────────────
const GROUPS = [
  { key: 'critical', label: 'Critical', tone: 'danger' },
  { key: 'warning', label: 'Warning', tone: 'warning' },
  { key: 'info', label: 'Watch', tone: 'info' },
  { key: null, label: 'Low risk', tone: 'neutral' },
];

function RiskMatrix({ points, onSelect, className = '' }) {
  useTheme(); // re-read theme colours on toggle
  const yMax = Math.ceil((Math.max(100, ...points.map((p) => p.pillows_at_risk || 0)) * 1.15) / 50) * 50;
  const ring = cssVar('--color-surface');
  const datasets = GROUPS.map((g) => {
    const pts = points.filter((p) => (p.severity || null) === g.key);
    const c = resolveColor(g.tone);
    const r = (p) => 3.5 + Math.min(7, (p.downtime_hours || 0) * 1.1);
    return {
      label: g.label, color: g.tone, showLine: false, fill: false, borderColor: c,
      data: pts.map((p) => ({ x: Math.max(0.1, (p.p7d || 0) * 100), y: p.pillows_at_risk, p })),
      pointRadius: pts.map(r), pointHoverRadius: pts.map((p) => r(p) + 2), pointHitRadius: pts.map((p) => r(p) + 3),
      pointStyle: pts.map((p) => (p.scheduled ? 'rectRot' : 'circle')),
      pointBackgroundColor: withAlpha(c, g.key ? 0.9 : 0.5), pointBorderColor: ring, pointBorderWidth: 1.5,
    };
  });
  const guide = (x, tone) => ({
    label: `guide-${x}`, isGuide: true, color: tone, data: [{ x, y: 0 }, { x, y: yMax }], showLine: true, fill: false,
    borderDash: [4, 4], borderWidth: 1, pointRadius: 0, pointHitRadius: 0, pointHoverRadius: 0,
  });
  datasets.push(guide(10, 'warning'), guide(60, 'danger'));
  const sc = axisOptions({ grid: true });
  const title = (text) => ({ display: true, text, color: cssVar('--chart-tick'), font: { size: 9 } });
  const options = {
    interaction: { mode: 'nearest', intersect: true },
    plugins: {
      legend: { display: false },
      thresholdLines: { lines: [] },
      tooltip: tooltipOptions({
        filter: (item) => !item.dataset.isGuide,
        callbacks: {
          title: (items) => { const p = items[0]?.raw?.p; return p ? `${p.code} · ${p.name}` : ''; },
          label: (ctx) => {
            const p = ctx.raw.p;
            return [` ${p.line} · ${p.factory}`, ` 7-day failure probability ${fmtProb(p.p7d)}`,
              ` ${num(p.pillows_at_risk)} pillows · ${num(p.downtime_hours, 1)} h stop if it fails`, ` ${p.failure_mode}`];
          },
        },
      }),
    },
    scales: {
      x: { type: 'logarithmic', min: 0.1, max: 100, grid: { color: cssVar('--chart-grid') },
        ticks: { ...sc.x.ticks, autoSkip: false, maxRotation: 0, callback: (v) => LOG_TICKS[Number(Number(v).toPrecision(2))] || '' },
        title: title('7-day failure probability (log scale)') },
      y: { ...sc.y, max: yMax, title: title('Pillows at risk if it fails') },
    },
    onClick: (_e, els, chart) => {
      const el = els[0];
      const p = el && chart.data.datasets[el.datasetIndex].data[el.index]?.p;
      if (p) onSelect(p.machine_id);
    },
    onHover: (e, els) => { if (e.native?.target) e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
  };
  const counts = GROUPS.map((g) => points.filter((p) => (p.severity || null) === g.key).length);
  return (
    <Card className={className} title="Risk matrix" subtitle="Each machine: probability of failure within 7 days vs pillows lost if it fails · click a point for detail">
      <MiniChart type="line" height={350} data={{ datasets }} options={options} />
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[10.5px] text-fg-muted">
        {GROUPS.map((g, i) => (
          <span key={g.label} className="inline-flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: `var(--color-${g.tone})` }} />{g.label} ({counts[i]})
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rotate-45" style={{ background: 'var(--color-text-muted)' }} />Work order scheduled</span>
        <span className="text-fg-faint">Bubble size = line stop if it fails · dashed lines at 10% (warning) and 60% (critical)</span>
      </div>
    </Card>
  );
}

function RulRanking({ rows, onSelect }) {
  return (
    <Card title="Lowest remaining useful life" subtitle="P50 with P10–P90 band, operating hours" padded={false} className="h-full">
      {rows.length === 0 ? <Empty>No running machines in scope.</Empty> : (
        <div className="divide-y divide-line">
          {rows.map((r) => {
            const tone = r.severity ? sevTone(r.severity) : 'neutral';
            return (
              <button key={r.id} type="button" onClick={() => onSelect(r.id)} className="w-full text-left px-4 py-2 hover:bg-surface-raised">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-[12px] font-semibold text-fg truncate">{r.code} <span className="font-normal text-fg-faint">{r.name}</span></span>
                  <span className="text-[11.5px] font-bold tabular-nums whitespace-nowrap" style={{ color: `var(--color-${tone === 'neutral' ? 'text' : tone})` }}>{fmtRul(r.rul_hours)}</span>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <div className="flex-1"><RulBand low={r.rul_low_hours} mid={r.rul_hours} high={r.rul_high_hours} tone={tone} /></div>
                  {r.scheduled && <Icon name="calendar" size={11} className="text-info" />}
                </div>
                <p className="text-[10px] text-fg-faint truncate">{r.line} · {r.factory} · {r.failure_mode}</p>
              </button>
            );
          })}
          <div className="px-4 py-2"><RulAxis /></div>
        </div>
      )}
    </Card>
  );
}

function ForecastCard({ forecast, className }) {
  const [metric, setMetric] = useState('failures');
  const fields = metric === 'failures'
    ? ['expected_failures', 'expected_failures_planned', 'Expected failures']
    : ['expected_downtime_h', 'expected_downtime_planned_h', 'Expected line-stop hours'];
  const total = (k) => forecast.reduce((a, f) => a + f[k], 0);
  return (
    <ChartCard className={className} type="bar" legend height={300}
      title={`14-day forecast · ${fields[2].toLowerCase()} per day`}
      subtitle={`Sum over machines of P(first failure that day). As-is ${num(total(fields[0]), metric === 'failures' ? 2 : 1)} vs ${num(total(fields[1]), metric === 'failures' ? 2 : 1)} with the recommended + scheduled jobs (machine restored after its window).`}
      actions={<FilterChips options={[{ value: 'failures', label: 'Failures' }, { value: 'downtime', label: 'Downtime h' }]} value={metric} onChange={setMetric} />}
      data={{
        labels: forecast.map((f) => f.label),
        datasets: [
          { label: 'As-is (run to failure)', data: forecast.map((f) => f[fields[0]]), color: AS_IS },
          { label: 'With maintenance plan', data: forecast.map((f) => f[fields[1]]), color: PLANNED },
        ],
      }} />
  );
}

function ReliabilityCard({ rows }) {
  const since = rows[0]?.observed_since;
  return (
    <Card title="MTBF / MTTR by machine type" padded={false} className="h-full"
      subtitle={`MTBF: Weibull mean life (illustrative) over observed simulated breakdowns${since ? ` since ${dateShort(since)}` : ''}. MTTR: planned / unplanned.`}>
      <DataTable rowKey="type" rows={rows} columns={[
        { key: 'type', header: 'Type', strong: true, render: (r) => <span>{r.type}<span className="block text-[10px] text-fg-faint font-normal whitespace-nowrap">{r.machines} × · β {r.beta} · η {num(r.eta)} h</span></span> },
        { key: 'mtbf', header: 'MTBF', render: (r) => (
          <span className="tabular-nums whitespace-nowrap">{num(r.model_mtbf_h)} h<span className="block text-[10px] text-fg-faint">
            {r.observed_failures ? `${num(r.observed_mtbf_h)} h (${r.observed_failures} fail.)` : `none in ${num(r.observed_op_hours)} h`}</span></span>
        ) },
        { key: 'mttr', header: 'MTTR', render: (r) => (
          <span className="tabular-nums whitespace-nowrap">{num(r.model_mttr_planned_h, 1)} / {num(r.model_mttr_unplanned_h, 1)} h
            {r.observed_mttr_h ? <span className="block text-[10px] text-fg-faint">obs. {num(r.observed_mttr_h, 1)} h</span> : null}</span>
        ) },
      ]} />
    </Card>
  );
}

function PlanCard({ plan, onSelect, onDone }) {
  return (
    <Card padded={false}
      title={<span className="flex items-center gap-2">Maintenance plan <Badge tone="success">{fmtUsd(plan.loss_avoided_usd)} LOSS AVOIDABLE</Badge></span>}
      subtitle="Recommended predictive jobs in the cheapest window by expected loss, plus open work orders">
      <DataTable rowKey="advisory_id" rows={plan.jobs} onRowClick={(j) => onSelect(j.machine.id)}
        emptyText="No predictive jobs needed: every machine's run-to-failure expected loss is below the cost of intervening."
        columns={[
          { key: 'machine', header: 'Machine', strong: true, render: (j) => <span>{j.machine.code} <span className="font-normal text-fg-faint">{j.machine.line} · {j.factory}</span></span> },
          { key: 'mode', header: 'Failure mode · part', render: (j) => <span>{j.failure_mode}<span className="block text-[10px] text-fg-faint">{j.part}</span></span> },
          { key: 'window', header: 'Window', render: (j) => <span>{windowLabel(j.window) || '—'}<span className="block text-[10px] text-fg-faint">{j.window.label}</span></span> },
          { key: 'crew', header: 'Crew', render: (j) => `${j.crew} × ${num(j.hours, 1)} h` },
          { key: 'p7', header: 'P(7 d)', render: (j) => <Prob p={j.failure_prob_7d} /> },
          { key: 'avoid', header: 'Loss avoided', render: (j) => (j.work_order ? '—' : fmtUsd(j.loss_avoided_usd)) },
          { key: 'status', header: '', render: (j) => (j.work_order
            ? <StatusBadge status={j.work_order.status} />
            : <ScheduleButton advisoryId={j.advisory_id} window={j.window} onDone={onDone} />) },
        ]} />
    </Card>
  );
}

// ─── related predictions ─────────────────────────────────────────────
const RISK_TONE = { High: 'danger', Medium: 'warning', Low: 'success' };

function FillWeightCard({ rows, onSelect }) {
  const counts = rows.reduce((a, r) => { a[r.risk] = (a[r.risk] || 0) + 1; return a; }, {});
  return (
    <Card title="Fill-weight drift risk" padded={false}
      subtitle={`Filling Head pressure level & trend → under-fill risk (limit ${rows[0]?.limit_pct ?? -4}% pressure)`}
      actions={<span className="text-[10.5px] text-fg-faint">{counts.High || 0} high · {counts.Medium || 0} medium · {counts.Low || 0} low</span>}>
      {rows.length === 0 ? <Empty>No filling heads in scope.</Empty> : (
        <div className="divide-y divide-line">
          {rows.slice(0, 6).map((r) => (
            <button key={r.machine.id} type="button" onClick={() => onSelect(r.machine.id)} className="w-full text-left px-4 py-2 hover:bg-surface-raised flex items-center gap-3">
              <span className="min-w-0 flex-1">
                <span className="block text-[12px] font-semibold text-fg truncate">{r.machine.code} <span className="font-normal text-fg-faint">{r.machine.line} · {r.factory}</span></span>
                <span className="block text-[10px] text-fg-faint truncate">{r.sku || 'No SKU'} · {r.fill_g} g fill · est. drift {r.est_fill_drift_g > 0 ? '+' : ''}{num(r.est_fill_drift_g, 1)} g{r.hours_to_limit !== null ? ` · limit in ${num(r.hours_to_limit)} op h` : ''}</span>
              </span>
              <span className="text-right shrink-0">
                <span className="block text-[11.5px] font-semibold tabular-nums text-fg">{r.deviation_pct > 0 ? '+' : ''}{num(r.deviation_pct, 1)}%</span>
                <span className="block text-[9.5px] text-fg-faint tabular-nums">24 h → {r.projected_24h_pct > 0 ? '+' : ''}{num(r.projected_24h_pct, 1)}%</span>
              </span>
              <Badge tone={RISK_TONE[r.risk]}>{r.risk.toUpperCase()}</Badge>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

function EnergyCard({ energy, onSelect }) {
  return (
    <Card title="Energy / power anomaly" padded={false}
      subtitle={`Power above baseline +3% while running · tariff $${energy.tariff_usd_kwh}/kWh (illustrative)`}>
      <div className="grid grid-cols-3 gap-2 px-4 pt-3">
        <div className="tile"><p className="label-caps">Excess / day</p><p className="text-base font-bold text-fg tabular-nums">{num(energy.excess_kwh_day, 1)} <span className="text-[10px] font-normal text-fg-faint">kWh</span></p></div>
        <div className="tile"><p className="label-caps">30-day cost</p><p className="text-base font-bold text-fg tabular-nums">{fmtUsd(energy.cost_30d_usd)}</p></div>
        <div className="tile"><p className="label-caps">Anomalies</p><p className="text-base font-bold tabular-nums" style={{ color: `var(--color-${energy.anomalies ? 'warning' : 'success'})` }}>{energy.anomalies}</p></div>
      </div>
      {energy.machines.length === 0 ? <Empty>No machine is drawing excess power.</Empty> : (
        <div className="divide-y divide-line mt-2">
          {energy.machines.slice(0, 5).map((r) => (
            <button key={r.machine.id} type="button" onClick={() => onSelect(r.machine.id)} className="w-full text-left px-4 py-1.5 hover:bg-surface-raised flex items-center gap-3 text-[11.5px]">
              <span className="flex-1 min-w-0 truncate"><b className="text-fg">{r.machine.code}</b> <span className="text-fg-faint">{r.machine.name} · {r.factory}</span></span>
              <span className="tabular-nums text-fg-muted">{r.power_kw} / {r.baseline_kw} kW</span>
              <span className="tabular-nums font-semibold w-12 text-right" style={{ color: `var(--color-${r.anomaly ? 'warning' : 'text-muted'})` }}>+{num(r.deviation_pct, 1)}%</span>
              <span className="tabular-nums text-fg w-14 text-right">{fmtUsd(r.cost_30d_usd)}</span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

function SparesCard({ rows }) {
  return (
    <Card title="Spare-parts demand · next 30 days" padded={false}
      subtitle="Planned jobs + Σ Weibull failure probabilities; stock to hold for 90% service (Poisson)">
      <DataTable rowKey="part" rows={rows.slice(0, 8)} emptyText="No parts demand expected." columns={[
        { key: 'part', header: 'Part', strong: true, render: (r) => <span>{r.part}<span className="block text-[10px] text-fg-faint font-normal truncate max-w-[180px]">{r.machines.map((x) => x.code).join(', ') || '—'}</span></span> },
        { key: 'planned_qty', header: 'Planned', render: (r) => num(r.planned_qty) },
        { key: 'unplanned_expected', header: 'Unplanned (exp.)', render: (r) => num(r.unplanned_expected, 2) },
        { key: 'recommended_stock_p90', header: 'Hold (P90)', render: (r) => <b>{num(r.recommended_stock_p90)}</b> },
        { key: 'value_usd', header: 'Value', render: (r) => fmtUsd(r.value_usd) },
      ]} />
    </Card>
  );
}

function TechniciansCard({ techs }) {
  useTheme();
  const night = techs.after_hours_tonight;
  const week = techs.week;
  return (
    <Card title="Maintenance technician workload" subtitle={techs.assumption}>
      <div className="space-y-2">
        {techs.shifts.filter((s) => !s.past).map((s) => (
          <div key={`${s.factory}-${s.shift}`}>
            <div className="flex justify-between text-[11px] mb-1 gap-2">
              <span className="text-fg-muted">{s.factory} · shift {s.shift} · {s.present}/{s.rostered} present</span>
              <span className="font-semibold text-fg tabular-nums">{num(s.demand_h, 1)} / {num(s.capacity_h, 1)} h{s.load_pct !== null ? ` · ${pct(s.load_pct, 0)}` : ''}</span>
            </div>
            <ProgressBar value={s.demand_h} max={s.capacity_h || 1} tone={s.load_pct === null ? 'neutral' : loadTone(s.load_pct, 70, 100)} />
          </div>
        ))}
      </div>
      <div className="tile mt-3 text-[11px] flex items-center gap-2">
        <Icon name="clock" size={13} className="text-accent" />
        {night.jobs
          ? <span className="text-fg">Tonight {dayTime(night.start)}–{hhmm(night.end)}: <b>{night.jobs} job(s)</b>, {num(night.technician_hours, 1)} technician-h, crew of {night.crew_needed} on overtime.</span>
          : <span className="text-fg-muted">No after-hours jobs planned tonight.</span>}
      </div>
      <p className="label-caps mt-3 mb-1">Next 7 days · hours</p>
      <MiniChart type="bar" stacked legend height={150} data={{
        labels: week.map((d) => d.label),
        datasets: [
          { label: 'Predictive (in shift)', data: week.map((d) => d.planned_h), color: 'chart-1' },
          { label: 'After-hours window', data: week.map((d) => d.after_hours_h), color: 'chart-5' },
          { label: 'Expected reactive', data: week.map((d) => d.reactive_expected_h), color: 'chart-8' },
          lineSeries('Available capacity', week.map((d) => d.capacity_h), 'chart-3', { stack: 'capacity', pointRadius: 0, borderDash: [5, 4] }),
        ],
      }} />
    </Card>
  );
}

function MachineTable({ rows, onSelect }) {
  const [view, setView] = useState('risk');
  const shown = view === 'risk' ? rows.filter((r) => r.severity || r.work_order || !r.available) : rows;
  return (
    <Card padded={false} title="Machine failure predictions"
      subtitle="Conditional probability of failure given current condition; RUL in operating hours (15.3 h per production day)"
      actions={<FilterChips options={[{ value: 'risk', label: 'Needs attention' }, { value: 'all', label: `All (${rows.length})` }]} value={view} onChange={setView} />}>
      <DataTable rowKey="machine_id" rows={shown} onRowClick={(r) => onSelect(r.machine_id)}
        emptyText="No machine needs attention — switch to All to see every prediction."
        columns={[
          { key: 'code', header: 'Machine', strong: true, render: (r) => <span>{r.code}<span className="block text-[10px] text-fg-faint font-normal">{r.name}</span></span> },
          { key: 'line', header: 'Line', render: (r) => <span>{r.line}<span className="block text-[10px] text-fg-faint">{r.factory}</span></span> },
          { key: 'health', header: 'Health', render: (r) => <HealthBar score={r.health_score} width={80} /> },
          { key: 'p24', header: 'P(24 h)', render: (r) => (r.available ? <Prob p={r.failure_prob_24h} /> : '—') },
          { key: 'p7', header: 'P(7 d)', render: (r) => (r.available ? <Prob p={r.failure_prob_7d} /> : '—') },
          { key: 'rul', header: 'RUL (P50)', render: (r) => (r.available
            ? <span className="tabular-nums">{fmtRul(r.rul_hours)}<span className="block text-[10px] text-fg-faint">{num(r.rul_low_hours)}–{num(r.rul_high_hours)} op h</span></span>
            : <StatusBadge status={r.status} />) },
          { key: 'mode', header: 'Likely failure mode', render: (r) => <span className="text-[11px]">{r.failure_mode || '—'}</span> },
          { key: 'sev', header: 'S!AP', render: (r) => <SevBadge severity={r.severity} wo={r.work_order} /> },
          { key: 'window', header: 'Window', render: (r) => (r.available && r.severity && !r.work_order && r.recommended ? <span className="text-[11px]">{windowLabel(r.window)}</span> : <span className="text-fg-faint">—</span>) },
        ]} />
    </Card>
  );
}

function ModelModal({ model, onClose }) {
  return (
    <Modal eyebrow="S!AP · model card" title={model.name} width={720} onClose={onClose}>
      <div className="space-y-4 text-[11.5px]">
        <p className="text-fg-muted leading-relaxed"><code className="text-fg">{model.formula}</code>. Calendar horizons are converted to operating hours with the shift calendar ({model.op_hours_per_day} h per production day; nothing runs 23:00–06:00). The vibration trend to the alarm threshold ({model.vibration_alarm_x_baseline}× baseline) overrides the Weibull median when it is more conservative.</p>
        <div>
          <p className="label-caps mb-1">Weibull parameters (illustrative)</p>
          <DataTable rowKey="type" rows={Object.entries(model.weibull).map(([type, v]) => ({ type, ...v }))} columns={[
            { key: 'type', header: 'Machine type', strong: true },
            { key: 'beta', header: 'Shape β' },
            { key: 'eta', header: 'Scale η (op h)', render: (r) => num(r.eta) },
            { key: 'mean_life_h', header: 'Mean life', render: (r) => `${num(r.mean_life_h)} h` },
            { key: 'dominant_failure', header: 'Dominant failures' },
          ]} />
        </div>
        <div>
          <p className="label-caps mb-1">Condition covariates · M = exp(Σ γ·x)</p>
          <DataTable rowKey="k" rows={Object.entries(model.covariates).map(([k, v]) => ({ k, ...v }))} columns={[
            { key: 'k', header: 'Covariate', strong: true },
            { key: 'gamma', header: 'γ' },
            { key: 'x', header: 'x (0 at baseline)' },
          ]} />
        </div>
        <div>
          <p className="label-caps mb-1">Cost & planning assumptions</p>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {Object.entries(model.assumptions).map(([k, v]) => (
              <div key={k} className="tile"><p className="label-caps" style={{ fontSize: 9 }}>{k.replace(/_/g, ' ')}</p><p className="font-semibold text-fg tabular-nums">{typeof v === 'number' ? num(v, v % 1 ? 2 : 0) : String(v)}</p></div>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  );
}

// ─── machine drawer ──────────────────────────────────────────────────
const MODE_METRIC = { seal: 'pressure', air: 'pressure', overheat: 'temperature', card: 'power', overload: 'power', belt: 'rpm' };

function telemetryScales(extraY = {}) {
  const sc = axisOptions({ grid: true });
  return { x: { ...sc.x, ticks: { ...sc.x.ticks, maxTicksLimit: 7, maxRotation: 0 } }, y: { ...sc.y, beginAtZero: false, ...extraY } };
}

function TrendChart({ tel, metric, threshold, thresholdLabel, projection }) {
  useTheme();
  const pts = tel.points.filter((p) => p.running);
  const proj = metric === 'vibration' ? projection || [] : [];
  const labels = [...pts.map((p) => dayTime(p.ts)), ...proj.slice(1).map((p) => dayTime(p.ts))];
  const n = pts.length;
  const base = tel.baseline?.[metric];
  const values = [...pts.map((p) => p[metric]), ...proj.map((p) => p[metric])].filter((v) => v !== null && v !== undefined);
  const lo = Math.min(...values, base ?? Infinity, threshold ?? Infinity);
  const hi = Math.max(...values, base ?? -Infinity, threshold ?? -Infinity);
  const pad = (hi - lo) * 0.08 || 1;
  const datasets = [
    { label: 'Running telemetry', data: [...pts.map((p) => p[metric]), ...proj.slice(1).map(() => null)], color: 'chart-1', fill: false, borderWidth: 1.6 },
    { label: 'Baseline', data: labels.map(() => base), color: 'chart-8', fill: false, borderDash: [5, 4], borderWidth: 1.2 },
  ];
  if (proj.length > 1) {
    datasets.push({ label: 'Projected trend', data: [...pts.map((_, i) => (i === n - 1 ? proj[0][metric] : null)), ...proj.slice(1).map((p) => p[metric])],
      color: 'chart-1', fill: false, borderDash: [3, 3], borderWidth: 1.6, spanGaps: true });
  }
  if (!pts.length) return <Empty icon="info">No running telemetry in the last 48 h.</Empty>;
  return (
    <>
      <MiniChart type="line" height={170} legend thresholds={threshold ? [{ value: threshold, tone: 'danger' }] : undefined}
        options={{ scales: telemetryScales({ min: lo - pad, max: hi + pad }) }} data={{ labels, datasets }} />
      {threshold && <p className="text-[10px] mt-1 flex items-center gap-1.5"><span className="w-4 border-t-2 border-dashed" style={{ borderColor: 'var(--color-danger)' }} /><span className="text-fg-muted">{thresholdLabel} {num(threshold, 2)} {tel.units?.[metric]}</span></p>}
    </>
  );
}

function CovariateBars({ covariates, mult, condMult }) {
  const maxLog = Math.max(1, ...covariates.map((c) => Math.log(c.factor || 1)));
  return (
    <div className="space-y-1.5">
      {covariates.map((c) => (
        <div key={c.key} className="grid items-center gap-2 text-[11px]" style={{ gridTemplateColumns: '110px 1fr 64px' }}>
          <span className="text-fg-muted truncate" title={c.definition}>{c.label} <span className="text-fg-faint">x={num(c.x, 2)}</span></span>
          <ProgressBar value={Math.log(c.factor || 1)} max={maxLog} tone={c.factor >= 5 ? 'danger' : c.factor >= 1.5 ? 'warning' : 'neutral'} />
          <span className="text-right font-semibold tabular-nums text-fg">×{num(c.factor, c.factor < 10 ? 2 : 0)}</span>
        </div>
      ))}
      <p className="text-[10.5px] text-fg-faint">Condition multiplier ×{num(condMult, 1)}{mult > condMult * 1.001 ? `, raised to ×${num(mult, 1)} so the median matches the vibration trend` : ''} (γ per covariate in the model card).</p>
    </div>
  );
}

export function PredictiveDrawer({ id, onClose, onOpenHealth }) {
  const { can } = useApp();
  const { data, loading, error, reload } = useApi(`/api/predictive/machines/${id}`, {}, { every: 4 });
  const [pick, setPick] = useState(null);
  const mc = data?.machine;
  const tel = data?.telemetry;
  const options = mc?.options || [];
  const chosen = options.find((o) => o.key === pick) || options.find((o) => o.recommended && o.key !== 'rtf') || options.find((o) => o.key === 'night');
  const curve = data?.curve || [];
  const hasPlan = curve.some((c) => c.p_fail_planned !== null && c.p_fail_planned !== undefined);
  const modeMetric = MODE_METRIC[mc?.failure_mode_key] || 'temperature';
  const thrFor = { pressure: [tel?.thresholds.pressure_underfill, 'Under-fill limit'], temperature: [tel?.thresholds.temperature_warning, 'Temperature tolerance'] };
  return (
    <Drawer eyebrow={<SiapMark size={11}>predictive maintenance</SiapMark>} width={800} onClose={onClose}
      title={mc ? `${mc.code} · ${mc.name}` : 'Loading…'}
      subtitle={mc ? `${mc.line} · ${mc.factory} · ${mc.stage} · ${num(mc.age_hours)} op h since service` : undefined}
      headerExtra={mc && <SevBadge severity={mc.severity} wo={mc.work_order} />}
      footer={mc && (
        <>
          <span className="text-[10.5px] text-fg-faint mr-auto leading-snug">{data.disclaimer}</span>
          {onOpenHealth && <Button size="sm" variant="secondary" icon="gauge" onClick={() => onOpenHealth(mc.machine_id)}>Machine health</Button>}
          {mc.available && !mc.work_order && can('maintenance.write') && chosen && chosen.key !== 'rtf' && (
            <ScheduleButton advisoryId={data.advisory_id} window={chosen} label={`Schedule ${dayTime(chosen.start)}`} onDone={reload} />
          )}
        </>
      )}>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <Loading rows={8} />}
      {mc && !mc.available && (
        <div className="tile tone-danger text-xs">{mc.code} is in {mc.status.toLowerCase()}{mc.unavailable_until ? ` until ${dayTime(mc.unavailable_until)}` : ''}. Predictions resume once it is back in service.</div>
      )}
      {mc && mc.available && (
        <>
          <PredictionStrip p={{ ...mc }} />
          <p className="text-[11px] text-fg-muted mt-2">
            72 h: <Prob p={mc.failure_prob_72h} /> · 30 d: <Prob p={mc.failure_prob_30d} /> · expected failure (P50) {mc.expected_failure_at ? dayTime(mc.expected_failure_at) : 'beyond 30 days'}
            {' '}· RUL by <b className="text-fg">{mc.method?.toLowerCase()}</b> (Weibull {num(mc.weibull_rul_hours)} h{mc.trend_hours_to_alarm !== null ? `, trend ${num(mc.trend_hours_to_alarm)} h` : ''}) · confidence {Math.round(mc.confidence * 100)}%
          </p>
          {mc.work_order && (
            <div className="tile tone-info text-[11px] mt-3">
              <b>{mc.work_order.number}</b> {mc.work_order.status.toLowerCase()} · {windowLabel({ start: mc.work_order.scheduled_start, end: mc.work_order.scheduled_end }) || 'no window'} · {fmtProb(mc.p_fail_before_work_order)} chance of failure before it starts.
            </div>
          )}

          <Sub>Cumulative failure probability · next 14 days</Sub>
          <MiniChart type="line" height={190} legend yMax={100}
            options={{ scales: telemetryScales({ beginAtZero: true, min: 0, max: 100, ticks: { ...axisOptions().y.ticks, callback: (v) => `${v}%` } }) }}
            data={{
              labels: curve.map((c) => dayTime(c.ts)),
              datasets: [
                { label: 'As-is (run to failure)', data: curve.map((c) => +(c.p_fail * 100).toFixed(1)), color: AS_IS, fill: false, borderWidth: 2 },
                ...(hasPlan ? [{ label: mc.work_order ? 'With scheduled work order' : 'With recommended window', data: curve.map((c) => +(c.p_fail_planned * 100).toFixed(1)), color: PLANNED, fill: false, borderWidth: 2, borderDash: [5, 3] }] : []),
              ],
            }} />

          <div className="grid md:grid-cols-2 gap-3 mt-1">
            <div>
              <Sub>Vibration vs alarm threshold</Sub>
              <TrendChart tel={tel} metric="vibration" threshold={tel.thresholds.vibration_alarm} thresholdLabel="Alarm threshold" projection={tel.projection} />
              {tel.trend && <p className="text-[10.5px] text-fg-faint mt-1">Trend {tel.trend.slope_per_op_h >= 0 ? '+' : ''}{tel.trend.slope_per_op_h} mm/s per op h · R² {tel.trend.r2}{tel.trend.hours_to_alarm !== null ? ` · alarm in ≈${num(tel.trend.hours_to_alarm)} op h` : ' · no significant rise'}</p>}
            </div>
            <div>
              <Sub>{modeMetric[0].toUpperCase() + modeMetric.slice(1)} ({tel.units?.[modeMetric]})</Sub>
              <TrendChart tel={tel} metric={modeMetric} threshold={thrFor[modeMetric]?.[0]} thresholdLabel={thrFor[modeMetric]?.[1]} />
              {data.fill_weight && <p className="text-[10.5px] text-fg-faint mt-1">Fill-weight drift est. {num(data.fill_weight.est_fill_drift_g, 1)} g · risk {data.fill_weight.risk}</p>}
            </div>
          </div>

          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <Sub>Why: hazard contributions</Sub>
              <CovariateBars covariates={mc.covariates} mult={mc.hazard_multiplier} condMult={mc.condition_multiplier} />
            </div>
            <div>
              <Sub>Failure mode & repair</Sub>
              <KV cols={2} items={[
                ['Likely failure mode', mc.failure_mode], ['Part', mc.repair.part],
                ['Crew', `${mc.repair.crew} technician${mc.repair.crew > 1 ? 's' : ''}`],
                ['MTTR planned / unplanned', `${num(mc.repair.planned_mttr_h, 1)} / ${num(mc.repair.unplanned_mttr_h, 1)} h`],
              ]} />
              <p className="text-[11px] text-fg mt-2"><b>Repair:</b> {mc.repair.repair} · part cost {fmtUsd(mc.repair.part_cost)}</p>
              <p className="text-[10.5px] text-fg-faint mt-0.5">Signature: {mc.mode_reason}</p>
            </div>
          </div>

          <Sub right={<span className="text-[10px] text-fg-faint">Expected loss = P(fail before) × run-to-failure + (1 − P) × planned cost</span>}>Maintenance window options</Sub>
          <DataTable rowKey="key" rows={options} onRowClick={(o) => o.key !== 'rtf' && setPick(o.key)} columns={[
            { key: 'label', header: 'Option', render: (o) => (
              <span className="flex items-center gap-2">
                {o.key !== 'rtf' && <span className="inline-block w-3 h-3 rounded-full border" style={{ borderColor: 'var(--color-primary)', background: chosen?.key === o.key ? 'var(--color-primary)' : 'transparent' }} />}
                <span className="font-semibold text-fg">{o.label}</span>
                {o.recommended && <Badge tone="accent">S!AP PICK</Badge>}
              </span>
            ) },
            { key: 'window', header: 'Window', render: (o) => <span className="whitespace-nowrap">{o.start ? windowLabel(o) : 'next 7 days'}</span> },
            { key: 'p', header: 'P(fail before)', render: (o) => <Prob p={o.p_fail_before} /> },
            { key: 'lost', header: 'Pillows lost', render: (o) => num(o.pillows_lost) },
            { key: 'loss', header: 'Expected loss', render: (o) => <b className="tabular-nums">{fmtUsd(o.expected_loss_usd)}</b> },
          ]} />

          <Sub>Impact if it runs to failure</Sub>
          <KV cols={4} items={[
            ['Pillows at risk', num(mc.impact.pillows_at_risk)], ['Line stop', `${num(mc.impact.downtime_hours, 1)} h`],
            ['Cost (illustrative)', fmtUsd(mc.impact.cost_usd)], ['Line rate', `${num(mc.impact.line_rate_per_h)} /h`],
          ]} />
          {mc.impact.orders.length > 0 && (
            <ul className="mt-2 space-y-1">
              {mc.impact.orders.map((o) => (
                <li key={o.po} className="text-[11px] flex items-center gap-2">
                  <b className="text-fg">{o.number}</b><span className="text-fg-muted">{o.customer} · {o.sku} · {num(o.remaining)} remaining · due {dayTime(o.due)}</span>
                  {o.late_if_failure && <Badge tone="danger">LATE IF IT FAILS</Badge>}
                </li>
              ))}
            </ul>
          )}

          <Sub>Evidence</Sub>
          <ul className="space-y-1 text-[11px] text-fg-muted list-disc pl-4">
            {mc.evidence.map((e, i) => <li key={i}>{e}</li>)}
          </ul>

          {data.work_orders?.length > 0 && (
            <>
              <Sub>Work orders</Sub>
              <ul className="space-y-1">
                {data.work_orders.map((w) => (
                  <li key={w.id} className="text-[11px] flex items-center gap-2"><b className="text-fg">{w.number}</b><StatusBadge status={w.status} /><span className="text-fg-muted">{w.type} · {windowLabel({ start: w.scheduled_start, end: w.scheduled_end }) || dayTime(w.created_at)}</span></li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </Drawer>
  );
}

// ─── tab ──────────────────────────────────────────────────────────────
export default function PredictiveTab({ factory, onOpenMachine }) {
  const { data, loading, error, reload } = useApi('/api/predictive/machines', { factory }, { every: 4 });
  const [sel, setSel] = useState(null);
  const [showModel, setShowModel] = useState(false);
  if (loading && !data) return <Loading rows={6} height={60} />;
  if (error && !data) return <ErrorBox error={error} onRetry={reload} />;
  const k = data.kpis;
  const f = data.fleet;
  const r = data.related;
  const openHealth = onOpenMachine ? (id) => { setSel(null); onOpenMachine(id); } : undefined;
  return (
    <div className="space-y-4">
      <div className="tile flex items-center gap-3 flex-wrap" style={{ background: 'var(--color-accent-soft)', borderColor: 'var(--color-accent-border)' }}>
        <SiapMark size={15}>Predictive maintenance</SiapMark>
        <span className="text-[11px] text-fg-muted flex-1 min-w-[240px]">{data.disclaimer}</span>
        <Badge tone="warning" dot>SIMULATED MODEL</Badge>
        <Button size="sm" variant="secondary" icon="info" onClick={() => setShowModel(true)}>Model card</Button>
      </div>

      <KpiRow cols={5}>
        <Kpi label="Machines at risk · 7 d" icon="alert" value={k.at_risk_7d} unit={`of ${k.available} running`}
          tone={k.critical ? 'danger' : k.at_risk_7d ? 'warning' : 'success'}
          sub={`${k.critical} critical · ${k.warning} warning · ${k.info} watch${k.unavailable ? ` · ${k.unavailable} down` : ''}`} />
        <Kpi label="Expected failures · 7 d" icon="trendUp" value={num(k.expected_failures_7d, 2)}
          tone={k.expected_failures_7d >= 1 ? 'warning' : undefined}
          sub={`${num(k.expected_failures_7d_planned, 2)} with the maintenance plan`} />
        <Kpi label="Pillows at risk" icon="factory" value={num(k.pillows_at_risk)} tone={k.pillows_at_risk ? 'danger' : 'success'}
          sub={`If at-risk machines fail · ≈${num(k.expected_pillows_lost_7d)} expected loss over 7 d (all machines)`} />
        <Kpi label="Loss avoidable" icon="dollar" value={fmtUsd(k.loss_avoidable_usd)} tone={k.loss_avoidable_usd ? 'success' : undefined}
          sub={`${k.recommended_jobs} recommended job(s) in planned windows · ${k.scheduled_jobs} scheduled`} />
        <Kpi label="Technician load" icon="users" value={pct(k.technician_peak_load_pct, 0)} tone={loadTone(k.technician_peak_load_pct, 70, 100)}
          sub={`Peak remaining shift today · ${num(k.technician_hours_planned, 1)} technician-h of predictive work`} />
      </KpiRow>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <RiskMatrix className="xl:col-span-2" points={f.risk_matrix} onSelect={setSel} />
        <RulRanking rows={f.lowest_rul.slice(0, 6)} onSelect={setSel} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <ForecastCard className="xl:col-span-2" forecast={f.forecast} />
        <ReliabilityCard rows={f.reliability} />
      </div>

      <PlanCard plan={f.plan} onSelect={setSel} onDone={reload} />

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
        <SiapAdvisoriesCard className="xl:col-span-2" factory={factory} category={MAINTENANCE_CATEGORIES} limit={4}
          title="Maintenance advisories" onOpenMachine={(id) => setSel(id)} />
        <TechniciansCard techs={r.technicians} />
      </div>

      <div>
        <p className="label-caps mb-2 flex items-center gap-1.5"><Icon name="sparkles" size={12} className="text-accent" />Related predictions</p>
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4 items-start">
          <FillWeightCard rows={r.fill_weight} onSelect={setSel} />
          <EnergyCard energy={r.energy} onSelect={setSel} />
          <SparesCard rows={r.spares} />
        </div>
      </div>

      <MachineTable rows={data.machines} onSelect={setSel} />

      {sel && <PredictiveDrawer id={sel} onClose={() => setSel(null)} onOpenHealth={openHealth} />}
      {showModel && <ModelModal model={data.model} onClose={() => setShowModel(false)} />}
    </div>
  );
}
