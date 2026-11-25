import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, FilterChips, Icon, ProgressBar, Tabs } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { Empty, ErrorBox, Loading } from '../../components/common';
import { SiapAdvisoryList } from '../../components/common/SiapAdvisories';
import { useApi } from '../../hooks/useApi';
import { compact, healthTone, hhmm, linkTo, num, pct, statusTone } from '../../utils';
import { createFacilityScene } from './scene';
import { ROLE_COLOR, STATE_COLOR } from './layout';
import WorkerDrawer, { STATE_LABEL, stateColor } from './WorkerDrawer';
import CctvWall from '../../components/cctv/CctvWall';

const LAYERS = [
  ['workers', 'Workers', 'users'], ['flow', 'Pillow flow', 'route'], ['counts', 'Counts', 'grid'],
  ['labels', 'Labels', 'list'], ['readers', 'RFID readers', 'signal'], ['heat', 'Away heatmap', 'gauge'],
];
const STAGE_ICON = { raw: 'truck', fibre: 'layers', filling: 'bolt', sewing: 'edit', bagging: 'box', packing: 'camera', qa: 'shield', pallet: 'grid', fg: 'box', dispatch: 'route' };
const STAGE_SHORT = { raw: 'Receiving', fibre: 'Fibre prep', filling: 'Filling', sewing: 'Sewing', bagging: 'Bagging', packing: 'Carton pack', qa: 'QA hold', pallet: 'Palletizing', fg: 'FG racks', dispatch: 'Dispatch' };
const STAGE_ROLE_LABEL = { raw: 'Forklift', fibre: 'Filling operator', filling: 'Filler', sewing: 'Sewers', bagging: 'Bagger', packing: 'Packers', qa: 'Supervisors', pallet: 'Forklift', fg: 'Warehouse', dispatch: 'Warehouse + forklift' };

function Stat({ label, value, sub, tone }) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-wide text-fg-faint font-semibold">{label}</p>
      <p className="text-[15px] font-bold tabular-nums leading-tight" style={tone ? { color: `var(--color-${tone})` } : undefined}>{value}</p>
      {sub && <p className="text-[10px] text-fg-faint leading-tight">{sub}</p>}
    </div>
  );
}

function WorkerChip({ w, onOpen, threshold }) {
  if (!w) return null;
  return (
    <button type="button" onClick={() => onOpen(w.id)} title={`${w.name} · ${STATE_LABEL[w.state] || w.state} · ${w.zone}`}
      className="inline-flex items-center gap-1.5 rounded-full pl-1 pr-2 py-0.5 text-[10.5px] border hover:border-sky-400/60"
      style={{ borderColor: 'var(--color-border-strong)', background: 'var(--color-surface-raised)' }}>
      <span className="w-2.5 h-2.5 rounded-full" style={{ background: stateColor(w, threshold), boxShadow: `0 0 0 2px ${ROLE_COLOR[w.role]}55` }} />
      <span className="font-semibold text-fg">{w.name}</span>
      {w.state === 'Away' && <span className="text-warning tabular-nums">{num(w.minutes_in_state)}m</span>}
      {w.state !== 'Away' && w.away_min > 0 && <span className="text-fg-faint tabular-nums">{num(w.away_min)}m away</span>}
    </button>
  );
}

function FlowStrip({ flow, active, onPick }) {
  return (
    <div className="twin-flow twin-scroll">
      {flow.map((s, i) => (
        <React.Fragment key={s.key}>
          <button type="button" className={`twin-stage ${active === s.key ? 'is-active' : ''}`} onClick={() => onPick(active === s.key ? null : s.key)}
            title={s.description}>
            <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-fg-faint">
              <Icon name={STAGE_ICON[s.key] || 'box'} size={11} />
              <span className="truncate">{STAGE_SHORT[s.key] || s.label}</span>
            </div>
            <div className="flex items-baseline gap-1 mt-1">
              <span className="text-[17px] font-bold tabular-nums text-fg">{s.today != null ? compact(s.today) : compact(s.now)}</span>
              <span className="text-[9.5px] text-fg-faint">{s.today != null ? (s.unit === 'pillows' ? 'today' : s.unit) : (s.unit === 'pillows' ? 'now' : s.unit)}</span>
            </div>
            {s.today != null && s.now != null && s.key !== 'filling' && (
              <p className="text-[10px] text-sky-300 tabular-nums">{num(s.now)} {s.key === 'dispatch' ? 'picked, waiting' : 'in process'}</p>
            )}
            {(s.today == null || s.now == null) && s.extra && <p className="text-[10px] text-sky-300 leading-tight line-clamp-2" title={s.extra}>{s.extra}</p>}
            <div className="flex items-center gap-1 mt-1.5 text-[10px] text-fg-muted">
              <Icon name="users" size={10} />
              {s.workers ? (
                <span className="truncate">
                  <span className="text-success">{s.at_station}</span>/{s.workers} {STAGE_ROLE_LABEL[s.key]}
                  {s.away ? <span className="text-warning"> · {s.away} away</span> : ''}
                </span>
              ) : <span className="truncate">{s.key === 'fibre' ? 'tended by fillers' : '—'}</span>}
            </div>
            {s.per_worker_hour != null && <p className="text-[10px] text-fg-faint tabular-nums">{num(s.per_worker_hour)} /worker-h</p>}
          </button>
          {i < flow.length - 1 && <span className="twin-arrow"><Icon name="chevronRight" size={14} /></span>}
        </React.Fragment>
      ))}
    </div>
  );
}

function LinePanel({ line, workers, onWorker, threshold, onClose }) {
  const navigate = useNavigate();
  const wm = useMemo(() => Object.fromEntries(workers.map((w) => [w.id, w])), [workers]);
  const s = line.stages;
  const steps = [
    { key: 'fibre', label: 'Fibre opening & carding', value: `${num(s.fibre.kg_today)} kg`, sub: `≈ ${num(s.fibre.pillow_equivalent)} pillows of fibre · ${num(s.fibre.kg_per_hour)} kg/h at rate`, crew: [], machines: s.fibre.machines },
    { key: 'filling', label: 'Pillow filling', value: num(s.filling.today), sub: 'pillows filled today', crew: s.filling.workers, machines: s.filling.machines },
    { key: 'sewing', label: 'Sewing & closing', value: num(s.sewing.today), sub: `sewn · ${num(s.sewing.wip)} waiting at the sewing table`, crew: s.sewing.workers },
    { key: 'bagging', label: 'Bagging & compression', value: num(s.bagging.today), sub: `bagged · ${num(s.bagging.wip)} waiting to bag`, crew: s.bagging.workers },
    { key: 'packing', label: 'To carton packing', value: num(s.packing.queue), sub: 'pillows queued at the packing cell', crew: s.packing.workers },
  ];
  const mById = Object.fromEntries(line.machines.map((mc) => [mc.id, mc]));
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-[15px] font-bold text-fg">{line.name} <span className="text-fg-faint font-semibold text-[12px]">{line.code}</span></p>
          <p className="text-[11px] text-fg-muted">{line.sku ? `${line.sku} · ${line.customer}` : 'No order loaded'}</p>
        </div>
        <Badge tone={statusTone(line.status)} dot>{line.status}</Badge>
        <button type="button" className="text-fg-faint hover:text-fg" onClick={onClose} aria-label="Close line"><Icon name="close" size={15} /></button>
      </div>
      {line.status_reason && <p className="tile tone-warning text-[11px]">{line.status_reason}</p>}
      <div className="grid grid-cols-3 gap-2 tile">
        <Stat label="Output today" value={num(line.output_today)} sub={`plan ${num(line.planned_to_date)}`} />
        <Stat label="Efficiency" value={pct(line.efficiency_pct, 0)} tone={line.efficiency_pct >= 95 ? 'success' : line.efficiency_pct >= 85 ? 'warning' : 'danger'} sub={`${num(line.last_hour)} last hour`} />
        <Stat label="Rate" value={`${num(line.rate_target)}/h`} sub={`${num(line.downtime_min)} min down`} />
      </div>
      {line.po && (
        <div className="tile">
          <div className="flex items-center justify-between text-[11px]">
            <span className="font-semibold text-fg">{line.po.number}{line.po.order ? ` · ${line.po.order}` : ''}</span>
            <Badge tone={statusTone(line.po.status)}>{line.po.status}</Badge>
          </div>
          <ProgressBar value={line.po.completed} max={line.po.required} className="mt-1.5" tone={line.po.status === 'Delayed' ? 'danger' : 'primary'} />
          <p className="text-[10.5px] text-fg-muted mt-1">{num(line.po.completed)} / {num(line.po.required)} · due {hhmm(line.po.due)} {line.po.due?.slice(5, 10)} · est. {hhmm(line.po.eta)}</p>
        </div>
      )}
      <p className="label-caps">Pillows at each station and who works it</p>
      <div className="space-y-2">
        {steps.map((st, i) => (
          <div key={st.key} className="tile relative">
            {i < steps.length - 1 && <span className="absolute left-[18px] -bottom-2.5 w-px h-2.5" style={{ background: 'var(--twin-line)' }} />}
            <div className="flex items-start gap-2.5">
              <span className="w-6 h-6 rounded-md grid place-items-center shrink-0 text-sky-300" style={{ background: 'rgba(56,189,248,.12)' }}>
                <Icon name={STAGE_ICON[st.key]} size={13} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-[11.5px] font-semibold text-fg">{st.label}</span>
                  <span className="text-[15px] font-bold tabular-nums text-fg">{st.value}</span>
                </div>
                <p className="text-[10.5px] text-fg-faint">{st.sub}</p>
                {(st.machines || []).length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-1.5">
                    {st.machines.map((id) => mById[id]).filter(Boolean).map((mc) => (
                      <span key={mc.id} className="text-[10px] px-1.5 py-0.5 rounded border" title={`${mc.name} · health ${mc.health}`}
                        style={{ borderColor: `var(--color-${healthTone(mc.health)}-border)`, color: `var(--color-${mc.status === 'Running' ? healthTone(mc.health) : 'danger'})` }}>
                        {mc.code} · {mc.status === 'Running' ? `${Math.round(mc.health)}` : mc.status}
                      </span>
                    ))}
                  </div>
                )}
                {st.crew.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-1.5">
                    {st.crew.map((id) => <WorkerChip key={id} w={wm[id]} onOpen={onWorker} threshold={threshold} />)}
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
      {line.sewer_away_min > 0 && (
        <p className="text-[11px] text-fg-muted">
          Sewers away from the table <b className="text-fg">{num(line.sewer_away_min)} min</b> this shift ≈ <b className="text-warning">{num(line.est_pillows_lost_to_away)} pillows</b> of sewing capacity.
        </p>
      )}
      <button type="button" className="link-btn" onClick={() => navigate(linkTo('production', 'line', line.id))}>OPEN LINE IN PRODUCTION →</button>
    </div>
  );
}

function PlantPanel({ data, onLine }) {
  const totalOut = data.lines.reduce((a, l) => a + l.output_today, 0);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2 tile">
        <Stat label="Output today" value={num(totalOut)} sub={`target ${num(data.factory.daily_target)}`} />
        <Stat label="Packing" value={pct(data.packing.accuracy_pct, 1)} sub={`${num(data.packing.cartons_today)} cartons`} />
        <Stat label="FG stock" value={compact(data.warehouse.total_pillows)} sub={`${data.warehouse.utilization_pct}% racks`} />
      </div>
      <p className="label-caps">Lines · click to inspect</p>
      <div className="space-y-1">
        {data.lines.map((l) => (
          <button key={l.id} type="button" onClick={() => onLine(l.id)}
            className="w-full text-left px-2.5 py-2 rounded-lg hover:bg-sky-400/5 border border-transparent hover:border-sky-400/30">
            <div className="flex items-center gap-2 text-[11.5px]">
              <span className="w-2 h-2 rounded-full" style={{ background: `var(--color-${statusTone(l.status)})` }} />
              <span className="font-semibold text-fg w-28 truncate">{l.name}</span>
              <span className="text-fg-faint truncate flex-1">{l.sku || '—'}</span>
              <span className="tabular-nums text-fg font-semibold">{num(l.output_today)}</span>
              <span className="tabular-nums w-10 text-right" style={{ color: `var(--color-${l.efficiency_pct >= 95 ? 'success' : l.efficiency_pct >= 85 ? 'warning' : 'danger'})` }}>{pct(l.efficiency_pct, 0)}</span>
            </div>
            {l.status !== 'Running' && <p className="text-[10px] text-warning pl-4 truncate">{l.status_reason || l.status}</p>}
          </button>
        ))}
      </div>
      <p className="label-caps">Docks</p>
      <div className="grid grid-cols-2 gap-2">
        {data.docks.map((d) => (
          <div key={d.dock} className="tile text-[11px]">
            <div className="flex items-center justify-between"><b className="text-fg">{d.dock}</b><Badge tone={d.truck ? 'info' : 'neutral'}>{d.status}</Badge></div>
            {d.truck ? (
              <>
                <p className="text-fg-muted truncate mt-1">{d.truck} · {d.customer}</p>
                <ProgressBar value={d.loaded_qty || 0} max={d.order_qty || 1} className="mt-1" />
              </>
            ) : <p className="text-fg-faint truncate mt-1">{d.next ? `Next ${hhmm(d.next.at)} · ${d.next.customer}` : 'No appointment'}</p>}
          </div>
        ))}
      </div>
    </div>
  );
}

function WorkforcePanel({ data, onWorker }) {
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const r = data.rfid;
  const list = data.workers
    .filter((w) => w.state !== 'Scheduled')
    .filter((w) => filter === 'all' || (filter === 'flag' ? !!w.flag : w.state === filter))
    .filter((w) => !q || `${w.name} ${w.role} ${w.line || ''} ${w.code}`.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => (b.flag ? 1 : 0) - (a.flag ? 1 : 0) || b.away_min - a.away_min);
  const maxZone = Math.max(1, ...r.zones.map((z) => z.minutes));
  return (
    <div className="space-y-3">
      <div className="tile">
        <div className="flex h-2.5 rounded-full overflow-hidden">
          <span style={{ flex: r.at_station, background: STATE_COLOR.Station }} />
          <span style={{ flex: r.away, background: STATE_COLOR.Away }} />
          <span style={{ flex: r.on_break, background: STATE_COLOR.Break }} />
        </div>
        <div className="grid grid-cols-4 gap-2 mt-2">
          <Stat label="On floor" value={r.badges_on_floor} sub={`${r.absent} absent/late`} />
          <Stat label="At station" value={r.at_station} tone="success" />
          <Stat label="Away" value={r.away} tone={r.away ? 'warning' : undefined} sub={r.long_trips_now.length ? `${r.long_trips_now.length} > ${r.alert_threshold_min} min` : undefined} />
          <Stat label="On break" value={r.on_break} />
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2 tile">
        <Stat label="At station" value={pct(r.availability_pct)} sub="of working time" tone={r.availability_pct >= 95 ? 'success' : r.availability_pct >= 90 ? 'warning' : 'danger'} />
        <Stat label="Time away" value={`${num(r.away_hours, 1)} h`} sub={`${r.trips} trips · ${num(r.avg_away_min_per_worker, 1)} min/person`} />
        <Stat label="Sewing capacity" value={`≈${num(r.est_pillows_lost_to_away)}`} sub="pillows lost to sewer time away" tone={r.est_pillows_lost_to_away > 300 ? 'warning' : undefined} />
      </div>
      {r.hourly.length > 0 && (
        <div className="tile">
          <p className="label-caps mb-1">Unscheduled minutes away, by hour</p>
          <MiniChart type="bar" height={110} data={{ labels: r.hourly.map((h) => h.hour), datasets: [{ label: 'Minutes away', data: r.hourly.map((h) => h.away_min), color: 'warning' }] }} />
        </div>
      )}
      <div className="tile">
        <p className="label-caps mb-1.5">Where time away goes (reader zones)</p>
        <div className="space-y-1.5">
          {r.zones.map((z) => (
            <div key={z.zone} className="flex items-center gap-2 text-[11px]">
              <span className="w-36 truncate text-fg-muted">{z.zone}</span>
              <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--color-surface)' }}>
                <div className="h-full rounded-full" style={{ width: `${(z.minutes / maxZone) * 100}%`, background: z.zone.startsWith('No read') ? '#94a3b8' : '#f59e0b' }} />
              </div>
              <span className="w-14 text-right tabular-nums text-fg">{num(z.minutes)} min</span>
            </div>
          ))}
        </div>
      </div>
      <div className="tile p-0 overflow-hidden">
        <table className="w-full text-[11px]">
          <thead><tr className="text-fg-faint text-left"><th className="px-2.5 py-1.5 font-semibold">Role</th><th className="font-semibold">People</th><th className="font-semibold">At station</th><th className="font-semibold">Avg away</th><th className="font-semibold pr-2.5">Effect.</th></tr></thead>
          <tbody>
            {r.by_role.map((x) => (
              <tr key={x.role} className="border-t" style={{ borderColor: 'var(--color-border)' }}>
                <td className="px-2.5 py-1.5"><span className="inline-block w-2 h-2 rounded-sm mr-1.5" style={{ background: ROLE_COLOR[x.role] }} />{x.role}</td>
                <td className="tabular-nums">{x.workers}</td>
                <td className="tabular-nums">{pct(x.availability_pct, 0)}</td>
                <td className="tabular-nums">{num(x.avg_away_min, 1)}m</td>
                <td className="tabular-nums pr-2.5">{x.effectiveness_pct != null ? pct(x.effectiveness_pct, 0) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-2">
        <input className="input select-sm flex-1" placeholder="Search name, role, line…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <FilterChips value={filter} onChange={setFilter} options={[
        { value: 'all', label: 'All' }, { value: 'Station', label: 'At station' }, { value: 'Away', label: 'Away' },
        { value: 'Break', label: 'Break' }, { value: 'flag', label: 'Flagged' },
      ]} />
      <div className="-mx-1">
        {list.length === 0 && <Empty>No one matches.</Empty>}
        {list.slice(0, 80).map((w) => (
          <button key={w.id} type="button" className="twin-worker-row" onClick={() => onWorker(w.id)}>
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: stateColor(w, r.alert_threshold_min) }} />
            <span className="min-w-0">
              <span className="block text-[11.5px] font-semibold text-fg truncate">{w.name} <span className="font-normal text-fg-faint">· {w.role}{w.line ? ` · ${w.line.replace(' Line ', ' ')}` : ''}</span></span>
              <span className="block text-[10.5px] text-fg-faint truncate">{STATE_LABEL[w.state] || w.state} · {w.zone}{w.flag ? ` · ⚠ ${w.flag}` : ''}</span>
            </span>
            <span className="text-right">
              <span className="block text-[11px] tabular-nums text-fg">{num(w.away_min)}m away</span>
              <span className="block text-[10px] tabular-nums text-fg-faint">{w.effectiveness_pct != null ? `eff ${pct(w.effectiveness_pct, 0)}` : ''}</span>
            </span>
          </button>
        ))}
      </div>
      <p className="text-[10px] text-fg-faint">{data.disclaimer}</p>
    </div>
  );
}

export default function FacilityTwin({ code }) {
  const { data, error, reload } = useApi(`/api/twin/facility/${code}`, {}, { every: 1 });
  const holder = useRef(null);
  const api = useRef(null);
  const [fail, setFail] = useState(null);
  const [view, setView] = useState('top');
  const [layers, setLayers] = useState({ workers: true, flow: true, counts: true, labels: true, readers: true, heat: false });
  const [line, setLine] = useState(null);
  const [stage, setStage] = useState(null);
  const [worker, setWorker] = useState(null);
  const [hover, setHover] = useState(null);
  const [tab, setTab] = useState('floor');
  const [area, setArea] = useState(null);
  const [legend, setLegend] = useState(false);

  useEffect(() => {
    if (!data || api.current || !holder.current) return;
    try {
      api.current = createFacilityScene(holder.current, data, {
        onSelectLine: (id) => { setLine(id); setArea(null); if (id) setTab('floor'); },
        onSelectWorker: (id) => setWorker(id),
        onSelectArea: (u) => { setArea(u); setLine(null); setTab('floor'); },
        onHover: (info, x, y) => setHover(info ? { info, x, y } : null),
      });
    } catch (e) {
      setFail(e.message || 'WebGL is not available in this browser.');
    }
  }, [data]);
  useEffect(() => () => { api.current?.dispose(); api.current = null; }, []);
  useEffect(() => { if (data && api.current) api.current.update(data); }, [data]);
  useEffect(() => { api.current?.select(line); }, [line]);
  useEffect(() => { api.current?.focusStage(stage); }, [stage]);

  const toggleLayer = (k) => setLayers((l) => {
    const n = { ...l, [k]: !l[k] };
    api.current?.setLayers(n);
    return n;
  });
  const selLine = data?.lines.find((l) => l.id === line);
  const r = data?.rfid;
  const totalOut = data ? data.lines.reduce((a, l) => a + l.output_today, 0) : 0;
  const running = data ? data.lines.filter((l) => l.status === 'Running').length : 0;
  const threshold = r?.alert_threshold_min || 10;

  return (
    <div className="flex-1 min-h-0 flex flex-col lg:flex-row">
      <div className="relative flex-1 min-w-0 flex flex-col min-h-[420px]">
        <div className="relative flex-1 min-h-0">
          <div ref={holder} className="absolute inset-0" />
          {!data && !error && <div className="absolute inset-0 grid place-items-center"><Loading rows={3} /></div>}
          {(error || fail) && <div className="absolute inset-x-4 top-4"><ErrorBox error={error || fail} onRetry={reload} /></div>}
          {data && (
            <>
              <div className="absolute left-3 top-3 twin-glass px-3.5 py-2.5 max-w-[calc(100%-24px)]" style={{ pointerEvents: 'auto' }}>
                <div className="flex items-center gap-2">
                  <span className="text-[14px] font-bold">{data.factory.name}, {data.factory.state}</span>
                  <Badge tone="info">SHIFT {data.shift}</Badge>
                  <span className="text-[11px] text-fg-faint">{hhmm(data.shift_window[0])}–{hhmm(data.shift_window[1])}</span>
                </div>
                <div className="flex gap-5 mt-1.5">
                  <Stat label="Pillows today" value={num(totalOut)} sub={`of ${compact(data.factory.daily_target)}`} />
                  <Stat label="Lines running" value={`${running}/${data.lines.length}`} tone={running < data.lines.length ? 'warning' : 'success'} />
                  <Stat label="At station" value={`${r.at_station}/${r.badges_on_floor}`} sub={`${pct(r.availability_pct, 1)} of time`} tone="success" />
                  <Stat label="Away now" value={r.away} tone={r.away ? 'warning' : undefined} sub={`${r.on_break} on break`} />
                </div>
              </div>
              <div className="absolute right-3 top-3 twin-glass p-1 flex flex-col gap-1" style={{ pointerEvents: 'auto' }}>
                <div className="flex gap-1">
                  {[['top', 'Top view'], ['3d', '3D']].map(([v, lbl]) => (
                    <button key={v} type="button" className={`twin-tool ${view === v ? 'is-active' : ''}`}
                      onClick={() => { setView(v); api.current?.setView(v); }}>{lbl}</button>
                  ))}
                  <button type="button" className="twin-tool" title="Zoom in" onClick={() => api.current?.zoomBy(1.3)}><Icon name="plus" size={13} /></button>
                  <button type="button" className="twin-tool" title="Zoom out" onClick={() => api.current?.zoomBy(1 / 1.3)}>−</button>
                  <button type="button" className="twin-tool" title="Reset view" onClick={() => { setLine(null); api.current?.resetView(); }}><Icon name="refresh" size={13} /></button>
                </div>
                <div className="flex flex-wrap gap-1 max-w-[340px]">
                  {LAYERS.map(([k, lbl, icon]) => (
                    <button key={k} type="button" className={`twin-tool ${layers[k] ? 'is-on' : ''}`} onClick={() => toggleLayer(k)} style={{ height: 26, fontSize: 10.5 }}>
                      <Icon name={icon} size={11} />{lbl}
                    </button>
                  ))}
                </div>
              </div>
              <div className="absolute left-3 bottom-3 twin-glass px-3 py-2 text-[10px] text-fg-muted hidden md:block" style={{ pointerEvents: 'auto' }}>
                <div className="flex gap-3 items-center">
                  <span className="font-bold text-fg-faint uppercase tracking-wide">RFID badge</span>
                  {[['At station', STATE_COLOR.Station], ['Away', STATE_COLOR.Away], [`Away > ${threshold} min`, STATE_COLOR.Long], ['Break', STATE_COLOR.Break], ['No read', STATE_COLOR.NoRead]].map(([k, c]) => (
                    <span key={k} className="flex items-center gap-1"><span className="twin-legend-dot" style={{ background: c }} />{k}</span>
                  ))}
                  <button type="button" className="link-btn ml-1" onClick={() => setLegend((v) => !v)}>{legend ? 'LESS' : 'ROLES & HELP'}</button>
                </div>
                {legend && (
                  <>
                    <div className="grid grid-cols-4 gap-x-3 gap-y-0.5 mt-1.5">
                      {Object.entries(ROLE_COLOR).map(([k, c]) => (
                        <span key={k} className="flex items-center gap-1 whitespace-nowrap"><span className="inline-block w-2 h-2 rounded-sm" style={{ background: c }} />{k.replace(' operator', '').replace(' technician', '').replace(' associate', '')}</span>
                      ))}
                    </div>
                    <p className="mt-1.5 text-fg-faint">{view === 'top' ? 'Drag to pan · scroll to zoom · click a line or a person' : 'Drag to orbit · right-drag to pan · scroll to zoom'}</p>
                  </>
                )}
              </div>
              {hover && <HoverCard hover={hover} threshold={threshold} />}
            </>
          )}
        </div>
        {data && (
          <div className="shrink-0 border-t px-3 pt-2 pb-2.5" style={{ borderColor: 'var(--twin-line)', background: 'rgba(6,12,24,.7)' }}>
            <div className="flex items-center gap-2 mb-1.5">
              <p className="label-caps">Pillow flow · {data.factory.name}</p>
              <span className="text-[10.5px] text-fg-faint">Pillows at each step, today and in process now, with the people working that step (RFID). Click a step to highlight it.</span>
            </div>
            <FlowStrip flow={data.flow} active={stage} onPick={setStage} />
          </div>
        )}
      </div>
      <aside className="w-full lg:w-[400px] shrink-0 flex flex-col min-h-0 border-t lg:border-t-0 lg:border-l" style={{ borderColor: 'var(--twin-line)', background: 'rgba(6,12,24,.6)' }}>
        <div className="px-3 pt-3">
          <Tabs size="sm" value={tab} onChange={setTab} tabs={[
            { value: 'floor', label: selLine ? selLine.code.split('-')[1] : 'Floor' },
            { value: 'workforce', label: 'Workforce · RFID' },
            { value: 'siap', label: 'S!AP advisories' },
            { value: 'cctv', label: 'CCTV' },
          ]} />
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto twin-scroll p-3">
          {!data && <Loading rows={6} />}
          {data && tab === 'floor' && (
            <>
              {area && !selLine && <AreaCard area={area} data={data} onClose={() => setArea(null)} />}
              {selLine
                ? <LinePanel line={selLine} workers={data.workers} onWorker={setWorker} threshold={threshold} onClose={() => setLine(null)} />
                : <PlantPanel data={data} onLine={setLine} />}
            </>
          )}
          {data && tab === 'workforce' && <WorkforcePanel data={data} onWorker={setWorker} />}
          {data && tab === 'cctv' && (
            <div className="space-y-2">
              <p className="text-[11px] text-fg-muted">Live cameras at {data.factory.name} with computer-vision overlays. Open one for worker identities (RFID), pillow counts and carton labels.</p>
              <CctvWall factory={code} cols={1} onOpenWorker={setWorker} />
            </div>
          )}
          {data && tab === 'siap' && (
            <div className="space-y-2">
              <p className="text-[11px] text-fg-muted">Predictions and recommendations for {data.factory.name}: machine repairs, quality risk, spares and RFID workforce patterns.</p>
              <SiapAdvisoryList factory={code} compact />
            </div>
          )}
        </div>
      </aside>
      {worker && <WorkerDrawer id={worker} onClose={() => setWorker(null)} threshold={threshold} />}
    </div>
  );
}

function AreaCard({ area, data, onClose }) {
  let title = '';
  let body = null;
  if (area.kind === 'qa') {
    title = 'QA hold';
    body = (
      <>
        <p className="text-[11px] text-fg-muted">{data.packing.blocked_cartons} blocked cartons · {num(data.packing.held_pillows)} pillows held for recount or rework.</p>
        <div className="mt-1.5 space-y-1">
          {data.packing.blocked.slice(0, 6).map((c) => (
            <p key={c.id} className="text-[10.5px] text-fg-faint">{c.code} · {c.result} · counted {c.detected}/{c.required}</p>
          ))}
        </div>
      </>
    );
  } else if (area.kind === 'packing') {
    const ls = [data.lines[area.cell * 2], data.lines[area.cell * 2 + 1]].filter(Boolean);
    title = `Packing cell ${area.cell + 1}`;
    body = <p className="text-[11px] text-fg-muted">Camera-verified carton packing for {ls.map((l) => l.name).join(' & ')} · {num(ls.reduce((a, l) => a + l.stages.packing.queue, 0))} pillows queued.</p>;
  } else if (area.kind === 'rack') {
    title = `Rack zone ${area.zone}`;
    body = <p className="text-[11px] text-fg-muted">{(data.warehouse.racks[area.zone] || []).length} of 48 pallet positions occupied · {num(data.warehouse.total_pillows)} pillows in finished goods ({num(data.warehouse.reserved_pillows)} reserved).</p>;
  } else if (area.kind === 'dock') {
    const d = data.docks.find((x) => x.dock === area.dock);
    title = `Dock ${area.dock}`;
    body = d && <p className="text-[11px] text-fg-muted">{d.truck ? `${d.truck} (${d.carrier}) · ${d.customer} · ${d.order} · loaded ${num(d.loaded_qty)} of ${num(d.order_qty)}` : 'Free'}</p>;
  }
  return (
    <div className="tile mb-3" style={{ borderColor: 'var(--twin-line)' }}>
      <div className="flex items-center justify-between mb-1"><p className="text-[12.5px] font-bold text-fg">{title}</p>
        <button type="button" onClick={onClose} className="text-fg-faint hover:text-fg" aria-label="Close"><Icon name="close" size={13} /></button></div>
      {body}
    </div>
  );
}

function HoverCard({ hover, threshold }) {
  const { info, x, y } = hover;
  let content = null;
  if (info.kind === 'worker' && info.worker) {
    const w = info.worker;
    content = (
      <>
        <p className="font-bold text-fg flex items-center gap-1.5"><span className="w-2 h-2 rounded-full" style={{ background: stateColor(w, threshold) }} />{w.name}</p>
        <p className="text-fg-muted">{w.role}{w.line ? ` · ${w.line}` : ''}</p>
        <p className="text-fg-muted">{STATE_LABEL[w.state] || w.state} · {w.zone}{w.minutes_in_state != null ? ` · ${num(w.minutes_in_state)} min` : ''}</p>
        <p className="text-fg-faint mt-1">Away {num(w.away_min)} min ({w.away_trips} trips) · at station {pct(w.availability_pct, 0)}</p>
        <p className="text-sky-300 mt-1">Click for badge timeline</p>
      </>
    );
  } else if (info.kind === 'line' && info.line) {
    const l = info.line;
    const stationNames = { fibre: 'Fibre opening & carding', filling: 'Pillow filling', sewing: 'Sewing & closing', bagging: 'Bagging & compression', packing: 'Conveyor to packing' };
    const mc = info.machine ? l.machines.find((m) => m.code === info.machine) : null;
    content = (
      <>
        <p className="font-bold text-fg">{l.name} · {stationNames[info.station] || ''}</p>
        {mc && <p className="text-fg-muted">{mc.code} {mc.name} · {mc.status} · health {Math.round(mc.health)}</p>}
        <p className="text-fg-muted">{l.status} · {num(l.output_today)} pillows today · {pct(l.efficiency_pct, 0)}</p>
        <p className="text-fg-faint">Filled {num(l.stages.filling.today)} · sewn {num(l.stages.sewing.today)} · bagged {num(l.stages.bagging.today)}</p>
      </>
    );
  } else if (info.kind === 'dock' && info.dock) {
    const d = info.dock;
    content = <><p className="font-bold text-fg">Dock {d.dock}</p><p className="text-fg-muted">{d.truck ? `${d.truck} · ${d.customer} · ${d.status}` : 'Free'}</p></>;
  } else if (info.kind === 'qa') content = <p className="font-bold text-fg">QA hold cage</p>;
  else if (info.kind === 'rack') content = <p className="font-bold text-fg">Finished goods rack zone {info.zone}</p>;
  else if (info.kind === 'packing') content = <p className="font-bold text-fg">Packing cell {info.cell + 1} · CV pillow count</p>;
  if (!content) return null;
  return <div className="twin-tooltip twin-glass" style={{ left: x, top: y }}>{content}</div>;
}
