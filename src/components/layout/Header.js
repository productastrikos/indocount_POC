import React, { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Icon, IconButton } from '../ui';
import { useTheme } from '../../theme/ThemeProvider';
import { useApp } from '../../data/DataProvider';
import { ROUTES } from '../../routes';
import { APP } from '../../config';
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
  const { alerts, sim, socket } = useApp();
  const route = ROUTES.find((r) => r.path === pathname);
  const critical = alerts.filter((a) => a.severity === 'critical').length;

  return (
    <header className="header">
      <IconButton icon="menu" label="Toggle sidebar" onClick={onToggleSidebar} />
      <div className="hidden md:flex flex-col leading-tight mr-1 min-w-0">
        <span className="text-[13px] font-bold text-fg truncate">{route?.label || APP.subtitle}</span>
        <span className="text-[10.5px] text-fg-faint flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: `var(--color-${socket === 'live' ? 'success' : 'warning'})` }} />
          {dateTime(sim?.sim_time)} · {socket === 'live' ? 'live' : 'reconnecting…'}
        </span>
      </div>
      <div className="flex-1 flex justify-center min-w-0">
        <Filters show={route?.filters || []} />
      </div>
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
