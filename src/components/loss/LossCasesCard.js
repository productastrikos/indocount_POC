import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Card, FilterChips } from '../ui';
import { Empty, ErrorBox, Loading } from '../common';
import { useApi } from '../../hooks/useApi';
import CaseRow from './CaseRow';
import LossIncidentDrawer from './LossIncidentDrawer';
import { usd } from './lossShared';

const STATUS = [{ value: 'active', label: 'Active' }, { value: 'Resolved', label: 'Closed' }, { value: '', label: 'All' }];

/**
 * Loss-prevention cases for one lifecycle stage (procurement, production, quality, warehouse, dispatch),
 * with the case drawer. The full queue, patterns and playbooks live on the Loss Prevention page.
 */
export default function LossCasesCard({ factory = 'ALL', stage, title = 'Loss prevention', subtitle, limit = 12, bare = false }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState('active');
  const [open, setOpen] = useState(null);
  const { data, loading, error, reload } = useApi('/api/loss/incidents', { factory, stage, status: status || undefined, limit }, { every: 3 });
  const rows = data?.incidents || [];
  const atRisk = rows.filter((c) => c.status !== 'Resolved').reduce((s, c) => s + (c.value_usd || 0), 0);
  const sub = subtitle || 'Pilferage signals at this stage: each one is a case with evidence, an owner, a playbook and an outcome';
  const all = <Button size="sm" variant="secondary" icon="external" onClick={() => navigate('/loss-prevention')}>All cases & patterns</Button>;
  const body = (
    <>
      <div className="px-4 py-2.5 border-b border-line flex items-center justify-between gap-3 flex-wrap">
        <FilterChips options={STATUS} value={status} onChange={setStatus} />
        <span className="flex items-center gap-3">
          {status === 'active' && data && <span className="text-[11px] text-fg-muted">{rows.length} active · {usd(atRisk)} at risk</span>}
          {bare && all}
        </span>
      </div>
      {bare && <p className="px-4 pt-2.5 text-[11px] text-fg-muted">{sub}</p>}
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data && <div className="p-4"><Loading rows={4} /></div>}
      {data && !rows.length && <Empty icon="shield">{status === 'active' ? 'No active loss-prevention case at this stage.' : 'No cases.'}</Empty>}
      <div className="divide-y divide-line">
        {rows.map((c) => <CaseRow key={c.id} c={c} now={data.sim_time} onOpen={setOpen} showPlant={factory === 'ALL'} />)}
      </div>
    </>
  );
  return (
    <>
      {bare ? body : <Card padded={false} title={title} subtitle={sub} actions={all}>{body}</Card>}
      {open && <LossIncidentDrawer id={open} onClose={() => setOpen(null)} />}
    </>
  );
}
