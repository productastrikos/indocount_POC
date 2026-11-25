import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from '../api/client';
import { useApp } from '../data/DataProvider';

/**
 * useApi(path, params, { live, every })
 *   Fetches `path` with query `params`. Re-fetches when params change and,
 *   when `live`, silently on every `every`-th simulation tick / user action.
 *   path = null skips the request.
 */
export function useApi(path, params = {}, { live = true, every = 1 } = {}) {
  const { version } = useApp();
  const key = path ? JSON.stringify([path, params]) : null;
  const [state, setState] = useState({ data: null, loading: !!path, error: null });
  const inflight = useRef(false);
  const lastVersion = useRef(version);

  const load = useCallback(async (silent = false) => {
    if (!key) return;
    const [p, prm] = JSON.parse(key);
    if (inflight.current && silent) return;
    inflight.current = true;
    if (!silent) setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await api(p, { params: prm });
      setState({ data, loading: false, error: null });
    } catch (e) {
      setState((s) => ({ data: silent ? s.data : null, loading: false, error: e.message }));
    } finally {
      inflight.current = false;
    }
  }, [key]);

  useEffect(() => { load(false); }, [load]);

  useEffect(() => {
    if (!live || !key) return;
    if (version - lastVersion.current >= every) {
      lastVersion.current = version;
      load(true);
    }
  }, [version, live, every, key, load]);

  return { ...state, reload: () => load(true) };
}

/** Reads ?focus=type:id from the URL (set when an alert or link is clicked). */
export function useFocus() {
  const { search } = useLocation();
  return useMemo(() => {
    const f = new URLSearchParams(search).get('focus');
    if (!f) return null;
    const [type, id] = f.split(':');
    return { type, id: id && !Number.isNaN(Number(id)) ? Number(id) : id };
  }, [search]);
}
