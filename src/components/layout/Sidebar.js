import React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Icon } from '../ui';
import { NAV_SECTIONS } from '../../routes';
import { APP } from '../../config';
import { useApp } from '../../data/DataProvider';
import { initials } from '../../utils';

export function BrandMark() {
  return APP.logo
    ? <img src={APP.logo} alt={APP.name} style={{ height: 32, width: 'auto' }} />
    : <div className="brand-mark">{APP.logoInitials || initials(APP.name)}</div>;
}

export default function Sidebar({ collapsed }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { alerts, user } = useApp();
  const counts = alerts.reduce((acc, a) => {
    if (a.severity === 'critical') acc[a.page] = (acc[a.page] || 0) + 1;
    return acc;
  }, {});

  return (
    <aside className={`sidebar ${collapsed ? 'is-collapsed' : ''}`}>
      <div className="sidebar-brand" onClick={() => navigate('/')}>
        <BrandMark />
        {!collapsed && (
          <span className="flex flex-col leading-tight min-w-0">
            <span className="brand-name">{APP.name}</span>
            <span className="text-[10px] text-fg-faint truncate">{APP.subtitle}</span>
          </span>
        )}
      </div>

      <nav className="nav">
        {NAV_SECTIONS.map((section) => (
          <div key={section.label}>
            {collapsed ? <div className="h-3" /> : <p className="nav-section-label">{section.label}</p>}
            {section.items.map((item) => {
              const page = item.path === '/' ? 'overview' : item.path.slice(1);
              const n = counts[page];
              return (
                <button
                  key={item.path}
                  type="button"
                  className={`nav-item ${pathname === item.path ? 'is-active' : ''}`}
                  onClick={() => navigate(item.path)}
                  title={collapsed ? item.label : undefined}
                >
                  <Icon name={item.icon} size={18} />
                  {!collapsed && <span className="truncate flex-1">{item.label}</span>}
                  {!collapsed && n > 0 && <span className="badge tone-danger" style={{ padding: '0 6px' }}>{n}</span>}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      {!collapsed && user && (
        <div className="sidebar-user">
          <div className="avatar" style={{ width: 30, height: 30, fontSize: 11, borderRadius: 8 }}>{initials(user.name)}</div>
          <div className="min-w-0">
            <p className="text-xs font-semibold text-fg truncate">{user.name}</p>
            <p className="text-[10px] text-fg-faint truncate">{user.role_label}</p>
          </div>
        </div>
      )}
    </aside>
  );
}
