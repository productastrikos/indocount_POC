import React, { useEffect, useMemo, useState } from 'react';
import { Badge, Card, FilterChips, Icon, Tabs } from '../components/ui';
import { BarRow, Empty, ErrorBox, Kpi, KpiRow, Loading, PageTitle } from '../components/common';
import { SiapAdvisoryList } from '../components/common/SiapAdvisories';
import ChartCard from '../components/charts/ChartCard';
import CctvWall from '../components/cctv/CctvWall';
import LossIncidentDrawer from '../components/loss/LossIncidentDrawer';
import CaseRow from '../components/loss/CaseRow';
import { OUTCOME_HELP, OUTCOME_TONE, STAGE_LABEL, usd } from '../components/loss/lossShared';
import { useApi, useFocus } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { dateShort } from '../utils';

const TABS = [
  { value: 'queue', label: 'Case queue' },
  { value: 'patterns', label: 'Patterns & hotspots' },
  { value: 'detection', label: 'Detection & playbooks' },
];
const STATUS_OPTS = [
  { value: 'active', label: 'Active' }, { value: 'Open', label: 'Open' }, { value: 'Investigating', label: 'Investigating' },
  { value: 'Resolved', label: 'Closed' }, { value: '', label: 'All' },
];
const FLOW = [
  ['Detect', 'RFID portals, door readers, counts, weighbridge, vision raise a signal'],
  ['Triage', 'S!AP opens a case with the evidence, value at standard cost and an owner'],
  ['Investigate', 'The owner works the playbook: recount, footage, logs, seals'],
  ['Close', 'Outcome recorded: confirmed loss, recovered, process error or false alarm'],
];

function Queue({ factory, kinds, kind, setKind, zone, setZone, onOpen }) {
  const [status, setStatus] = useState('active');
  const [stage, setStage] = useState('');
  const { data, loading, error, reload } = useApi('/api/loss/incidents', { factory, status: status || undefined, stage: stage || undefined, kind: kind || undefined }, { every: 3 });
  const rows = (data?.incidents || []).filter((c) => !zone || c.zone === zone);
  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <Card className="xl:col-span-2" padded={false}
        title={`Cases${data ? ` · ${rows.length}` : ''}`}
        subtitle="Active first, by severity. Open a case to see the evidence, work the playbook and close it with an outcome.">
        <div className="px-4 py-3 space-y-2 border-b border-line">
          <FilterChips label="Status" options={STATUS_OPTS} value={status} onChange={setStatus} />
          <div className="flex flex-wrap gap-2 items-center">
            <select className="select select-sm" value={stage} onChange={(e) => setStage(e.target.value)} aria-label="Stage">
              <option value="">All stages</option>
              {Object.entries(STAGE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select className="select select-sm" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Signal">
              <option value="">All signals</option>
              {Object.entries(kinds || {}).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
            {zone && <button type="button" className="chip is-active" onClick={() => setZone('')}>{zone} ✕</button>}
          </div>
        </div>
        <ErrorBox error={error} onRetry={reload} />
        {loading && !data && <div className="p-4"><Loading rows={6} /></div>}
        {data && !rows.length && <Empty icon="shield">No cases match these filters.</Empty>}
        <div className="divide-y divide-line">
          {rows.map((c) => <CaseRow key={c.id} c={c} now={data.sim_time} onOpen={onOpen} showPlant={factory === 'ALL'} />)}
        </div>
      </Card>
      <div className="space-y-4">
        <Card title="How a case is handled" subtitle="Every signal becomes a case with an owner and a playbook">
          <ol className="space-y-2.5">
            {FLOW.map(([t, d], i) => (
              <li key={t} className="flex gap-2.5">
                <span className="w-6 h-6 rounded-full grid place-items-center text-[11px] font-bold shrink-0 bg-primary text-white">{i + 1}</span>
                <span><b className="text-[12px] text-fg">{t}</b><span className="block text-[11px] text-fg-muted leading-snug">{d}</span></span>
              </li>
            ))}
          </ol>
        </Card>
        <Card title="Outcomes">
          <div className="space-y-2">
            {Object.entries(OUTCOME_HELP).map(([o, h]) => (
              <div key={o} className="flex gap-2 items-start">
                <Badge tone={OUTCOME_TONE[o]} dot>{o}</Badge>
                <span className="text-[11px] text-fg-muted leading-snug flex-1">{h}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

function Patterns({ factory, ov, onKind, onZone }) {
  const maxKind = Math.max(1, ...ov.by_kind.map((k) => k.count));
  const maxZone = Math.max(1, ...ov.by_zone.map((z) => z.value));
  const outcomeData = {
    labels: Object.keys(ov.outcomes),
    datasets: [{ label: 'Closed cases', data: Object.values(ov.outcomes), color: ['danger', 'success', 'info', '#94a3b8'] }],
  };
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <ChartCard title="Cases per day" subtitle="Last 7 days incl. today, all signals" type="bar" height={200}
          data={{ labels: ov.trend.map((t) => dateShort(t.date)), datasets: [{ label: 'Cases', data: ov.trend.map((t) => t.cases) }] }} />
        <ChartCard title="Confirmed loss per day" subtitle="Standard cost of material written off" type="bar" height={200}
          data={{ labels: ov.trend.map((t) => dateShort(t.date)), datasets: [{ label: 'Confirmed loss ($)', data: ov.trend.map((t) => t.confirmed_value), color: 'danger' }] }} />
        <ChartCard title="How closed cases ended" subtitle="Cases closed in the last 7 days incl. today" type="doughnut" height={200} legend data={outcomeData} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="Cases by signal" subtitle="Last 7 days incl. today · click to see the cases">
          <div className="space-y-2.5">
            {ov.by_kind.filter((k) => k.count).sort((a, b) => b.count - a.count).map((k) => (
              <BarRow key={k.kind} label={k.label} value={k.count} max={maxKind} tone={k.confirmed_value ? 'danger' : 'primary'}
                display={`${k.count} case${k.count === 1 ? '' : 's'}${k.active ? ` · ${k.active} active` : ''}`}
                sub={k.confirmed_value ? `${usd(k.confirmed_value)} confirmed loss` : 'no confirmed loss'} onClick={() => onKind(k.kind)} />
            ))}
          </div>
        </Card>
        <Card title="Hotspots" subtitle="Where cases are raised, by value at standard cost · click to filter">
          <div className="space-y-2.5">
            {ov.by_zone.map((z) => (
              <BarRow key={z.zone} label={z.zone} value={z.value} max={maxZone} tone={z.active ? 'warning' : 'primary'}
                display={`${usd(z.value)} · ${z.count} case${z.count === 1 ? '' : 's'}`} sub={z.active ? `${z.active} active` : undefined}
                onClick={() => onZone(z.zone)} />
            ))}
          </div>
        </Card>
      </div>
      <Card title={<span className="flex items-center gap-1.5 text-accent"><Icon name="sparkles" size={14} />S!AP · loss-prevention patterns</span>}
        subtitle="Repeat signals and hotspots across cases, with the control change that would stop them"
        style={{ borderColor: 'var(--color-accent-border)' }}>
        <SiapAdvisoryList factory={factory} category="Loss prevention" limit={6} compact showSummary={false}
          emptyText="No repeat patterns in the last 7 days." />
      </Card>
    </div>
  );
}

function Detection({ factory, ov, onKind }) {
  const count = Object.fromEntries(ov.by_kind.map((k) => [k.kind, k]));
  return (
    <div className="space-y-4">
      <Card title="Controls in place" subtitle="The sensors and procedures that raise and settle loss-prevention cases">
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2.5">
          {ov.controls.map((c) => (
            <div key={c.name} className="tile">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[12px] font-semibold text-fg">{c.name}</span>
                <Badge tone="success" dot>{c.status}</Badge>
              </div>
              <p className="text-[11px] text-fg-muted mt-1 leading-snug">{c.detail}</p>
            </div>
          ))}
        </div>
      </Card>
      <Card title="Signals and handling playbooks" subtitle="What each signal detects, who owns it, the checks before a case can be closed">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {Object.entries(ov.kinds).map(([key, k]) => (
            <div key={key} className="tile">
              <div className="flex items-start gap-2.5">
                <span className="w-8 h-8 rounded-lg grid place-items-center shrink-0 bg-surface text-fg-muted"><Icon name={k.icon} size={15} /></span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12.5px] font-semibold text-fg">{k.label}</p>
                  <p className="text-[11px] text-fg-muted">{k.channel} · {STAGE_LABEL[k.stage]} · owner {k.owner}</p>
                </div>
                <button type="button" className="link-btn shrink-0" onClick={() => onKind(key)}>
                  {count[key]?.count || 0} CASES / 7 D
                </button>
              </div>
              <ol className="mt-2 space-y-1 text-[11.5px] text-fg list-decimal ml-5">
                {k.playbook.map((s) => <li key={s.key}>{s.text}</li>)}
              </ol>
              <div className="flex flex-wrap gap-1 mt-2">
                {k.outcomes.map((o) => <Badge key={o} tone={OUTCOME_TONE[o]}>{o}</Badge>)}
              </div>
            </div>
          ))}
        </div>
      </Card>
      <Card title="CCTV · badge-only zones" padded
        subtitle="Cameras that watch finished-goods aisles and staging lanes. An entry without an RFID badge read raises an access check and the clip is retained as evidence.">
        <CctvWall factory={factory} filter={(f) => (f.summary?.badge_checks || 0) > 0} cols={4}
          emptyText="No camera with a badge-only zone at this plant." />
        <p className="text-[11px] text-fg-faint mt-3 flex items-start gap-1.5">
          <Icon name="info" size={12} className="mt-0.5 shrink-0" />
          Vision does not identify, name or accuse people in the footage. A no-badge entry is checked by security against the badge logs and
          the shift roster; most turn out to be a badge not worn or an authorised visitor.
        </p>
      </Card>
    </div>
  );
}

/**
 * Loss Prevention: material pilferage across the plant — how it is detected, how each case is
 * worked to an outcome and where losses concentrate.
 */
export default function LossPrevention() {
  const { filters } = useApp();
  const factory = filters.factory;
  const focus = useFocus();
  const [tab, setTab] = useState('queue');
  const [openId, setOpenId] = useState(null);
  const [kind, setKind] = useState('');
  const [zone, setZone] = useState('');
  const { data: ov, loading, error, reload } = useApi('/api/loss/overview', { factory }, { every: 4 });

  useEffect(() => {
    if (!focus) return;
    if (focus.type === 'loss_incident' && focus.id) { setOpenId(focus.id); setTab('queue'); }
    if (focus.type === 'loss_kind' && focus.id) { setKind(String(focus.id)); setTab('queue'); }
    if (focus.type === 'loss_zone' && focus.id) { setZone(decodeURIComponent(String(focus.id))); setTab('queue'); }
  }, [focus]);

  const k = ov?.kpis;
  const kpis = useMemo(() => (k ? [
    { label: 'Active cases', value: k.active, sub: `${k.open} open · ${k.investigating} investigating`, tone: k.critical_active ? 'danger' : undefined, icon: 'shield' },
    { label: 'Value at risk', value: usd(k.value_at_risk), sub: `${k.critical_active} critical case${k.critical_active === 1 ? '' : 's'} active`, tone: k.critical_active ? 'warning' : undefined, icon: 'dollar' },
    { label: 'Confirmed loss · 7 d', value: usd(k.confirmed_loss_7d), sub: k.shrinkage_pct != null ? `${k.shrinkage_pct}% of output at standard cost (shrinkage)` : '', tone: 'danger', icon: 'trendDown' },
    { label: 'Recovered · 7 d', value: usd(k.recovered_value_7d), sub: 'material found or returned', tone: 'success', icon: 'refresh' },
    { label: 'Cases · 7 d', value: k.cases_7d, sub: `${ov.window || 'last 7 days'} · ${k.closed_7d} closed · ${k.false_alarm_pct ?? '—'}% false alarm`, icon: 'list' },
    { label: 'Time to close', value: k.mean_hours_to_close != null ? `${k.mean_hours_to_close} h` : '—', sub: 'mean, detection to outcome', icon: 'clock' },
  ] : []), [k, ov]);

  return (
    <div className="space-y-4">
      <PageTitle title="Loss Prevention"
        subtitle="Material pilferage across receiving, the material store, lines, the warehouse and the docks — detected, investigated and closed with an outcome">
      </PageTitle>
      <ErrorBox error={error} onRetry={reload} />
      {loading && !ov && <Loading rows={4} />}
      {k && <KpiRow cols={6}>{kpis.map((x) => <Kpi key={x.label} {...x} />)}</KpiRow>}
      <Tabs tabs={TABS} value={tab} onChange={setTab} variant="underline" />
      {tab === 'queue' && <Queue factory={factory} kinds={ov?.kinds} kind={kind} setKind={setKind} zone={zone} setZone={setZone} onOpen={setOpenId} />}
      {tab === 'patterns' && ov && (
        <Patterns factory={factory} ov={ov}
          onKind={(x) => { setKind(x); setZone(''); setTab('queue'); }}
          onZone={(z) => { setZone(z); setKind(''); setTab('queue'); }} />
      )}
      {tab === 'detection' && ov && <Detection factory={factory} ov={ov} onKind={(x) => { setKind(x); setTab('queue'); }} />}
      {ov?.disclaimer && <p className="text-[10.5px] text-fg-faint">{ov.disclaimer}</p>}
      {openId && <LossIncidentDrawer id={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}
