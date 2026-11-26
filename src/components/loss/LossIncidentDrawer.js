import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button, Drawer, Icon } from '../ui';
import { ErrorBox, KV, Loading } from '../common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { post } from '../../api/client';
import { dateTime, dayTime } from '../../utils';
import CctvViewer from '../cctv/CctvViewer';
import { OUTCOME_HELP, OUTCOME_TONE, SEVERITY_TONE, STAGE_LABEL, STATUS_TONE, qtyText, usd } from './lossShared';

function Section({ title, children, extra }) {
  return (
    <div className="mt-5">
      <div className="flex items-center justify-between gap-2 mb-2">
        <p className="label-caps">{title}</p>
        {extra}
      </div>
      {children}
    </div>
  );
}

/**
 * Loss-prevention case: what was detected, the evidence, the handling playbook and the outcome.
 * Anyone can read a case; working it needs the security.write permission.
 */
export default function LossIncidentDrawer({ id, onClose }) {
  const { can, act } = useApp();
  const navigate = useNavigate();
  const { data: c, loading, error, reload } = useApi(id ? `/api/loss/incidents/${id}` : null, {}, { every: 2 });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [outcome, setOutcome] = useState('');
  const [recovered, setRecovered] = useState('');
  const [cctv, setCctv] = useState(null);
  const allowed = can('security.write');
  useEffect(() => { setOutcome(''); setNote(''); setRecovered(''); }, [id]);

  const run = async (body, msg) => {
    setBusy(true);
    try {
      await act(() => post(`/api/loss/incidents/${id}/actions`, body), msg);
      setNote('');
      reload();
    } catch (e) { /* toast shown by act() */ } finally { setBusy(false); }
  };

  const active = c && c.status !== 'Resolved';
  const l = c?.links || {};
  const linkChips = c ? [
    l.location && { icon: 'box', text: `Location ${l.location.code}`, to: '/warehouse' },
    l.truck && { icon: 'truck', text: `${l.truck.ref} · dock ${l.truck.dock || '—'} · ${l.truck.status}`, to: '/dispatch' },
    l.shipment && { icon: 'truck', text: `Inbound ${l.shipment.ref} · ${l.shipment.status}`, to: '/procurement' },
    l.line && { icon: 'factory', text: l.line.name, to: `/production?focus=line:${l.line.id}` },
    l.employee && { icon: 'signal', text: `Badge ${l.employee.code} · ${l.employee.role} · shift ${l.employee.shift}`, to: `/production?focus=employee:${l.employee.id}` },
  ].filter(Boolean) : [];

  return (
    <>
      <Drawer eyebrow={c ? `Loss prevention · ${c.kind_label}` : 'Loss prevention'} title={c ? `${c.ref} · ${c.title}` : 'Loading…'} width={720} onClose={onClose}
        headerExtra={c && (
          <>
            <Badge tone={SEVERITY_TONE[c.severity] || 'neutral'}>{c.severity.toUpperCase()}</Badge>
            <Badge tone={c.outcome ? OUTCOME_TONE[c.outcome] : STATUS_TONE[c.status]} dot>{(c.outcome || c.status).toUpperCase()}</Badge>
          </>
        )}
        footer={c && (
          <span className="text-[10.5px] text-fg-faint mr-auto">
            CCTV vision only raises access checks and retains footage; it never identifies or accuses a person in the video.
          </span>
        )}>
        <ErrorBox error={error} onRetry={reload} />
        {loading && !c && <Loading rows={8} />}
        {c && (
          <>
            <p className="text-[12.5px] text-fg leading-relaxed">{c.summary}</p>
            <div className="mt-3">
              <KV cols={4} items={[
                ['Plant', c.factory_name || c.factory], ['Where', c.zone], ['Detected', dateTime(c.detected_at)],
                ['Detected by', c.channel], ['Material', c.item || '—'], ['Quantity', qtyText(c)],
                ['Value (std cost)', usd(c.value_usd)], ['Owner', c.assigned_to || '—'],
                ['Stage', STAGE_LABEL[c.stage] || c.stage],
                c.recovered_qty ? ['Recovered', `${Math.round(c.recovered_qty).toLocaleString('en-US')} ${c.unit || ''}`] : null,
                c.resolved_at ? ['Closed', dateTime(c.resolved_at)] : null,
              ]} />
            </div>

            {(linkChips.length > 0 || c.camera) && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {linkChips.map((k) => (
                  <button key={k.text} type="button" className="badge tone-neutral hover:opacity-80" onClick={() => navigate(k.to)}>
                    <Icon name={k.icon} size={11} />{k.text}
                  </button>
                ))}
                {c.camera && c.feed_id && (
                  <button type="button" className="badge tone-info hover:opacity-80" onClick={() => setCctv({ feed: c.feed_id, t: c.feed_t })}>
                    <Icon name="camera" size={11} />{c.feed_t != null ? `Play CCTV event · ${c.camera} @ ${c.feed_t.toFixed(1)} s` : `Open camera ${c.camera} (footage retained)`}
                  </button>
                )}
                {c.camera && !c.feed_id && <span className="badge tone-neutral"><Icon name="camera" size={11} />{c.camera} · footage retained</span>}
              </div>
            )}

            <Section title="Evidence">
              <ul className="space-y-1.5">
                {(c.evidence || []).map((e, i) => (
                  <li key={i} className="tile flex gap-2.5 items-start py-2">
                    <span className="badge tone-neutral shrink-0">{e.source}</span>
                    <span className="text-[11.5px] text-fg flex-1 leading-snug">{e.text}</span>
                    <span className="text-[10.5px] text-fg-faint tabular-nums shrink-0">{dayTime(e.ts)}</span>
                  </li>
                ))}
              </ul>
            </Section>

            <Section title={`Handling playbook · ${c.progress[0]}/${c.progress[1]} checks done`}
              extra={active && c.next_step && <span className="text-[10.5px] font-semibold text-warning">Next: {c.next_step.text}</span>}>
              <ol className="space-y-1.5">
                {c.playbook.map((s, i) => (
                  <li key={s.key} className="flex items-center gap-2.5 text-[12px]">
                    <span className={`w-5 h-5 rounded-full grid place-items-center text-[10px] font-bold shrink-0 ${s.done ? 'bg-success text-white' : 'border border-line text-fg-faint'}`}>
                      {s.done ? <Icon name="check" size={11} /> : i + 1}
                    </span>
                    <span className={`flex-1 ${s.done ? 'text-fg-muted' : 'text-fg'}`}>{s.text}</span>
                    {active && !s.done && allowed && (
                      <Button size="sm" variant="secondary" disabled={busy}
                        onClick={() => run({ action: 'step', step: s.key, note: note.trim() || undefined }, `${c.ref}: “${s.text}” recorded`)}>
                        Mark done
                      </Button>
                    )}
                  </li>
                ))}
              </ol>
            </Section>

            {allowed && (
              <Section title={active ? 'Work the case' : 'Case closed'}>
                {active ? (
                  <div className="space-y-2.5">
                    <textarea className="input w-full text-[12px]" rows={2} value={note} onChange={(e) => setNote(e.target.value)}
                      placeholder="Finding or note (used with the next action)…" />
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="secondary" icon="user" disabled={busy} onClick={() => run({ action: 'assign', note: note.trim() || undefined }, `${c.ref} assigned to you`)}>Assign to me</Button>
                      <Button size="sm" variant="secondary" icon="edit" disabled={busy || !note.trim()} onClick={() => run({ action: 'note', note }, 'Note added')}>Add note</Button>
                    </div>
                    <div className="tile space-y-2">
                      <p className="text-[11.5px] font-semibold text-fg">Close with an outcome</p>
                      <div className="flex flex-wrap gap-1.5">
                        {c.outcomes.map((o) => (
                          <button key={o} type="button" onClick={() => setOutcome(o)}
                            className={`badge ${outcome === o ? `tone-${OUTCOME_TONE[o]}` : 'tone-neutral'} ${outcome === o ? 'ring-1 ring-current' : ''}`}>{o}</button>
                        ))}
                      </div>
                      {outcome && <p className="text-[11px] text-fg-muted">{OUTCOME_HELP[outcome]}</p>}
                      {outcome === 'Recovered' && c.qty > 0 && (
                        <label className="flex items-center gap-2 text-[11.5px] text-fg-muted">
                          Recovered quantity
                          <input type="number" min={0} max={c.qty} className="input w-28" value={recovered} placeholder={String(c.qty)}
                            onChange={(e) => setRecovered(e.target.value)} />
                          <span>{c.unit}</span>
                        </label>
                      )}
                      {outcome === 'Confirmed loss' && !note.trim() && <p className="text-[11px] text-danger">Add a note: what was found and who was informed.</p>}
                      <Button size="sm" icon="check" disabled={busy || !outcome || (outcome === 'Confirmed loss' && !note.trim())}
                        onClick={() => run({ action: 'resolve', outcome, note: note.trim() || undefined,
                          recovered_qty: outcome === 'Recovered' && recovered !== '' ? Number(recovered) : undefined }, `${c.ref} closed — ${outcome}`)}>
                        Close case
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-3 flex-wrap">
                    <p className="text-[12px] text-fg flex-1">{c.note}</p>
                    <Button size="sm" variant="secondary" icon="refresh" disabled={busy} onClick={() => run({ action: 'reopen', note: note.trim() || 'Reopened for review.' }, `${c.ref} reopened`)}>Reopen</Button>
                  </div>
                )}
              </Section>
            )}
            {!allowed && active && <p className="text-[11px] text-fg-faint mt-4">Working a case needs the security role (Plant Manager or Administrator).</p>}

            <Section title="Case timeline">
              <ol className="relative border-l border-line ml-2 space-y-2.5">
                {[...(c.timeline || [])].reverse().map((t, i) => (
                  <li key={i} className="ml-3.5">
                    <span className="absolute -left-[5px] mt-1 w-2.5 h-2.5 rounded-full" style={{ background: t.step === 'resolve' ? `var(--color-${OUTCOME_TONE[t.outcome] || 'success'})` : t.user ? 'var(--color-primary)' : 'var(--color-text-faint)' }} />
                    <p className="text-[11.5px] text-fg leading-snug">{t.text}</p>
                    <p className="text-[10.5px] text-fg-faint">{dayTime(t.ts)} · {t.by}</p>
                  </li>
                ))}
              </ol>
            </Section>
          </>
        )}
      </Drawer>
      {cctv && <CctvViewer feedId={cctv.feed} startAt={cctv.t} onClose={() => setCctv(null)} />}
    </>
  );
}
