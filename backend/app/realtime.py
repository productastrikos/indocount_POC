"""WebSocket broadcasting and the background simulation loop."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from fastapi import WebSocket

from .config import get_settings
from .db import WRITE_LOCK, SessionLocal
from .services.common import iso, state

log = logging.getLogger("indocount.realtime")


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, msg: dict):
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def publish(self, msg: dict):
        """Thread-safe publish from sync code (routers run in a threadpool)."""
        if self.loop and self.clients:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.loop)


hub = Hub()


class SimRunner:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error: str | None = None
        self.last_tick_ms: float = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sim-runner", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        from .simulation import engine
        interval = get_settings().sim_tick_seconds
        while not self._stop.wait(interval):
            try:
                with WRITE_LOCK:
                    db = SessionLocal()
                    try:
                        st = state(db)
                        if not st or not st.running:
                            continue
                        t0 = time.perf_counter()
                        engine.step(db, st.speed or 1)
                        db.commit()
                        self.last_tick_ms = round((time.perf_counter() - t0) * 1000)
                        msg = {"type": "tick", "sim_time": iso(st.sim_time), "tick": st.tick, "speed": st.speed,
                               "running": st.running, "tick_ms": self.last_tick_ms}
                    except Exception as exc:  # keep the loop alive
                        db.rollback()
                        self.last_error = repr(exc)
                        log.exception("simulation tick failed")
                        continue
                    finally:
                        db.close()
                hub.publish(msg)
            except Exception:
                log.exception("runner error")


runner = SimRunner()


def notify_change(topic: str, detail: str | None = None):
    hub.publish({"type": "change", "topic": topic, "detail": detail})
