"""Document numbering: the live simulation must never stall on a duplicate number."""
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.db import SessionLocal
from app.main import app
from app.services.common import next_number, state
from app.simulation import engine


@pytest.fixture(scope="module", autouse=True)
def seeded_app():
    with TestClient(app) as c:  # app startup migrates and seeds the database
        yield c


def test_new_orders_never_reuse_a_seeded_number():
    """Regression: once the order book reached 495 rows, the provisional number
    ORD-{max id + 1} was ORD-496, a seeded order, so every new order raised an
    IntegrityError and the simulation stalled for good."""
    db = SessionLocal()
    rng = engine.RNG.getstate()
    try:
        first = db.scalar(select(m.CustomerOrder).order_by(m.CustomerOrder.id))
        assert first.number == "ORD-496"
        cols = {c: getattr(first, c) for c in ("customer_id", "factory_id", "sku_id", "qty", "order_date",
                                               "ship_window_start", "ship_window_end", "scheduled_dispatch", "priority")}
        top = db.scalar(select(func.max(m.CustomerOrder.id)))
        db.add_all(m.CustomerOrder(number=f"T-{i}", status="Delivered", **cols) for i in range(top + 1, 496))
        db.flush()

        # the shared helper skips numbers that are already taken
        n = next_number(db, m.CustomerOrder, "ORD-", 0)
        assert db.scalar(select(m.CustomerOrder.id).where(m.CustomerOrder.number == n)) is None

        # and the engine books a new order without a collision
        day = state(db).sim_time.date()
        for k in range(10):
            a = datetime.combine(day + timedelta(days=k), time(6))
            ctx = engine.TickCtx(db, a, a + timedelta(hours=8))
            if ctx.total_pm > 0:
                break
        f = max(ctx.factories, key=lambda x: x.daily_target or 0)
        before = db.scalar(select(func.count(m.CustomerOrder.id)))
        engine.maybe_new_order(ctx, f)
        db.flush()
        assert db.scalar(select(func.count(m.CustomerOrder.id))) == before + 1
        new = db.scalar(select(m.CustomerOrder).order_by(m.CustomerOrder.id.desc()))
        assert new.number == f"ORD-{500 + new.id}"
    finally:
        db.rollback()
        db.close()
        engine.RNG.setstate(rng)
