/* Shared formatting and status helpers. */

export const STATUS = {
  normal:   { label: 'Normal',   tone: 'success' },
  warning:  { label: 'Warning',  tone: 'warning' },
  critical: { label: 'Critical', tone: 'danger'  },
};

export function getStatus(value, thresholds, inverted = false) {
  if (!thresholds) return 'normal';
  const v = parseFloat(value) || 0;
  const { good, warn } = thresholds;
  if (!inverted) return v >= good ? 'normal' : v >= warn ? 'warning' : 'critical';
  return v <= good ? 'normal' : v <= warn ? 'warning' : 'critical';
}

export function loadTone(pct, warnAt = 60, dangerAt = 85) {
  return pct > dangerAt ? 'danger' : pct > warnAt ? 'warning' : 'success';
}

export const titleCase = (s = '') => s.replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

export const initials = (name = '') =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('') || '?';

// ─── numbers ───────────────────────────────────────────────────────────
export const num = (v, digits = 0) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? '—'
    : Number(v).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
export const pct = (v, digits = 1) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(digits)}%`);
export const money = (v, digits = 0) =>
  v === null || v === undefined ? '—'
    : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
export const compact = (v) => {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return num(n);
};

// ─── simulated timestamps (ISO "YYYY-MM-DDTHH:MM", no timezone) ─────────
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export const parseTs = (s) => (s ? new Date(s.length <= 10 ? `${s}T00:00` : s) : null);
export const hhmm = (s) => (s ? s.slice(11, 16) : '—');
export function dayTime(s) {
  const d = parseTs(s);
  return d ? `${DAYS[d.getDay()]} ${hhmm(s)}` : '—';
}
export function dateShort(s) {
  const d = parseTs(s);
  return d ? `${MONTHS[d.getMonth()]} ${d.getDate()}` : '—';
}
export function dateTime(s) {
  const d = parseTs(s);
  return d ? `${MONTHS[d.getMonth()]} ${d.getDate()}, ${hhmm(s)}` : '—';
}
export function minutesBetween(a, b) {
  const x = parseTs(a);
  const y = parseTs(b);
  return x && y ? Math.round((y - x) / 60000) : null;
}
export function duration(min) {
  if (min === null || min === undefined) return '—';
  const m = Math.round(Math.abs(min));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${String(m % 60).padStart(2, '0')} min`;
}

// ─── status colours ────────────────────────────────────────────────────
const TONES = {
  success: ['Running', 'Received', 'Passed', 'Completed', 'Ready for Dispatch', 'Delivered', 'Departed', 'Stored', 'Healthy',
    'OK', 'Normal', 'Online', 'Correct', 'Reworked', 'Ready', 'Resolved', 'Loaded', 'Present'],
  info: ['In Transit', 'Scheduled', 'Planned', 'In Progress', 'Dispatched', 'Loading', 'Receiving', 'Open', 'Arrived',
    'Changeover', 'Acknowledged', 'Picking Pending', 'Pending Put-away', 'Proposed', 'Accepted', 'Late'],
  warning: ['Partially Received', 'Warning', 'Low', 'Idle', 'Maintenance', 'Quality Pending', 'Inventory Pending', 'Constrained',
    'Attention', 'Overridden', 'Medium', 'Production Pending', 'Watch'],
  danger: ['Delayed', 'Discrepancy', 'Breakdown', 'Critical', 'Blocked', 'Rejected', 'Underpacked', 'Overpacked', 'Defect',
    'Bottleneck', 'Down', 'High', 'At Risk', 'Absent', 'Offline', 'Not Ready'],
  neutral: ['Cancelled', 'No demand', 'Depleted', 'Rejected run', 'Low priority', 'Scheduled shift'],
};
const TONE_OF = Object.fromEntries(Object.entries(TONES).flatMap(([tone, list]) => list.map((s) => [s, tone])));
export const statusTone = (s) => TONE_OF[s] || 'neutral';
export const severityTone = (s) => ({ critical: 'danger', high: 'danger', warning: 'warning', medium: 'warning', info: 'info', low: 'info' }[s] || 'neutral');

export function healthTone(score) {
  if (score === null || score === undefined) return 'neutral';
  return score >= 85 ? 'success' : score >= 65 ? 'warning' : 'danger';
}

export function effTone(p, good = 95, warn = 85) {
  if (p === null || p === undefined) return 'neutral';
  return p >= good ? 'success' : p >= warn ? 'warning' : 'danger';
}

/** Build a link to a page with a focused record, e.g. linkTo('production', 'line', 3). */
export function linkTo(page, type, id) {
  const path = page === 'overview' ? '/' : `/${page}`;
  return type ? `${path}?focus=${type}:${id ?? ''}` : path;
}
