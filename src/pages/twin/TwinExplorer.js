import React, { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { Badge, Icon, IconButton } from '../../components/ui';
import { ErrorBox, Loading } from '../../components/common';
import { useApi } from '../../hooks/useApi';
import { useApp } from '../../data/DataProvider';
import { dateTime } from '../../utils';
import UsNetworkMap from './UsNetworkMap';
import FacilityTwin from './FacilityTwin';
import './twin.css';

/**
 * Full-screen digital twin explorer.
 *   target: 'us' (network map) or a factory code (KNV, PHX, CMH)
 *   onNavigate(target) / onClose()
 */
export default function TwinExplorer({ target, onNavigate, onClose }) {
  const { sim, socket } = useApp();
  const isMap = !target || target === 'us';
  const { data: net, error, reload } = useApi('/api/twin/network', {}, { every: 2 });
  const plant = !isMap ? net?.factories.find((f) => f.code === target) : null;

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && !document.querySelector('.drawer') && onClose();
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev; };
  }, [onClose]);

  // Portalled to <body> so no transformed ancestor can offset the fixed overlay.
  return createPortal(
    <div className="twin-root" role="dialog" aria-modal="true" aria-label="Digital twin">
      <div className="twin-topbar">
        <div className="twin-brand">
          <span className="twin-brand-icon"><Icon name="globe" size={17} /></span>
          <div className="min-w-0">
            <p className="text-[13px] font-bold leading-tight">Digital Twin</p>
            <nav className="twin-crumbs" aria-label="Breadcrumb">
              <button type="button" className={isMap ? 'is-current' : ''} onClick={() => onNavigate('us')}>US network</button>
              {!isMap && <><Icon name="chevronRight" size={11} /><span className="is-current truncate">{plant ? `${plant.name}, ${plant.state}` : target}</span></>}
            </nav>
          </div>
        </div>
        {!isMap && net && (
          <div className="hidden md:flex items-center gap-1 ml-2">
            {net.factories.map((f) => (
              <button key={f.code} type="button" onClick={() => onNavigate(f.code)}
                className={`twin-tool ${f.code === target ? 'is-on' : ''}`} style={{ height: 28 }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: { Healthy: '#22c55e', Watch: '#f59e0b', Critical: '#ef4444' }[f.status] }} />
                {f.code}
              </button>
            ))}
          </div>
        )}
        <div className="flex-1" />
        <span className="twin-live hidden sm:inline-flex">
          <i style={socket === 'live' && sim?.running ? undefined : { background: '#f59e0b', animation: 'none' }} />
          {dateTime(sim?.sim_time)} · {sim?.running ? `live ${sim.speed}×` : 'paused'}
        </span>
        <Badge tone="warning" className="hidden md:inline-flex">SIMULATED · RFID</Badge>
        <IconButton icon="close" label="Close digital twin" onClick={onClose} />
      </div>
      {error && <div className="p-4"><ErrorBox error={error} onRetry={reload} /></div>}
      {isMap && !net && !error && <div className="p-8"><Loading rows={4} height={40} /></div>}
      {isMap && net && <UsNetworkMap data={net} onOpen={(code) => onNavigate(code)} />}
      {!isMap && <FacilityTwin key={target} code={target} />}
    </div>,
    document.body,
  );
}
