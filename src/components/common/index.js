import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button, Card, Icon, ProgressBar } from '../ui';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { linkTo, severityTone, statusTone } from '../../utils';

/** Status pill coloured by status name (see utils.statusTone). */
export function StatusBadge({ status, tone, dot = true }) {
  if (!status) return <span className="text-fg-faint">—</span>;
  return <Badge tone={tone || statusTone(status)} dot={dot}>{status}</Badge>;
}

/**
 * Kpi: compact clickable KPI tile.
 *   label, value, unit, sub (secondary line), tone (status colour of the value),
 *   delta {value, good} trend chip, onClick opens drill-down, active highlights.
 */
export function Kpi({ label, value, unit, sub, tone, delta, onClick, active, icon, footer }) {
  const Tag = onClick ? 'button' : 'div';
  return (
    <Tag
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={`card text-left p-3.5 flex flex-col gap-1 min-w-0 ${onClick ? 'card-hoverable cursor-pointer' : ''}`}
      style={active ? { borderColor: 'var(--color-primary)', boxShadow: '0 0 0 2px var(--color-primary-soft)' } : undefined}
    >
      <div className="flex items-center gap-1.5 min-w-0">
        {icon && <Icon name={icon} size={13} className="text-fg-faint" />}
        <span className="label-caps truncate">{label}</span>
        {delta && delta.value !== null && delta.value !== undefined && (
          <span className={`trend ml-auto ${delta.good ? 'trend-up' : 'trend-down'}`}>
            {delta.value >= 0 ? '▲' : '▼'} {Math.abs(delta.value).toFixed(1)}{delta.unit ?? ''}
          </span>
        )}
      </div>
      <div className="flex items-baseline gap-1 min-w-0">
        <span className="text-2xl font-bold leading-tight truncate" style={{ color: tone ? `var(--color-${tone})` : 'var(--color-text)' }}>{value}</span>
        {unit && <span className="text-xs text-fg-faint">{unit}</span>}
      </div>
      {sub && <div className="text-[11px] text-fg-muted leading-snug">{sub}</div>}
      {footer}
    </Tag>
  );
}

export function KpiRow({ children, cols = 6 }) {
  const c = { 3: 'lg:grid-cols-3', 4: 'lg:grid-cols-4', 5: 'lg:grid-cols-5', 6: 'lg:grid-cols-6' }[cols] || 'lg:grid-cols-6';
  return <div className={`grid grid-cols-2 md:grid-cols-3 ${c} gap-3`}>{children}</div>;
}

export function Loading({ rows = 3, height = 18 }) {
  return (
    <div className="space-y-2 animate-pulse" aria-busy="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="rounded" style={{ height, background: 'var(--color-surface-raised)', width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function Empty({ children = 'Nothing to show.', icon = 'check' }) {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center text-xs text-fg-faint gap-2">
      <Icon name={icon} size={20} />
      {children}
    </div>
  );
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null;
  return (
    <div className="tile tone-danger text-xs flex items-center gap-3">
      <Icon name="alert" size={15} />
      <span className="flex-1">{error}</span>
      {onRetry && <Button size="sm" variant="secondary" onClick={onRetry}>Retry</Button>}
    </div>
  );
}

/** Page title row with optional right-side content. */
export function PageTitle({ title, subtitle, children }) {
  return (
    <div className="flex items-end justify-between flex-wrap gap-3">
      <div className="min-w-0">
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {children && <div className="flex items-center gap-2 flex-wrap">{children}</div>}
    </div>
  );
}

/** Horizontal labelled bar (used in comparisons and breakdowns). */
export function BarRow({ label, value, max, display, tone = 'primary', sub, onClick }) {
  const Tag = onClick ? 'button' : 'div';
  return (
    <Tag type={onClick ? 'button' : undefined} onClick={onClick} className={`w-full text-left ${onClick ? 'hover:opacity-80' : ''}`}>
      <div className="flex items-center justify-between text-[11px] mb-1 gap-2">
        <span className="text-fg-muted truncate">{label}</span>
        <span className="font-semibold text-fg tabular-nums whitespace-nowrap">{display ?? value}</span>
      </div>
      <ProgressBar value={value} max={max} tone={tone} />
      {sub && <div className="text-[10px] text-fg-faint mt-0.5">{sub}</div>}
    </Tag>
  );
}

/** Contextual AI insights for a page, computed by the backend from live records. */
export function InsightsCard({ page, limit = 4, compact = false, onAction, title = 'S!AP advisories' }) {
  const { filters } = useApp();
  const navigate = useNavigate();
  const { data, loading } = useApi('/api/insights', { page, factory: filters.factory }, { every: 4 });
  const items = (data?.insights || []).slice(0, limit);
  return (
    <Card
      title={<span className="flex items-center gap-1.5 text-accent"><Icon name="sparkles" size={14} />{title}</span>}
      subtitle={compact ? undefined : 'Generated from live operational records'}
      style={{ borderColor: 'var(--color-accent-border)' }}
      padded={false}
    >
      {loading && !data && <div className="p-4"><Loading /></div>}
      {data && items.length === 0 && <Empty>No issues detected for this scope.</Empty>}
      <div className="divide-y divide-line">
        {items.map((ins) => (
          <div key={ins.id} className="px-4 py-3">
            <div className="flex items-start gap-2">
              <Badge tone={severityTone(ins.severity)}>{ins.severity.toUpperCase()}</Badge>
              <div className="min-w-0 flex-1">
                <p className="text-[12.5px] font-semibold text-fg leading-snug">{ins.title}</p>
                {ins.detail && <p className="text-[11px] text-fg-muted mt-0.5 leading-snug">{ins.detail}</p>}
                <p className="text-[11px] text-accent mt-1 leading-snug"><b>Action:</b> {ins.recommendation}</p>
                <div className="flex gap-2 mt-1.5 flex-wrap">
                  {ins.link && (
                    <button type="button" className="link-btn" onClick={() => navigate(linkTo(ins.link.page, ins.link.entity_type, ins.link.entity_id))}>
                      OPEN RECORD
                    </button>
                  )}
                  {ins.action && onAction && (
                    <button type="button" className="link-btn" style={{ color: 'var(--color-accent)' }} onClick={() => onAction(ins.action, ins)}>
                      {ins.action.label.toUpperCase()}
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

/** Open alerts for one stage (procurement, production, …). */
export function StageAlerts({ stage, limit = 4 }) {
  const { alerts } = useApp();
  const navigate = useNavigate();
  const items = alerts.filter((a) => a.stage === stage).slice(0, limit);
  if (!items.length) return null;
  return (
    <div className="grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
      {items.map((a) => (
        <button
          key={a.id}
          type="button"
          onClick={() => navigate(linkTo(a.page, a.entity_type, a.entity_id))}
          className={`tile tone-${severityTone(a.severity)} text-left flex gap-2 items-start hover:opacity-90`}
        >
          <Icon name="alert" size={14} className="mt-0.5" />
          <span className="min-w-0">
            <span className="block text-xs font-semibold text-fg leading-snug">{a.title}</span>
            <span className="block text-[10.5px] text-fg-muted leading-snug line-clamp-2">{a.recommended_action}</span>
          </span>
        </button>
      ))}
    </div>
  );
}

/** Small label/value list used inside drawers. */
export function KV({ items, cols = 2 }) {
  return (
    <div className="grid gap-x-4 gap-y-2" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
      {items.filter(Boolean).map(([k, v]) => (
        <div key={k} className="min-w-0">
          <p className="label-caps">{k}</p>
          <div className="text-[13px] font-semibold text-fg truncate">{v ?? '—'}</div>
        </div>
      ))}
    </div>
  );
}

export function SimTag() {
  return <Badge tone="warning" dot>SIMULATED</Badge>;
}
