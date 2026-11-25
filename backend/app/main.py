"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, select

from . import models as m
from .config import get_settings
from .db import WRITE_LOCK, SessionLocal, engine
from .realtime import hub, runner
from .routers import ai, cctv, core, dispatch, overview, predictive, procurement, production, quality, twin, warehouse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("indocount")
BASE = Path(__file__).resolve().parent.parent


def migrate() -> None:
    """Apply Alembic migrations (creates the schema on first run)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")


def ensure_seeded() -> None:
    with WRITE_LOCK:
        db = SessionLocal()
        try:
            if not db.scalar(select(m.SimState)):
                from .seed import seed
                log.info("Seeding demo data (runs the simulated morning shift)…")
                seed(db)
                log.info("Seed complete")
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()
    ensure_seeded()
    hub.loop = asyncio.get_running_loop()
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="Indocount Smart Factory Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-CV-Provider", "X-Detected-Count"])

for r in (core, overview, procurement, production, quality, warehouse, dispatch, ai, predictive, twin, cctv):
    app.include_router(r.router)


@app.get("/api/health")
def health():
    return {"ok": True, "tables": len(inspect(engine).get_table_names())}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive pings from the client
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception:
        hub.disconnect(websocket)


# Optional single-port mode: serve the production frontend build (npm run build) if present.
FRONTEND = Path(os.environ["FRONTEND_DIR"]).resolve() if os.environ.get("FRONTEND_DIR") else BASE.parent / "build"
if FRONTEND.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(404, "Not found")
        file = FRONTEND / path
        if path and file.is_file() and FRONTEND in file.resolve().parents and file.name != "index.html":
            # analytics files change when a feed is re-analysed; hashed bundles and media may cache
            return FileResponse(file, headers={"Cache-Control": "no-cache"} if file.suffix == ".json" else None)
        # the app shell must always be revalidated so a redeploy shows up on the next load
        return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-cache"})
