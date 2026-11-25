import React, { useEffect, useRef } from 'react';
import './cctv.css';
import { ACT, OBJ, cartonState, osdTime, sessionTotals, simNow } from './cctvShared';

const W = 960;
const H = 540;
const LIMBS = [[5, 7], [7, 9], [6, 8], [8, 10], [5, 6], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16]];
const TYPE_TONE = { packing: 'PACKING CV', line: 'LINE CV', area: 'AREA CV' };

function rr(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function poly(ctx, pts) {
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
}

function bounds(pts) {
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

/** Text chip; returns its width. */
function chip(ctx, x, y, text, { u, size = 11, bg = 'rgba(5,10,20,0.82)', fg = '#e2e8f0', bold = false, align = 'left' }) {
  ctx.font = `${bold ? 700 : 600} ${size * u}px Inter, system-ui, sans-serif`;
  const w = ctx.measureText(text).width + 8 * u;
  const h = (size + 6) * u;
  const x0 = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
  ctx.fillStyle = bg;
  rr(ctx, x0, y, w, h, 3 * u);
  ctx.fill();
  ctx.fillStyle = fg;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x0 + 4 * u, y + h / 2 + 0.5 * u);
  return w;
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function idleSeconds(d, tid, f) {
  let k = f;
  while (k > 0 && f - k < 600) {
    const p = d.p[k - 1].find((q) => q[0] === tid);
    if (!p || p[6] !== 0) break;
    k -= 1;
  }
  return (f - k) / d.fps;
}

export default function CctvPlayer({
  feed, analytics, identities, layers, compact = false, playing = true, selected = null,
  onSelect, onFrame, onClick, className = '', seekRef,
}) {
  const wrap = useRef(null);
  const video = useRef(null);
  const canvas = useRef(null);
  const st = useRef({ loops: 0, lastT: 0, lastF: -1, lastDraw: 0, hover: null, size: [0, 0] });
  const props = useRef({});
  props.current = { feed, analytics, identities, layers, compact, selected, onFrame };

  // play / pause from the parent (off-screen tiles pause)
  useEffect(() => {
    const v = video.current;
    if (!v) return;
    if (playing) v.play().catch(() => {});
    else v.pause();
  }, [playing, feed?.video]);

  useEffect(() => {
    if (seekRef) seekRef.current = (sec) => { if (video.current) video.current.currentTime = sec; };
  }, [seekRef]);

  // canvas follows the element size (crisp on HiDPI)
  useEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const c = canvas.current;
      c.width = Math.max(1, Math.round(r.width * dpr));
      c.height = Math.max(1, Math.round(r.height * dpr));
      st.current.size = [r.width, r.height];
      st.current.lastF = -1;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    let raf;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      const v = video.current;
      const c = canvas.current;
      const P = props.current;
      const d = P.analytics;
      if (!v || !c) return;
      const s = st.current;
      const t = v.currentTime || 0;
      if (t + 0.5 < s.lastT) s.loops += 1;
      s.lastT = t;
      const now = performance.now();
      const f = d ? Math.max(0, Math.min(d.n - 1, Math.floor(t * d.fps + 0.001))) : 0;
      if (f === s.lastF && now - s.lastDraw < (P.compact ? 500 : 250)) return;
      if (f !== s.lastF && P.onFrame) P.onFrame(f, s.loops);
      s.lastF = f;
      s.lastDraw = now;
      draw(c, d, f, s, P, now);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const toLocal = (e) => {
    const r = wrap.current.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * W, ((e.clientY - r.top) / r.height) * H];
  };
  const pick = (e) => {
    const d = props.current.analytics;
    if (!d || st.current.lastF < 0) return null;
    const [x, y] = toLocal(e);
    const hits = d.p[st.current.lastF].filter((p) => x >= p[1] && x <= p[1] + p[3] && y >= p[2] && y <= p[2] + p[4]);
    hits.sort((a, b) => a[3] * a[4] - b[3] * b[4]);
    return hits[0]?.[0] ?? null;
  };

  return (
    <div
      ref={wrap}
      className={`cctv-frame ${onClick || onSelect ? 'is-clickable' : ''} ${className}`}
      onMouseMove={compact ? undefined : (e) => { const h = pick(e); if (h !== st.current.hover) { st.current.hover = h; st.current.lastF = -1; } }}
      onMouseLeave={() => { st.current.hover = null; st.current.lastF = -1; }}
      onClick={(e) => {
        if (onClick) { onClick(e); return; }
        if (onSelect) onSelect(pick(e));
      }}
    >
      <video ref={video} src={feed.video} poster={feed.poster} muted loop playsInline autoPlay preload="auto" />
      <canvas ref={canvas} />
      {!analytics && <div className="cctv-wait">Connecting to analytics…</div>}
    </div>
  );
}

/* ── drawing ─────────────────────────────────────────────────────────── */
function draw(c, d, f, s, P, now) {
  const ctx = c.getContext('2d');
  const [cw] = s.size;
  if (!cw) return;
  const u = W / cw; // drawing units per CSS pixel
  ctx.setTransform(c.width / W, 0, 0, c.height / H, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const L = P.layers || {};
  const compact = P.compact;
  if (d) {
    if (L.heat) drawHeat(ctx, d);
    if (L.zones) drawZones(ctx, d, f, s.loops, u, compact);
    if (L.objects) drawObjects(ctx, d, f, u, compact);
    if (L.pose && !compact) drawPose(ctx, d, f, u);
    if (L.people) drawPeople(ctx, d, f, u, compact, P.identities, P.selected, s.hover);
    if (L.ocr && !compact) drawOcr(ctx, d, f, u);
    drawEvents(ctx, d, f, u, compact, P.feed);
  }
  drawOsd(ctx, d, f, s, u, compact, P.feed, now);
  if (d && s.hover != null && !compact) drawTooltip(ctx, d, f, u, s.hover, P.identities);
}

function drawHeat(ctx, d) {
  const gh = d.heat.length;
  const gw = d.heat[0].length;
  const cw = W / gw;
  const ch = H / gh;
  for (let y = 0; y < gh; y += 1) {
    for (let x = 0; x < gw; x += 1) {
      const v = d.heat[y][x] / 100;
      if (v < 0.03) continue;
      const r = 255;
      const g = Math.round(220 - 180 * v);
      ctx.fillStyle = `rgba(${r},${g},40,${0.12 + 0.42 * v})`;
      ctx.fillRect(x * cw, y * ch, cw, ch);
    }
  }
}

function recent(d, type, f, zone, within) {
  return d.ev.find((e) => e.type === type && (!zone || e.zone === zone) && e.f <= f && f - e.f < within);
}

function drawZones(ctx, d, f, loops, u, compact) {
  d.zones.forEach((z) => {
    if (z.kind === 'carton') {
      const hit = recent(d, 'pillow_in', f, z.id, 14);
      poly(ctx, z.pts);
      ctx.fillStyle = hit ? 'rgba(34,197,94,0.22)' : 'rgba(34,211,238,0.07)';
      ctx.fill();
      ctx.setLineDash([7 * u, 5 * u]);
      ctx.lineWidth = 1.6 * u;
      ctx.strokeStyle = hit ? '#4ade80' : '#22d3ee';
      ctx.stroke();
      ctx.setLineDash([]);
      const b = bounds(z.pts);
      const cs = cartonState(d, z, f, loops);
      const label = z.capacity
        ? `${compact ? '' : `${z.name} · `}${cs.packs} / ${Math.round(z.capacity / (z.per_event || 1))} packs`
        : `${compact ? '' : `${z.name} · `}${cs.inCarton}/${cs.spec}`;
      const y = Math.min(H - 40 * u, b.y1 + 4 * u); // under the opening: people labels sit above heads
      const wTxt = chip(ctx, b.x0, y, label, { u, size: compact ? 10 : 11, bg: 'rgba(8,47,73,0.9)', fg: '#a5f3fc', bold: true });
      const pct = cs.spec ? cs.inCarton / cs.spec : 0;
      ctx.fillStyle = 'rgba(148,163,184,0.35)';
      ctx.fillRect(b.x0, y + (compact ? 16 : 17) * u, wTxt, 3 * u);
      ctx.fillStyle = pct >= 1 ? '#38bdf8' : '#22c55e';
      ctx.fillRect(b.x0, y + (compact ? 16 : 17) * u, wTxt * Math.min(1, pct), 3 * u);
    } else if (z.kind === 'station' && !compact) {
      poly(ctx, z.pts);
      ctx.setLineDash([3 * u, 5 * u]);
      ctx.lineWidth = 1 * u;
      ctx.strokeStyle = 'rgba(226,232,240,0.35)';
      ctx.stroke();
      ctx.setLineDash([]);
      const b = bounds(z.pts);
      ctx.font = `600 ${9.5 * u}px Inter, system-ui, sans-serif`;
      ctx.fillStyle = 'rgba(226,232,240,0.7)';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(z.name.toUpperCase(), b.x0 + 3 * u, b.y1 - 4 * u);
    } else if (z.kind === 'count_line') {
      const [[x0, y0], [x1, y1]] = z.pts;
      const hit = recent(d, 'line_cross', f, null, 8);
      ctx.lineWidth = (hit ? 3.5 : 2) * u;
      ctx.strokeStyle = hit ? '#fde047' : '#facc15';
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
      const n = d.ev.filter((e) => e.type === 'line_cross' && e.f <= f).length + loops * d.lineTotal;
      chip(ctx, x0 + 4 * u, Math.min(y0, y1) - 20 * u, `COUNT ${n}`, { u, size: 10.5, bg: 'rgba(66,32,6,0.9)', fg: '#fde68a', bold: true });
    } else if (z.kind === 'conveyor') {
      poly(ctx, z.pts);
      ctx.fillStyle = 'rgba(167,139,250,0.09)';
      ctx.fill();
      ctx.lineWidth = 1.2 * u;
      ctx.strokeStyle = 'rgba(167,139,250,0.6)';
      ctx.stroke();
      if (d.flow.length) {
        const v = Math.abs(d.flow[Math.min(d.flow.length - 1, Math.floor(f / 3))] || 0);
        const ms = z.m_per_px ? (v * z.m_per_px).toFixed(2) : null;
        const b = bounds(z.pts);
        chip(ctx, b.x1 - 4 * u, b.y1 + 4 * u, ms ? `BELT ${ms} m/s` : `BELT ${Math.round(v)} px/s`,
          { u, size: compact ? 9.5 : 10.5, bg: 'rgba(46,16,101,0.88)', fg: '#ddd6fe', bold: true, align: 'right' });
      }
    } else if (z.kind === 'restricted') {
      poly(ctx, z.pts);
      const hit = recent(d, 'restricted', f, z.id, 30);
      ctx.fillStyle = hit ? 'rgba(239,68,68,0.25)' : 'rgba(239,68,68,0.07)';
      ctx.fill();
      ctx.lineWidth = 1.4 * u;
      ctx.strokeStyle = 'rgba(248,113,113,0.8)';
      ctx.stroke();
      if (!compact) {
        const b = bounds(z.pts);
        chip(ctx, b.x0 + 3 * u, b.y0 + 3 * u, z.name || 'RESTRICTED', { u, size: 9.5, bg: 'rgba(127,29,29,0.85)', fg: '#fecaca', bold: true });
      }
    }
  });
}

function drawObjects(ctx, d, f, u, compact) {
  (d.o[f] || []).forEach((o) => {
    const [id, cls, x, y, w, h, conf] = o;
    if (id >= 2000 && cls === 2 && conf < 25) return; // weak open-vocabulary pallet hits are mostly stripes/racks
    const m = OBJ[cls] || OBJ[0];
    if (id >= 1000 && id < 2000) {
      const tr = (d.trails[id] || []).filter((p) => p[0] <= f && f - p[0] <= 14);
      if (tr.length > 1) {
        ctx.lineWidth = 2 * u;
        for (let i = 1; i < tr.length; i += 1) {
          ctx.strokeStyle = hexA(m.color, 0.15 + 0.6 * (i / tr.length));
          ctx.beginPath();
          ctx.moveTo(tr[i - 1][1], tr[i - 1][2]);
          ctx.lineTo(tr[i][1], tr[i][2]);
          ctx.stroke();
        }
      }
    }
    ctx.lineWidth = 1.6 * u;
    ctx.strokeStyle = m.color;
    ctx.strokeRect(x, y, w, h);
    if (!compact) {
      chip(ctx, x, Math.max(0, y - 15 * u), `${m.label} ${(conf / 100).toFixed(2)}`, { u, size: 9, bg: hexA(m.color, 0.9), fg: '#1f0a17', bold: true });
    }
  });
}

function drawPose(ctx, d, f, u) {
  const acts = Object.fromEntries((d.p[f] || []).map((p) => [p[0], p[6]]));
  (d.k[f] || []).forEach((k) => {
    const tid = k[0];
    const pt = (i) => [k[1 + i * 2], k[2 + i * 2]];
    const col = (ACT[acts[tid]] || ACT[4]).color;
    ctx.lineWidth = 2.2 * u;
    ctx.strokeStyle = hexA(col, 0.85);
    LIMBS.forEach(([a, b]) => {
      const [xa, ya] = pt(a);
      const [xb, yb] = pt(b);
      if (xa < 0 || xb < 0) return;
      ctx.beginPath();
      ctx.moveTo(xa, ya);
      ctx.lineTo(xb, yb);
      ctx.stroke();
    });
    ctx.fillStyle = '#f8fafc';
    for (let i = 5; i < 17; i += 1) {
      const [x, y] = pt(i);
      if (x < 0) continue;
      ctx.beginPath();
      ctx.arc(x, y, (i === 9 || i === 10 ? 3.2 : 2.2) * u, 0, Math.PI * 2);
      ctx.fill();
    }
  });
}

function drawPeople(ctx, d, f, u, compact, ids, selected, hover) {
  (d.p[f] || []).forEach((p) => {
    const [tid, x, y, w, h, conf, act] = p;
    const a = ACT[act] || ACT[4];
    const who = ids?.[tid];
    const isSel = selected === tid || hover === tid;
    const L = Math.max(6, Math.min(w, h) * 0.2);
    ctx.strokeStyle = hexA(a.color, 0.35);
    ctx.lineWidth = 1 * u;
    ctx.strokeRect(x, y, w, h);
    ctx.strokeStyle = a.color;
    ctx.lineWidth = (isSel ? 3.4 : 2.2) * u;
    ctx.beginPath();
    [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]].forEach(([cx, cy, sx, sy]) => {
      ctx.moveTo(cx + sx * L, cy);
      ctx.lineTo(cx, cy);
      ctx.lineTo(cx, cy + sy * L);
    });
    ctx.stroke();
    if (isSel) {
      ctx.fillStyle = hexA(a.color, 0.08);
      ctx.fillRect(x, y, w, h);
    }
    if (h < (compact ? 70 : 34)) return; // tiny background figures: box only
    let state = a.label;
    if (act === 0) {
      const sec = idleSeconds(d, tid, f);
      if (sec >= 1) state = `Idle ${Math.round(sec)}s`;
    }
    const name = who ? who.name : `Person #${tid}`;
    const text = compact ? (who ? `${who.name.split(' ')[0]} · ${state}` : state) : `${name} · ${state}`;
    const ty = y - (compact ? 15 : 18) * u < 0 ? y + 2 * u : y - (compact ? 15 : 18) * u;
    const wChip = chip(ctx, x, ty, text, { u, size: compact ? 9.5 : 11, bg: hexA(a.color, 0.9), fg: '#05121f', bold: true });
    if (!compact && who) {
      chip(ctx, x + wChip + 3 * u, ty, `RFID ${who.code}`, { u, size: 9.5, bg: 'rgba(5,10,20,0.8)', fg: '#bae6fd' });
    }
    if (!compact && h > 120) {
      ctx.font = `600 ${9 * u}px Inter, system-ui, sans-serif`;
      ctx.fillStyle = 'rgba(241,245,249,0.85)';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(`${(conf / 100).toFixed(2)}`, x + 3 * u, y + h - 4 * u);
    }
  });
}

function drawOcr(ctx, d, f, u) {
  let fs = null;
  for (let i = d.ocrFrames.length - 1; i >= 0; i -= 1) if (d.ocrFrames[i] <= f) { fs = d.ocrFrames[i]; break; }
  if (fs == null) return;
  const age = f - fs;
  (d.ocrBy[fs] || []).forEach((o) => {
    const [x, y, w, h] = o.box;
    ctx.lineWidth = 1.3 * u;
    ctx.strokeStyle = age < 6 ? '#fde047' : 'rgba(253,224,71,0.75)';
    ctx.strokeRect(x - 2, y - 2, w + 4, h + 4);
    const raw = o.raw.replace(/\s+/g, ' ');
    const txt = raw.toUpperCase() === o.text.toUpperCase() ? `OCR “${o.text}” ${o.conf.toFixed(2)}` : `OCR “${raw}” → ${o.text} ${o.conf.toFixed(2)}`;
    chip(ctx, x - 2, y + h + 4 * u, txt, { u, size: 9, bg: 'rgba(66,52,6,0.88)', fg: '#fef08a' });
  });
}

function drawEvents(ctx, d, f, u, compact, feed) {
  d.ev.forEach((e) => {
    const age = f - e.f;
    if (age < 0 || age > 45) return;
    if (e.type === 'pillow_in' && age <= 22) {
      const z = d.zoneById[e.zone];
      if (!z) return;
      const b = bounds(z.pts);
      const cx = (b.x0 + b.x1) / 2;
      const cy = b.y0 - age * 1.6 * u;
      ctx.globalAlpha = Math.max(0, 1 - age / 22);
      ctx.font = `800 ${(compact ? 20 : 24) * u}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineWidth = 4 * u;
      ctx.strokeStyle = 'rgba(5,10,20,0.75)';
      const label = `+${e.n}`;
      ctx.strokeText(label, cx, cy);
      ctx.fillStyle = '#4ade80';
      ctx.fillText(label, cx, cy);
      if (!compact) {
        ctx.font = `700 ${10 * u}px Inter, system-ui, sans-serif`;
        ctx.strokeText(e.n > 1 ? `${e.n}-PACK IN` : 'PILLOW IN', cx, cy + 18 * u);
        ctx.fillText(e.n > 1 ? `${e.n}-PACK IN` : 'PILLOW IN', cx, cy + 18 * u);
      }
      ctx.textAlign = 'left';
      ctx.globalAlpha = 1;
    }
    if ((e.type === 'carton_complete' || e.type === 'carton_sealed') && age <= 36) {
      const z = d.zoneById[e.zone];
      if (!z) return;
      const b = bounds(z.pts);
      ctx.globalAlpha = Math.max(0, 1 - age / 36);
      chip(ctx, (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2 - 9 * u, e.type === 'carton_complete' ? `✓ CARTON COMPLETE ${e.count}/${e.count}` : '▣ CARTON SEALED',
        { u, size: compact ? 10 : 12, bg: 'rgba(3,105,161,0.95)', fg: '#f0f9ff', bold: true, align: 'center' });
      ctx.globalAlpha = 1;
    }
    if (e.type === 'label_read' && !compact && age <= 45) {
      ctx.globalAlpha = Math.min(1, (45 - age) / 10);
      chip(ctx, W / 2, 46 * u, `CARTON LABEL READ · ${e.text}${e.customer ? `  →  ${e.customer.toUpperCase()}` : ''}`,
        { u, size: 12, bg: 'rgba(76,29,149,0.92)', fg: '#ede9fe', bold: true, align: 'center' });
      ctx.globalAlpha = 1;
    }
    if (e.type === 'restricted' && age <= 30) {
      ctx.lineWidth = 6 * u;
      ctx.strokeStyle = `rgba(239,68,68,${0.8 * (1 - age / 30)})`;
      ctx.strokeRect(3, 3, W - 6, H - 6);
    }
  });
  if (feed?.kind === 'packing' && !compact) {
    // nothing else: the carton chips carry the live count
  }
}

function drawOsd(ctx, d, f, s, u, compact, feed, now) {
  const top = (compact ? 22 : 30) * u;
  const g = ctx.createLinearGradient(0, 0, 0, top * 1.6);
  g.addColorStop(0, 'rgba(0,0,0,0.6)');
  g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, top * 1.6);
  const fs = (compact ? 10 : 12) * u;
  ctx.font = `700 ${fs}px "JetBrains Mono", ui-monospace, Menlo, monospace`;
  ctx.textBaseline = 'middle';
  const blink = Math.floor(now / 700) % 2 === 0;
  ctx.fillStyle = blink ? '#ef4444' : 'rgba(239,68,68,0.35)';
  ctx.beginPath();
  ctx.arc(12 * u, top / 2, 4 * u, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#f8fafc';
  const head = compact ? feed.camera : `${feed.camera}  ${feed.title.toUpperCase()}`;
  ctx.fillText(head, 22 * u, top / 2);
  ctx.textAlign = 'right';
  ctx.fillText(osdTime(simNow()), W - 10 * u, top / 2);
  ctx.textAlign = 'left';
  if (!compact) {
    ctx.font = `600 ${10 * u}px "JetBrains Mono", ui-monospace, Menlo, monospace`;
    ctx.fillStyle = 'rgba(226,232,240,0.75)';
    ctx.fillText(`${feed.factory} · ${feed.line || ''}${feed.ptz ? ' · PTZ TOUR' : ' · FIXED'}`, 22 * u, top / 2 + 15 * u);
  }
  // bottom band
  const bh = (compact ? 20 : 26) * u;
  const g2 = ctx.createLinearGradient(0, H - bh * 1.8, 0, H);
  g2.addColorStop(0, 'rgba(0,0,0,0)');
  g2.addColorStop(1, 'rgba(0,0,0,0.65)');
  ctx.fillStyle = g2;
  ctx.fillRect(0, H - bh * 1.8, W, bh * 1.8);
  const by = H - bh / 2 - 2 * u;
  ctx.font = `700 ${(compact ? 9.5 : 10.5) * u}px Inter, system-ui, sans-serif`;
  ctx.fillStyle = '#38bdf8';
  const tag = `AI ▸ ${TYPE_TONE[feed.kind] || 'CV'}`;
  ctx.fillText(tag, 10 * u, by);
  if (!compact && d) {
    const tw = ctx.measureText(tag).width;
    ctx.font = `500 ${10 * u}px Inter, system-ui, sans-serif`;
    ctx.fillStyle = 'rgba(226,232,240,0.8)';
    const m = d.models;
    ctx.fillText(`  ${[(m.people || 'YOLO11m').split(' ')[0], 'ByteTrack', m.pose && !(m.people || '').includes('pose') ? 'Pose' : null, m.pillows ? 'Motion seg.' : null, m.objects ? 'YOLOE' : null, m.ocr ? 'OCR' : null, m.flow ? 'Flow' : null].filter(Boolean).join(' · ')} · ${d.fps} fps`, 10 * u + tw, by);
  }
  if (d) {
    const ps = d.p[f] || [];
    const active = ps.filter((p) => p[6] === 1 || p[6] === 2).length;
    const tot = sessionTotals(d, f, s.loops);
    const parts = [`${ps.length} people`, `${active} active`];
    if (feed.kind === 'packing') parts.push(`${tot.pillows} pillows`);
    if (d.lineTotal) parts.push(`${tot.crossings} counted`);
    ctx.font = `700 ${(compact ? 9.5 : 11) * u}px Inter, system-ui, sans-serif`;
    ctx.fillStyle = '#f8fafc';
    ctx.textAlign = 'right';
    ctx.fillText(parts.join('  ·  '), W - 10 * u, by);
    ctx.textAlign = 'left';
  }
}

function drawTooltip(ctx, d, f, u, tid, ids) {
  const p = (d.p[f] || []).find((q) => q[0] === tid);
  const sum = d.people[tid];
  if (!p || !sum) return;
  const who = ids?.[tid];
  const lines = who
    ? [who.name, `${who.role} · ${who.code}`, `RFID ${who.badge || ''}`.trim(), `Badge state: ${who.state}${who.zone ? ` · ${who.zone}` : ''}`]
    : [`Person #${tid}`, 'No badge matched at this station', 'Floor staff or visitor'];
  lines.push(`On camera: ${sum.pct.handling + sum.pct.lifting}% active · ${sum.pct.walking}% walking · ${sum.pct.idle}% idle`);
  if (sum.placed) lines.push(`Placed ${sum.placed} into cartons this clip`);
  const x = Math.min(W - 270 * u, p[1] + p[3] + 8 * u);
  const y = Math.max(40 * u, p[2]);
  ctx.font = `600 ${11 * u}px Inter, system-ui, sans-serif`;
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 18 * u;
  const h = lines.length * 16 * u + 12 * u;
  ctx.fillStyle = 'rgba(8,15,28,0.94)';
  rr(ctx, x, y, w, h, 6 * u);
  ctx.fill();
  ctx.strokeStyle = 'rgba(56,189,248,0.5)';
  ctx.lineWidth = 1 * u;
  ctx.stroke();
  lines.forEach((l, i) => {
    ctx.font = `${i === 0 ? 700 : 500} ${(i === 0 ? 12 : 10.5) * u}px Inter, system-ui, sans-serif`;
    ctx.fillStyle = i === 0 ? '#f8fafc' : '#b6c6dc';
    ctx.textBaseline = 'middle';
    ctx.fillText(l, x + 9 * u, y + 14 * u + i * 16 * u);
  });
}
