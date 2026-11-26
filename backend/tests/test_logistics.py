"""Finished-goods flow: make-to-order pegging, picking ahead of the truck, readiness and dispatch counts."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.db import SessionLocal
from app.main import app
from app.services import inventory, readiness
from app.services.common import sim_now
from app.simulation import engine


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def db(client):
    s = SessionLocal()
    engine.advance_to(s, sim_now(s) + timedelta(hours=4), step_min=15)  # a few hours of live flow
    s.commit()
    yield s
    s.close()


def test_own_production_is_reserved_for_its_order(db):
    """Make-to-order pallets are reserved for their order as soon as they are stored."""
    for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.status == "Open")):
        if o.allocated_qty >= o.qty:
            continue
        own = inventory.own_po_ids(db, o.id)
        loose = db.scalar(select(m.Pallet).where(m.Pallet.po_id.in_(own or [0]), m.Pallet.status == "Stored",
                                                 m.Pallet.cartons > m.Pallet.allocated_cartons))
        assert loose is None, f"{o.number} is short but its own pallet {loose.code} is unreserved"


def test_no_order_takes_another_orders_production(db):
    pegged = {fid: inventory.pegged_po_ids(db, fid) for fid in db.scalars(select(m.Factory.id))}
    for a in db.scalars(select(m.Allocation).where(m.Allocation.status == "Reserved")):
        p = a.pallet
        if p.po_id in pegged[p.factory_id]:
            po = db.get(m.ProductionOrder, p.po_id)
            assert po.customer_order_id == a.order_id, f"{a.order_id} holds pallet {p.code} made for order {po.customer_order_id}"


def test_allocations_reconcile(db):
    for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.status == "Open")):
        alloc = sum(a.pillows for a in db.scalars(select(m.Allocation).where(m.Allocation.order_id == o.id)))
        assert alloc == o.allocated_qty and o.picked_qty <= o.allocated_qty <= o.qty
    for p in db.scalars(select(m.Pallet).where(m.Pallet.status == "Stored")):
        assert 0 <= p.allocated_cartons <= p.cartons


def test_finished_orders_close_their_last_pallet(db):
    """A completed production order never leaves a partial pallet open once nothing is left to pack."""
    for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.status == "Completed")):
        if po.pack_queue >= po.sku.pillows_per_carton:
            continue
        open_p = db.scalar(select(m.Pallet).where(m.Pallet.po_id == po.id, m.Pallet.status == "Open", m.Pallet.cartons > 0))
        if open_p is not None:
            # only acceptable inside the tick that completed it
            assert po.completed_at and po.completed_at >= sim_now(db) - timedelta(minutes=15), f"{po.number} left {open_p.code} open"


def test_readiness_window_adds_up(db):
    fids = list(db.scalars(select(m.Factory.id)))
    rc = readiness.readiness_counts(db, fids, sim_now(db))
    assert rc["horizon_h"] == engine.PICK_HORIZON_H
    assert rc["ready"] + rc["on_track"] + rc["at_risk"] == rc["scheduled"]
    assert rc["scheduled"] <= rc["due_48h"]


def test_risk_only_before_loading_and_eta_known_when_production_open(db):
    now = sim_now(db)
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)
    for o in orders:
        i = info[o.id]
        t = i["truck"]
        if i["risk"]["at_risk"]:
            assert t is not None and t.status in ("Scheduled", "Arrived"), f"{o.number} at risk with truck {t and t.status}"
            assert i["readiness"]["blocking"] is not None
        if i["readiness"]["blocking"] is None:
            assert i["ready_eta"] == now and not i["risk"]["at_risk"]
        if any(p.status != "Completed" and p.completed_qty < p.required_qty for p in i["pos"]):
            assert i["ready_eta"] is not None


def test_new_orders_are_in_the_plants_range_and_feasible(db):
    stocked = {(s.factory_id, s.material_id) for s in db.scalars(select(m.MaterialStock))}
    for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.order_date >= sim_now(db) - timedelta(days=1))):
        sku = o.sku
        ev = db.scalar(select(m.EventLog).where(m.EventLog.event_type == "order_received", m.EventLog.entity_id == o.id))
        if ev is None:
            continue  # seeded order
        assert (o.factory_id, sku.fill_material_id) in stocked and (o.factory_id, sku.shell_material_id) in stocked
        assert o.scheduled_dispatch > o.order_date + timedelta(hours=engine.SHIP_BUFFER_H)


def test_dispatch_summary_counts(client, h, db):
    s = client.get("/api/dispatch/summary", headers=h).json()
    assert s["ready"] + s["on_track"] + s["at_risk_window"] == s["due_ready_window"]
    assert s["trucks_departed_today"] + s["trucks_waiting"] <= s["trucks_today"]
    day0 = sim_now(db).replace(hour=0, minute=0, second=0, microsecond=0)
    cancelled = db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.scheduled_arrival >= day0, m.TruckAppointment.scheduled_arrival < day0 + timedelta(days=1),
        m.TruckAppointment.status == "Cancelled")).all()
    assert s["trucks_released_today"] == len(cancelled)


def test_detention_reasons_are_bucketed():
    assert readiness.reason_bucket("Released after 7 h wait (Picking Pending)") == "Picking Pending · truck released"
    assert readiness.reason_bucket("Released (Production Pending)") == "Production Pending · truck released"
    assert readiness.reason_bucket(None) == "Order not ready"
