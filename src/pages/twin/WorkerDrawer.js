import React from 'react';
import { Badge, Drawer, Icon } from '../../components/ui';
import { KV, Loading } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { hhmm, num, pct } from '../../utils';
import { ROLE_COLOR, STATE_COLOR } from './layout';

export const STATE_LABEL = { Station: 'At station', Away: 'Away', Break: 'On break', Absent: 'Absent', Off: 'Shift ended', Scheduled: 'Scheduled', 'Not arrived': 'Not arrived' };
export const stateColor = (w, threshold = 10) => {
  if (!w) return STATE_COLOR.NoRead;
  if (w.state === 'Away') return (w.zone || '').startsWith('No read') ? STATE_COLOR.NoRead
    : (w.minutes_in_state || 0) >= threshold ? STATE_COLOR.Long : STATE_COLOR.Away;
  return STATE_COLOR[w.state] || '#64748b';
};
const SEG_COLOR = { Station: '#22c55e', Away: '#f59e0b', Break: '#38bdf8' };
const toMin = (s) => {
  if (!s) return null;
  const [h, m] = s.slice(11, 16).split(':').map(Number);
  return h * 60 + m + (Number(s.slice(17, 19)) || 0) / 60;
};

function Timeline({ timeline, window, now }) {
  const a = toMin(window[0]);
  const b = toMin(window[1]);
  const span = b - a || 1;
  const nowMin = Math.min(b, toMin(now));
  const hours = [];
  for (let h = Math.ceil(a / 60); h * 60 <= b; h += 1) hours.push(h);
  return (
    <div>
      <div className="twin-gantt">
        {timeline.map((iv, i) => {
          const s = Math.max(a, toMin(iv.start));
          const e = Math.min(b, iv.end ? toMin(iv.end) : nowMin);
          if (e <= s) return null;
          const noRead = (iv.zone || '').startsWith('No read');
          return (
            <span key={i} title={`${iv.state} · ${iv.zone} · ${hhmm(iv.start)}–${iv.end ? hhmm(iv.end) : 'now'} (${num(iv.minutes, 1)} min)`}
              style={{ left: `${((s - a) / span) * 100}%`, width: `${Math.max(0.35, ((e - s) / span) * 100)}%`,
                background: noRead ? 'repeating-linear-gradient(45deg,#94a3b8 0 3px,#475569 3px 6px)' : SEG_COLOR[iv.state] || '#64748b',
                opacity: iv.state === 'Station' ? 0.85 : 1 }} />
          );
        })}
        <span style={{ left: `${((nowMin - a) / span) * 100}%`, width: 2, background: '#f8fafc' }} title="Now" />
      </div>
      <div className="relative h-4 mt-1">
        {hours.map((h) => (
          <span key={h} className="absolute text-[9.5px] text-fg-faint -translate-x-1/2" style={{ left: `${((h * 60 - a) / span) * 100}%` }}>{String(h).padStart(2, '0')}:00</span>
        ))}
      </div>
    </div>
  );
}

export default function WorkerDrawer({ id, onClose, threshold }) {
  const { data, loading } = useApi(id ? `/api/twin/workers/${id}` : null, {}, { every: 2 });
  const w = data?.worker;
  const away = (data?.timeline || []).filter((iv) => iv.state === 'Away');
  return (
    <Drawer onClose={onClose} width={460} eyebrow="RFID badge · worker"
      title={w ? w.name : 'Loading…'}
      subtitle={w ? `${w.role} · ${w.line || 'Plant'} · shift ${w.shift} · ${w.code}` : undefined}>
      {loading && !data && <Loading rows={5} />}
      {w && (
        <div className="space-y-4">
          <div className="tile flex items-center gap-3">
            <span className="w-3 h-3 rounded-full shrink-0" style={{ background: stateColor(w, threshold), boxShadow: `0 0 10px ${stateColor(w, threshold)}` }} />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-bold text-fg">{STATE_LABEL[w.state] || w.state}{w.minutes_in_state != null && w.state !== 'Absent' ? ` · ${num(w.minutes_in_state)} min` : ''}</p>
              <p className="text-[11px] text-fg-muted truncate">{w.zone}{w.reader ? ` · reader ${w.reader}` : ''}{w.since ? ` · since ${hhmm(w.since)}` : ''}</p>
            </div>
            <span className="w-3 h-3 rounded-sm shrink-0" title={`Role colour: ${w.role}`} style={{ background: ROLE_COLOR[w.role] }} />
          </div>
          {w.flag && <div className="tile tone-warning text-[11.5px] flex gap-2"><Icon name="alert" size={14} />{w.flag}</div>}
          <KV cols={3} items={[
            ['At station', pct(w.availability_pct)],
            ['Performance', w.performance_pct != null ? pct(w.performance_pct) : 'n/a'],
            ['Effectiveness', pct(w.effectiveness_pct)],
            ['Time away', `${num(w.away_min)} min`],
            ['Trips away', num(w.away_trips)],
            ['Longest trip', `${num(w.longest_away_min)} min`],
            ['Breaks taken', `${num(w.break_min)} min`],
            ['Break overrun', `${num(w.overrun_min)} min`],
            ['Pillows credited', w.units != null ? num(w.units) : '—'],
          ]} />
          <p className="text-[10.5px] text-fg-faint leading-snug -mt-1">
            Effectiveness = time at station (RFID) × output rate vs standard{w.performance_pct == null ? ' (no individual output standard for this role, so availability only)' : ''}.
            Scheduled breaks and lunch never count as time away.
          </p>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <p className="label-caps">Shift timeline</p>
              <span className="flex gap-3 text-[10px] text-fg-muted">
                <span className="flex items-center gap-1"><span className="twin-legend-dot" style={{ background: SEG_COLOR.Station }} />Station</span>
                <span className="flex items-center gap-1"><span className="twin-legend-dot" style={{ background: SEG_COLOR.Away }} />Away</span>
                <span className="flex items-center gap-1"><span className="twin-legend-dot" style={{ background: SEG_COLOR.Break }} />Break</span>
              </span>
            </div>
            <Timeline timeline={data.timeline} window={data.shift_window} now={data.sim_time} />
          </div>
          <div>
            <p className="label-caps mb-1.5">Where the time away went</p>
            {data.zones.length === 0 && <p className="text-[11px] text-fg-faint">No unscheduled time away this shift.</p>}
            <div className="space-y-1.5">
              {data.zones.map((z) => (
                <div key={z.zone} className="flex items-center gap-2 text-[11px]">
                  <span className="w-36 truncate text-fg-muted">{z.zone}</span>
                  <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--color-surface-raised)' }}>
                    <div className="h-full rounded-full" style={{ width: `${(z.minutes / Math.max(...data.zones.map((x) => x.minutes))) * 100}%`, background: '#f59e0b' }} />
                  </div>
                  <span className="w-12 text-right tabular-nums text-fg">{num(z.minutes)} min</span>
                </div>
              ))}
            </div>
            {data.peer_avg_away_min != null && (
              <p className="text-[11px] text-fg-muted mt-2">
                Role average on this shift: <b className="text-fg">{num(data.peer_avg_away_min)} min</b> away ({data.peer_count} {w.role.toLowerCase()}s)
                {w.away_min > data.peer_avg_away_min * 1.5 && <Badge tone="warning" className="ml-1.5">{(w.away_min / Math.max(1, data.peer_avg_away_min)).toFixed(1)}× average</Badge>}
              </p>
            )}
          </div>
          {away.length > 0 && (
            <div>
              <p className="label-caps mb-1.5">Badge reads away from the station</p>
              <div className="divide-y divide-line text-[11px]">
                {away.slice().reverse().slice(0, 12).map((iv, i) => (
                  <div key={i} className="flex items-center gap-2 py-1.5">
                    <span className="tabular-nums text-fg-faint w-24">{hhmm(iv.start)}–{iv.end ? hhmm(iv.end) : 'now'}</span>
                    <span className="flex-1 truncate text-fg">{iv.zone}</span>
                    <span className="tabular-nums text-fg-muted">{num(iv.minutes, 1)} min</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {data.history.length > 0 && (
            <div>
              <p className="label-caps mb-1.5">Previous days</p>
              <div className="grid grid-cols-5 gap-1.5 text-center text-[10.5px]">
                {data.history.map((h) => (
                  <div key={h.date} className="tile p-1.5">
                    <p className="text-fg-faint">{h.date.slice(5)}</p>
                    <p className="font-bold text-fg">{num(h.away_min)}m</p>
                    <p className="text-fg-faint">{pct(h.availability_pct, 0)}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="tile text-[10.5px] text-fg-muted leading-snug">
            <b className="text-fg">Badge {w.badge}</b> · passive UHF RFID read by zone readers at stations, doors and amenities.
            Location is recorded per zone only (no cameras, no facial recognition).
          </div>
        </div>
      )}
    </Drawer>
  );
}
