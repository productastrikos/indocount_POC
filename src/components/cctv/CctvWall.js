import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useApi } from '../../hooks/useApi';
import CctvPlayer from './CctvPlayer';
import CctvViewer from './CctvViewer';
import { COMPACT_LAYERS, useAnalytics, useSimClock } from './cctvShared';
import './cctv.css';

function useVisible(ref) {
  const [vis, setVis] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') { setVis(true); return undefined; }
    const io = new IntersectionObserver(([e]) => setVis(e.isIntersecting), { threshold: 0.15 });
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return vis;
}

function stat(feed) {
  const s = feed.summary || {};
  if (feed.kind === 'packing' && s.pillows_per_min) return `${s.pillows_per_min} pillows/min placed`;
  if (s.line_crossings) return `${s.line_crossings} pillows counted per loop`;
  if (s.station_active_pct != null) return `station ${s.station_active_pct}% active`;
  return `${s.people_peak ?? 0} people in view`;
}

export function CctvTile({ feed, onOpen, size = 'md' }) {
  const ref = useRef(null);
  const visible = useVisible(ref);
  const { data } = useAnalytics(feed.analytics, visible);
  return (
    <button ref={ref} type="button" className="cctv-tile text-left w-full" onClick={() => onOpen(feed.id)}
      title={`${feed.camera} · ${feed.title}`}>
      <CctvPlayer feed={feed} analytics={data} layers={COMPACT_LAYERS} compact playing={visible} />
      <div className={`flex items-start justify-between gap-2 ${size === 'sm' ? 'mt-1' : 'mt-1.5'}`}>
        <div className="min-w-0">
          <p className="text-xs font-semibold text-fg truncate">{feed.camera} · {feed.title}</p>
          <p className="text-[10.5px] text-fg-faint truncate">{[feed.factory, feed.line, feed.area, feed.ptz && 'PTZ'].filter(Boolean).join(' · ')}</p>
        </div>
        <span className="text-[10.5px] text-fg-muted whitespace-nowrap">{stat(feed)}</span>
      </div>
    </button>
  );
}

/**
 * Grid of live CCTV tiles with CV overlays. `filter(feed)` selects the cameras,
 * `chips` optionally adds a quick filter row [{ key, label, test }].
 */
export default function CctvWall({ factory = 'ALL', filter, chips, cols = 3, emptyText = 'No cameras for this selection.', onOpenWorker }) {
  useSimClock();
  const { data, loading, error } = useApi('/api/cctv/feeds', { factory }, { live: false });
  const [chip, setChip] = useState(chips?.[0]?.key || null);
  const [open, setOpen] = useState(null);
  const list = useMemo(() => {
    let out = (data?.feeds || []).filter((f) => !filter || filter(f));
    const c = chips?.find((x) => x.key === chip);
    if (c?.test) out = out.filter(c.test);
    return out;
  }, [data, filter, chips, chip]);
  const grid = { 1: '', 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-2 xl:grid-cols-3', 4: 'sm:grid-cols-2 xl:grid-cols-4' }[cols] ?? 'sm:grid-cols-2 xl:grid-cols-3';

  return (
    <div>
      {chips && (
        <div className="flex flex-wrap gap-1.5 mb-3">
          {chips.map((c) => {
            const n = (data?.feeds || []).filter((f) => (!filter || filter(f)) && (!c.test || c.test(f))).length;
            return (
              <button key={c.key} type="button" onClick={() => setChip(c.key)}
                className={`px-2.5 py-1 rounded-full text-xs border ${chip === c.key ? 'bg-primary text-white border-primary font-semibold' : 'border-line text-fg-muted hover:bg-surface-raised'}`}>
                {c.label} <span className="opacity-70">{n}</span>
              </button>
            );
          })}
        </div>
      )}
      {error && <p className="text-xs text-danger">Could not load cameras: {error}</p>}
      {loading && !data && <p className="text-xs text-fg-faint">Connecting to cameras…</p>}
      {data && !list.length && <p className="text-xs text-fg-faint">{emptyText}</p>}
      <div className={`grid grid-cols-1 ${grid} gap-3`}>
        {list.map((f) => <CctvTile key={f.id} feed={f} onOpen={setOpen} />)}
      </div>
      {open && <CctvViewer feedId={open} feeds={list} onClose={() => setOpen(null)}
        onOpenWorker={onOpenWorker ? (id) => { setOpen(null); onOpenWorker(id); } : undefined} />}
    </div>
  );
}
