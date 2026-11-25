import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { api, getToken, post, setToken, setUnauthorizedHandler } from '../api/client';
import { connectSocket } from '../api/socket';

/* App-wide state: signed-in user, master data, global filters (factory,
   date, shift, customer), the simulated clock and open alerts. Every page
   reads the same backend; `version` increments on each simulation tick or
   user action so pages refresh their data. */

const AppContext = createContext(null);
const FILTER_KEY = 'ic_filters';

function loadFilters() {
  try {
    const f = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
    return { factory: f.factory || 'ALL', shift: f.shift || '', customer: f.customer || '', date: '' };
  } catch (e) {
    return { factory: 'ALL', shift: '', customer: '', date: '' };
  }
}

export function DataProvider({ children }) {
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [meta, setMeta] = useState(null);
  const [filters, setFilters] = useState(loadFilters);
  const [sim, setSim] = useState(null);
  const [version, setVersion] = useState(0);
  const [alerts, setAlerts] = useState([]);
  const [socket, setSocket] = useState('connecting');
  const [toasts, setToasts] = useState([]);
  const lastAlertFetch = useRef(0);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    setMeta(null);
  }, []);

  useEffect(() => { setUnauthorizedHandler(logout); }, [logout]);

  // Restore session
  useEffect(() => {
    if (!getToken()) { setAuthChecked(true); return; }
    api('/api/auth/me').then(setUser).catch(() => setToken(null)).finally(() => setAuthChecked(true));
  }, []);

  const login = useCallback(async (username, password) => {
    const res = await post('/api/auth/login', { username, password });
    setToken(res.token);
    setUser(res.user);
    return res.user;
  }, []);

  const loadMeta = useCallback(() => api('/api/meta').then((m) => { setMeta(m); setSim(m.sim); }), []);
  useEffect(() => { if (user) loadMeta().catch(() => {}); }, [user, loadMeta]);

  const fetchAlerts = useCallback(() => {
    lastAlertFetch.current = Date.now();
    return api('/api/alerts', { params: { factory: filters.factory } }).then(setAlerts).catch(() => {});
  }, [filters.factory]);
  useEffect(() => { if (user) fetchAlerts(); }, [user, fetchAlerts]);

  // Live updates
  useEffect(() => {
    if (!user) return undefined;
    return connectSocket((msg) => {
      if (msg.type === 'tick') {
        setSim((s) => ({ ...(s || {}), sim_time: msg.sim_time, tick: msg.tick, speed: msg.speed, running: msg.running, tick_ms: msg.tick_ms }));
        setVersion((v) => v + 1);
      } else if (msg.type === 'change') {
        setVersion((v) => v + 1);
        if (msg.topic === 'reset' || msg.topic === 'sim' || msg.topic === 'scenario') {
          api('/api/sim/state').then(setSim).catch(() => {});
        }
      }
    }, setSocket);
  }, [user]);

  // Alerts refresh at most every 6 seconds
  useEffect(() => {
    if (user && Date.now() - lastAlertFetch.current > 6000) fetchAlerts();
  }, [version, user, fetchAlerts]);

  const setFilter = useCallback((key, value) => {
    setFilters((f) => {
      const next = { ...f, [key]: value };
      try { localStorage.setItem(FILTER_KEY, JSON.stringify({ factory: next.factory, shift: next.shift, customer: next.customer })); } catch (e) { /* ignore */ }
      return next;
    });
  }, []);

  const bump = useCallback(() => setVersion((v) => v + 1), []);

  const toast = useCallback((message, tone = 'success') => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, message, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === 'danger' ? 7000 : 4500);
  }, []);

  const can = useCallback((perm) => {
    const p = user?.permissions || [];
    return p.includes('*') || p.includes(perm);
  }, [user]);

  // Run a mutation, show the result, refresh live data.
  const act = useCallback(async (fn, successMsg) => {
    try {
      const res = await fn();
      if (successMsg) toast(typeof successMsg === 'function' ? successMsg(res) : successMsg);
      setVersion((v) => v + 1);
      fetchAlerts();
      return res;
    } catch (e) {
      toast(e.message || 'Action failed', 'danger');
      throw e;
    }
  }, [toast, fetchAlerts]);

  const factoryById = useMemo(() => Object.fromEntries((meta?.factories || []).map((f) => [f.id, f])), [meta]);
  const selectedFactory = useMemo(() => (meta?.factories || []).find((f) => f.code === filters.factory) || null, [meta, filters.factory]);

  const value = useMemo(() => ({
    user, authChecked, login, logout, meta, loadMeta, filters, setFilter, sim, setSim, version, bump,
    alerts, fetchAlerts, socket, toasts, toast, can, act, factoryById, selectedFactory,
  }), [user, authChecked, login, logout, meta, loadMeta, filters, setFilter, sim, version, bump, alerts, fetchAlerts,
    socket, toasts, toast, can, act, factoryById, selectedFactory]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export const useApp = () => useContext(AppContext);
export const useData = useApp;
