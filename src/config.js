/* ═══════════════════════════════════════════════════════════════════════
   APP CONFIG: name, logo, backend endpoints and header features.
   Pages and navigation are set in routes.js; colours in theme/theme.css.
   ═══════════════════════════════════════════════════════════════════════ */

export const APP = {
  name: 'Indocount',
  subtitle: 'Smart Factory Intelligence',
  logo: null,                        // e.g. '/logo.svg' in /public; null shows the initials mark
  logoInitials: 'IC',
  locale: 'en-US',

  features: {
    clock: true,
    notifications: true,
    insights: true,                  // page-level AI insights side panel
    themeToggle: true,
  },
};

// REST API base. Empty = same origin (CRA dev proxy or the nginx container).
export const API_BASE = process.env.REACT_APP_API_URL || '';

// WebSocket for live simulation updates.
export const WS_URL = process.env.REACT_APP_WS_URL || (() => {
  if (typeof window === 'undefined') return '';
  const { protocol, hostname, port, host } = window.location;
  const wsProto = protocol === 'https:' ? 'wss:' : 'ws:';
  if (port === '3000') return `${wsProto}//${hostname}:8000/ws`; // CRA dev server -> local backend
  return `${wsProto}//${host}/ws`;
})();
