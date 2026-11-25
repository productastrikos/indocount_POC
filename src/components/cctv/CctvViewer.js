import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../ui';
import { useApi } from '../../hooks/useApi';
import CctvPlayer from './CctvPlayer';
import {
  ACT, DEFAULT_LAYERS, EVENT_META, LAYERS, assignIdentities, cartonState, initials, sessionTotals, useAnalytics, useSimClock,
} from './cctvShared';
import './cctv.css';

const STATE_TONE = { Station: '#22c55e', Away: '#f59e0b', Break: '#38bdf8' };

function useThrottledFrame() {
  const [pos, setPos] = useState({ f: 0, loops: 0 });
  const ref = useRef({ f: 0, loops: 0, last: 0 });
  const onFrame = (f, loops) => {
    ref.current.f = f;
    ref.current.loops = loops;
    const now = performance.now();
    if (now - ref.current.last > 220) {
      ref.current.last = now;
      setPos({ f, loops });
    }
  };
  return [pos, onFrame];
}

function Timeline({ d, f, onSeek }) {
  const cv = useRef(null);
  useEffect(() => {
    const c = cv.current;
    if (!c || !d) return;
    const r = c.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = r.width * dpr;
    c.height = r.height * dpr;
    const ctx = c.getContext('2d');
    ctx.scale(dpr, dpr);
    const W = r.width;
    const H = r.height;
    ctx.clearRect(0, 0, W, H);
    const n = d.series.length;
    const peak = Math.max(1, ...d.series.map((s) => s[0]));
    const bw = W / n;
    d.series.forEach(([people, working], i) => {
      const hp = (people / peak) * (H - 14);
      const hw = (working / peak) * (H - 14);
      ctx.fillStyle = 'rgba(148,163,184,0.25)';
      ctx.fillRect(i * bw + 0.5, H - hp, bw - 1, hp);
      ctx.fillStyle = 'rgba(34,197,94,0.75)';
      ctx.fillRect(i * bw + 0.5, H - hw, bw - 1, hw);
    });
    const x = (fr) => (fr / d.n) * W;
    d.ev.forEach((e) => {
      const m = EVENT_META[e.type];
      if (!m) return;
      ctx.fillStyle = m.color;
      if (e.end != null) {
        ctx.globalAlpha = 0.35;
        ctx.fillRect(x(e.f), 0, Math.max(2, x(e.end * d.fps) - x(e.f)), 5);
        ctx.globalAlpha = 1;
      }
      ctx.fillRect(x(e.f) - 1, 0, 2.5, e.type === 'pillow_in' ? 12 : 8);
    });
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(x(f) - 1, 0, 2, H);
  }, [d, f]);
  return (
    <div className="cctv-strip" title="Activity on camera (grey: people · green: active) and events — click to seek"
      onClick={(e) => {
        if (!d) return;
        const r = e.currentTarget.getBoundingClientRect();
        onSeek(((e.clientX - r.left) / r.width) * d.dur);
      }}>
      <canvas ref={cv} />
    </div>
  );
}

function Kpi({ label, value, sub }) {
  return (
    <div className="cctv-kpi">
      <span>{label}</span>
      <b>{value}</b>
      {sub && <span>{sub}</span>}
    </div>
  );
}

function ActBar({ pct }) {
  const parts = [['handling', ACT[1].color], ['lifting', ACT[2].color], ['walking', ACT[3].color], ['idle', ACT[0].color], ['present', ACT[4].color]];
  return (
    <div className="cctv-actbar">
      {parts.map(([k, c]) => (pct[k] ? <span key={k} style={{ width: `${pct[k]}%`, background: c }} title={`${k} ${pct[k]}%`} /> : null))}
    </div>
  );
}

export default function CctvViewer({ feedId, feeds = [], onClose, onOpenWorker }) {
  useSimClock();
  const [id, setId] = useState(feedId);
  const detail = useApi(id ? `/api/cctv/feeds/${id}` : null, {}, { every: 3 });
  const base = feeds.find((f) => f.id === id);
  const feed = detail.data || base;
  const { data: d, error: aErr } = useAnalytics(feed?.analytics);
  const [layers, setLayers] = useState(DEFAULT_LAYERS);
  const [selected, setSelected] = useState(null);
  const [pos, onFrame] = useThrottledFrame();
  const seek = useRef(null);
  const ids = useMemo(() => assignIdentities(d, detail.data?.workers), [d, detail.data?.workers]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
      const i = feeds.findIndex((f) => f.id === id);
      if (e.key === 'ArrowRight' && i >= 0 && feeds[i + 1]) setId(feeds[i + 1].id);
      if (e.key === 'ArrowLeft' && i > 0) setId(feeds[i - 1].id);
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = prev; };
  }, [id, feeds, onClose]);

  useEffect(() => { setSelected(null); }, [id]);

  if (!feed) return null;
  const tot = d ? sessionTotals(d, pos.f, pos.loops) : null;
  const rec = feed.recognition || d?.recognition;
  const order = detail.data?.order;
  const people = d ? (d.p[pos.f] || []) : [];
  const active = people.filter((p) => p[6] === 1 || p[6] === 2).length;
  const cartons = d ? d.zones.filter((z) => z.kind === 'carton') : [];
  const recentEvents = d ? [
    ...d.ev.filter((e) => e.f <= pos.f).map((e) => ({ ...e, ago: (pos.f - e.f) / d.fps })),
    ...(pos.loops > 0 ? d.ev.filter((e) => e.f > pos.f).map((e) => ({ ...e, ago: (pos.f + d.n - e.f) / d.fps })) : []),
  ].sort((a, b) => a.ago - b.ago).slice(0, 40) : [];
  const idx = feeds.findIndex((f) => f.id === id);
  const sum = d?.summary || feed.summary || {};
  // one row per person: tracker fragments of the same badge holder are combined
  const workersOnCam = (() => {
    if (!d) return [];
    const rows = {};
    Object.entries(d.people).forEach(([tid, p]) => {
      const who = ids[tid];
      const dur = p.last - p.first + 1;
      if (!who && !p.zone && (p.h < 150 || dur < 3 * d.fps)) return;
      const key = who ? `w${who.id}` : `t${tid}`;
      const r = rows[key] || (rows[key] = { key, tid: Number(tid), tids: [], who, zone: p.zone, h: p.h, placed: 0, frames: 0, acc: {} });
      r.tids.push(Number(tid));
      r.placed += p.placed;
      r.frames += dur;
      r.h = Math.max(r.h, p.h);
      Object.entries(p.pct).forEach(([k2, v]) => { r.acc[k2] = (r.acc[k2] || 0) + v * dur; });
    });
    return Object.values(rows).map((r) => ({
      ...r,
      pct: Object.fromEntries(Object.entries(r.acc).map(([k2, v]) => [k2, Math.round(v / r.frames)])),
    })).sort((a, b) => (b.who ? 1 : 0) - (a.who ? 1 : 0) || b.placed - a.placed || b.frames - a.frames).slice(0, 6);
  })();

  return createPortal(
    <div className="cctv-root" role="dialog" aria-modal="true">
      <div className="cctv-top">
        <span className="w-8 h-8 rounded-lg grid place-items-center" style={{ background: 'rgba(56,189,248,0.15)', color: '#38bdf8' }}>
          <Icon name="camera" size={16} />
        </span>
        <div className="min-w-0">
          <p className="text-[13px] font-bold truncate">{feed.camera} · {feed.title}</p>
          <p className="text-[11px] truncate" style={{ color: 'var(--color-text-faint)' }}>
            {feed.factory_name || feed.factory} · {feed.line}{feed.station ? ` · ${feed.station}` : ''} · {feed.ptz ? 'PTZ tour' : 'Fixed camera'} · recorded footage, edge-inference replay
          </p>
        </div>
        <div className="flex-1" />
        <div className="hidden lg:flex items-center gap-1.5 flex-wrap">
          {LAYERS.map((l) => (
            <button key={l.key} type="button" className={`cctv-chip ${layers[l.key] ? 'is-on' : ''}`}
              onClick={() => setLayers((s) => ({ ...s, [l.key]: !s[l.key] }))}>{l.label}</button>
          ))}
        </div>
        {feeds.length > 1 && (
          <div className="flex items-center gap-1">
            <button type="button" className="cctv-tool" disabled={idx <= 0} onClick={() => setId(feeds[idx - 1].id)} title="Previous camera (←)">
              <Icon name="chevronRight" size={14} className="rotate-180" />
            </button>
            <span className="text-[11px] tabular-nums" style={{ color: 'var(--color-text-faint)' }}>{idx + 1}/{feeds.length}</span>
            <button type="button" className="cctv-tool" disabled={idx >= feeds.length - 1} onClick={() => setId(feeds[idx + 1].id)} title="Next camera (→)">
              <Icon name="chevronRight" size={14} />
            </button>
          </div>
        )}
        <button type="button" className="cctv-tool" onClick={onClose} title="Close (Esc)"><Icon name="close" size={16} /></button>
      </div>

      <div className="cctv-body">
        <div className="cctv-main">
          <div style={{ maxWidth: 'calc((100vh - 230px) * 16 / 9)', width: '100%', margin: '0 auto' }}>
            <CctvPlayer key={feed.id} feed={feed} analytics={d} identities={ids} layers={layers} selected={selected}
              onSelect={(tid) => setSelected(tid)} onFrame={onFrame} seekRef={seek} />
            {aErr && <p className="text-xs mt-2" style={{ color: '#fca5a5' }}>Analytics unavailable: {aErr}</p>}
            <div className="mt-2.5"><Timeline d={d} f={pos.f} onSeek={(s) => seek.current?.(s)} /></div>
            <div className="mt-1 flex items-center gap-3 flex-wrap text-[10.5px]" style={{ color: 'var(--color-text-faint)' }}>
              {ACT.map((a) => <span key={a.key} className="inline-flex items-center gap-1"><i className="cctv-dot" style={{ background: a.color }} />{a.label}</span>)}
              <span className="inline-flex items-center gap-1"><i className="cctv-dot" style={{ background: '#f472b6' }} />Pillow track</span>
              <span className="inline-flex items-center gap-1"><i className="cctv-dot" style={{ background: '#22d3ee' }} />Carton opening</span>
              <span className="inline-flex items-center gap-1"><i className="cctv-dot" style={{ background: '#fde047' }} />OCR read</span>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-2">
            <Kpi label="People in view" value={people.length} sub={`peak ${sum.people_peak ?? '—'} · ${sum.tracks ?? '—'} tracked`} />
            <Kpi label="Active now" value={active} sub={sum.station_active_pct != null ? `station active ${sum.station_active_pct}%` : 'handling or lifting'} />
            {feed.kind === 'packing' && <Kpi label="Pillows placed" value={tot?.pillows ?? 0} sub="since you opened this feed" />}
            {feed.kind === 'packing' && <Kpi label="Placement rate" value={sum.pillows_per_min != null ? `${sum.pillows_per_min}/min` : '—'} sub={sum.pillows_per_min ? `≈ ${Math.round(sum.pillows_per_min * 60)} pillows/h` : ''} />}
            {feed.kind === 'packing' && (cartons.some((z) => z.capacity)
              ? <Kpi label="Display fill" value={`${cartonState(d, cartons.find((z) => z.capacity), pos.f, pos.loops).packs} packs`} sub={`of ${Math.round(cartons.find((z) => z.capacity).capacity / (cartons.find((z) => z.capacity).per_event || 1))} per display`} />
              : <Kpi label="Cartons completed" value={tot?.cartons ?? 0} sub={detail.data?.today ? `${detail.data.today.cartons} today at station` : ''} />)}
            {!!d?.lineTotal && <Kpi label="Pillows counted" value={tot?.crossings ?? 0} sub="crossing the count line" />}
            {sum.belt_speed_px_s != null && d?.zones.find((z) => z.kind === 'conveyor')?.m_per_px && (
              <Kpi label="Belt speed" value={`${(Math.abs(sum.belt_speed_px_s) * d.zones.find((z) => z.kind === 'conveyor').m_per_px).toFixed(2)} m/s`} sub="optical flow, median" />
            )}
            <Kpi label="Idle alerts" value={sum.idle_events ?? 0} sub="≥ 4 s idle at a station" />
            {rec && <Kpi label="Label reads" value={sum.ocr_reads ?? 0} sub="OCR on carton print" />}
          </div>
        </div>

        <aside className="cctv-side">
          {rec && (
            <div className="cctv-panel">
              <h4>{rec.customer ? 'Customer identified' : 'Product identified'}</h4>
              {rec.customer && <p className="text-lg font-bold leading-tight">{rec.customer}</p>}
              <p className="text-xs" style={{ color: 'var(--color-text-muted)' }}>{rec.brand} · {rec.product}</p>
              <div className="flex flex-wrap gap-1.5 mt-2">
                {(rec.evidence || []).map((e) => (
                  <span key={e.text} className="cctv-chip" title={`${e.reads} reads · mean confidence ${e.conf}`}>“{e.text}” · {Math.round(e.conf * 100)}%</span>
                ))}
                {!(rec.evidence || []).length && <span className="text-xs" style={{ color: 'var(--color-text-faint)' }}>No legible label in this clip.</span>}
              </div>
              <p className="text-[10.5px] mt-2" style={{ color: 'var(--color-text-faint)' }}>{rec.label}: {rec.rule}</p>
              {order && order.order && (
                <div className="mt-2.5 pt-2.5" style={{ borderTop: '1px solid var(--color-border)' }}>
                  <p className="text-[10.5px] font-semibold uppercase tracking-wide" style={{ color: '#7dd3fc' }}>Next open {order.customer} order from this plant</p>
                  <p className="text-sm font-semibold">{order.order} · {order.sku}</p>
                  <p className="text-[11px]" style={{ color: 'var(--color-text-muted)' }}>
                    {order.qty?.toLocaleString()} pillows · {order.pillows_per_carton} per carton · ships {order.ship ? new Date(order.ship).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '—'} · {order.priority}
                  </p>
                </div>
              )}
              {order && !order.order && <p className="text-[11px] mt-2" style={{ color: 'var(--color-text-faint)' }}>No open {order.customer} order at this plant right now.</p>}
            </div>
          )}

          {cartons.length > 0 && d && (
            <div className="cctv-panel">
              <h4>Cartons at this station</h4>
              <div className="space-y-2">
                {cartons.map((z) => {
                  const cs = cartonState(d, z, pos.f, pos.loops);
                  const pct = cs.spec ? Math.min(100, (cs.inCarton / cs.spec) * 100) : 0;
                  return (
                    <div key={z.id}>
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-semibold">{z.name}</span>
                        <span className="tabular-nums" style={{ color: 'var(--color-text-muted)' }}>
                          {z.capacity ? `${cs.packs} / ${Math.round(z.capacity / (z.per_event || 1))} packs (${cs.inCarton} pillows)` : `${cs.inCarton} / ${cs.spec} pillows`}
                        </span>
                      </div>
                      <div className="cctv-actbar" style={{ height: 6 }}><span style={{ width: `${pct}%`, background: pct >= 100 ? '#38bdf8' : '#22c55e' }} /></div>
                    </div>
                  );
                })}
              </div>
              <p className="text-[10.5px] mt-2" style={{ color: 'var(--color-text-faint)' }}>
                {cartons.some((z) => z.hands)
                  ? `A pillow counts when the packer's hands lift it above the carton rim and plunge into the carton (pose keypoints), corroborated by the pillow's motion track. Carton spec at this station: ${cartons.find((z) => z.hands).spec} pillows.`
                  : cartons.some((z) => z.capacity)
                    ? `A ${cartons.find((z) => z.capacity).per_event || 1}-pack counts when its motion track comes down into the display opening. Display holds ${cartons.find((z) => z.capacity).capacity} pillows.`
                    : 'A pillow counts when its track ends inside the carton opening after coming down into it.'}
              </p>
            </div>
          )}

          <div className="cctv-panel">
            <h4>People on camera</h4>
            {!workersOnCam.length && <p className="text-xs" style={{ color: 'var(--color-text-faint)' }}>No one is tracked in this clip.</p>}
            {workersOnCam.map((p) => {
              const cur = people.find((q) => p.tids.includes(q[0]));
              const col = cur ? ACT[cur[6]].color : '#475569';
              const isSel = p.tids.includes(selected);
              return (
                <button key={p.key} type="button" className={`cctv-row w-full text-left ${isSel ? 'is-active' : ''}`}
                  onClick={() => setSelected(isSel ? null : (cur ? cur[0] : p.tid))}>
                  <span className="cctv-avatar" style={{ background: col }}>{p.who ? initials(p.who.name) : `#${p.tid}`}</span>
                  <span className="min-w-0">
                    <span className="block text-xs font-semibold truncate">
                      {p.who ? p.who.name : `Person #${p.tid}`}
                      <span className="font-normal" style={{ color: 'var(--color-text-faint)' }}> · {p.who ? `${p.who.role} · ${p.who.code}` : (p.zone ? 'no badge matched' : 'floor')}</span>
                    </span>
                    <ActBar pct={p.pct} />
                    <span className="block text-[10.5px] mt-0.5" style={{ color: 'var(--color-text-faint)' }}>
                      {p.pct.handling + p.pct.lifting}% active · {p.pct.idle}% idle{p.placed ? ` · placed ${p.placed}` : ''}{cur ? ` · now ${ACT[cur[6]].label.toLowerCase()}` : ' · out of view'}
                    </span>
                  </span>
                  <span className="text-right">
                    {p.who && <span className="block text-[10px] font-semibold" style={{ color: STATE_TONE[p.who.state] || '#94a3b8' }}>RFID {p.who.state}</span>}
                    {p.who && onOpenWorker && (
                      <span role="link" tabIndex={0} className="text-[10px] underline" style={{ color: '#7dd3fc' }}
                        onClick={(e) => { e.stopPropagation(); onOpenWorker(p.who.id); }}>badge history</span>
                    )}
                  </span>
                </button>
              );
            })}
            <p className="text-[10.5px] mt-1.5" style={{ color: 'var(--color-text-faint)' }}>
              Identity comes from the RFID badge read at this station, fused with the tracked person — no facial recognition.
            </p>
          </div>

          <div className="cctv-panel">
            <h4>Events</h4>
            {!recentEvents.length && <p className="text-xs" style={{ color: 'var(--color-text-faint)' }}>Waiting for the first event…</p>}
            <div className="max-h-[220px] overflow-y-auto">
              {recentEvents.map((e, i) => {
                const m = EVENT_META[e.type] || { label: e.type, color: '#94a3b8', icon: '•' };
                const who = e.by != null ? (ids[e.by]?.name || `Person #${e.by}`) : null;
                const z = e.zone && d?.zoneById[e.zone];
                let text = m.label;
                if (e.type === 'pillow_in') {
                  const cue = e.cue && e.cue.split('+').map((c) => (c === 'hands' ? 'hand pose' : 'blob track')).join(' + ');
                  text = `${e.n > 1 ? `${e.n}-pack` : 'Pillow'} into ${z?.name || 'carton'}${who ? ` by ${who}` : ''}${cue ? ` · ${cue}` : ''}`;
                }
                if (e.type === 'carton_complete') text = `${z?.name || 'Carton'} complete · ${e.count} pillows`;
                if (e.type === 'idle') text = `${who} idle ${e.seconds}s at ${z?.name || 'station'}`;
                if (e.type === 'left_station') text = `${who} left ${z?.name || 'station'} for ${e.seconds}s`;
                if (e.type === 'label_read') text = `Label read: ${e.text}${e.customer ? ` → ${e.customer}` : ''}`;
                if (e.type === 'line_cross') text = 'Pillow passed the count line';
                return (
                  <div key={`${e.type}-${e.f}-${i}`} className="cctv-log">
                    <time>{e.ago < 1 ? 'now' : `${Math.round(e.ago)}s ago`}</time>
                    <span style={{ color: m.color, fontWeight: 800 }}>{m.icon}</span>
                    <span style={{ color: 'var(--color-text-muted)' }}>{text}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {d && (
            <div className="cctv-panel">
              <h4>Vision pipeline</h4>
              <div className="space-y-1 text-[11.5px]">
                {Object.entries(d.models).map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-3"><span style={{ color: 'var(--color-text-faint)' }}>{k}</span><span className="text-right">{v}</span></div>
                ))}
                <div className="flex justify-between gap-3"><span style={{ color: 'var(--color-text-faint)' }}>frames</span><span>{d.n} @ {d.fps} fps · {d.dur}s loop</span></div>
              </div>
              <p className="text-[10.5px] mt-2" style={{ color: 'var(--color-text-faint)' }}>
                Plant-floor footage analysed frame by frame; results are replayed in sync with the video exactly as an edge
                device streams them. Counts continue across loops of the clip.
              </p>
            </div>
          )}
        </aside>
      </div>
    </div>,
    document.body,
  );
}
