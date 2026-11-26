import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, Icon } from '../../components/ui';
import { ErrorBox } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { hhmm, num, severityTone } from '../../utils';
import LossIncidentDrawer from '../../components/loss/LossIncidentDrawer';
import { usd } from '../../components/loss/lossShared';
import { createPlantScene, HALL } from './factoryTwin/plantScene';
import { LINE_STAGES, MACHINE_STAGE, STAGES, STAGE_BY_KEY, STATUS_TONE, SUPPORT, risksByStage, stageStatus } from './factoryTwin/stages';
import './factoryTwin/factoryTwin.css';

const CAMERAS = [['overview', 'Overview'], ['front', 'Front'], ['side', 'Side'], ['top', 'Top']];
const SPEEDS = [0.5, 1, 2, 4];
const STEP_MS = 4200;
const LINE_STATUS = { Running: '#22c55e', Changeover: '#f59e0b', Idle: '#64748b', Breakdown: '#ef4444', Maintenance: '#f97316' };
const SEV_COLOR = { danger: '#ef4444', warning: '#f59e0b', info: '#38bdf8', neutral: '#94a3b8' };

/* ── 3D viewport (one scene per plant) ───────────────────────────────── */
function PlantViewport({ data, sceneRef, onSelectStage, onOpenLine, onOpenMachine, onOpenLoss, onHover, preset }) {
  const host = useRef(null);
  const cbs = useRef({});
  cbs.current = { onSelectStage, onOpenLine, onOpenMachine, onOpenLoss, onHover };
  const first = useRef(data);
  useEffect(() => {
    const s = createPlantScene(host.current, first.current, {
      onSelectStage: (k) => cbs.current.onSelectStage?.(k),
      onOpenLine: (id) => cbs.current.onOpenLine?.(id),
      onOpenMachine: (id) => cbs.current.onOpenMachine?.(id),
      onOpenLoss: (id) => cbs.current.onOpenLoss?.(id),
      onHover: (u, x, y) => cbs.current.onHover?.(u, x, y),
    }, { preset });
    sceneRef.current = s;
    return () => { s.dispose(); sceneRef.current = null; };
  }, [sceneRef, preset]);
  useEffect(() => {
    if (!sceneRef.current || !data) return;
    sceneRef.current.update(data);
    sceneRef.current.setIncidents(data.loss || []);   // loss-prevention beacons, also in compact mode
  }, [data, sceneRef]);
  return <div ref={host} className="ftw-canvas" />;
}

/* ── stage detail (canvas: stage modal → side panel) ─────────────────── */
function lineMetric(ln, key) {
  const st = ln.stages || {};
  switch (key) {
    case 'opening': return `${num(st.fibre?.kg_today)} kg`;
    case 'carding': return `${num(st.fibre?.kg_per_hour)} kg/h`;
    case 'filling': return `${num(st.filling?.today)} filled`;
    case 'sewing': return `${num(st.sewing?.today)} · WIP ${num(st.sewing?.wip)}`;
    case 'bagging': return `${num(st.bagging?.today)} · WIP ${num(st.bagging?.wip)}`;
    case 'packing': return `queue ${num(st.packing?.queue)}`;
    default: return '';
  }
}

function stageKpis(key, fac, flow, summary) {
  const lines = fac.lines || [];
  const sum = (f) => lines.reduce((s, l) => s + (f(l) || 0), 0);
  const running = lines.filter((l) => l.status === 'Running').length;
  const mc = lines.flatMap((l) => l.machines.filter((m) => MACHINE_STAGE[m.stage] === key));
  const healthy = mc.filter((m) => m.health_status === 'Healthy').length;
  switch (key) {
    case 'raw': return [['Fibre on hand', `${num(fac.raw?.fibre_kg)} kg`], ['Pillow shells', num(fac.raw?.shells)],
      ['Trucks unloading', num((fac.receiving || []).length)], ['Fibre for', `≈ ${num((fac.raw?.fibre_kg || 0) / Math.max(1, sum((l) => l.stages?.fibre?.kg_per_hour)))} h`]];
    case 'opening': return [['Opened today', `${num(flow.today)} kg`], ['Opening rate', `${num(sum((l) => l.stages?.fibre?.kg_per_hour))} kg/h`],
      ['Machines healthy', `${healthy}/${mc.length}`], ['Lines running', `${running}/${lines.length}`]];
    case 'carding': return [['Bun output', `${num(sum((l) => l.stages?.fibre?.kg_per_hour))} kg/h`], ['Pillow equivalent', flow.extra || '—'],
      ['Machines healthy', `${healthy}/${mc.length}`], ['Garnett / blow', `${lines.filter((l) => l.type === 'Garnett').length} / ${lines.filter((l) => l.type === 'Blow').length}`]];
    case 'filling': return [['Filled today', num(flow.today)], ['Waiting at sewing', num(fac.flow?.find((f) => f.key === 'sewing')?.now)],
      ['Machines healthy', `${healthy}/${mc.length}`], ['Per worker-hour', num(flow.per_worker_hour)]];
    case 'sewing': return [['Sewn today', num(flow.today)], ['WIP to sew', num(flow.now)],
      ['Pillows lost to time away', num(sum((l) => l.est_pillows_lost_to_away))], ['Per worker-hour', num(flow.per_worker_hour)]];
    case 'bagging': return [['Bagged today', num(flow.today)], ['WIP to bag', num(flow.now)],
      ['Output vs target', `${summary?.production?.achievement_pct ?? '—'}%`], ['Per worker-hour', num(flow.per_worker_hour)]];
    case 'packing': return [['Cartons today', num(fac.packing?.cartons_today)], ['Pillows packed', num(fac.packing?.pillows_today)],
      ['CV count accuracy', `${fac.packing?.accuracy_pct ?? '—'}%`], ['Queue at packing', num(fac.packing?.queue)]];
    case 'qa': return [['Blocked cartons', num(fac.packing?.blocked_cartons)], ['Pillows on hold', num(fac.packing?.held_pillows)],
      ['CV accuracy', `${fac.packing?.accuracy_pct ?? '—'}%`], ['Quality (OEE)', `${summary?.oee?.quality_pct ?? '—'}%`]];
    case 'pallet': return [['Open pallets', num(fac.palletizing?.open_pallets)], ['To put away', num(fac.palletizing?.pending_putaway)],
      ['Pillows on pallets', num(fac.palletizing?.pillows)], ['Put-away pillows', num(fac.warehouse?.pending_putaway_pillows)]];
    case 'fg': return [['Stored pallets', `${num(fac.warehouse?.stored_pallets)}/${num(fac.warehouse?.capacity_pallets)}`],
      ['Rack utilization', `${fac.warehouse?.utilization_pct ?? '—'}%`], ['Available pillows', num(fac.warehouse?.available_pillows)],
      ['Reserved pillows', num(fac.warehouse?.reserved_pillows)]];
    case 'dispatch': return [['Docks busy', `${(fac.docks || []).filter((d) => d.truck).length}/${(fac.docks || []).length}`],
      ['Loaded today', num(flow.today)], ['To pick', num(fac.warehouse?.pending_pick_pillows)], ['Trucks out', flow.extra || '—']];
    default: return [];
  }
}

function StageDetail({ stageKey, fac, st, summary, onClose, onOpenLine, onOpenMachine, onOpenLoss }) {
  const s = STAGE_BY_KEY[stageKey];
  const n = STAGES.findIndex((x) => x.key === stageKey) + 1;
  const flow = st.flow || {};
  const kpis = stageKpis(stageKey, fac, flow, summary);
  const lines = LINE_STAGES.includes(stageKey) ? fac.lines : [];
  const machines = fac.lines.flatMap((l) => l.machines.filter((m) => MACHINE_STAGE[m.stage] === stageKey).map((m) => ({ ...m, line: l.name })));
  // every loss-prevention case at this stage + the first four other risks (sorted by severity)
  const others = st.risks.filter((r) => !r.lossId).slice(0, 4);
  const risks = st.risks.filter((r) => r.lossId || others.includes(r));
  const actions = [...new Set([...risks.slice(0, 2).map((r) => r.text).filter(Boolean), ...s.actions])].slice(0, 4);
  return (
    <div className="ftw-detail ftw-glass" style={{ '--c': s.color }}>
      <div className="ftw-detail-head flex items-start gap-2.5">
        <span className="ftw-num">{n}</span>
        <div className="flex-1 min-w-0">
          <p className="text-[13.5px] font-bold text-white leading-tight">{s.name}</p>
          <div className="flex items-center gap-2 mt-1">
            <span className={`ftw-pill t-${STATUS_TONE[st.status]}`}>{st.status.toUpperCase()}</span>
            <span className="text-[11px] text-sky-200 truncate">{st.stat}</span>
          </div>
        </div>
        <button type="button" className="ftw-close" onClick={onClose} aria-label="Close stage detail"><Icon name="close" size={15} /></button>
      </div>
      <div className="ftw-detail-body space-y-3">
        <p className="text-[11.5px] leading-relaxed text-sky-100/90">{s.desc}</p>
        <div className="ftw-kv">{kpis.map(([k, v]) => <div key={k}><span>{k}</span><b title={String(v)}>{v}</b></div>)}</div>
        {flow.workers > 0 && (
          <div>
            <p className="ftw-sub">People at this stage (RFID)</p>
            <div className="ftw-kv">
              <div><span>At station</span><b>{num(flow.at_station)} / {num(flow.workers)}</b></div>
              <div><span>Away · break</span><b>{num(flow.away)} · {num(flow.on_break)}</b></div>
              <div><span>Availability</span><b>{flow.availability_pct ?? '—'}%</b></div>
              <div><span>Away today</span><b>{num(flow.away_min)} min</b></div>
            </div>
          </div>
        )}
        {lines.length > 0 && (
          <div>
            <p className="ftw-sub">Lines · click to open</p>
            {lines.map((l) => (
              <button key={l.id} type="button" className="ftw-row" onClick={() => onOpenLine(l.id)}>
                <span className="ftw-dot" style={{ background: LINE_STATUS[l.status] || '#64748b' }} />
                <span className="flex-1 min-w-0 truncate">{l.name}</span>
                <span className="text-sky-300 tabular-nums">{lineMetric(l, stageKey)}</span>
              </button>
            ))}
          </div>
        )}
        {machines.length > 0 && (
          <div>
            <p className="ftw-sub">Machines · health</p>
            {machines.map((m) => (
              <button key={m.id} type="button" className="ftw-row" onClick={() => onOpenMachine(m.id)}>
                <span className="ftw-dot" style={{ background: m.health_status === 'Critical' ? '#ef4444' : m.health_status === 'Warning' ? '#f59e0b' : '#22c55e' }} />
                <span className="flex-1 min-w-0 truncate"><b className="text-white">{m.code}</b> {m.name} · {m.line}</span>
                <span className="tabular-nums text-sky-300">{Math.round(m.health)}%</span>
              </button>
            ))}
          </div>
        )}
        <div>
          <p className="ftw-sub">Alerts & risks</p>
          {risks.length === 0 && <p className="text-[11.5px] text-sky-200/80">No open alerts or S!AP advisories for this stage.</p>}
          <div className="space-y-1.5">
            {risks.map((r) => (r.lossId ? (
              <button key={r.id} type="button" className="ftw-risk" style={{ '--c': SEV_COLOR[severityTone(r.severity)] }}
                onClick={() => onOpenLoss(r.lossId)} title="Open the loss-prevention case">
                <span className="ftw-risk-open">OPEN →</span>
                <span className="ftw-tag is-loss">{r.source}</span>{r.title}
                <small>{r.ref} · {r.zone}{r.next ? ` · Next: ${r.next}` : ''}</small>
              </button>
            ) : (
              <div key={r.id} className="ftw-risk" style={{ '--c': SEV_COLOR[severityTone(r.severity)] }}>
                <span className="ftw-tag">{r.source}</span>{r.title}
              </div>
            )))}
          </div>
        </div>
        <div>
          <p className="ftw-sub">Suggested actions</p>
          <ul className="ftw-actions space-y-1">{actions.map((a) => <li key={a}>{a}</li>)}</ul>
        </div>
      </div>
    </div>
  );
}

/* ── main tab ────────────────────────────────────────────────────────── */
/**
 * The pillow-plant digital twin. `compact` renders the always-visible Production page card
 * (shorter viewport, stage chips, no control/info panels); `onExpand(plant, stage)` opens the
 * full twin, which then starts on that plant and stage (`initialPlant`, `initialStage`).
 */
export default function FactoryTwinTab({ factory, onOpenLine, onOpenMachine, compact = false, onExpand, initialPlant, initialStage }) {
  const { meta } = useApp();
  const [plant, setPlant] = useState(initialPlant || (factory && factory !== 'ALL' ? factory : 'KNV'));
  useEffect(() => { if (factory && factory !== 'ALL') setPlant(factory); }, [factory]);
  const fac = useApi(`/api/twin/facility/${plant}`, {}, { every: 2 });
  const summary = useApi('/api/production/summary', { factory: plant }, { every: 3 });
  const pred = useApi('/api/predictive/advisories', { factory: plant, limit: 60 }, { every: 10 });
  const tadv = useApi('/api/twin/advisories', { factory: plant }, { every: 10 });
  const alerts = useApi('/api/alerts', { factory: plant }, { every: 5 });

  const sceneRef = useRef(null);
  const [stage, setStage] = useState(null);
  const home = compact ? 'card' : 'overview';
  const [cam, setCam] = useState(home);
  const [layers, setLayers] = useState({ roof: true, markers: true, flow: true, labels: !compact });
  const [showInfo, setShowInfo] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const [hover, setHover] = useState(null);
  const [walk, setWalk] = useState({ playing: false, step: -1, speed: 1 });
  const [progress, setProgress] = useState(0);
  const [lossId, setLossId] = useState(null);

  const data = fac.data && fac.data.factory?.code === plant ? fac.data : null;
  const risks = useMemo(() => (data ? risksByStage(data, [...(pred.data?.advisories || []), ...(tadv.data?.advisories || [])], alerts.data || []) : {}),
    [data, pred.data, tadv.data, alerts.data]);
  const status = useMemo(() => (data ? stageStatus(data, risks) : {}), [data, risks]);

  // scene sync
  useEffect(() => { sceneRef.current?.setLayers(layers); }, [layers, data]);
  const select = useCallback((key, fly = true) => {
    setStage(key);
    sceneRef.current?.focusStage(key, fly);
    if (key) setCam(null);
  }, []);
  const camera = (p) => { setCam(p); sceneRef.current?.setCamera(p); };
  // opened from the Production page card: start on the stage that was selected there
  const pendingStage = useRef(initialStage || null);
  useEffect(() => {
    if (data && sceneRef.current && pendingStage.current) {
      select(pendingStage.current);
      pendingStage.current = null;
    }
  }, [data, select]);

  // walkthrough: step through the stages, the token travels along a running line
  const demoLine = useMemo(() => {
    const i = (data?.lines || []).findIndex((l) => l.status === 'Running');
    return i >= 0 ? i : 3;
  }, [data]);
  useEffect(() => {
    if (!walk.playing) return undefined;
    let raf;
    let start = performance.now() - progress * (STEP_MS / walk.speed);
    let step = Math.max(0, walk.step);
    let lastUi = 0;
    if (walk.step < 0) { setWalk((w) => ({ ...w, step: 0 })); }
    select(STAGES[step].key);
    const loop = (now) => {
      const dur = STEP_MS / walk.speed;
      let p = (now - start) / dur;
      if (p >= 1) {
        if (step >= STAGES.length - 1) {
          sceneRef.current?.setToken(STAGES[step].key, 1, demoLine);
          setProgress(1);
          setWalk((w) => ({ ...w, playing: false }));
          return;
        }
        step += 1;
        start = now;
        p = 0;
        setWalk((w) => ({ ...w, step }));
        select(STAGES[step].key);
      }
      sceneRef.current?.setToken(STAGES[step].key, p, demoLine);
      if (now - lastUi > 120) { setProgress(p); lastUi = now; }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [walk.playing, walk.speed, demoLine]);
  const resetWalk = () => {
    setWalk((w) => ({ ...w, playing: false, step: -1 }));
    setProgress(0);
    sceneRef.current?.setToken(null);
    select(null, false);
    camera(home);
  };

  const plants = meta?.factories || [];
  const plantName = plants.find((f) => f.code === plant)?.name || plant;
  const lines = data?.lines || [];
  const running = lines.filter((l) => l.status === 'Running').length;
  const prod = summary.data?.production;
  const area = Math.round(((HALL.x1 - HALL.x0) * (HALL.z1 - HALL.z0)) / 100) * 100;
  const curStep = walk.step >= 0 ? STAGES[walk.step] : null;

  const hoverText = (u) => {
    if (!u) return null;
    const s = STAGE_BY_KEY[u.stage];
    if (u.kind === 'loss') {
      // case facts only: never a person's name next to a loss marker
      const cs = (u.ids || [u.id]).map((id) => (data?.loss || []).find((c) => c.id === id)).filter(Boolean);
      const c = cs[0];
      if (!c) return null;
      return cs.length === 1
        ? { title: `${c.ref} · ${c.kind_label}`, sub: `${c.title} · ${c.zone}${c.value_usd ? ` · ${usd(c.value_usd)}` : ''}`, hint: 'Click to open the loss-prevention case' }
        : { title: `${cs.length} loss-prevention cases · ${s?.name || ''}`, sub: cs.slice(0, 3).map((x) => `${x.ref} ${x.kind_label}`).join(' · '), hint: 'Click to open the most severe case' };
    }
    if (u.kind === 'machine') {
      const ln = lines.find((l) => l.id === u.lineId);
      const m = ln?.machines.find((x) => x.id === u.id);
      return { title: `${u.code} · ${u.name}`, sub: `${u.line} · ${s?.name}${m ? ` · health ${Math.round(m.health)}% · ${m.status}` : ''}`, hint: 'Click to open the machine' };
    }
    if (u.kind === 'line') {
      const ln = lines.find((l) => l.id === u.lineId);
      return { title: `${u.line} · ${s?.name}`, sub: ln ? `${ln.status} · ${num(ln.output_today)} today · ${ln.efficiency_pct ?? '—'}% eff · ${lineMetric(ln, u.stage)}` : '', hint: 'Click to open the line' };
    }
    return { title: s?.name || u.stage, sub: status[u.stage]?.stat || '', hint: 'Click to highlight this stage' };
  };
  const ht = hover ? hoverText(hover.u) : null;

  return (
    <Card padded={false}
      title={compact ? 'Digital twin · pillow plant' : 'Pillow plant digital twin'}
      subtitle={data ? `${plantName} · ${lines.length} lines (${running} running) · ${STAGES.length} process stages · live ${hhmm(data.sim_time)}` : `${plantName} · loading…`}
      actions={(
        <div className="flex items-center gap-3 flex-wrap justify-end">
          {!compact && (
            <div className="hidden lg:flex items-center gap-2.5 text-[11px] text-fg-muted">
              {Object.entries(LINE_STATUS).map(([k, c]) => <span key={k} className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: c }} />{k}</span>)}
            </div>
          )}
          {factory === 'ALL' && plants.length > 1 && (
            <div className="ftw-seg" role="group" aria-label="Plant">
              {plants.map((f) => (
                <button key={f.code} type="button" className={plant === f.code ? 'is-on' : ''} onClick={() => { resetWalk(); setPlant(f.code); }}>{f.code}</button>
              ))}
            </div>
          )}
          {compact && onExpand && (
            <Button size="sm" icon="external" onClick={() => onExpand(plant, stage)}>Open full twin</Button>
          )}
        </div>
      )}
    >
      <div className={`p-3 pt-0 ${compact ? 'space-y-2' : 'space-y-3'}`}>
        <ErrorBox error={fac.error} onRetry={fac.reload} />
        <div className={`ftw-view ${compact ? 'is-compact' : ''} ${stage && LINE_STAGES.includes(stage) ? 'is-lines' : ''} ${cam === 'top' ? 'is-top' : ''}`}>
          {data ? (
            <PlantViewport key={plant} data={data} sceneRef={sceneRef}
              onSelectStage={(k) => k && select(k)}
              onOpenLine={onOpenLine} onOpenMachine={onOpenMachine} onOpenLoss={setLossId}
              onHover={(u, x, y) => setHover(u ? { u, x, y } : null)} preset={home} />
          ) : <div className="ftw-loading">Building the {plantName} plant…</div>}

          {/* compact card: one line of plant stats instead of the panels */}
          {compact && data && (
            <div className="ftw-mini ftw-glass">
              {prod && <span>Output <b>{num(prod.actual)}</b> / {num(prod.target)}</span>}
              {summary.data?.oee?.oee_pct != null && <span>OEE <b>{summary.data.oee.oee_pct}%</b></span>}
              <span>People <b>{num(data.rfid?.at_station)}</b> at station · {num(data.rfid?.away)} away</span>
              <span className="hidden md:inline">Drag to rotate · click a stage</span>
            </div>
          )}

          {/* view controls (canvas control panel) */}
          {!compact && (
          <div className={`ftw-controls ftw-glass ${collapsed ? 'is-collapsed' : ''}`}>
            <div className="flex items-center justify-between">
              <p className="ftw-h" style={{ margin: 0 }}>View controls</p>
              <button type="button" className="ftw-close" style={{ width: 22, height: 22 }} onClick={() => setCollapsed((c) => !c)} aria-label="Toggle controls">
                <Icon name={collapsed ? 'chevronDown' : 'close'} size={13} />
              </button>
            </div>
            {!collapsed && (
              <>
                <p className="ftw-sub">Camera views</p>
                <div className="ftw-btns">
                  {CAMERAS.map(([k, l]) => (
                    <button key={k} type="button" className={`ftw-btn ${cam === k ? 'is-active' : ''}`} onClick={() => { select(null, false); camera(k); }}>{l}</button>
                  ))}
                </div>
                <p className="ftw-sub">Display</p>
                <div className="ftw-toggles">
                  {[['roof', 'Roof'], ['markers', 'Markers'], ['flow', 'Flow'], ['labels', 'Labels']].map(([k, l]) => (
                    <button key={k} type="button" className={`ftw-toggle ${layers[k] ? 'is-on' : ''}`} onClick={() => setLayers((s) => ({ ...s, [k]: !s[k] }))}>{l}</button>
                  ))}
                  <button type="button" className={`ftw-toggle ${showInfo ? 'is-on' : ''}`} onClick={() => setShowInfo((v) => !v)}>Info</button>
                </div>
                <p className="ftw-sub">Process walkthrough</p>
                <div className="flex items-center gap-1.5">
                  {walk.playing ? (
                    <button type="button" className="ftw-btn is-active" onClick={() => setWalk((w) => ({ ...w, playing: false }))}><Icon name="pause" size={12} />Pause</button>
                  ) : (
                    <button type="button" className="ftw-btn" disabled={!data} onClick={() => {
                      if (walk.step >= STAGES.length - 1 && progress >= 1) { setProgress(0); setWalk((w) => ({ ...w, step: -1, playing: true })); return; }
                      setWalk((w) => ({ ...w, playing: true }));
                    }}><Icon name="play" size={12} />Play</button>
                  )}
                  <button type="button" className="ftw-btn" onClick={resetWalk} title="Reset"><Icon name="refresh" size={12} /></button>
                  <select className="ftw-select" value={walk.speed} onChange={(e) => setWalk((w) => ({ ...w, speed: Number(e.target.value) }))} aria-label="Walkthrough speed">
                    {SPEEDS.map((v) => <option key={v} value={v}>{v}×</option>)}
                  </select>
                </div>
                <p className="ftw-hint">Drag to rotate · right-drag to pan · scroll to zoom. Click a machine, a line or a stage.</p>
              </>
            )}
          </div>
          )}

          {/* walkthrough caption (canvas simulation: current step) */}
          {curStep && !compact && (
            <div className="ftw-caption ftw-glass" style={{ '--c': curStep.color }}>
              <div className="flex items-center gap-2">
                <span className="ftw-num" style={{ width: 22, height: 22, fontSize: 11 }}>{walk.step + 1}</span>
                <p className="text-[12.5px] font-bold text-white flex-1 min-w-0 truncate">{curStep.name}</p>
                <span className="text-[11px] text-sky-200 tabular-nums">{status[curStep.key]?.stat}</span>
              </div>
              <p>{curStep.desc}</p>
              <div className="ftw-steps">
                {STAGES.map((s, i) => <i key={s.key} className={i < walk.step ? 'is-done' : i === walk.step ? 'is-now' : ''} style={i === walk.step ? { '--c': s.color, opacity: 0.5 + progress / 2 } : undefined} />)}
              </div>
            </div>
          )}

          {/* facility info panel */}
          {showInfo && data && !compact && (
            <div className="ftw-info ftw-glass">
              <p className="ftw-h">{plantName} pillow plant</p>
              <div className="ftw-info-grid">
                <div className="ftw-info-item"><span>Floor area</span><b>{num(area)} m²</b><i>{HALL.x1 - HALL.x0} × {HALL.z1 - HALL.z0} m hall</i></div>
                <div className="ftw-info-item"><span>Production lines</span><b>{running}/{lines.length} running</b><i>{lines.filter((l) => l.type === 'Garnett').length} Garnett · {lines.filter((l) => l.type === 'Blow').length} blow</i></div>
                <div className="ftw-info-item"><span>Daily capacity</span><b>{num(data.factory?.daily_target)}</b><i>pillows / day target</i></div>
                <div className="ftw-info-item"><span>Key zones</span><b>{STAGES.length} stages</b><i>+ {SUPPORT.length} support areas</i></div>
                <div className="ftw-info-item"><span>Employees</span><b>{num(data.rfid?.badges_on_floor)} on floor</b><i>{num(data.rfid?.at_station)} at station · {num(data.rfid?.away)} away</i></div>
                <div className="ftw-info-item"><span>Operating hours</span><b>06:00–23:00</b><i>Shift {data.shift} · {hhmm(data.shift_window?.[0])}–{hhmm(data.shift_window?.[1])}</i></div>
              </div>
              {prod && (
                <div className="mt-2">
                  <div className="flex justify-between text-[10.5px] text-sky-200">
                    <span>Output today {num(prod.actual)} / {num(prod.target)}</span>
                    <span>OEE {summary.data?.oee?.oee_pct ?? '—'}%</span>
                  </div>
                  <div className="ftw-bar"><span style={{ width: `${Math.min(100, prod.achievement_pct || 0)}%` }} /></div>
                </div>
              )}
            </div>
          )}

          {stage && data && status[stage] && (
            <StageDetail stageKey={stage} fac={data} st={status[stage]} summary={summary.data}
              onClose={() => { select(null, false); camera(home); }}
              onOpenLine={onOpenLine} onOpenMachine={onOpenMachine} onOpenLoss={setLossId} />
          )}
          {ht && hover && (
            <div className="ftw-tooltip ftw-glass" style={{ left: hover.x, top: hover.y }}>
              <b>{ht.title}</b>
              {ht.sub && <span>{ht.sub}</span>}
              <span style={{ color: '#7dd3fc' }}>{ht.hint}</span>
            </div>
          )}
        </div>

        {/* compact card: stage chips (status dot) — click to highlight that stage on every line */}
        {compact && (
          <div className="ftw-chips" role="list" aria-label="Manufacturing stages">
            {STAGES.map((s, i) => {
              const st = status[s.key];
              return (
                <button key={s.key} type="button" role="listitem" className={`ftw-chip ${stage === s.key ? 'is-active' : ''}`} style={{ '--c': s.color }}
                  onClick={() => (stage === s.key ? (select(null, false), camera(home)) : select(s.key))}
                  title={`${s.name} · ${st?.status || '…'}${st?.stat ? ` · ${st.stat}` : ''}`}>
                  <span className="ftw-chip-n">{i + 1}</span>
                  {s.short}
                  <span className={`ftw-chip-dot t-${STATUS_TONE[st?.status] || 'neutral'}`} />
                </button>
              );
            })}
          </div>
        )}

        {/* stage strip (canvas end-to-end flow) */}
        {!compact && (
        <div className="ftw-strip" role="list" aria-label="Manufacturing stages">
          {STAGES.map((s, i) => {
            const st = status[s.key];
            return (
              <button key={s.key} type="button" role="listitem" className={`ftw-stage ${stage === s.key ? 'is-active' : ''}`} style={{ '--c': s.color }}
                onClick={() => (stage === s.key ? (select(null, false), camera(home)) : select(s.key))} title={s.desc}>
                <div className="ftw-stage-top">
                  <span className="ftw-stage-n">{i + 1}</span>
                  <span className="ftw-stage-name">{s.short}</span>
                </div>
                <div className="ftw-stage-stat">{st?.stat || '—'}</div>
                <div className="ftw-stage-bottom">
                  <span className={`ftw-pill t-${STATUS_TONE[st?.status] || 'neutral'}`}>{st?.status || '…'}</span>
                  {st?.risks?.length > 0 && <span className="text-[10px] text-fg-faint">{st.risks.length} risk{st.risks.length > 1 ? 's' : ''}</span>}
                </div>
                {i < STAGES.length - 1 && <span className="ftw-arrow" aria-hidden="true">▶</span>}
              </button>
            );
          })}
        </div>
        )}
      </div>
      {/* loss-prevention case (beacon or stage risk click); a fixed overlay, so it is not clipped by the card */}
      {lossId && <LossIncidentDrawer id={lossId} onClose={() => setLossId(null)} />}
    </Card>
  );
}
