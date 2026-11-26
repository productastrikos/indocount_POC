/* Loss prevention (pilferage) — shared labels, tones and formatting. */
export const STATUS_TONE = { Open: 'danger', Investigating: 'warning', Resolved: 'success' };
export const OUTCOME_TONE = { 'Confirmed loss': 'danger', Recovered: 'success', 'Process error': 'info', 'False alarm': 'neutral' };
export const SEVERITY_TONE = { critical: 'danger', warning: 'warning', info: 'info' };
export const SEVERITY_COLOR = { critical: '#ef4444', warning: '#f59e0b', info: '#38bdf8' };

export const OUTCOME_HELP = {
  'Confirmed loss': 'Material is gone and cannot be explained: written off at standard cost, reported to HR / security.',
  Recovered: 'The material was found or returned (at the gate, in another location, a supplier credit).',
  'Process error': 'A process gap, not theft: mis-slotting, missing scan, over-fill, wrong ASN — fix the process.',
  'False alarm': 'The signal was wrong: stray RFID read, authorised visit, badge not worn.',
};

export const STAGE_LABEL = {
  procurement: 'Procurement & Inbound', production: 'Production', quality: 'Quality & Packaging',
  warehouse: 'Warehouse', dispatch: 'Dispatch',
};

export const usd = (v) => (v == null ? '—' : `$${Math.round(v).toLocaleString('en-US')}`);

export function qtyText(c) {
  if (!c || !c.qty) return '—';
  const q = c.qty >= 100 ? Math.round(c.qty).toLocaleString('en-US') : Number(c.qty.toFixed(1)).toLocaleString('en-US');
  return `${q}${c.unit ? ` ${c.unit}` : ''}`;
}

/** "12 min ago" / "3 h ago" / "2 d ago" relative to the plant clock. */
export function ago(ts, now) {
  if (!ts || !now) return '';
  const m = (new Date(now) - new Date(ts)) / 60000;
  if (m < 1) return 'just now';
  if (m < 60) return `${Math.round(m)} min ago`;
  if (m < 60 * 24) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}
