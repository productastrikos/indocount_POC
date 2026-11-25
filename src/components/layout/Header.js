import React, { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Badge, Button, Icon, IconButton } from '../ui';
import { useTheme } from '../../theme/ThemeProvider';
import { useApp } from '../../data/DataProvider';
import { ROUTES } from '../../routes';
import { APP } from '../../config';
import { post } from '../../api/client';
import { dateTime, initials } from '../../utils';

function useDismiss(ref, open, onClose) {
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => ref.current && !ref.current.contains(e.target) && onClose();
    const onKey = (e) => e.key === 'Escape' && onClose();
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [ref, open, onClose]);
}

const SCENARIOS = [
  ['machine_failure', 'Machine failure', 'wrench'],
  ['production_delay', 'Production delay', 'hourglass'],
  ['material_shortage', 'Material shortage', 'layers'],
  ['packaging_mismatch', 'Packaging mismatch', 'box'],
  ['workforce_decline', 'Workforce productivity decline', 'users'],
  ['truck_delay', 'Truck delay', 'truck'],
];

/** Unobtrusive simulation control (administrators only). */
function SimControl() {
  const { sim, setSim, act, filters, selectedFactory, meta } = useApp();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(null);
  const [last, setLast] = useState(null);
  const ref = useRef(null);
  useDismiss(ref, open, () => setOpen(false));

  const run = async (key, fn, msg) => {
    setBusy(key);
    try {
      const res = await act(fn, msg);
      if (res?.sim) setSim(res.sim);
      else if (res?.sim_time) setSim(res);
      if (res?.message) setLast(res.message);
    } catch (e) { /* toast shown */ } finally { setBusy(null); }
  };

  const target = selectedFactory?.id || null;
  return (
    <div className="relative" ref={ref}>
      <Button variant={sim?.running ? 'soft' : 'secondary'} size="sm" icon="bolt" onClick={() => setOpen((o) => !o)}
        title="Simulation control">
        <span className="hidden lg:inline">{sim?.running ? `Sim ${sim.speed}×` : 'Sim paused'}</span>
      </Button>
      {open && (
        <div className="menu right-0 w-80" role="dialog" aria-label="Simulation control">
          <div className="px-4 py-3 border-b">
            <p className="text-xs font-bold text-fg flex items-center gap-2">Simulation <Badge tone="warning">DEMO DATA</Badge></p>
            <p className="text-[10.5px] text-fg-faint mt-0.5">Simulated clock {dateTime(sim?.sim_time)} · tick {sim?.tick ?? '—'}</p>
          </div>
          <div className="menu-section">
            <div className="flex gap-1.5 px-2 py-1">
              {sim?.running
                ? <Button size="sm" variant="secondary" icon="clock" disabled={busy} onClick={() => run('pause', () => post('/api/sim/pause'))}>Pause</Button>
                : <Button size="sm" icon="bolt" disabled={busy} onClick={() => run('start', () => post('/api/sim/start'))}>Start</Button>}
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => run('step', () => post('/api/sim/step', { speed: 15 }), 'Advanced 15 simulated minutes')}>+15 min</Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => {
                if (window.confirm('Reset all simulated data to the start of the demo day? This takes about 30 seconds.')) {
                  run('reset', () => post('/api/sim/reset'), 'Simulation reset to 11:00');
                }
              }}>{busy === 'reset' ? 'Resetting…' : 'Reset'}</Button>
            </div>
            <div className="px-2 py-1.5">
              <p className="label-caps mb-1">Speed (simulated minutes per 3-second tick)</p>
              <div className="timeframe">
                {[1, 5, 15, 30].map((s) => (
                  <button key={s} type="button" className={sim?.speed === s ? 'is-active' : ''}
                    onClick={() => run('speed', () => post('/api/sim/speed', { speed: s }))}>{s}×</button>
                ))}
              </div>
            </div>
          </div>
          <div className="menu-section">
            <p className="label-caps px-2 pt-1 pb-1.5">Trigger scenario · {target ? meta?.factories.find((f) => f.id === target)?.name : 'any factory'}</p>
            {SCENARIOS.map(([kind, label, icon]) => (
              <button key={kind} type="button" className="menu-item" disabled={!!busy}
                onClick={() => run(kind, () => post('/api/sim/scenario/run', { kind, factory_id: target }), (r) => r.message)}>
                <Icon name={icon} size={15} />
                <span className="flex-1">{busy === kind ? 'Running…' : `Generate ${label.toLowerCase()}`}</span>
              </button>
            ))}
            {last && <p className="tile tone-info text-[10.5px] m-2">{last}</p>}
            {filters.factory === 'ALL' && <p className="text-[10px] text-fg-faint px-2 pb-1">Select a factory in the header to target it.</p>}
          </div>
        </div>
      )}
    </div>
  );
}

function ProfileMenu() {
  const { user, logout } = useApp();
  const { toggleTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useDismiss(ref, open, () => setOpen(false));
  if (!user) return null;
  return (
    <div className="relative" ref={ref}>
      <button type="button" className="avatar" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open} title={user.name}>
        {initials(user.name)}
      </button>
      {open && (
        <div className="menu right-0" role="menu">
          <div className="flex items-center gap-3 px-4 py-3 border-b">
            <div className="avatar">{initials(user.name)}</div>
            <div className="min-w-0">
              <p className="text-xs font-bold text-fg truncate">{user.name}</p>
              <p className="text-[10px] text-fg-faint">{user.role_label} · {user.username}</p>
            </div>
          </div>
          <div className="menu-section">
            <button type="button" className="menu-item" onClick={() => { setOpen(false); toggleTheme(); }}><Icon name="sun" size={15} />Toggle theme</button>
            <button type="button" className="menu-item" onClick={logout}><Icon name="logout" size={15} />Sign out</button>
          </div>
        </div>
      )}
    </div>
  );
}

function Filters({ show }) {
  const { meta, filters, setFilter, sim } = useApp();
  if (!meta) return null;
  const today = sim?.sim_time?.slice(0, 10);
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <select className="select select-sm" aria-label="Factory" value={filters.factory} onChange={(e) => setFilter('factory', e.target.value)}>
        <option value="ALL">All factories</option>
        {meta.factories.map((f) => <option key={f.code} value={f.code}>{f.name}, {f.state}</option>)}
      </select>
      {show.includes('date') && (
        <input type="date" className="input select-sm" aria-label="Date" style={{ width: 128 }} max={today}
          value={filters.date || today || ''} onChange={(e) => setFilter('date', e.target.value === today ? '' : e.target.value)} />
      )}
      {show.includes('shift') && (
        <select className="select select-sm" aria-label="Shift" value={filters.shift} onChange={(e) => setFilter('shift', e.target.value)}>
          <option value="">Both shifts</option>
          <option value="1">Shift 1 · 06:00–14:30</option>
          <option value="2">Shift 2 · 14:30–23:00</option>
        </select>
      )}
      {show.includes('customer') && (
        <select className="select select-sm" aria-label="Customer" value={filters.customer} onChange={(e) => setFilter('customer', e.target.value)}>
          <option value="">All customers</option>
          {meta.customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      )}
    </div>
  );
}

export default function Header({ onToggleSidebar, activePanel, onTogglePanel }) {
  const { pathname } = useLocation();
  const { theme, toggleTheme } = useTheme();
  const { alerts, sim, socket, can } = useApp();
  const route = ROUTES.find((r) => r.path === pathname);
  const critical = alerts.filter((a) => a.severity === 'critical').length;

  return (
    <header className="header">
      <IconButton icon="menu" label="Toggle sidebar" onClick={onToggleSidebar} />
      <div className="hidden md:flex flex-col leading-tight mr-1 min-w-0">
        <span className="text-[13px] font-bold text-fg truncate">{route?.label || APP.subtitle}</span>
        <span className="text-[10.5px] text-fg-faint flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: `var(--color-${socket === 'live' && sim?.running ? 'success' : 'warning'})` }} />
          {dateTime(sim?.sim_time)} {sim?.running ? '· live' : '· paused'}
        </span>
      </div>
      <div className="flex-1 flex justify-center min-w-0">
        <Filters show={route?.filters || []} />
      </div>
      <Badge tone="warning" className="hidden xl:inline-flex">SIMULATED DATA</Badge>
      {can('simulation') && <SimControl />}
      {APP.features.themeToggle && (
        <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label="Toggle theme" onClick={toggleTheme} />
      )}
      {APP.features.insights && route?.stage !== 'ai' && (
        <IconButton icon="sparkles" label="S!AP advisories" active={activePanel === 'insights'} onClick={() => onTogglePanel('insights')} />
      )}
      <IconButton icon="bell" label="Alerts" active={activePanel === 'notifications'} onClick={() => onTogglePanel('notifications')}>
        {alerts.length > 0 && (
          <span className="notify-count" style={{ background: `var(--color-${critical ? 'danger' : 'warning'})` }}>{alerts.length}</span>
        )}
      </IconButton>
      <ProfileMenu />
    </header>
  );
}
