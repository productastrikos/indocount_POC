import { API_BASE } from '../config';

const TOKEN_KEY = 'ic_token';
let token = null;
try { token = localStorage.getItem(TOKEN_KEY); } catch (e) { /* storage unavailable */ }
let onUnauthorized = null;

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export function setToken(t) {
  token = t;
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* storage unavailable */ }
}

export const getToken = () => token;
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

export function buildUrl(path, params) {
  const url = new URL(API_BASE + path, window.location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  });
  return url.toString();
}

/** fetch wrapper: JSON in/out, bearer token, readable errors. */
export async function api(path, { method = 'GET', params, body } = {}) {
  const res = await fetch(buildUrl(path, params), {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== '/api/auth/login') {
    onUnauthorized?.();
    throw new ApiError('Your session has expired. Please sign in again.', 401);
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join('; ')
      : res.statusText;
    throw new ApiError(msg || 'Request failed', res.status);
  }
  return data;
}

export const post = (path, body) => api(path, { method: 'POST', body: body || {} });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
