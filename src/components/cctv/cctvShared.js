import { useEffect, useState } from 'react';
import { useApp } from '../../data/DataProvider';

/* Activity codes written by tools/cctv/analyze.py */
export const ACT = [
  { key: 'idle', label: 'Idle', color: '#f59e0b' },
  { key: 'handling', label: 'Handling', color: '#22c55e' },
  { key: 'lifting', label: 'Lifting', color: '#2dd4bf' },
  { key: 'walking', label: 'Walking', color: '#60a5fa' },
  { key: 'present', label: 'On floor', color: '#94a3b8' },
];
export const OBJ = [
  { label: 'pillow', color: '#f472b6' },
  { label: 'carton', color: '#e0a458' },
  { label: 'pallet', color: '#a5b4fc' },
];
export const EVENT_META = {
  pillow_in: { label: 'Pillow placed in carton', color: '#22c55e', icon: '+' },
  carton_complete: { label: 'Carton complete', color: '#38bdf8', icon: '✓' },
  carton_sealed: { label: 'Carton sealed', color: '#38bdf8', icon: '▣' },
  line_cross: { label: 'Pillow counted on conveyor', color: '#facc15', icon: '→' },
  idle: { label: 'Idle at station', color: '#f59e0b', icon: '!' },
  left_station: { label: 'Left station', color: '#f97316', icon: '↗' },
  restricted: { label: 'Restricted zone entry', color: '#ef4444', icon: '⚠' },
  label_read: { label: 'Carton label read', color: '#a78bfa', icon: 'A' },
};
export const LAYERS = [
  { key: 'people', label: 'People' },
  { key: 'pose', label: 'Pose' },
  { key: 'objects', label: 'Pillows' },
  { key: 'zones', label: 'Zones' },
  { key: 'ocr', label: 'OCR' },
  { key: 'heat', label: 'Heatmap' },
];
export const DEFAULT_LAYERS = { people: true, pose: true, objects: true, zones: true, ocr: true, heat: false };
export const COMPACT_LAYERS = { people: true, pose: false, objects: true, zones: true, ocr: false, heat: false };

/* ── analytics files ─────────────────────────────────────────────────── */
const cache = new Map();

function prepare(d) {
  d.ocrFrames = [...new Set(d.ocr.map((o) => o.f))].sort((a, b) => a - b);
  d.ocrBy = {};
  d.ocr.forEach((o) => { (d.ocrBy[o.f] ||= []).push(o); });
  d.trails = {};
  d.o.forEach((objs, f) => objs.forEach((o) => {
    if (o[0] >= 1000 && o[0] < 2000) (d.trails[o[0]] ||= []).push([f, o[2] + o[4] / 2, o[3] + o[5] / 2]);
  }));
  d.zoneById = Object.fromEntries(d.zones.map((z) => [z.id, z]));
  d.placeEvents = d.ev.filter((e) => e.type === 'pillow_in');
  d.clipPillows = {};
  d.placeEvents.forEach((e) => { d.clipPillows[e.zone] = (d.clipPillows[e.zone] || 0) + e.n; });
  d.lineTotal = d.ev.filter((e) => e.type === 'line_cross').length;
  return d;
}

export function loadAnalytics(url) {
  if (!cache.has(url)) {
    cache.set(url, fetch(url).then((r) => {
      if (!r.ok) throw new Error(`Analytics ${r.status}`);
      return r.json();
    }).then(prepare).catch((e) => { cache.delete(url); throw e; }));
  }
  return cache.get(url);
}

export function useAnalytics(url, enabled = true) {
  const [state, setState] = useState({ data: null, error: null });
  useEffect(() => {
    if (!url || !enabled) return undefined;
    let alive = true;
    loadAnalytics(url).then((data) => alive && setState({ data, error: null }))
      .catch((e) => alive && setState({ data: null, error: e.message }));
    return () => { alive = false; };
  }, [url, enabled]);
  return state;
}

/* ── identity fusion: tracked person → RFID badge at that station ───── */
/**
 * Tracks in a station zone are joined into person chains: a later track continues a chain
 * when the chain's last track has ended (the tracker lost the person behind a carton) and
 * the body size matches. Tracks alive at the same time are different people, so two boxes
 * on screen never carry the same badge. Badge order: the main chain of every station,
 * then the other station chains, then large foreground people outside the stations.
 */
function personChains(tracks) {
  const chains = [];
  [...tracks].sort((a, b) => a.first - b.first).forEach((t) => {
    let best = null;
    chains.forEach((c) => {
      const last = c[c.length - 1];
      const ratio = t.h / Math.max(1, last.h);
      if (last.last <= t.first + 3 && ratio > 0.6 && ratio < 1.6 && (!best || last.last > best[best.length - 1].last)) best = c;
    });
    if (best) best.push(t); else chains.push([t]);
  });
  return chains
    .map((c) => ({ tracks: c, dur: c.reduce((s, t) => s + t.dur, 0) }))
    .sort((a, b) => b.dur - a.dur);
}

export function assignIdentities(d, workers) {
  const out = {};
  if (!d || !workers?.length) return out;
  const people = Object.entries(d.people).map(([tid, p]) => ({ tid: Number(tid), ...p, dur: p.last - p.first }));
  let k = 0;
  const give = (tracks) => {
    if (k >= workers.length) return;
    tracks.forEach((t) => { out[t.tid] = workers[k]; });
    k += 1;
  };
  const zones = [...new Set(people.filter((p) => p.zone).map((p) => p.zone))].sort();
  const chainsBy = Object.fromEntries(zones.map((z) => [z, personChains(people.filter((p) => p.zone === z))]));
  zones.forEach((z) => give(chainsBy[z][0].tracks));
  zones.forEach((z) => chainsBy[z].slice(1).filter((c) => c.dur >= 2 * d.fps).forEach((c) => give(c.tracks)));
  people.filter((p) => !p.zone && p.h >= 150 && p.dur >= 3 * d.fps)
    .sort((a, b) => b.h * b.dur - a.h * a.dur)
    .forEach((p) => give([p]));
  return out;
}

export function initials(name = '') {
  return name.split(/\s+/).map((s) => s[0]).join('').slice(0, 2).toUpperCase();
}

/* ── counting across video loops ─────────────────────────────────────── */
/** Pillows in the current carton of a zone at frame f of loop `loops`. */
export function cartonState(d, zone, f, loops) {
  const evs = d.placeEvents.filter((e) => e.zone === zone.id);
  const done = evs.filter((e) => e.f <= f);
  const now = done.reduce((s, e) => s + e.n, 0);
  const clip = d.clipPillows[zone.id] || 0;
  if (zone.capacity) { // display pallet: keeps filling across loops
    const total = (zone.base || 0) + loops * clip + now;
    return { inCarton: total % zone.capacity, spec: zone.capacity, packs: Math.floor((total % zone.capacity) / (zone.per_event || 1)), seq: Math.floor(total / zone.capacity), total };
  }
  // a full carton stays at spec/spec until the next pillow starts a new carton
  const spec = zone.spec || 99;
  const inThisLoop = (zone.offset || 0) + now;
  const inCarton = inThisLoop === 0 ? 0 : ((inThisLoop - 1) % spec) + 1;
  return { inCarton, spec: zone.spec, seq: loops, total: loops * clip + now };
}

/** Running totals for the KPI chips (this session, across loops). */
export function sessionTotals(d, f, loops) {
  const placed = d.placeEvents.filter((e) => e.f <= f).reduce((s, e) => s + e.n, 0);
  const clipPlaced = d.placeEvents.reduce((s, e) => s + e.n, 0);
  const cartons = d.ev.filter((e) => e.type === 'carton_complete');
  const lines = d.ev.filter((e) => e.type === 'line_cross' && e.f <= f).length;
  return {
    pillows: loops * clipPlaced + placed,
    cartons: loops * cartons.length + cartons.filter((e) => e.f <= f).length,
    crossings: loops * d.lineTotal + lines,
  };
}

/* ── plant clock for the camera OSD ──────────────────────────────────── */
const clock = { simMs: null, realMs: 0, rate: 1 };

export function useSimClock() {
  const { sim } = useApp();
  useEffect(() => {
    if (!sim?.sim_time) return;
    const s = new Date(sim.sim_time).getTime();
    const r = Date.now();
    if (clock.simMs != null && s > clock.simMs && r > clock.realMs) {
      const rate = (s - clock.simMs) / (r - clock.realMs);
      clock.rate = Math.max(1, Math.min(120, clock.rate * 0.6 + rate * 0.4));
    }
    clock.simMs = s;
    clock.realMs = r;
  }, [sim?.sim_time]);
  return clock;
}

export function simNow() {
  if (clock.simMs == null) return new Date();
  return new Date(clock.simMs + Math.min(90_000, (Date.now() - clock.realMs) * clock.rate));
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export function osdTime(dt) {
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${DAYS[dt.getDay()]} ${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`;
}
