import React, { useMemo, useState } from 'react';
import { feature } from 'topojson-client';
import { geoAlbersUsa, geoPath } from 'd3-geo';
import us from 'us-atlas/states-albers-10m.json';
import { Badge, Icon, ProgressBar } from '../../components/ui';
import { compact, num, pct } from '../../utils';
import './twin.css';

/* US network map. The us-atlas "albers" topology is pre-projected with
   geoAlbersUsa().scale(1300).translate([487.5, 305]) into a 975×610 frame, so
   plant and DC coordinates are projected with the same transform. */
const VB = { w: 975, h: 610 };
const projection = geoAlbersUsa().scale(1300).translate([487.5, 305]);
const path = geoPath();
const STATES = feature(us, us.objects.states).features.map((f) => ({ id: f.id, name: f.properties.name, d: path(f) }));
const NATION = path(feature(us, us.objects.nation));
const HOME_STATES = { 37: 'KNV', '04': 'PHX', 39: 'CMH' };
export const STATUS_COLOR = { Healthy: '#22c55e', Watch: '#f59e0b', Critical: '#ef4444' };
const STATUS_TONE = { Healthy: 'success', Watch: 'warning', Critical: 'danger' };

const project = (lat, lon) => {
  const p = lat != null && lon != null ? projection([lon, lat]) : null;
  return p ? { x: p[0], y: p[1] } : null;
};

function arc(a, b) {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const c = { x: mx, y: my - Math.min(90, len * 0.28) };  // lanes bow northwards
  return { c, d: `M${a.x},${a.y} Q${c.x},${c.y} ${b.x},${b.y}` };
}
const bez = (a, c, b, t) => ({
  x: (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * c.x + t * t * b.x,
  y: (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * c.y + t * t * b.y,
});

function useGeo(data) {
  return useMemo(() => {
    if (!data) return { plants: [], lanes: [], dcs: [] };
    const plants = data.factories.map((f) => ({ ...f, p: project(f.lat, f.lon) })).filter((f) => f.p);
    const byCode = Object.fromEntries(plants.map((f) => [f.code, f]));
    const dcs = data.destinations.map((d) => ({ ...d, p: project(d.lat, d.lon) })).filter((d) => d.p);
    const lanes = data.lanes.map((l) => {
      const from = byCode[l.factory]?.p;
      const to = project(l.lat, l.lon);
      if (!from || !to) return null;
      const { c, d } = arc(from, to);
      return { ...l, d, truck: bez(from, c, to, l.progress) };
    }).filter(Boolean);
    return { plants, lanes, dcs };
  }, [data]);
}

/** Compact, theme-aware map for the Executive Overview card. */
export function NetworkMiniMap({ data, onOpen }) {
  const { plants, lanes } = useGeo(data);
  return (
    <div className="relative w-full" style={{ aspectRatio: `${VB.w} / ${VB.h}` }}>
      <svg viewBox={`0 0 ${VB.w} ${VB.h}`} className="absolute inset-0 w-full h-full" role="img" aria-label="Map of the three Indocount plants">
        <g>
          {STATES.map((s) => (
            <path key={s.id} d={s.d} style={{
              fill: HOME_STATES[s.id] ? 'var(--color-primary-soft)' : 'var(--color-surface-raised)',
              stroke: 'var(--color-border-strong)', strokeWidth: 0.7,
            }} />
          ))}
          <path d={NATION} style={{ fill: 'none', stroke: 'var(--color-primary-border)', strokeWidth: 1.1 }} />
        </g>
        {lanes.map((l) => (
          <g key={l.truck + l.order}>
            <path d={l.d} className="us-lane" fill="none" style={{ stroke: 'var(--color-primary)' }} />
            <circle cx={l.truck.x} cy={l.truck.y} r={4.5} style={{ fill: 'var(--color-primary)' }} />
          </g>
        ))}
        {plants.map((f) => (
          <g key={f.code} transform={`translate(${f.p.x},${f.p.y})`} style={{ cursor: 'pointer' }} onClick={() => onOpen(f.code)}>
            <circle r={10} className="us-pulse" style={{ fill: STATUS_COLOR[f.status] }} />
            <circle r={8} style={{ fill: STATUS_COLOR[f.status], stroke: 'var(--color-surface)', strokeWidth: 3 }} />
          </g>
        ))}
      </svg>
      {plants.map((f) => (
        <button key={f.code} type="button" onClick={() => onOpen(f.code)}
          className="absolute tile text-left px-2 py-1 hover:opacity-90"
          style={{ left: `${(f.p.x / VB.w) * 100}%`, top: `${(f.p.y / VB.h) * 100}%`, transform: f.code === 'PHX' ? 'translate(12px, -110%)' : 'translate(12px, -50%)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)' }}>
          <span className="flex items-center gap-1.5 text-[11px] font-bold text-fg whitespace-nowrap">
            <span className="w-2 h-2 rounded-full" style={{ background: STATUS_COLOR[f.status] }} />{f.name}, {f.state}
          </span>
          <span className="block text-[10px] text-fg-muted whitespace-nowrap">
            {compact(f.output)} / {compact(f.target)} · {f.lines_running}/{f.lines_total} lines
          </span>
        </button>
      ))}
    </div>
  );
}

// card placement around each marker, chosen so the eastern plants don't collide
const CARD_POS = { PHX: 'right', CMH: 'above', KNV: 'below' };

function PlantCard({ f, hover, setHover, onOpen }) {
  const pos = CARD_POS[f.code] || (f.p.x > VB.w * 0.62 ? 'left' : 'right');
  return (
    <div className={`us-card twin-glass p-3 pos-${pos} ${hover ? 'is-hover' : ''}`}
      style={{ left: `${(f.p.x / VB.w) * 100}%`, top: `${(f.p.y / VB.h) * 100}%` }}
      onMouseEnter={() => setHover(f.code)} onMouseLeave={() => setHover(null)} onClick={() => onOpen(f.code)}
      role="button" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && onOpen(f.code)}>
      <div className="flex items-center gap-2">
        <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: STATUS_COLOR[f.status], boxShadow: `0 0 8px ${STATUS_COLOR[f.status]}` }} />
        <span className="text-[13px] font-bold text-fg">{f.name}, {f.state}</span>
        <Badge tone={STATUS_TONE[f.status]} className="ml-auto">{f.status.toUpperCase()}</Badge>
      </div>
      <div className="flex items-baseline justify-between mt-2">
        <span className="text-lg font-bold text-fg tabular-nums">{num(f.output)}</span>
        <span className="text-[10.5px] text-fg-faint">of {num(f.target)} pillows · {pct(f.achievement_pct, 0)}</span>
      </div>
      <ProgressBar value={f.output} max={f.target} tone={STATUS_TONE[f.status] === 'danger' ? 'warning' : 'primary'} className="mt-1" />
      <div className="grid grid-cols-3 gap-2 mt-2.5 text-[10.5px]">
        <div><p className="text-fg-faint">Lines</p><p className="font-bold text-fg">{f.lines_running}/{f.lines_total}</p></div>
        <div><p className="text-fg-faint">Efficiency</p><p className="font-bold text-fg">{pct(f.efficiency_pct, 0)}</p></div>
        <div><p className="text-fg-faint">At station</p><p className="font-bold text-fg">{pct(f.rfid_availability_pct, 0)}</p></div>
      </div>
      <div className="flex items-center gap-2 mt-2 text-[10.5px] text-fg-muted">
        <Icon name="users" size={12} />{f.badges.on_floor} badges · <span className="text-success">{f.badges.at_station}</span> at station ·
        <span className="text-warning">{f.badges.away}</span> away
      </div>
      {f.lines_down.length > 0 && (
        <p className="text-[10.5px] text-danger mt-1.5 font-semibold">{f.lines_down.map((l) => `${l.name} ${l.status.toLowerCase()}`).join(' · ')}</p>
      )}
      {!f.lines_down.length && f.top_alert && <p className="text-[10.5px] text-fg-muted mt-1.5 line-clamp-2">⚠ {f.top_alert}</p>}
      <p className="text-[10.5px] font-bold mt-2 flex items-center gap-1" style={{ color: 'var(--twin-cyan)' }}>
        Open facility twin <Icon name="arrowRight" size={12} />
      </p>
    </div>
  );
}

/** Full network view inside the twin explorer. */
export default function UsNetworkMap({ data, onOpen }) {
  const [hover, setHover] = useState(null);
  const { plants, lanes, dcs } = useGeo(data);
  const totals = useMemo(() => {
    const fs = data?.factories || [];
    const sum = (k) => fs.reduce((a, f) => a + (f[k] || 0), 0);
    return {
      output: sum('output'), target: sum('target'),
      lines: fs.reduce((a, f) => a + f.lines_running, 0), linesTotal: sum('lines_total'),
      onFloor: fs.reduce((a, f) => a + f.badges.on_floor, 0), atStation: fs.reduce((a, f) => a + f.badges.at_station, 0),
      away: fs.reduce((a, f) => a + f.badges.away, 0), onBreak: fs.reduce((a, f) => a + f.badges.on_break, 0),
      alerts: fs.reduce((a, f) => a + f.alerts.critical, 0),
    };
  }, [data]);
  if (!data) return null;
  return (
    <div className="flex-1 min-h-0 flex flex-col lg:flex-row">
      <div className="relative flex-1 min-w-0 min-h-[320px] flex items-center justify-center p-4 lg:p-8 overflow-hidden">
        <div className="relative w-full max-w-[1200px]" style={{ aspectRatio: `${VB.w} / ${VB.h}` }}>
          <svg viewBox={`0 0 ${VB.w} ${VB.h}`} className="absolute inset-0 w-full h-full" role="img" aria-label="US manufacturing network">
            <defs>
              <radialGradient id="plantGlow">
                <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.35" />
                <stop offset="100%" stopColor="#38bdf8" stopOpacity="0" />
              </radialGradient>
            </defs>
            {STATES.map((s) => (
              <path key={s.id} d={s.d} className={`us-map-state ${HOME_STATES[s.id] ? 'is-home' : ''}`}>
                <title>{s.name}</title>
              </path>
            ))}
            <path d={NATION} className="us-map-nation" />
            {lanes.map((l) => {
              const dim = hover && hover !== l.factory;
              return (
                <g key={`${l.truck}-${l.order}`}>
                  <path d={l.d} className={`us-lane ${dim ? 'is-dim' : ''}`} fill="none" style={{ stroke: '#38bdf8' }} />
                  <g transform={`translate(${l.truck.x},${l.truck.y})`} opacity={dim ? 0.2 : 1}>
                    <circle r={7} fill="#38bdf8" opacity={0.25} />
                    <circle r={3.8} fill="#e0f2fe" />
                    <title>{`${l.truck} · ${l.carrier} → ${l.destination}\n${l.order} · ${num(l.pillows)} pillows · ${Math.round(l.progress * 100)}% of route`}</title>
                  </g>
                </g>
              );
            })}
            {dcs.map((d) => (
              <g key={d.code} transform={`translate(${d.p.x},${d.p.y})`}>
                <rect x={-4} y={-4} width={8} height={8} rx={1.5} fill="#0b1324" stroke="#7dd3fc" strokeWidth={1.2} />
                <title>{d.name}</title>
              </g>
            ))}
            {plants.map((f) => (
              <g key={f.code} transform={`translate(${f.p.x},${f.p.y})`} style={{ cursor: 'pointer' }}
                onMouseEnter={() => setHover(f.code)} onMouseLeave={() => setHover(null)} onClick={() => onOpen(f.code)}>
                <circle r={46} fill="url(#plantGlow)" />
                <circle r={11} className="us-pulse" fill={STATUS_COLOR[f.status]} />
                <circle r={11} className="us-pulse" fill={STATUS_COLOR[f.status]} style={{ animationDelay: '1.1s' }} />
                <circle r={9} fill={STATUS_COLOR[f.status]} stroke="#050a14" strokeWidth={3} />
                <circle r={3} fill="#050a14" />
              </g>
            ))}
          </svg>
          {plants.map((f) => <PlantCard key={f.code} f={f} hover={hover === f.code} setHover={setHover} onOpen={onOpen} />)}
        </div>
        <div className="absolute left-4 bottom-4 twin-glass px-3 py-2 text-[10.5px] text-fg-muted flex flex-wrap gap-x-4 gap-y-1 max-w-[90%]">
          {Object.entries(STATUS_COLOR).map(([k, c]) => (
            <span key={k} className="flex items-center gap-1.5"><span className="twin-legend-dot" style={{ background: c }} />{k}</span>
          ))}
          <span className="flex items-center gap-1.5"><span className="twin-legend-dot" style={{ background: '#e0f2fe' }} />Truck in transit</span>
          <span className="flex items-center gap-1.5"><span className="inline-block w-2.5 h-2.5 rounded-sm border" style={{ borderColor: '#7dd3fc' }} />Customer DC</span>
        </div>
      </div>
      <aside className="w-full lg:w-[340px] shrink-0 border-t lg:border-t-0 lg:border-l twin-scroll overflow-y-auto p-4 space-y-3"
        style={{ borderColor: 'var(--twin-line)', background: 'rgba(6, 12, 24, 0.55)' }}>
        <div>
          <p className="label-caps">US manufacturing network</p>
          <p className="text-[11px] text-fg-faint mt-0.5">Live · shift {data.shift}</p>
        </div>
        <div className="twin-glass p-3">
          <div className="flex items-baseline justify-between">
            <span className="text-2xl font-bold tabular-nums">{num(totals.output)}</span>
            <span className="text-[11px] text-fg-faint">of {num(totals.target)} pillows today</span>
          </div>
          <ProgressBar value={totals.output} max={totals.target} className="mt-1.5" />
          <div className="grid grid-cols-3 gap-2 mt-3 text-[11px]">
            <div><p className="text-fg-faint">Lines running</p><p className="font-bold">{totals.lines}/{totals.linesTotal}</p></div>
            <div><p className="text-fg-faint">Badges on floor</p><p className="font-bold">{totals.onFloor}</p></div>
            <div><p className="text-fg-faint">Critical alerts</p><p className={`font-bold ${totals.alerts ? 'text-danger' : ''}`}>{totals.alerts}</p></div>
          </div>
          <div className="flex h-2 rounded-full overflow-hidden mt-3" title="RFID badge states across the network">
            <span style={{ flex: totals.atStation, background: '#22c55e' }} />
            <span style={{ flex: totals.away, background: '#f59e0b' }} />
            <span style={{ flex: totals.onBreak, background: '#38bdf8' }} />
          </div>
          <p className="text-[10.5px] text-fg-muted mt-1.5">
            <span className="text-success">{totals.atStation} at station</span> · <span className="text-warning">{totals.away} away</span> · <span style={{ color: '#38bdf8' }}>{totals.onBreak} on break</span>
          </p>
        </div>
        <p className="label-caps pt-1">Plants</p>
        {plants.map((f) => (
          <button key={f.code} type="button" onClick={() => onOpen(f.code)} onMouseEnter={() => setHover(f.code)} onMouseLeave={() => setHover(null)}
            className="w-full text-left twin-glass p-3 hover:border-sky-400/50 transition-colors" style={hover === f.code ? { borderColor: 'rgba(56,189,248,.55)' } : undefined}>
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full" style={{ background: STATUS_COLOR[f.status] }} />
              <span className="text-[12.5px] font-bold">{f.name}, {f.state}</span>
              <span className="ml-auto text-[11px] text-fg-muted tabular-nums">{pct(f.achievement_pct, 0)}</span>
            </div>
            <div className="flex gap-3 text-[10.5px] text-fg-faint mt-1">
              <span>{f.lines_running}/{f.lines_total} lines</span>
              <span>{f.machines_at_risk} machines at risk</span>
              <span>{f.alerts.total} alerts</span>
            </div>
          </button>
        ))}
        <p className="label-caps pt-1">On the road · {lanes.length}</p>
        {lanes.length === 0 && <p className="text-[11px] text-fg-faint">No loads in transit right now.</p>}
        {lanes.slice(0, 8).map((l) => (
          <div key={`${l.truck}-${l.order}`} className="text-[11px] flex items-center gap-2">
            <Icon name="truck" size={13} className="text-fg-faint shrink-0" />
            <span className="min-w-0 flex-1 truncate"><b>{l.factory}</b> → {l.destination.split(' · ')[1] || l.destination}</span>
            <span className="text-fg-faint tabular-nums">{Math.round(l.progress * 100)}%</span>
          </div>
        ))}
      </aside>
    </div>
  );
}
