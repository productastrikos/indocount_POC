"""Supply-side numbers add up: procurement tiles equal their drill-down lists, the raw-material position
balances, allocation cover counts each carton once, pillows are conserved through packing and rework,
and the S!AP / AI windows match the pages they summarise.

Read-only checks go through the API on the shared test database. Anything that writes (receipts,
purchase orders, simulation steps) runs on a private copy of the database with the simulator's random
streams restored afterwards, so later test modules see exactly the state they would have seen."""
import contextlib
import copy
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.ai import tools
from app.db import engine as app_engine
from app.main import app
from app.routers import procurement as rproc
from app.services import insights, inventory, procurement as proc, quality as qsvc
from app.services.common import sim_now


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def get(client, h, url, **params):
    r = client.get(url, headers=h, params=params)
    assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:300]}"
    return r.json()


def _sim_streams():
    from app.services import loss
    from app.simulation import engine, rfid
    rngs = (engine.RNG, engine.RNG_WIP, rfid.RNG, loss.RNG)
    dicts = (engine.PICK_BUDGET, engine.PUTAWAY_BUDGET, engine.PACK_BUDGET, rfid.AWAY_MIN)
    return rngs, [r.getstate() for r in rngs], dicts, [copy.deepcopy(d) for d in dicts]


@contextlib.contextmanager
def scratch_db(client):
    """Session on a private copy of the test database (simulator RNG / budgets restored on exit)."""
    if app_engine.url.get_backend_name() != "sqlite":
        pytest.skip("scratch copies need the SQLite test database")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "scratch.db")
    src, dst = sqlite3.connect(app_engine.url.database), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    eng = create_engine(f"sqlite:///{path}")
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    rngs, states, dicts, saved = _sim_streams()
    try:
        yield db
    finally:
        db.close()
        eng.dispose()
        for r, s in zip(rngs, states):
            r.setstate(s)
        for d, s in zip(dicts, saved):
            d.clear()
            d.update(s)


# ─── procurement ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("fac", ["ALL", "KNV", "PHX", "CMH"])
def test_procurement_tiles_equal_their_lists(client, h, fac):
    s = get(client, h, "/api/procurement/summary", factory=fac)
    open_pos = get(client, h, "/api/procurement/purchase-orders", factory=fac, view="open")
    assert s["open_pos"] == len(open_pos)
    assert all(p["status"] in proc.OPEN_PO for p in open_pos)
    assert s["open_po_value"] == pytest.approx(sum(p["pending_qty"] * (p["unit_price"] or 0) for p in open_pos), abs=1)
    for tile, view in (("expected_inbound", "expected"), ("received_today", "received_today"),
                       ("pending_receipts", "pending"), ("delayed", "delayed")):
        rows = get(client, h, "/api/procurement/shipments", factory=fac, view=view)
        assert s[tile] == len(rows), f"{tile}: tile {s[tile]} vs list {len(rows)}"
    now = datetime.fromisoformat(get(client, h, "/api/meta")["sim"]["sim_time"])
    for r in get(client, h, "/api/procurement/shipments", factory=fac, view="delayed"):
        assert r["delayed"], "a shipment in the delayed list is flagged delayed (same tolerance)"
    for r in get(client, h, "/api/procurement/shipments", factory=fac, view="expected"):
        assert datetime.fromisoformat(r["expected_arrival"]) <= now + proc.EXPECTED_WINDOW
    mats = get(client, h, "/api/procurement/materials", factory=fac)
    assert s["shortages"] == sum(1 for r in mats if r["status"] in ("Critical", "Low"))
    assert s["critical"] == sum(1 for r in mats if r["status"] == "Critical")


def test_short_receipt_closes_po_and_counts_once(client):
    with scratch_db(client) as db:
        now = sim_now(db)
        open_ship = select(m.InboundShipment).where(m.InboundShipment.status.in_(("Scheduled", "In Transit")))
        ship = next(s for s in db.scalars(open_ship)
                    if db.scalar(select(func.count()).select_from(m.InboundShipment).where(
                        m.InboundShipment.po_id == s.po_id, m.InboundShipment.status.in_(proc.OPEN_SHIP))) == 1)
        fac = db.get(m.Factory, ship.factory_id).code

        def stale():  # received short earlier, nothing left in the pipeline, still counted as open
            return [p.number for p in db.scalars(select(m.PurchaseOrder).where(
                m.PurchaseOrder.factory_id == ship.factory_id, m.PurchaseOrder.status.in_(proc.OPEN_PO),
                m.PurchaseOrder.received_qty > 0))
                if not db.scalar(select(m.InboundShipment.id).where(m.InboundShipment.po_id == p.id,
                                                                    m.InboundShipment.status.in_(proc.OPEN_SHIP)).limit(1))]
        legacy = len(stale())
        before = rproc.summary(fac, db=db, user=None)
        proc.mark_arrived(db, ship, "R1", now, "test")
        proc.confirm_receipt(db, ship, ship.qty - 200, 0, now, "test")
        db.flush()
        po = db.get(m.PurchaseOrder, ship.po_id)
        assert po.status == proc.CLOSED_SHORT, "short with nothing left in the pipeline -> closed short, not open forever"
        after = rproc.summary(fac, db=db, user=None)
        assert after["received_today"] == before["received_today"] + 1
        assert after["received_today"] == len(rproc.shipments(fac, view="received_today", db=db, user=None))
        # this PO plus any older ones received short with no shipment left are settled by the same rule
        assert not stale()
        assert after["open_pos"] == before["open_pos"] - 1 - legacy
        assert after["open_pos"] == len(rproc.list_pos(fac, view="open", db=db, user=None))


# ─── raw materials ────────────────────────────────────────────────────────
def _expected_status(r):
    if r["status"] in ("Untracked",):
        return "Untracked"
    if r["days_cover"] is None:
        return r["status"]
    basis = min(r["days_cover"], r["free_days"])
    return "Critical" if basis < inventory.CRITICAL_DAYS else "Low" if basis < r["reorder_days"] else "OK"


def test_raw_material_position_balances(client, h):
    rows = get(client, h, "/api/procurement/materials")
    assert rows
    for r in rows:
        tag = f"{r['code']} {r['factory']}"
        assert r["available"] + r["reserved"] - r["shortfall"] == pytest.approx(r["on_hand"], abs=0.3), tag
        assert r["available"] == pytest.approx(max(0, r["on_hand"] - r["reserved"]), abs=0.2), tag
        assert r["uncovered"] == pytest.approx(max(0, r["reserved"] - r["on_hand"] - r["incoming"]), abs=0.2), tag
        assert r["incoming_in_time"] <= r["incoming"] + 0.1, tag
        if r["days_cover"] is not None:
            assert r["on_hand_days"] - 0.05 <= r["days_cover"] <= r["projected_days_cover"] + 0.05, tag
            assert (r["late_delivery"] is None) == (r["incoming_in_time"] >= r["incoming"] - 0.1), tag
        assert r["status"] == _expected_status(r), tag
        if not r["tracked"]:
            assert r["status"] == "Untracked" and r["on_hand"] == 0 and r["reserved"] > 0, tag
    # packaging is committed too: one bag per pillow still to pack, as quality._consume_packaging draws it
    with scratch_db(client) as db:
        skus = {s.id: s for s in db.scalars(select(m.Sku))}
        bags = {}
        for po in db.scalars(select(m.ProductionOrder).where(
                m.ProductionOrder.status.in_(inventory.OPEN_PRODUCTION) | (m.ProductionOrder.pack_queue > 0))):
            sku = skus[po.sku_id]
            n = (max(0, po.required_qty - po.packed_qty - po.held_qty) if po.status in inventory.OPEN_PRODUCTION
                 else po.pack_queue)
            if sku.bag_material_id and n:
                bags[(po.factory_id, sku.bag_material_id)] = bags.get((po.factory_id, sku.bag_material_id), 0) + n
    by_key = {(r["factory_id"], r["material_id"]): r for r in rows}
    assert bags, "open production orders still have pillows to bag"
    for k, n in bags.items():
        assert by_key[k]["reserved"] == pytest.approx(n, abs=0.5)


def test_delivery_before_runout_extends_cover_everywhere(client):
    with scratch_db(client) as db:
        now = sim_now(db)
        rows = inventory.material_rows(db, [f.id for f in db.scalars(select(m.Factory))], now)
        r = next(x for x in rows if x["tracked"] and x["consumption_per_day"] > 0 and x["days_cover"] < 30
                 and x["late_delivery"] is None)
        sup = next(s for s in db.scalars(select(m.Supplier)) if r["code"] in (s.material_codes or []))
        add_days = 6
        po = proc.create_po(db, r["factory_id"], sup.id, r["material_id"], r["consumption_per_day"] * add_days,
                            now + timedelta(hours=2), now, "test")
        db.flush()
        key = lambda x: (x["factory_id"], x["material_id"])  # noqa: E731
        after = next(x for x in inventory.material_rows(db, [r["factory_id"]], now) if key(x) == key(r))
        assert after["days_cover"] == pytest.approx(r["days_cover"] + add_days, abs=0.15)
        assert after["late_delivery"] is None
        # the AI does not list it as running out within a horizon the delivery covers
        horizon = after["days_cover"] - 0.5
        listed = tools.material_runout(db, after["factory"], now, days=horizon)["data"]
        assert not any(key(x) == key(r) for x in listed)
        # nobody recommends expediting a delivery that arrives before the stock-out
        ins = insights.procurement(db, [r["factory_id"]], now)
        assert not any(po.number in (i["recommendation"] or "") or po.number in i["title"] for i in ins)

        # a delivery that lands after the stock-out does not count toward cover and is the one to expedite
        r2 = next(x for x in inventory.material_rows(db, [f.id for f in db.scalars(select(m.Factory))], now)
                  if x["tracked"] and x["consumption_per_day"] > 0 and x["days_cover"] < 20 and x["late_delivery"] is None
                  and key(x) != key(r))
        sup2 = next(s for s in db.scalars(select(m.Supplier)) if r2["code"] in (s.material_codes or []))
        late = proc.create_po(db, r2["factory_id"], sup2.id, r2["material_id"], 1000, now + timedelta(days=r2["days_cover"] + 5), now, "test")
        db.flush()
        after2 = next(x for x in inventory.material_rows(db, [r2["factory_id"]], now) if key(x) == key(r2))
        assert after2["days_cover"] == pytest.approx(r2["days_cover"], abs=0.05)
        assert after2["late_delivery"] and after2["late_delivery"]["po"] == late.number
        ins = insights.procurement(db, [r2["factory_id"]], now)
        per_material = [i for i in ins if late.number in (i["recommendation"] or "") or late.number in i["title"]]
        assert len(per_material) == 1, "exactly one expedite recommendation for the material"


# ─── finished goods ───────────────────────────────────────────────────────
def test_allocation_cover_counts_each_carton_once(client, h):
    al = get(client, h, "/api/warehouse/allocation", factory="ALL", days=14)
    stock = {(r["factory"], r["sku"]): r for r in al["stock_by_sku"]}
    used = {}
    for r in al["orders"]:
        assert 0 <= r["from_stock"] <= r["pending_requirement"]
        assert r["shortage"] == r["pending_requirement"] - r["from_stock"]
        used[(r["factory"], r["sku"])] = used.get((r["factory"], r["sku"]), 0) + r["from_stock"]
    for k, n in used.items():
        assert n <= (stock[k]["available"] if k in stock else 0), f"{k}: stock promised to several orders"
    assert al["totals"]["shortage"] == sum(r["shortage"] for r in al["orders"])
    assert al["totals"]["pending_requirements"] == al["totals"]["from_stock"] + al["totals"]["shortage"]


def test_packed_never_exceeds_completed(client):
    def slack_violations(db, skip=()):
        return [(p.number, p.completed_qty, p.pack_queue, p.held_qty, p.packed_qty) for p in db.scalars(select(m.ProductionOrder))
                if p.id not in skip and (p.packed_qty or 0) + (p.held_qty or 0) + (p.pack_queue or 0) > p.completed_qty]

    from app.simulation import engine
    with scratch_db(client) as db:
        legacy = {p[0] for p in slack_violations(db)}
        skip = {p.id for p in db.scalars(select(m.ProductionOrder)) if p.number in legacy}
        # a count above what waits at the station is capped: the carton cannot hold pillows never made
        line = next(ln for ln in db.scalars(select(m.ProductionLine)) if ln.current_po_id and ln.id)
        po = db.get(m.ProductionOrder, line.current_po_id)
        if po.id in skip:
            pytest.skip("current order already inconsistent in the seed")
        now = sim_now(db)
        queued = po.pack_queue
        c = qsvc.pack_carton(db, po, line, now, detected=queued + 3)
        assert c.detected_count == queued and po.pack_queue == 0
        # an underpacked carton reworked with nothing left at the station: the missing pillows are made, not invented
        req = c.required_count
        po.completed_qty += req - 1
        po.pack_queue += req - 1
        c2 = qsvc.pack_carton(db, po, line, now, detected=req - 1)
        assert c2.status == "Blocked" and po.pack_queue == 0
        made_before, packed_before = po.completed_qty, po.packed_qty
        loose = po.completed_qty - po.pack_queue - po.held_qty - po.packed_qty
        qsvc.rework(db, c2, now, "test")
        assert po.packed_qty == packed_before + req
        assert po.completed_qty == made_before + max(0, 1 - loose)
        # overpacked rework returns the extra pillow to the queue
        po.completed_qty += req + 1
        po.pack_queue += req + 1
        c3 = qsvc.pack_carton(db, po, line, now, detected=req + 1)
        q = po.pack_queue
        qsvc.rework(db, c3, now, "test")
        assert po.pack_queue == q + 1
        db.flush()
        assert not slack_violations(db, skip)
        for _ in range(16):  # four hours of simulated production, packing, rework and dispatch
            engine.step(db, 15)
        db.flush()
        assert not slack_violations(db, skip), "packed + held + queued pillows never exceed completed"
        assert not [p.number for p in db.scalars(select(m.ProductionOrder)) if p.id not in skip and p.packed_qty > p.completed_qty]


# ─── insight / assistant windows ──────────────────────────────────────────
def test_when_dates_far_timestamps():
    now = datetime(2026, 12, 14, 21, 16)
    assert insights.when(datetime(2026, 12, 14, 22, 0), now) == "Mon 22:00"
    assert insights.when(datetime(2026, 12, 15, 8, 0), now) == "Tue 08:00"
    assert insights.when(datetime(2026, 12, 22, 8, 0), now) == "Tue Dec 22 08:00"
    assert insights.when(datetime(2027, 1, 4, 12, 48), now) == "Mon Jan 04 12:48"
    assert insights.when(None, now) == "—"


def test_warehouse_workload_counts_only_the_window(client, h):
    with scratch_db(client) as db:
        now = sim_now(db)
        fids = [f.id for f in db.scalars(select(m.Factory))]
        wl = insights.workload(db, fids, now)
        end = now + timedelta(hours=wl["window_h"])
        trucks = {}
        for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")))):
            if t.order_id and (t.order_id not in trucks or t.scheduled_arrival < trucks[t.order_id]):
                trucks[t.order_id] = t.scheduled_arrival
        due = 0
        for a in db.scalars(select(m.Allocation).where(m.Allocation.status == "Reserved")):
            o = db.get(m.CustomerOrder, a.order_id)
            if (trucks.get(o.id) or o.scheduled_dispatch) <= end:
                due += a.cartons
        assert wl["cartons_to_pick"] == due <= wl["cartons_reserved_total"]
        S = m.InboundShipment
        at_dock = db.scalar(select(func.count()).select_from(S).where(S.status.in_(("Arrived", "Receiving"))))
        assert wl["inbound_at_dock"] == at_dock
        assert wl["inbound_shipments"] == wl["inbound_at_dock"] + wl["inbound_due"]
    api = get(client, h, "/api/warehouse/workload")
    assert api["window_h"] == insights.WORKLOAD_WINDOW_H
    fte = next((i for i in get(client, h, "/api/insights", page="warehouse")["insights"] if i["id"] == "wh-workforce"), None)
    if fte:
        assert f"{api['required_fte']} FTE" in fte["title"] and f"{api['cartons_to_pick']:,} cartons" in fte["detail"]


def test_quality_windows(client, h):
    a = get(client, h, "/api/quality/analytics")
    assert a["window_days"] == 7
    with scratch_db(client) as db:
        now = sim_now(db)
        day0 = datetime.combine(now.date(), datetime.min.time())
        expect = 0
        for s in db.scalars(select(m.ShiftSummary).where(m.ShiftSummary.date >= now.date() - timedelta(days=6),
                                                         m.ShiftSummary.date < now.date())):
            expect += sum((s.defects or {}).values())
        expect += db.scalar(select(func.count()).select_from(m.DefectRecord).where(m.DefectRecord.created_at >= day0))
        # a closed day's summary must only count that day's cartons
        past = now.date() - timedelta(days=1)
        p0 = datetime.combine(past, datetime.min.time())
        rework = db.scalar(select(func.coalesce(func.sum(m.Carton.required_count), 0)).where(
            m.Carton.status == "Reworked", m.Carton.created_at >= p0, m.Carton.created_at < p0 + timedelta(days=1)))
    assert sum(d["count"] for d in a["defect_distribution"]) == expect
    assert get(client, h, "/api/quality/summary", date=past.isoformat())["rework_qty"] == rework
    # customer error rate: same window and minimum sample in the chart, the S!AP insight and the assistant
    ranked = [r for r in a["by_customer"] if r["ranked"]]
    assert all(r["inspected"] >= a["min_cartons_ranked"] for r in ranked)
    assert all(not r["ranked"] for r in a["by_customer"][len(ranked):]), "ranked customers sort first"
    ins = next((i for i in get(client, h, "/api/insights", page="quality")["insights"] if i["id"] == "customer-errors"), None)
    if ranked and ranked[0]["errors"]:
        assert ins and ins["title"].startswith(ranked[0]["customer"]) and f"{ranked[0]['error_rate_pct']:.2f}%" in ins["detail"]
        with scratch_db(client) as db:
            ai = tools.packaging_errors_by_customer(db, "ALL", sim_now(db))
        assert ai["summary"].startswith(ranked[0]["customer"])


def test_loss_zone_ranked_by_value(client):
    with scratch_db(client) as db:
        from app.services import loss
        now = sim_now(db)
        fids = [f.id for f in db.scalars(select(m.Factory))]
        conf = [r for r in loss.incidents(db, fids) if r.detected_at and r.detected_at >= loss.window_start(now)
                and r.outcome == "Confirmed loss"]
        ins = next((i for i in insights.loss_prevention(db, fids, now) if i["id"] == "loss-zone"), None)
    if not conf:
        assert ins is None
        return
    val = {}
    for r in conf:
        val[r.zone] = val.get(r.zone, 0) + (r.value_usd or 0)
    assert ins["evidence"]["value_usd"] == pytest.approx(max(val.values()), abs=0.01)


def test_station_cards_and_assistant_match_pages(client, h):
    summary = get(client, h, "/api/quality/summary", factory="KNV")["counts"]
    cards = get(client, h, "/api/quality/stations", factory="KNV")
    assert sum(c["cartons_today"] for c in cards) == summary["inspected"]
    assert sum(c["passed_first_today"] for c in cards) == summary["passed_first"]
    for c in cards:
        if c["cartons_today"]:
            assert c["accuracy_pct"] == pytest.approx(100 * c["passed_first_today"] / c["cartons_today"], abs=0.01)
        if c["spec"] and c["queue"] and c["sku"] and c["full_cartons_waiting"] is not None:
            assert c["loose_pillows"] <= c["queue"]
    with scratch_db(client) as db:
        from app.services import metrics
        now = sim_now(db)
        knv = [db.scalar(select(m.Factory.id).where(m.Factory.code == "KNV"))]
        a, b = metrics.scope_window(now.date(), None, now)
        dd = metrics.downtime_detail(db, knv, a, b, now)
        res = tools.line_downtime(db, "KNV", now)
        assert sum(r["total_min"] for r in res["data"]) == pytest.approx(
            sum(v["down"] for lid, v in dd["by_line"].items()), abs=0.5)
        assert all(r["total_min"] == pytest.approx(sum(r.get(c, 0) for c in metrics.DOWNTIME_CATEGORIES), abs=0.3)
                   for r in res["data"])
        snap = tools.kpi_snapshot(db, "ALL", now)
        sh = snap["data"]["shipment"]
        assert f"{sh['ready']}/{sh['scheduled']} orders due within {sh['horizon_h']} h" in snap["summary"]
