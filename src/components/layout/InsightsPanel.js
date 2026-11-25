import React, { useState } from 'react';
import { useLocation } from 'react-router-dom';
import { IconButton, Icon, Tabs } from '../ui';
import { InsightsCard } from '../common';
import { SiapAdvisoryList } from '../common/SiapAdvisories';
import { useApp } from '../../data/DataProvider';
import { ROUTES } from '../../routes';

const TABS = [
  { value: 'predictive', label: 'Predictive' },
  { value: 'page', label: 'This page' },
];

/** S!AP Advisories side panel: ranked predictive advisories + the current page's recommendations. */
export default function InsightsPanel({ onClose }) {
  const { pathname } = useLocation();
  const { filters, meta } = useApp();
  const [tab, setTab] = useState('predictive');
  const route = ROUTES.find((r) => r.path === pathname);
  const page = route?.stage && route.stage !== 'ai' ? route.stage : 'overview';
  const scope = filters.factory === 'ALL' ? 'All factories' : (meta?.factories || []).find((f) => f.code === filters.factory)?.name || filters.factory;
  return (
    <aside className="side-panel w-[440px] max-w-full" style={{ borderLeft: '2px solid var(--color-accent-border)' }}>
      <div className="panel-header">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold text-accent"><Icon name="sparkles" size={15} />S!AP Advisories</h3>
          <p className="text-xs text-fg-faint truncate">
            {tab === 'predictive' ? `${scope} · predictive model on simulated telemetry` : `${route?.label || 'Executive Overview'} · computed from live records`}
          </p>
        </div>
        <IconButton icon="close" label="Close" plain onClick={onClose} />
      </div>
      <div className="px-3 pt-3">
        <Tabs tabs={TABS} value={tab} onChange={setTab} size="sm" />
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {tab === 'predictive' ? (
          <div className="card overflow-hidden" style={{ borderColor: 'var(--color-accent-border)' }}>
            <SiapAdvisoryList factory={filters.factory} limit={15} compact />
          </div>
        ) : (
          <InsightsCard page={page} limit={10} compact title="Recommendations for this page" />
        )}
      </div>
    </aside>
  );
}
