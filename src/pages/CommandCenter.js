import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button, Card, Icon } from '../components/ui';
import { MiniChart } from '../components/charts/ChartCard';
import { PageTitle } from '../components/common';
import { useApi } from '../hooks/useApi';
import { useApp } from '../data/DataProvider';
import { post } from '../api/client';
import { linkTo, titleCase } from '../utils';

const HISTORY_KEY = 'ic_ai_history';
const PAGE_LABEL = { overview: 'Overview', procurement: 'Procurement', production: 'Production', quality: 'Quality', warehouse: 'Warehouse', dispatch: 'Dispatch' };

function AnswerChart({ chart }) {
  const type = chart.type || 'bar';
  const ds = chart.datasets.map((d) => ({ ...d }));
  const horizontal = type === 'bar' && chart.labels.length > 6;
  return (
    <div className="tile mt-2">
      <p className="label-caps mb-2">{chart.title}</p>
      <MiniChart type={type} height={type === 'doughnut' ? 180 : Math.max(170, horizontal ? chart.labels.length * 22 : 170)}
        horizontal={horizontal} stacked={!!chart.stacked} legend={ds.length > 1 || type === 'doughnut'}
        data={{ labels: chart.labels, datasets: ds }} />
    </div>
  );
}

function AnswerTable({ table }) {
  return (
    <div className="tile mt-2 overflow-x-auto p-0">
      <table className="table">
        <thead><tr>{table.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {table.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={j === 0 ? 'cell-strong' : ''}>{String(v)}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

function Message({ m, navigate }) {
  if (m.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-xl px-3.5 py-2 text-[13px]" style={{ background: 'var(--color-primary)', color: 'var(--color-on-primary)' }}>{m.content}</div>
      </div>
    );
  }
  const r = m.result || {};
  return (
    <div className="flex gap-2.5">
      <div className="w-7 h-7 rounded-lg shrink-0 flex items-center justify-center" style={{ background: 'var(--color-accent-soft)', color: 'var(--color-accent)' }}>
        <Icon name="sparkles" size={15} />
      </div>
      <div className="min-w-0 flex-1 max-w-[860px]">
        <div className="card px-4 py-3">
          {m.pending ? (
            <p className="text-xs text-fg-faint flex items-center gap-2"><span className="animate-pulse">●</span> Querying factory data…</p>
          ) : (
            <>
              <div className="text-[13px] text-fg leading-relaxed whitespace-pre-line">{m.content}</div>
              {r.charts?.map((c, i) => <AnswerChart key={i} chart={c} />)}
              {r.tables?.map((t, i) => <AnswerTable key={i} table={t} />)}
              {r.recommendations?.length > 0 && (
                <div className="tile tone-accent mt-2">
                  <p className="label-caps mb-1" style={{ color: 'var(--color-accent)' }}>Recommended actions</p>
                  <ul className="space-y-1">{r.recommendations.map((x) => <li key={x} className="text-xs text-fg">• {x}</li>)}</ul>
                </div>
              )}
              {r.suggestions?.length > 0 && <p className="text-[11px] text-fg-faint mt-2">Try: {r.suggestions.slice(0, 3).join(' · ')}</p>}
              <div className="flex items-center gap-2 flex-wrap mt-2.5">
                {r.links?.map((lk, i) => (
                  <Button key={i} size="sm" variant="soft" icon="external" onClick={() => navigate(linkTo(lk.page, lk.entity_type, lk.entity_id))}>
                    Open in {PAGE_LABEL[lk.page] || lk.page}
                  </Button>
                ))}
                {r.sources?.length > 0 && <span className="text-[10px] text-fg-faint">Data: {r.sources.map(titleCase).join(', ')}</span>}
                <Badge tone={r.mode === 'anthropic' ? 'accent' : 'neutral'} className="ml-auto">{r.mode === 'anthropic' ? 'Claude' : 'Local engine'}</Badge>
              </div>
              {r.note && <p className="text-[10.5px] text-warning mt-1.5">{r.note}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function CommandCenter() {
  const { filters, meta } = useApp();
  const navigate = useNavigate();
  const { data: config } = useApi('/api/ai/config', {}, { live: false });
  const [messages, setMessages] = useState(() => {
    try { return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]'); } catch (e) { return []; }
  });
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(messages.filter((m) => !m.pending).slice(-30))); } catch (e) { /* ignore */ }
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const ask = async (q) => {
    const text = (q ?? input).trim();
    if (!text || busy) return;
    setInput('');
    setBusy(true);
    const history = messages.filter((m) => !m.pending).slice(-6).map((m) => ({ role: m.role, content: m.content }));
    setMessages((ms) => [...ms, { role: 'user', content: text }, { role: 'assistant', pending: true }]);
    try {
      const res = await post('/api/ai/chat', { message: text, factory: filters.factory, history });
      setMessages((ms) => [...ms.slice(0, -1), { role: 'assistant', content: res.answer, result: res }]);
    } catch (e) {
      setMessages((ms) => [...ms.slice(0, -1), { role: 'assistant', content: `The request failed: ${e.message}`, result: {} }]);
    } finally {
      setBusy(false);
    }
  };

  const scope = filters.factory === 'ALL' ? 'All factories' : meta.factories.find((f) => f.code === filters.factory)?.name;
  const groups = [
    ['Procurement', [0, 1]], ['Production', [2, 3, 4]], ['Workforce', [5, 6]], ['Quality', [7, 8]],
    ['Warehouse', [9, 10]], ['Dispatch', [11, 12]],
  ];

  return (
    <div className="flex flex-col gap-4" style={{ minHeight: 'calc(100vh - var(--header-height) - 40px)' }}>
      <PageTitle title="AI Command Center" subtitle={`Ask about any stage — answers come from the same records as the dashboards · scope: ${scope}`}>
        <Badge tone={config?.provider === 'anthropic' ? 'accent' : 'neutral'} dot>
          {config?.provider === 'anthropic' ? `Claude (${config.model})` : 'Local engine (no external LLM)'}
        </Badge>
        {messages.length > 0 && <Button size="sm" variant="ghost" icon="refresh" onClick={() => setMessages([])}>New conversation</Button>}
      </PageTitle>
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-4 flex-1 min-h-0">
        <div className="xl:col-span-3 flex flex-col min-h-[520px] card overflow-hidden">
          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {messages.length === 0 && (
              <div className="py-6">
                <p className="text-sm font-semibold text-fg mb-1">What do you want to know about operations?</p>
                <p className="text-xs text-fg-faint mb-4">The assistant identifies the factory and period, retrieves records, calculates the answer, draws a chart and recommends actions. If the data isn't in the system it says so.</p>
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                  {config && groups.map(([g, idx]) => (
                    <div key={g}>
                      <p className="label-caps mb-1.5">{g}</p>
                      <div className="space-y-1.5">
                        {idx.map((i) => config.examples[i] && (
                          <button key={i} type="button" className="chip w-full text-left" style={{ padding: '7px 10px' }} onClick={() => ask(config.examples[i])}>
                            {config.examples[i]}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) => <Message key={i} m={m} navigate={navigate} />)}
            <div ref={endRef} />
          </div>
          <form className="border-t p-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); ask(); }}>
            <input className="input" placeholder="e.g. Why is production behind target in Kernersville?" value={input}
              onChange={(e) => setInput(e.target.value)} disabled={busy} aria-label="Ask the assistant" />
            <Button type="submit" icon="send" disabled={busy || !input.trim()}>Ask</Button>
          </form>
        </div>
        <div className="space-y-4">
          <Card title="How answers are produced">
            <ol className="text-xs text-fg-muted space-y-1.5 list-decimal list-inside">
              <li>Detect factory and period from the question (default: header selection).</li>
              <li>Call data tools that query the live operational database.</li>
              <li>Calculate and explain; attach charts, tables and record links.</li>
              <li>Recommend actions tied to the records found.</li>
            </ol>
            <p className="text-[11px] text-fg-faint mt-3">Provider is configurable (<span className="font-mono">LLM_PROVIDER</span>). Without an API key the local engine answers from the same tools.</p>
          </Card>
          <Card title="Data tools" subtitle={`${config?.tools?.length || 0} tools over one database`} padded={false}>
            <div className="max-h-[420px] overflow-y-auto divide-y divide-line">
              {config?.tools?.map((t) => (
                <div key={t.name} className="px-4 py-2">
                  <p className="text-[11.5px] font-semibold text-fg">{titleCase(t.name)}</p>
                  <p className="text-[10.5px] text-fg-faint leading-snug">{t.description}</p>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
