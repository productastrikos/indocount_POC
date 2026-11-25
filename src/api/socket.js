import { WS_URL } from '../config';

/* Live updates from the simulation engine. Reconnects automatically.
   Messages: {type: 'tick', sim_time, tick, speed, running}
             {type: 'change', topic, detail} after any user action */
export function connectSocket(onMessage, onStatus) {
  let ws = null;
  let closed = false;
  let retry = 1000;
  let ping = null;

  const open = () => {
    if (closed || !WS_URL) return;
    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      setTimeout(open, retry);
      return;
    }
    ws.onopen = () => {
      retry = 1000;
      onStatus?.('live');
      ping = setInterval(() => { try { ws.send('ping'); } catch (e) { /* ignore */ } }, 25000);
    };
    ws.onmessage = (ev) => {
      try { onMessage(JSON.parse(ev.data)); } catch (e) { /* ignore malformed */ }
    };
    ws.onclose = () => {
      clearInterval(ping);
      onStatus?.('offline');
      if (!closed) {
        setTimeout(open, retry);
        retry = Math.min(retry * 2, 15000);
      }
    };
    ws.onerror = () => ws.close();
  };

  open();
  return () => {
    closed = true;
    clearInterval(ping);
    ws?.close();
  };
}
