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

    @staticmethod
    def _follow_wall_clock(db, st, engine, t0, budget_s: float = 1.5) -> bool:
        """Advance the plant clock to the wall clock (minute resolution), catching up in 5-minute steps.
        Overnight (23:00-06:00) the lines are closed: the clock waits instead of jumping to the next morning."""
        from datetime import datetime as _dt, timedelta as _td
        from .services import calendar as cal
        target = _dt.now().replace(second=0, microsecond=0)
        moved = False
        while st.sim_time < target and time.perf_counter() - t0 < budget_s:
            a = st.sim_time
            day_start = _dt.combine(a.date(), cal.DAY_START)
            day_end = _dt.combine(a.date(), cal.DAY_END)
            if a >= day_end and target < day_start + _td(days=1):
                break  # closed for the night
            if a < day_start and target < day_start:
                break
            minutes = 5.0 if a >= day_end or a < day_start else min(5.0, (min(target, day_end) - a).total_seconds() / 60)
            engine.step(db, max(minutes, 1.0) if minutes > 0 else 1.0)
            moved = True
        return moved

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
                        if get_settings().sim_realtime:
                            if not self._follow_wall_clock(db, st, engine, t0):
                                continue
                        else:
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
