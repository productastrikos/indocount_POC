"""Loss prevention: pilferage cases from every signal, the handling workflow and where cases surface."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import loss


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, user, pw):
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def h(client):
    """Admin headers; other test modules may have advanced the clock and closed every case, so make sure
    each plant has an active case to look at."""
    import random
    from app.db import SessionLocal
    from app import models as m
    from app.services import alerts
    from app.services.common import sim_now
    from sqlalchemy import select
    db = SessionLocal()
    try:
        now = sim_now(db)
        for f in db.scalars(select(m.Factory)):
            if not db.scalar(select(m.LossIncident.id).where(m.LossIncident.factory_id == f.id, m.LossIncident.status != "Resolved")):
                for kind in ("exit_gate_tag", "cctv_unbadged", "after_hours_access", "fg_cycle_count"):
                    if loss._create(db, f, kind, now, random.Random(1)) is not None:
                        break
        alerts.evaluate(db, now)
        db.commit()
    finally:
        db.close()
    return login(client, "admin", "admin123")


def test_baseline_covers_every_signal_with_outcomes(client, h):
    from app.db import SessionLocal
    from app import models as m
    from sqlalchemy import func, select
    rows = client.get("/api/loss/incidents", headers=h).json()["incidents"]
    kinds = {r["kind"] for r in rows}
    # scrap reconciliation is only raised from real QA rejects; the simulation reworks blocked cartons, so a fresh
    # database may have none — every other signal has an example case
    assert set(loss.KINDS) - {"scrap_reconciliation"} <= kinds, "every pilferage signal has an example case"
    db = SessionLocal()
    try:
        if "scrap_reconciliation" in kinds:
            assert db.scalar(select(func.count(m.Carton.id)).where(m.Carton.status == "Rejected"))
    finally:
        db.close()
    assert {r["factory"] for r in rows} == {"KNV", "PHX", "CMH"}
    closed = [r for r in rows if r["status"] == "Resolved"]
    assert closed and all(r["outcome"] in loss.OUTCOMES for r in closed)
    assert len({r["outcome"] for r in closed}) >= 3, "history shows several ways a case ends"
    active = [r for r in rows if r["status"] != "Resolved"]
    assert any(r["severity"] == "critical" for r in active)
    for r in active:
        assert r["next_step"] and r["next_step"]["text"], r["ref"]
    cv = [r for r in rows if r["kind"] == "cctv_unbadged"]
    assert cv and all(r["camera"] and r["feed_id"] and r["feed_t"] is not None for r in cv), \
        "CCTV access checks point at a real vision event in a feed"


def test_overview_and_detail(client, h):
    ov = client.get("/api/loss/overview", headers=h, params={"factory": "KNV"}).json()
    k = ov["kpis"]
    assert k["cases_7d"] >= 8 and k["active"] >= 1 and k["value_at_risk"] > 0
    assert len(ov["trend"]) == 7 and {b["kind"] for b in ov["by_kind"]} == set(loss.KINDS)
    assert set(ov["kinds"]) == set(loss.KINDS) and all(v["playbook"] for v in ov["kinds"].values())
    case = client.get("/api/loss/incidents", headers=h, params={"factory": "KNV", "kind": "fg_cycle_count", "limit": 1}).json()["incidents"][0]
    d = client.get(f"/api/loss/incidents/{case['id']}", headers=h).json()
    assert d["evidence"] and d["timeline"] and d["playbook"]
    assert d["links"]["location"] and d["links"]["location"]["code"] in d["zone"]
    assert client.get("/api/loss/incidents/999999", headers=h).status_code == 404
    assert client.get("/api/loss/incidents", headers=h, params={"kind": "nope"}).status_code == 400


def test_handling_workflow(client, h):
    case = next(r for r in client.get("/api/loss/incidents", headers=h, params={"status": "active"}).json()["incidents"])
    url = f"/api/loss/incidents/{case['id']}/actions"
    # executives can read but not work a case
    ex = login(client, "exec", "exec123")
    assert client.post(url, headers=ex, json={"action": "assign"}).status_code == 403
    d = client.post(url, headers=h, json={"action": "assign", "assignee": "Gate officer R. Diaz"}).json()
    assert d["assigned_to"] == "Gate officer R. Diaz" and d["status"] == "Investigating"
    todo = next(s for s in d["playbook"] if not s["done"])
    d = client.post(url, headers=h, json={"action": "step", "step": todo["key"], "note": "checked"}).json()
    assert next(s for s in d["playbook"] if s["key"] == todo["key"])["done"]
    assert client.post(url, headers=h, json={"action": "step", "step": todo["key"]}).status_code == 409
    # confirming a loss needs a note
    assert client.post(url, headers=h, json={"action": "resolve", "outcome": "Confirmed loss"}).status_code == 400
    assert client.post(url, headers=h, json={"action": "resolve", "outcome": "Lost"}).status_code == 400
    d = client.post(url, headers=h, json={"action": "resolve", "outcome": "Recovered", "note": "Returned to stock",
                                          "recovered_qty": case["qty"]}).json()
    assert d["status"] == "Resolved" and d["outcome"] == "Recovered" and d["recovered_qty"] == case["qty"]
    assert d["timeline"][-1]["step"] == "resolve"
    assert client.post(url, headers=h, json={"action": "note", "note": "x"}).status_code == 409
    d = client.post(url, headers=h, json={"action": "reopen", "note": "New footage"}).json()
    assert d["status"] == "Investigating" and d["outcome"] is None


def test_cases_surface_in_alerts_advisories_twin_and_ai(client, h):
    alerts = client.get("/api/alerts", headers=h).json()
    lp = [a for a in alerts if a["entity_type"] == "loss_incident"]
    assert lp and all(a["page"] == "loss-prevention" for a in lp)
    adv = client.get("/api/predictive/advisories", headers=h, params={"category": "Loss prevention"}).json()
    assert adv["total"] >= 1 and all(a["link"]["page"] == "loss-prevention" for a in adv["advisories"])
    ins = client.get("/api/insights", headers=h, params={"page": "loss-prevention"}).json()["insights"]
    assert ins and all(i["link"]["page"] == "loss-prevention" for i in ins)
    cams = client.get("/api/loss/incidents", headers=h, params={"camera": "CAM-KNV-04"}).json()["incidents"]
    assert all(c["camera"] == "CAM-KNV-04" for c in cams)
    fac = client.get("/api/twin/facility/KNV", headers=h).json()
    assert fac["loss"] and all(c["status"] != "Resolved" and c["area"] for c in fac["loss"])
    r = client.post("/api/ai/chat", headers=h, json={"message": "Any pilferage cases open right now?", "factory": "ALL"}).json()
    assert "Loss prevention" in r["answer"] or "loss prevention" in r["answer"].lower()


def test_simulation_raises_and_works_cases(client, h):
    from app.db import SessionLocal
    from app import models as m
    from app.services.common import sim_now
    from sqlalchemy import func, select
    db = SessionLocal()
    try:
        now = sim_now(db)
        before = db.scalar(select(func.count(m.LossIncident.id)))
        state = loss.RNG.getstate()
        try:
            loss.RNG.seed(7)
            from datetime import timedelta
            for i in range(40):  # ~13 simulated hours of handling, no production engine involved
                loss.simulate(db, now + timedelta(minutes=20 * i), now + timedelta(minutes=20 * (i + 1)), list(db.scalars(select(m.Factory))))
            after = db.scalar(select(func.count(m.LossIncident.id)))
            assert after > before, "new cases are raised over a shift"
            touched = [r for r in db.scalars(select(m.LossIncident)) if any(t.get("by") != loss.SYSTEM for t in r.timeline)]
            assert touched, "owners work the playbook"
        finally:
            loss.RNG.setstate(state)
            db.rollback()
    finally:
        db.close()


# ─── numbers and records agree ─────────────────────────────────────────
def _session():
    from app.db import SessionLocal
    return SessionLocal()


def _check_allocations(db, m, pallet, orders):
    from sqlalchemy import func, select
    reserved = db.scalar(select(func.coalesce(func.sum(m.Allocation.cartons), 0)).where(
        m.Allocation.pallet_id == pallet.id, m.Allocation.status == "Reserved"))
    assert pallet.cartons >= pallet.allocated_cartons == reserved
    for o in orders:
        assert o.allocated_qty == db.scalar(select(func.coalesce(func.sum(m.Allocation.pillows), 0)).where(
            m.Allocation.order_id == o.id)), o.number


def test_confirmed_fg_loss_is_written_off_stock(client, h):
    """A confirmed cycle-count loss leaves the book: the pallet loses the cartons, reservations on lost cartons are
    released consistently (Allocation rows, pallet.allocated_cartons, order.allocated_qty), an inventory adjustment
    is logged; closing the reopened case as Recovered puts the cartons back."""
    import random
    from app import models as m
    from app.services.common import sim_now
    from sqlalchemy import select
    db = _session()
    try:
        now = sim_now(db)
        f = db.scalar(select(m.Factory).where(m.Factory.code == "KNV"))
        inc = loss._create(db, f, "fg_cycle_count", now, random.Random(5))
        assert inc is not None
        # point the case at the most reserved pallet so the loss reaches into its reservations
        p = max(db.scalars(select(m.Pallet).where(m.Pallet.factory_id == f.id, m.Pallet.status == "Stored",
                                                  m.Pallet.cartons >= 2)), key=lambda p: (p.allocated_cartons, p.id))
        lost = min(p.cartons, (p.cartons - p.allocated_cartons) + min(2, p.allocated_cartons)) or 1
        ppc = db.get(m.Sku, p.sku_id).pillows_per_carton
        tl = [dict(t) for t in inc.timeline]
        tl[0]["data"] = {"pallet_id": p.id, "pallet": p.code, "cartons": lost, "ppc": ppc, "sku_id": p.sku_id}
        inc.timeline, inc.qty = tl, lost * ppc
        order_ids = list(db.scalars(select(m.Allocation.order_id).where(
            m.Allocation.pallet_id == p.id, m.Allocation.status == "Reserved")))
        before, reserved_before = p.cartons, p.allocated_cartons
        db.commit()
        case_id, pallet_id = inc.id, p.id
    finally:
        db.close()
    url = f"/api/loss/incidents/{case_id}/actions"
    d = client.post(url, headers=h, json={"action": "resolve", "outcome": "Confirmed loss", "note": "Recount confirmed"}).json()
    assert d["outcome"] == "Confirmed loss" and d["stock_adjustment"]["pallets"][0]["cartons"] == lost
    db = _session()
    try:
        p = db.get(m.Pallet, pallet_id)
        assert p.cartons == before - lost
        released = max(0, lost - (before - reserved_before))
        assert p.allocated_cartons == reserved_before - released
        _check_allocations(db, m, p, [db.get(m.CustomerOrder, i) for i in order_ids])
        assert db.scalar(select(m.EventLog.id).where(m.EventLog.event_type == "inventory_adjustment",
                                                     m.EventLog.entity_type == "pallet", m.EventLog.entity_id == pallet_id))
    finally:
        db.close()
    # reopened and closed as recovered: the cartons are back on the book, nothing is booked twice
    client.post(url, headers=h, json={"action": "reopen", "note": "Cartons found"})
    d = client.post(url, headers=h, json={"action": "resolve", "outcome": "Recovered", "note": "Found in staging"}).json()
    assert d["stock_adjustment"] is None
    db = _session()
    try:
        p = db.get(m.Pallet, pallet_id)
        assert p.cartons == before and p.cartons >= p.allocated_cartons
    finally:
        db.close()


def test_baseline_fg_write_offs_match_pallets(client, h):
    """Every confirmed FG case carries the adjustment that was booked, and the pallet book never goes below reservations."""
    from app import models as m
    from sqlalchemy import select
    db = _session()
    try:
        for inc in db.scalars(select(m.LossIncident).where(m.LossIncident.kind.in_(loss.FG_KINDS),
                                                           m.LossIncident.outcome == "Confirmed loss")):
            adj = loss._booked(inc)
            if loss._data(inc).get("pallet_id"):
                assert adj and adj["pallets"][0]["pallet_id"] == loss._data(inc)["pallet_id"], inc.ref
        assert not db.scalars(select(m.Pallet).where(m.Pallet.cartons < m.Pallet.allocated_cartons)).first()
    finally:
        db.close()


def test_receiving_cases_quote_their_goods_receipt(client, h):
    """A receiving shortage is raised only from a receipt booked short, with the receipt's own figures;
    synthetic history references never collide with a real shipment."""
    import random
    import re
    from app import models as m
    from app.services.common import sim_now
    from sqlalchemy import select
    db = _session()
    try:
        s = db.scalar(select(m.InboundShipment).join(m.Factory, m.Factory.id == m.InboundShipment.factory_id).where(
            m.Factory.code == "KNV", m.InboundShipment.status.in_(("Scheduled", "In Transit"))).order_by(m.InboundShipment.id))
        sid, qty = (s.id, s.qty) if s else (None, None)
    finally:
        db.close()
    if sid is None:
        pytest.skip("no open inbound shipment")
    assert client.post(f"/api/procurement/shipments/{sid}/arrive", headers=h, json={"dock": "R2"}).status_code == 200
    r = client.post(f"/api/procurement/shipments/{sid}/receipt/confirm", headers=h,
                    json={"received_qty": qty - 250, "damaged_qty": 0}).json()
    assert r["receipt"]["shortage_qty"] == 250
    db = _session()
    try:
        now = sim_now(db)
        f = db.scalar(select(m.Factory).where(m.Factory.code == "KNV"))
        inc = None
        for _ in range(20):  # earlier modules may have left other short receipts without a case
            inc = loss._create(db, f, "receiving_shortage", now, random.Random(3))
            if inc is None or inc.shipment_id == sid:
                break
        assert inc is not None and inc.shipment_id == sid and inc.qty == 250
        assert f"received {qty - 250:,.0f} of {qty:,.0f}" in inc.summary
        assert loss._create(db, f, "receiving_shortage", now, random.Random(3)) is None, "one case per short receipt"
        for c in db.scalars(select(m.LossIncident).where(m.LossIncident.kind == "receiving_shortage")):
            if c.shipment_id:
                gr = db.scalar(select(m.GoodsReceipt).where(m.GoodsReceipt.shipment_id == c.shipment_id))
                assert gr and gr.shortage_qty > 0 and abs(gr.shortage_qty - c.qty) < 1e-6, c.ref
                assert f"received {gr.received_qty:,.0f} of {gr.expected_qty:,.0f}" in c.summary, c.ref
            else:
                ref = re.match(r"(\S+):", c.title).group(1)
                assert not db.scalar(select(m.InboundShipment.id).where(m.InboundShipment.ref == ref)), c.ref
        db.rollback()
    finally:
        db.close()


def test_dispatch_cases_agree_with_the_truck(client, h):
    """A load count is only raised on a truck still loading (manifest = the order the truck will carry); a departed
    truck is only flagged by weight, with a dock count equal to its loaded quantity. Synthetic history refs are not real trucks."""
    import random
    import re
    from datetime import timedelta
    from app import models as m
    from app.services.common import sim_now
    from sqlalchemy import select
    db = _session()
    try:
        now = sim_now(db)

        def check(c):
            d = loss._data(c)
            t = db.get(m.TruckAppointment, c.truck_id) if c.truck_id else None
            if t is None:
                ref = re.match(r"(TRK-\d+)", c.title).group(1)
                assert d.get("flavor") == "weigh" and not db.scalar(
                    select(m.TruckAppointment.id).where(m.TruckAppointment.ref == ref)), c.ref
                return
            o = db.get(m.CustomerOrder, t.order_id)
            ppc = db.get(m.Sku, o.sku_id).pillows_per_carton
            if d["flavor"] == "weigh":
                assert t.status == "Departed" and d["manifest_cartons"] == -(-t.loaded_qty // ppc), c.ref
                assert c.detected_at >= t.departed_at, c.ref
            else:
                assert d["manifest_cartons"] == -(-o.qty // ppc) and d["counted"] > d["manifest_cartons"], c.ref
                if t.status == "Departed":  # the truck left with the manifest quantity
                    assert -(-t.loaded_qty // ppc) == d["manifest_cartons"], c.ref
            assert f"{d['manifest_cartons']}" in c.title, c.ref
        for c in db.scalars(select(m.LossIncident).where(m.LossIncident.kind == "dispatch_mismatch")):
            check(c)
        # a truck that is loading → a count check against the order it carries
        f = db.scalar(select(m.Factory).where(m.Factory.code == "PHX"))
        t = db.scalar(select(m.TruckAppointment).where(m.TruckAppointment.factory_id == f.id, m.TruckAppointment.order_id.is_not(None),
                                                       m.TruckAppointment.status.in_(("Scheduled", "Arrived"))))
        if t is not None:
            for x in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.factory_id == f.id,
                                                                 m.TruckAppointment.status == "Loading")):
                x.status = "Arrived"
            t.status, t.loading_start = "Loading", now - timedelta(minutes=10)
            db.flush()
            inc = loss._create(db, f, "dispatch_mismatch", now, random.Random(11))
            assert inc.truck_id == t.id and loss._data(inc)["flavor"] == "count"
            check(inc)
        db.rollback()
    finally:
        db.close()


def test_scrap_case_needs_rejected_cartons(client, h):
    """Scrap reconciliation counts the pillows of cartons rejected since the last weigh-out (not reworked ones),
    valued at the rejected SKUs; without rejected cartons there is no case."""
    import random
    from datetime import timedelta
    from app import models as m
    from app.services import quality
    from app.services.common import sim_now
    from sqlalchemy import select
    db = _session()
    try:
        now = sim_now(db)
        f = db.scalar(select(m.Factory).where(m.Factory.code == "KNV"))
        recent = db.scalar(select(m.Carton.id).where(m.Carton.factory_id == f.id, m.Carton.status == "Rejected",
                                                     m.Carton.created_at > now - timedelta(hours=24)))
        if recent is None:
            assert loss._create(db, f, "scrap_reconciliation", now, random.Random(2)) is None
        blocked = db.scalar(select(m.Carton).where(m.Carton.factory_id == f.id, m.Carton.status == "Blocked",
                                                   m.Carton.created_at > now - timedelta(hours=20)))
        if blocked is None:  # make one: a carton packed short and held at QA
            blocked = db.scalar(select(m.Carton).where(m.Carton.factory_id == f.id, m.Carton.status == "Passed",
                                                       m.Carton.created_at > now - timedelta(hours=20)).order_by(m.Carton.id.desc()))
            blocked.status, blocked.detected_count = "Blocked", blocked.required_count - 1
        quality.reject(db, blocked, now, "test")
        db.flush()
        inc = loss._create(db, f, "scrap_reconciliation", now, random.Random(2))
        d = loss._data(inc)
        rej = [db.get(m.Carton, i) for i in d["carton_ids"]]
        assert blocked.id in d["carton_ids"] and all(c.status == "Rejected" for c in rej)
        assert d["logged"] == sum(c.detected_count for c in rej) and 0 < inc.qty <= d["logged"]
        unit = sum(c.detected_count * loss.std_cost(db.get(m.Sku, c.sku_id)) for c in rej) / d["logged"]
        assert inc.value_usd == pytest.approx(inc.qty * unit, abs=0.01)
        db.rollback()
    finally:
        db.close()


def test_kpi_window_trend_and_shrinkage(client, h):
    """cases_7d, the trend and shrinkage all use the last 7 calendar days incl. today; output is valued at the
    standard cost of the SKUs made, the same basis as the case values."""
    from datetime import datetime, time, timedelta
    from app import models as m
    from sqlalchemy import func, select
    ov = client.get("/api/loss/overview", headers=h, params={"factory": "ALL"}).json()
    k = ov["kpis"]
    now = datetime.fromisoformat(ov["sim_time"])
    start = datetime.combine(now.date() - timedelta(days=6), time(0))
    assert ov["window"] == k["window"] == loss.WINDOW_LABEL
    assert [t["date"] for t in ov["trend"]] == [(start.date() + timedelta(days=i)).isoformat() for i in range(7)]
    assert sum(t["cases"] for t in ov["trend"]) == k["cases_7d"]
    assert sum(b["count"] for b in ov["by_kind"]) == k["cases_7d"]
    db = _session()
    try:
        cases = db.scalar(select(func.count(m.LossIncident.id)).where(m.LossIncident.detected_at >= start,
                                                                     m.LossIncident.detected_at <= now))
        assert cases == k["cases_7d"]
        out = db.scalar(select(func.coalesce(func.sum(m.ProductionHourly.actual), 0)).where(
            m.ProductionHourly.date >= start.date(), m.ProductionHourly.date <= now.date()))
        assert k["output_pillows_7d"] == int(out)
        assert k["output_value_7d"] == pytest.approx(out * k["output_std_cost"], rel=1e-3)
        prices = [loss.std_cost(s) for s in db.scalars(select(m.Sku))]
        assert min(prices) <= k["output_std_cost"] <= max(prices)
        if k["output_value_7d"]:
            assert k["shrinkage_pct"] == pytest.approx(100 * k["confirmed_loss_7d"] / k["output_value_7d"], abs=0.001)
    finally:
        db.close()
