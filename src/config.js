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

// REST API base, from REACT_APP_API_URL (.env.production / .env.development). Empty = same origin.
export const API_BASE = process.env.REACT_APP_API_URL || '';

// WebSocket for live plant updates, from REACT_APP_WS_URL. Empty = /ws on the page's own host.
export const WS_URL = process.env.REACT_APP_WS_URL || (() => {
  if (typeof window === 'undefined') return '';
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss:' : 'ws:'}//${host}/ws`;
})();
