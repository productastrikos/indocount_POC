import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';
import NotificationsPanel from './NotificationsPanel';
import InsightsPanel from './InsightsPanel';
import { useApp } from '../../data/DataProvider';
import { Icon } from '../ui';

function Toasts() {
  const { toasts } = useApp();
  return (
    <div className="fixed bottom-4 right-4 z-[1500] flex flex-col gap-2 max-w-sm" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`tile tone-${t.tone} shadow-lg text-xs font-semibold flex items-start gap-2 animate-fade-in`}
          style={{ background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)' }}>
          <Icon name={t.tone === 'danger' ? 'alert' : 'check'} size={14} className="mt-0.5" />
          <span className="text-fg">{t.message}</span>
        </div>
      ))}
    </div>
  );
}

/* App shell: sidebar, header, scrollable content area and slide-out panels. */
export default function Layout({ children }) {
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 1100);
  const [panel, setPanel] = useState(null);
  const { pathname } = useLocation();
  const togglePanel = (name) => setPanel((p) => (p === name ? null : name));
  useEffect(() => { setPanel(null); }, [pathname]);

  return (
    <div className="shell">
      <Sidebar collapsed={collapsed} />
      <div className="flex-1 flex flex-col min-w-0">
        <Header onToggleSidebar={() => setCollapsed((c) => !c)} activePanel={panel} onTogglePanel={togglePanel} />
        <main className="flex-1 overflow-y-auto p-4 md:p-5">{children}</main>
      </div>
      {panel === 'notifications' && <NotificationsPanel onClose={() => setPanel(null)} />}
      {panel === 'insights' && <InsightsPanel onClose={() => setPanel(null)} />}
      <Toasts />
    </div>
  );
}
