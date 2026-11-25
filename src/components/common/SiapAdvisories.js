import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button, Card, Icon, ProgressBar } from '../ui';
import { Empty, ErrorBox, Loading } from './index';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { post } from '../../api/client';
import { dayTime, hhmm, linkTo, num } from '../../utils';

/* S!AP: the predictive / prescriptive advisory surface. Advisories come from
   /api/predictive/advisories (illustrative model on simulated telemetry). */

export const OP_HOURS_PER_DAY = 15.33; // 2 shifts x 460 productive minutes

export const SEVERITY = {
  critical: { tone: 'danger', label: 'CRITICAL', icon: 'alert' },
  warning: { tone: 'warning', label: 'WARNING', icon: 'alert' },
  info: { tone: 'info', label: 'INFO', icon: 'info' },
};

const CATEGORY_ICON = {
  'Predictive maintenance': 'wrench',
  'Quality risk': 'shield',
  'Spare parts': 'box',
  'Maintenance capacity': 'users',
  'Energy anomaly': 'bolt',
  'Workforce · RFID': 'signal',
};

export const MAINTENANCE_CATEGORIES = ['Predictive maintenance', 'Spare parts', 'Maintenance capacity'];

/** Probability as a percentage ("<1%" for tiny values). */
export const fmtProb = (p) => (p === null || p === undefined ? '—' : p > 0 && p < 0.01 ? '<1%' : `${Math.round(p * 100)}%`);

/** Status tone for a failure probability. */
export const probTone = (p) => {
  if (p === null || p === undefined) return 'neutral';
  if (p >= 0.5) return 'danger';
  if (p >= 0.1) return 'warning';
  if (p >= 0.05) return 'info';
  return 'success';
};

/** Remaining useful life in operating hours, or production days when long. */
export function fmtRul(h) {
  if (h === null || h === undefined) return '—';
  if (h < 48) return `${num(h)} op h`;
  const d = h / OP_HOURS_PER_DAY;
  return d < 365 ? `${num(d)} prod. days` : '> 1 year';
}

export const fmtUsd = (v) => {
  if (v === null || v === undefined) return '—';
  return Math.abs(v) >= 10000 ? `$${(v / 1000).toFixed(1)}k` : `$${num(v)}`;
};

export function windowLabel(w) {
  if (!w || !w.start) return null;
  return `${dayTime(w.start)}${w.end ? `–${hhmm(w.end)}` : ''}`;
}

/** Brand mark: sparkles + S!AP. */
export function SiapMark({ size = 14, className = '', children }) {
  return (
    <span className={`inline-flex items-center gap-1.5 font-bold text-accent ${className}`}>
      <Icon name="sparkles" size={size} />
      <span>S!AP</span>
      {children && <span className="font-semibold">{children}</span>}
    </span>
  );
}

/** Polls the advisories endpoint. category: string or array of category names. */
export function useSiapAdvisories({ factory, limit, category, every = 3 } = {}) {
  const { filters } = useApp();
  const cat = Array.isArray(category) ? category.join(',') : category;
  return useApi('/api/predictive/advisories', { factory: factory || filters.factory, limit, category: cat }, { every });
}

function Metric({ label, value, tone, sub, bar }) {
  return (
    <div className="tile min-w-0" style={{ padding: '6px 8px' }}>
      <p className="label-caps truncate" style={{ fontSize: 9 }}>{label}</p>
      <p className="text-[13px] font-bold leading-tight tabular-nums truncate" style={{ color: tone ? `var(--color-${tone})` : 'var(--color-text)' }}>{value}</p>
      {bar !== undefined && <ProgressBar value={bar} max={1} tone={tone || 'primary'} className="mt-1" />}
      {sub && <p className="text-[9.5px] text-fg-faint truncate mt-0.5">{sub}</p>}
    </div>
  );
}

/** Compact prediction strip: 24 h / 7 d failure probability, RUL band, failure mode. */
export function PredictionStrip({ p, compact = false }) {
  if (!p) return null;
  return (
    <div className={`grid gap-1.5 mt-2 ${compact ? 'grid-cols-2' : 'grid-cols-2 md:grid-cols-4'}`}>
      <Metric label="Failure · 24 h" value={fmtProb(p.failure_prob_24h)} tone={probTone(p.failure_prob_24h)} bar={p.failure_prob_24h} />
      <Metric label="Failure · 7 days" value={fmtProb(p.failure_prob_7d)} tone={probTone(p.failure_prob_7d)} bar={p.failure_prob_7d} />
      <Metric label="Remaining life (P50)" value={fmtRul(p.rul_hours)}
        sub={p.rul_low_hours !== null && p.rul_low_hours !== undefined ? `P10–P90 ${num(p.rul_low_hours)}–${num(p.rul_high_hours)} op h` : undefined} />
      <Metric label="Likely failure mode" value={<span className="text-[11.5px]" title={p.failure_mode}>{p.failure_mode}</span>}
        sub={p.method} />
    </div>
  );
}

function ImpactLine({ impact }) {
  if (!impact) return null;
  const parts = [];
  if (impact.pillows_at_risk) parts.push(<span key="p"><Icon name="factory" size={11} /> ≈{num(impact.pillows_at_risk)} pillows at risk</span>);
  if (impact.downtime_hours) parts.push(<span key="d"><Icon name="clock" size={11} /> {num(impact.downtime_hours, 1)} h line stop</span>);
  if (impact.cost_usd) parts.push(<span key="c"><Icon name="dollar" size={11} /> {fmtUsd(impact.cost_usd)}</span>);
  const orders = impact.orders || [];
  if (orders.length) {
    const o = orders[0];
    parts.push(
      <span key="o" title={orders.map((x) => `${x.number} · ${x.customer || '—'} · due ${dayTime(x.due)}`).join('\n')}>
        <Icon name="clipboard" size={11} /> {impact.orders_count || orders.length} order{(impact.orders_count || orders.length) > 1 ? 's' : ''} affected
        <span className="text-fg-faint"> (next {o.number}{o.customer ? ` · ${o.customer}` : ''}{o.due ? ` · due ${dayTime(o.due)}` : ''})</span>
      </span>,
    );
  }
  if (!parts.length) return null;
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[10.5px] text-fg-muted [&_svg]:inline [&_svg]:-mt-0.5 [&_svg]:text-fg-faint">
      {parts}
    </div>
  );
}

/** One advisory. onOpenMachine(id, advisory) overrides navigation to the machine record. */
export function SiapAdvisoryItem({ a, compact = false, onOpenMachine, onChanged }) {
  const { act, can } = useApp();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const sev = SEVERITY[a.severity] || SEVERITY.info;
  const action = (a.actions || []).find((x) => x.type === 'schedule_work_order');
  const allowed = can('maintenance.write');
  const wo = a.work_order;

  const schedule = async () => {
    setBusy(true);
    try {
      await act(() => post(`/api/predictive/advisories/${encodeURIComponent(a.id)}/schedule`, { start: action.start, end: action.end }),
        (r) => `${r.number} scheduled for ${r.machine} · ${dayTime(r.scheduled_start)}–${hhmm(r.scheduled_end)}`);
      onChanged?.();
    } catch (e) { /* toast shown by act() */ } finally { setBusy(false); }
  };
  const openMachine = () => {
    if (onOpenMachine && a.machine) onOpenMachine(a.machine.id, a);
    else if (a.link) navigate(linkTo(a.link.page, a.link.entity_type, a.link.entity_id));
  };
  const win = windowLabel(a.window);

  return (
    <div className="px-4 py-3" style={{ boxShadow: `inset 3px 0 0 var(--color-${sev.tone})` }}>
      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge tone={sev.tone} dot>{sev.label}</Badge>
        <span className="badge tone-neutral" title="Category">
          <Icon name={CATEGORY_ICON[a.category] || 'sparkles'} size={11} />{a.category}
        </span>
        {a.factory && <span className="text-[10.5px] font-semibold text-fg-faint">{a.factory}</span>}
        {wo && <Badge tone="info">{wo.number} {wo.status.toUpperCase()}</Badge>}
        <span className="ml-auto text-[10.5px] text-fg-faint tabular-nums" title="Model confidence: data sufficiency, agreement of methods, clarity of the anomaly signature">
          {Math.round((a.confidence || 0) * 100)}% conf.
        </span>
      </div>
      <p className="text-[12.5px] font-semibold text-fg leading-snug mt-1.5">{a.title}</p>
      {a.summary && <p className={`text-[11px] text-fg-muted mt-0.5 leading-snug ${compact ? 'line-clamp-3' : ''}`}>{a.summary}</p>}
      <PredictionStrip p={a.prediction} compact={compact} />
      <ImpactLine impact={a.impact} />
      {a.recommendation && (
        <div className="mt-2 rounded-md px-2.5 py-2 text-[11px] leading-snug"
          style={{ background: 'var(--color-accent-soft)', border: '1px solid var(--color-accent-border)' }}>
          <div className="flex items-center gap-2 flex-wrap mb-0.5">
            <SiapMark size={11} className="text-[10px]">recommends</SiapMark>
            {win && <span className="text-[10px] font-semibold text-fg-muted"><Icon name="calendar" size={10} className="inline -mt-0.5" /> {a.window.label ? `${a.window.label} · ` : ''}{win}</span>}
          </div>
          <span className="text-fg">{a.recommendation}</span>
        </div>
      )}
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        {action && allowed && (
          <Button size="sm" variant="accent" icon="calendar" disabled={busy} onClick={schedule}>
            {busy ? 'Scheduling…' : action.label || 'Schedule'}
          </Button>
        )}
        {action && !allowed && <span className="text-[10.5px] text-fg-faint">Scheduling needs the maintenance role</span>}
        {(a.machine || a.link) && (
          <Button size="sm" variant="secondary" icon="external" onClick={openMachine}>Open machine</Button>
        )}
        {a.evidence?.length > 0 && (
          <button type="button" className="link-btn inline-flex items-center gap-1" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            <Icon name={open ? 'chevronDown' : 'chevronRight'} size={11} />WHY
          </button>
        )}
      </div>
      {open && (
        <ul className="mt-2 space-y-1 text-[10.5px] text-fg-muted list-disc pl-4">
          {a.evidence.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
  );
}

/** Ranked S!AP advisories for a factory scope (defaults to the global factory filter). */
export function SiapAdvisoryList({ factory, limit, compact = false, category, onOpenMachine, every = 3, showSummary = true, emptyText }) {
  const { data, loading, error, reload } = useSiapAdvisories({ factory, limit, category, every });
  const items = data?.advisories || [];
  return (
    <div>
      {loading && !data && <div className="p-4"><Loading rows={4} /></div>}
      {error && !data && <div className="p-3"><ErrorBox error={error} onRetry={reload} /></div>}
      {data && showSummary && (
        <div className="px-4 py-2 flex items-center gap-2 flex-wrap border-b">
          {['critical', 'warning', 'info'].map((s) => (
            <Badge key={s} tone={SEVERITY[s].tone} plain dot>{data.counts?.[s] || 0} {SEVERITY[s].label}</Badge>
          ))}
          <span className="ml-auto text-[10px] text-fg-faint">{items.length < data.total ? `Top ${items.length} of ${data.total}` : `${data.total} advisories`} · {dayTime(data.sim_time)}</span>
        </div>
      )}
      {data && items.length === 0 && <Empty icon="check">{emptyText || 'No S!AP advisories for this scope.'}</Empty>}
      <div className="divide-y divide-line">
        {items.map((a) => <SiapAdvisoryItem key={a.id} a={a} compact={compact} onOpenMachine={onOpenMachine} onChanged={reload} />)}
      </div>
      {data && <p className="px-4 py-2 text-[10px] text-fg-faint border-t leading-snug">{data.disclaimer}</p>}
    </div>
  );
}

/** SiapAdvisoryList inside a Card. */
export function SiapAdvisoriesCard({ factory, limit = 6, title = 'S!AP advisories', subtitle, category, compact = false, onOpenMachine, className = '' }) {
  return (
    <Card
      title={<SiapMark size={14}>{title.replace(/^S!AP\s*/i, '')}</SiapMark>}
      subtitle={subtitle || 'Predictive & prescriptive advisories · illustrative model on simulated telemetry'}
      style={{ borderColor: 'var(--color-accent-border)' }}
      padded={false}
      className={className}
    >
      <SiapAdvisoryList factory={factory} limit={limit} compact={compact} category={category} onOpenMachine={onOpenMachine} />
    </Card>
  );
}

export default SiapAdvisoriesCard;
