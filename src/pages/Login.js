import React, { useEffect, useState } from 'react';
import { Badge, Button, Icon } from '../components/ui';
import { BrandMark } from '../components/layout/Sidebar';
import { useApp } from '../data/DataProvider';
import { api } from '../api/client';
import { APP } from '../config';

const ROLE_HINT = {
  admin: 'Everything, including simulation controls and scenarios',
  executive: 'Read-only management view and AI assistant',
  plant_manager: 'Production, optimization, quality, warehouse, dispatch',
  maintenance_manager: 'Machine health and maintenance work orders',
  quality_supervisor: 'Carton validation and supervisor overrides',
  warehouse_operator: 'Receiving, put-away, allocation and picking',
  procurement: 'Purchase orders and goods receipts',
  logistics: 'Truck appointments, loading and dispatch',
};

export default function Login() {
  const { login } = useApp();
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ username: '', password: '' });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [backend, setBackend] = useState('checking');

  useEffect(() => {
    api('/api/auth/demo-users').then((u) => { setUsers(u); setBackend('ok'); }).catch(() => setBackend('down'));
  }, []);

  const submit = async (username, password) => {
    setBusy(true);
    setError(null);
    try { await login(username, password); } catch (e) { setError(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="min-h-full overflow-y-auto flex items-center justify-center p-6" style={{ background: 'var(--color-bg)' }}>
      <div className="w-full max-w-4xl grid md:grid-cols-5 gap-5">
        <div className="md:col-span-2 card p-6 flex flex-col">
          <div className="flex items-center gap-3 mb-6">
            <BrandMark />
            <div>
              <p className="text-base font-bold text-fg leading-tight">{APP.name}</p>
              <p className="text-xs text-fg-faint">{APP.subtitle}</p>
            </div>
          </div>
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); submit(form.username, form.password); }}>
            <label className="block">
              <span className="label-caps field-label">Username</span>
              <input className="input" autoComplete="username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            </label>
            <label className="block">
              <span className="label-caps field-label">Password</span>
              <input className="input" type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </label>
            {error && <p className="tile tone-danger text-xs">{error}</p>}
            <Button type="submit" block disabled={busy || !form.username}>{busy ? 'Signing in…' : 'Sign in'}</Button>
          </form>
          <div className="mt-auto pt-6 text-[11px] text-fg-faint space-y-1">
            <p>US operations: Kernersville NC · Phoenix AZ · Columbus OH</p>
            <p className="flex items-center gap-1.5"><Badge tone="warning">SIMULATED</Badge> All data in this demo is simulated.</p>
            {backend === 'down' && <p className="text-danger">Backend not reachable. Start the API on port 8000.</p>}
          </div>
        </div>
        <div className="md:col-span-3 card p-5">
          <p className="text-sm font-bold text-fg">Demo accounts</p>
          <p className="text-xs text-fg-faint mb-3">Role-based access: each role sees everything but can act only within its responsibilities.</p>
          <div className="grid sm:grid-cols-2 gap-2">
            {users.map((u) => (
              <button key={u.username} type="button" disabled={busy} onClick={() => submit(u.username, u.password)}
                className="tile text-left hover:border-primary transition-colors">
                <div className="flex items-center gap-2">
                  <Icon name="user" size={14} className="text-primary" />
                  <span className="text-xs font-bold text-fg">{u.role_label}</span>
                </div>
                <p className="text-[11px] text-fg-muted mt-1">{u.name} · <span className="font-mono">{u.username}</span></p>
                <p className="text-[10.5px] text-fg-faint mt-0.5 leading-snug">{ROLE_HINT[u.role]}</p>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
