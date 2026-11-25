import React, { useEffect, useState } from 'react';
import { Badge, Button, Card, Icon } from '../../components/ui';
import { MiniChart } from '../../components/charts/ChartCard';
import { Kpi } from '../../components/common';
import { useApp } from '../../data/DataProvider';
import { api, post } from '../../api/client';
import { dayTime, num } from '../../utils';

const CONSIDERS = ['Open production orders', 'Customer priority & tier', 'SKU / changeover time', 'Line capacity & efficiency',
  'Machine availability', 'Material availability', 'Truck & ship dates'];

function Seq({ orders }) {
  if (!orders.length) return <span className="text-[11px] text-fg-faint">Idle — no orders</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {orders.map((o) => (
        <span key={o.po_id} className={`tile px-2 py-1 text-[10.5px] ${o.late_h > 0 ? 'tone-danger' : ''}`} title={`${o.customer || ''} ${o.order || ''} · due ${dayTime(o.due)}`}>
          <b className="text-fg">{o.po}</b> <span className="font-mono text-fg-muted">{o.sku}</span>
          <span className="block text-fg-faint">{num(o.remaining)} pcs · ends {dayTime(o.end)}{o.late_h > 0 ? ` · +${o.late_h} h` : ''}</span>
        </span>
      ))}
    </div>
  );
}

export default function OptimizationTab({ factory, presetFactoryId, focusRunId }) {
  const { meta, act, can } = useApp();
  const fixed = meta.factories.find((f) => f.code === factory);
  const [factoryId, setFactoryId] = useState(fixed?.id || presetFactoryId || meta.factories[0]?.id);
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (fixed) setFactoryId(fixed.id); }, [fixed]);
  useEffect(() => { if (presetFactoryId && !fixed) setFactoryId(presetFactoryId); }, [presetFactoryId, fixed]);
  useEffect(() => {
    if (!focusRunId) return;
    api(`/api/production/optimize/${focusRunId}`).then((r) => { setRun(r); setFactoryId(r.factory_id); }).catch(() => {});
  }, [focusRunId]);
  const allowed = can('optimize');

  const optimize = async () => {
    setBusy(true);
    try {
      const r = await act(() => post('/api/production/optimize', { factory_id: Number(factoryId) }),
        (res) => `Optimization #${res.id} ready: ${res.metrics.delay_reduction_h} h delay reduction, ${res.metrics.changeovers_saved} changeovers saved`);
      setRun(r);
    } catch (e) { /* toast */ } finally { setBusy(false); }
  };
  const decide = async (decision) => {
    setBusy(true);
    try {
      const r = await act(() => post(`/api/production/optimize/${run.id}/${decision}`),
        decision === 'accept' ? 'Optimized schedule applied — lines re-sequenced and estimates recalculated' : 'Recommendation rejected; current schedule kept');
      setRun(r);
    } catch (e) { /* toast */ } finally { setBusy(false); }
  };

  const b = run?.before.summary;
  const a = run?.after.summary;
  const m = run?.metrics;
  const fname = meta.factories.find((f) => f.id === Number(factoryId))?.name;
  return (
    <div className="space-y-4">
      <Card title="Production plan optimization" subtitle="Runs the scheduling algorithm on live orders, lines and materials">
        <div className="flex items-center gap-3 flex-wrap">
          {!fixed && (
            <select className="select select-sm" value={factoryId} onChange={(e) => { setFactoryId(Number(e.target.value)); setRun(null); }} aria-label="Factory to optimize">
              {meta.factories.map((f) => <option key={f.id} value={f.id}>{f.name}, {f.state}</option>)}
            </select>
          )}
          <Button icon="sparkles" variant="accent" disabled={!allowed || busy} onClick={optimize}>{busy && !run ? 'Optimizing…' : 'Optimize Production Plan'}</Button>
          {!allowed && <span className="text-[11px] text-fg-faint">Your role cannot run the optimizer.</span>}
          {focusRunId && run?.id === focusRunId && <span className="text-[11px] text-accent">Showing proposal #{focusRunId} drafted by the AI assistant ({run.status}).</span>}
        </div>
        <div className="flex flex-wrap gap-1.5 mt-3">
          {CONSIDERS.map((c) => <span key={c} className="chip" style={{ cursor: 'default' }}>{c}</span>)}
        </div>
      </Card>

      {run && (
        <>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-fg">Recommendation #{run.id} · {fname}</span>
            <Badge tone={run.status === 'Accepted' ? 'success' : run.status === 'Rejected' ? 'neutral' : 'info'} dot>{run.status.toUpperCase()}</Badge>
            <span className="text-[11px] text-fg-faint">{m.algorithm} · {m.orders_considered} orders</span>
            {run.status === 'Proposed' && (
              <span className="ml-auto flex gap-2">
                <Button variant="ghost" disabled={busy} onClick={() => decide('reject')}>Reject</Button>
                <Button icon="check" disabled={busy || !allowed} onClick={() => decide('accept')}>Accept &amp; apply</Button>
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
            <Kpi label="Late orders" value={`${b.late_orders} → ${a.late_orders}`} tone={a.late_orders < b.late_orders ? 'success' : undefined} sub="Projected past due date" />
            <Kpi label="Total delay" value={`${num(a.total_delay_h, 1)} h`} tone={m.delay_reduction_h > 0 ? 'success' : undefined}
              sub={`Was ${num(b.total_delay_h, 1)} h · reduction ${num(m.delay_reduction_h, 1)} h`} />
            <Kpi label="Changeovers" value={`${b.changeovers} → ${a.changeovers}`} tone={m.changeovers_saved > 0 ? 'success' : undefined} sub={`${m.changeovers_saved} saved`} />
            <Kpi label="Output next 24 h" value={num(a.output_24h)} tone={m.output_gain_24h > 0 ? 'success' : undefined}
              sub={`${m.output_gain_24h >= 0 ? '+' : ''}${num(m.output_gain_24h)} vs current`} />
            <Kpi label="Avg completion" value={`${num(a.avg_completion_h, 1)} h`} sub={`Was ${num(b.avg_completion_h, 1)} h from now`} />
            <Kpi label="All orders complete" value={dayTime(a.makespan)} sub={`Was ${dayTime(b.makespan)}`} />
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
            <Card title="Current vs optimized">
              <MiniChart type="bar" legend height={220} data={{
                labels: ['Late orders', 'Delay (h)', 'Changeovers'],
                datasets: [
                  { label: 'Current', data: [b.late_orders, b.total_delay_h, b.changeovers], color: 'chart-8' },
                  { label: 'Optimized', data: [a.late_orders, a.total_delay_h, a.changeovers], color: 'primary' },
                ],
              }} />
            </Card>
            <Card title="Line allocation and order sequence" subtitle="Current schedule (left) vs recommended (right); red = projected late" padded={false} className="xl:col-span-2">
              <div className="divide-y divide-line max-h-[520px] overflow-y-auto">
                {run.after.lines.map((ln) => {
                  const before = run.before.lines.find((x) => x.line_id === ln.line_id) || { orders: [] };
                  const changed = before.orders.map((o) => o.po_id).join() !== ln.orders.map((o) => o.po_id).join();
                  return (
                    <div key={ln.line_id} className="grid grid-cols-[150px_1fr_20px_1fr] gap-2 px-4 py-2.5 items-start">
                      <div>
                        <p className="text-[12px] font-semibold text-fg">{ln.line}</p>
                        <p className="text-[10px] text-fg-faint">{ln.status} · {num(ln.rate)}/h</p>
                        {changed && <Badge tone="accent">CHANGED</Badge>}
                      </div>
                      <Seq orders={before.orders} />
                      <Icon name="arrowRight" size={14} className="text-fg-faint mt-2" />
                      <Seq orders={ln.orders} />
                    </div>
                  );
                })}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
