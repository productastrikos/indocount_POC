import React from 'react';
import { Badge, Icon } from '../ui';
import { SEVERITY_COLOR, STATUS_TONE, OUTCOME_TONE, ago, qtyText, usd } from './lossShared';

/** One row of the case queue. */
export default function CaseRow({ c, now, onOpen, showPlant = true }) {
  return (
    <button type="button" onClick={() => onOpen(c.id)}
      className="w-full text-left px-4 py-2.5 flex items-start gap-3 hover:bg-surface-raised transition-colors">
      <span className="mt-1 w-2.5 h-2.5 rounded-full shrink-0" style={{ background: c.status === 'Resolved' ? 'var(--color-text-faint)' : SEVERITY_COLOR[c.severity] }} />
      <span className="w-8 h-8 rounded-lg grid place-items-center shrink-0 bg-surface-raised text-fg-muted"><Icon name={c.icon || 'shield'} size={15} /></span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2 flex-wrap">
          <span className="text-[12.5px] font-semibold text-fg">{c.ref}</span>
          <span className="text-[12.5px] text-fg truncate">{c.title}</span>
        </span>
        <span className="block text-[11px] text-fg-muted truncate">
          {c.kind_label} · {c.zone}{showPlant && c.factory ? ` · ${c.factory}` : ''}{c.qty ? ` · ${qtyText(c)}` : ''}
          {c.camera ? ` · ${c.camera}` : ''}
        </span>
        {c.status !== 'Resolved' && c.next_step && (
          <span className="block text-[11px] text-warning truncate">Next: {c.next_step.text}{c.assigned_to ? ` · ${c.assigned_to}` : ''}</span>
        )}
        {c.status === 'Resolved' && <span className="block text-[11px] text-fg-faint truncate">Closed {ago(c.resolved_at, now)}{c.assigned_to ? ` · ${c.assigned_to}` : ''}</span>}
      </span>
      <span className="text-right shrink-0">
        <Badge tone={c.outcome ? OUTCOME_TONE[c.outcome] : STATUS_TONE[c.status]} dot>{c.outcome || c.status}</Badge>
        <span className="block text-[12px] font-semibold text-fg tabular-nums mt-1">{usd(c.value_usd)}</span>
        <span className="block text-[10.5px] text-fg-faint">{ago(c.detected_at, now)} · {c.progress[0]}/{c.progress[1]} checks</span>
      </span>
    </button>
  );
}
