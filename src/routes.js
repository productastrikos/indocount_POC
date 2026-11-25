/* ═══════════════════════════════════════════════════════════════════════
   ROUTES: the seven lifecycle pages shown in the sidebar.
   icon      a name from src/components/ui/Icon.js
   stage     the backend stage key used for alerts and insights
   filters   which global filters are relevant on the page
   ═══════════════════════════════════════════════════════════════════════ */
import Overview from './pages/Overview';
import Procurement from './pages/Procurement';
import Production from './pages/Production';
import Quality from './pages/Quality';
import Warehouse from './pages/Warehouse';
import Dispatch from './pages/Dispatch';
import CommandCenter from './pages/CommandCenter';

export const NAV_SECTIONS = [
  {
    label: 'Manufacturing lifecycle',
    items: [
      { path: '/', label: 'Executive Overview', short: 'Overview', icon: 'home', stage: 'overview', element: <Overview />, filters: ['date', 'shift', 'customer'] },
      { path: '/procurement', label: 'Procurement & Inbound', short: 'Procurement', icon: 'truck', stage: 'procurement', element: <Procurement />, filters: [] },
      { path: '/production', label: 'Production & Manufacturing', short: 'Production', icon: 'settings', stage: 'production', element: <Production />, filters: ['date', 'shift'] },
      { path: '/quality', label: 'Quality & Packaging', short: 'Quality', icon: 'shield', stage: 'quality', element: <Quality />, filters: ['date'] },
      { path: '/warehouse', label: 'Warehouse & Inventory', short: 'Warehouse', icon: 'box', stage: 'warehouse', element: <Warehouse />, filters: [] },
      { path: '/dispatch', label: 'Dispatch & Delivery', short: 'Dispatch', icon: 'route', stage: 'dispatch', element: <Dispatch />, filters: ['customer'] },
    ],
  },
  {
    label: 'Intelligence',
    items: [
      { path: '/ai', label: 'AI Command Center', short: 'AI', icon: 'sparkles', stage: 'ai', element: <CommandCenter />, filters: [] },
    ],
  },
];

export const ROUTES = NAV_SECTIONS.flatMap((s) => s.items);
