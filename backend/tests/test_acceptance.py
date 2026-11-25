"""End-to-end acceptance scenarios (Part 13 of the build brief).

Runs against a freshly seeded SQLite database through the public REST API,
with the background simulation paused so each step is deterministic."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(c, user, pw):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


def get(c, h, url, **params):
    r = c.get(url, headers=h, params=params)
    assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:300]}"
    return r.json()


def post(c, h, url, body=None, expect=200):
    r = c.post(url, headers=h, json=body or {})
    assert r.status_code == expect, f"{url}: {r.status_code} {r.text[:300]}"
    return r.json()


def test_00_every_page_endpoint_responds(client, admin):
    for url, params in [
        ("/api/meta", {}), ("/api/overview", {"factory": "ALL"}), ("/api/overview", {"factory": "KNV", "shift": 1}),
        ("/api/procurement/summary", {}), ("/api/procurement/purchase-orders", {}), ("/api/procurement/shipments", {}),
        ("/api/procurement/materials", {}), ("/api/procurement/suppliers", {}), ("/api/procurement/discrepancies", {}),
        ("/api/production/summary", {}), ("/api/production/orders", {}), ("/api/production/lines", {"factory": "KNV"}),
        ("/api/production/hourly", {"factory": "KNV"}), ("/api/production/machines", {}), ("/api/production/maintenance", {}),
        ("/api/production/workforce", {"factory": "KNV"}), ("/api/production/changeovers", {}),
        ("/api/quality/summary", {}), ("/api/quality/stations", {"factory": "KNV"}), ("/api/quality/cameras", {}),
        ("/api/quality/specs", {}), ("/api/quality/cartons", {}), ("/api/quality/defects", {}), ("/api/quality/analytics", {}),
        ("/api/warehouse/summary", {}), ("/api/warehouse/layout", {"factory": "KNV"}), ("/api/warehouse/receiving", {}),
        ("/api/warehouse/allocation", {}), ("/api/warehouse/picking", {}), ("/api/warehouse/workload", {}),
        ("/api/dispatch/summary", {}), ("/api/dispatch/orders", {}), ("/api/dispatch/appointments", {}),
        ("/api/dispatch/risk", {}), ("/api/dispatch/detention", {}), ("/api/alerts", {}), ("/api/events", {}),
    ] + [("/api/insights", {"page": p}) for p in ("overview", "procurement", "production", "quality", "warehouse", "dispatch")]:
        get(client, admin, url, **params)


def test_01_procurement_to_production(client, admin):
    """Receive a raw material shipment; goods receipt makes it available for production."""
    ships = get(client, admin, "/api/procurement/shipments", factory="KNV")
    s = next(x for x in ships if x["sku"] == "F-101" and x["status"] in ("Arrived", "In Transit", "Scheduled"))
    before = next(r for r in get(client, admin, "/api/procurement/materials", factory="KNV") if r["code"] == "F-101")
    if s["status"] != "Arrived":
        post(client, admin, f"/api/procurement/shipments/{s['id']}/arrive", {"dock": "R1"})
    post(client, admin, f"/api/procurement/shipments/{s['id']}/receipt/start")
    post(client, admin, f"/api/procurement/shipments/{s['id']}/receipt/scan", {"qty": s["qty"] - 200})
    res = post(client, admin, f"/api/procurement/shipments/{s['id']}/receipt/confirm", {"received_qty": s["qty"] - 200})
    assert res["receipt"]["shortage_qty"] == 200
    assert res["shipment"]["status"] == "Discrepancy"
    after = next(r for r in get(client, admin, "/api/procurement/materials", factory="KNV") if r["code"] == "F-101")
    assert after["available"] == pytest.approx(before["available"] + s["qty"] - 200, abs=1)
    disc = get(client, admin, "/api/procurement/discrepancies", factory="KNV")
    assert any(d["kind"] == "Shortage" and d["qty"] == 200 and d["po"] == s["po"] for d in disc)
    # Production order requirements now see the new stock.
    po = next(p for p in get(client, admin, "/api/production/orders", factory="KNV") if p["sku"] == "WMT-MS-Q" and p["status"] != "Completed")
    bom = get(client, admin, f"/api/production/orders/{po['id']}")["materials"]
    fill = next(b for b in bom if b["code"] == "F-101")
    assert fill["available"] == pytest.approx(after["available"], abs=1) and fill["sufficient"]


def test_02_production_to_quality(client, admin):
    """Running production generates pillows that flow into packing and inspection."""
    q0 = get(client, admin, "/api/quality/summary", factory="KNV")
    lines0 = {ln["id"]: ln for ln in get(client, admin, "/api/production/lines", factory="KNV")}
    post(client, admin, "/api/sim/step", {"speed": 15})
    q1 = get(client, admin, "/api/quality/summary", factory="KNV")
    lines1 = {ln["id"]: ln for ln in get(client, admin, "/api/production/lines", factory="KNV")}
    assert sum(ln["actual"] for ln in lines1.values()) > sum(ln["actual"] for ln in lines0.values())
    assert q1["workflow"][0]["qty"] > q0["workflow"][0]["qty"]       # pillows received from production
    assert q1["counts"]["inspected"] > q0["counts"]["inspected"]     # cartons inspected


def test_03_packaging_mismatch_blocks_carton(client, admin):
    stations = get(client, admin, "/api/quality/stations", factory="KNV")
    st = next(s for s in stations if s["po_id"] and s["spec"])
    req = st["spec"]["pillows_per_carton"]
    carton = post(client, admin, f"/api/quality/stations/{st['line_id']}/pack", {"detected": req - 1})
    assert carton["status"] == "Blocked" and carton["result"] == "Underpacked" and carton["carton_code"] is None
    alerts = get(client, admin, "/api/alerts", factory="KNV")
    assert any(a["stage"] == "quality" and "carton" in a["title"].lower() for a in alerts)
    scan = post(client, admin, "/api/warehouse/scan", {"code": carton["hold_code"]})
    assert scan["ok"] is False                                        # cannot enter finished goods
    # Final approval requires rework or an authorised override with a reason.
    r = client.post(f"/api/quality/cartons/{carton['id']}/override", headers=admin, json={"reason": ""})
    assert r.status_code == 400
    wh = login(client, "warehouse", "wh123")
    r = client.post(f"/api/quality/cartons/{carton['id']}/override", headers=wh, json={"reason": "Customer accepted"})
    assert r.status_code == 403                                       # not a supervisor
    qs = login(client, "quality", "quality123")
    ok = post(client, qs, f"/api/quality/cartons/{carton['id']}/override", {"reason": "Customer QA accepted 6-pack for this lot"})
    assert ok["status"] == "Overridden" and ok["carton_code"].startswith("CTN-")


def test_04_quality_to_warehouse(client, admin):
    stations = get(client, admin, "/api/quality/stations", factory="KNV")
    st = next(s for s in stations if s["po_id"] and s["spec"])
    carton = post(client, admin, f"/api/quality/stations/{st['line_id']}/pack", {"detected": st["spec"]["pillows_per_carton"]})
    assert carton["status"] == "Passed" and carton["carton_code"]
    rec = get(client, admin, "/api/warehouse/receiving", factory="KNV")
    pallet = next((p for p in rec["building"] if p["id"] == carton["pallet_id"]), None) or \
        next(p for p in rec["pending"] if p["id"] == carton["pallet_id"])
    scan = post(client, admin, "/api/warehouse/scan", {"code": carton["carton_code"]})
    assert scan["ok"] and scan["pallet"]["id"] == pallet["id"]
    fg0 = get(client, admin, "/api/warehouse/summary", factory="KNV")["total_pillows"]
    ps = post(client, admin, "/api/warehouse/scan", {"code": pallet["code"]})
    loc = ps["pallet"].get("suggested_location") or next(
        {"id": x["id"]} for z in get(client, admin, "/api/warehouse/layout", factory="KNV")["zones"] for x in z["locations"] if x["occupied"] < x["capacity"])
    stored = post(client, admin, "/api/warehouse/putaway", {"pallet_id": pallet["id"], "location_id": loc["id"],
                                                            "confirmed_sku": pallet["sku"], "confirmed_cartons": ps["pallet"]["cartons"]})
    assert stored["status"] == "Stored" and stored["location"]
    detail = get(client, admin, f"/api/warehouse/locations/{loc['id']}")
    assert any(p["id"] == pallet["id"] for p in detail["pallets"])
    assert get(client, admin, "/api/warehouse/summary", factory="KNV")["total_pillows"] > fg0


def test_05_warehouse_to_dispatch(client, admin):
    # Allocate stored finished goods to an order with an open requirement, pick, and watch readiness move.
    alloc = get(client, admin, "/api/warehouse/allocation", factory="ALL", days=10)
    row = next(r for r in alloc["orders"] if r["pending_requirement"] > 0 and r["sku_unallocated"] > 0)
    before = get(client, admin, f"/api/dispatch/orders/{row['order_id']}")["order"]
    res = post(client, admin, "/api/warehouse/allocate", {"order_id": row["order_id"]})
    assert res["allocated_cartons"] > 0
    picks = [r for r in get(client, admin, "/api/warehouse/picking") if r["order_id"] == row["order_id"]]
    assert picks
    for p in picks:
        post(client, admin, f"/api/warehouse/picking/{p['id']}/confirm")
    after = get(client, admin, f"/api/dispatch/orders/{row['order_id']}")["order"]
    assert after["readiness"]["allocation"] > before["readiness"]["allocation"]
    assert after["readiness"]["picking"] > before["readiness"]["picking"]
    assert after["readiness_pct"] > before["readiness_pct"]
    if after["blocking"] is not None:   # the remainder is linked to its production order
        assert any(r["order_id"] == row["order_id"] for r in get(client, admin, "/api/warehouse/allocation", days=10)["orders"])

    # A fully picked order goes through dock check-in, loading, departure and delivery.
    ready = next(o for o in get(client, admin, "/api/dispatch/orders", factory="ALL")
                 if o["status"] == "Ready for Dispatch" and o["truck"])
    t = ready["truck"]
    steps = {"Scheduled": ["arrive", "start-loading"], "Arrived": ["start-loading"], "Loading": [], "Loaded": []}[t["status"]]
    for s in steps:
        post(client, admin, f"/api/dispatch/appointments/{t['id']}/{s}")
    if t["status"] != "Loaded":
        post(client, admin, f"/api/dispatch/appointments/{t['id']}/confirm-loaded", {"loaded_qty": ready["picked"]})
    dep = post(client, admin, f"/api/dispatch/appointments/{t['id']}/depart")
    assert dep["status"] == "Departed" and dep["loaded_qty"] == ready["picked"]
    assert get(client, admin, f"/api/dispatch/orders/{ready['id']}")["order"]["status"] == "Dispatched"
    post(client, admin, f"/api/dispatch/orders/{ready['id']}/deliver")
    assert get(client, admin, f"/api/dispatch/orders/{ready['id']}")["order"]["status"] == "Delivered"


def test_06_machine_breakdown_recalculates(client, admin):
    orders = get(client, admin, "/api/dispatch/orders", factory="KNV")
    o = next(x for x in orders if x["number"] == "ORD-501")
    po = get(client, admin, f"/api/dispatch/orders/{o['id']}")["production_orders"][0]
    est0, eta0 = po["estimated_completion"], o["ready_eta"]
    res = post(client, admin, "/api/sim/scenario/run", {"kind": "machine_failure", "line_id": po["line_id"]})
    assert "broke down" in res["message"]
    line = next(ln for ln in get(client, admin, "/api/production/lines", factory="KNV") if ln["id"] == po["line_id"])
    assert line["status"] == "Breakdown"
    po1 = get(client, admin, f"/api/production/orders/{po['id']}")["po"]
    assert po1["estimated_completion"] > est0
    o1 = get(client, admin, f"/api/dispatch/orders/{o['id']}")["order"]
    assert o1["ready_eta"] > eta0 and o1["at_risk"]
    assert any(a["entity_type"] == "machine" and "breakdown" in a["title"].lower() for a in get(client, admin, "/api/alerts", factory="KNV"))
    # Packaging expectations and warehouse forecasts follow the production estimate.
    ov = get(client, admin, "/api/overview", factory="KNV")
    assert ov["kpis"]["shipment"]["at_risk"] >= 1


def test_07_truck_delay_detention(client, admin):
    det0 = get(client, admin, "/api/dispatch/detention", factory="CMH")
    res = post(client, admin, "/api/sim/scenario/run", {"kind": "truck_delay", "factory_id": 3})
    assert "waiting" in res["message"]
    det1 = get(client, admin, "/api/dispatch/detention", factory="CMH")
    assert det1["incurred_estimate"] == det0["incurred_estimate"] + det1["charge_per_truck"]
    waiting = [t for t in det1["trucks"] if t["status"] == "Arrived"]
    assert waiting and waiting[0]["wait_min"] >= 140
    assert any(a["stage"] == "dispatch" and "detention" in a["title"].lower() for a in get(client, admin, "/api/alerts", factory="CMH"))


def _num(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", text)]


def test_08_ai_assistant_matches_records(client, admin):
    def ask(q, factory="ALL"):
        return post(client, admin, "/api/ai/chat", {"message": q, "factory": factory})

    q = get(client, admin, "/api/quality/summary", factory="ALL")["counts"]
    a = ask("How many cartons failed inspection today?")
    assert q["failed"] in _num(a["answer"]) and "carton_failures" in a["sources"]

    mats = [r for r in get(client, admin, "/api/procurement/materials") if r["days_cover"] is not None and r["days_cover"] <= 5]
    a = ask("Which materials may run out in the next five days?")
    assert all(r["code"] in a["answer"] for r in mats)

    a = ask("Which trucks are at risk of missing their appointments?")
    assert "ORD-501" in a["answer"]

    stocked = next(r for r in get(client, admin, "/api/warehouse/stock", factory="KNV") if r["locations"])
    a = ask(f"Where is SKU {stocked['sku']} stored?", "KNV")
    real = {x["code"] for z in get(client, admin, "/api/warehouse/layout", factory="KNV")["zones"]
            for x in z["locations"] if x["sku"] and stocked["sku"] in x["sku"]}
    cited = [row[0] for row in a["tables"][0]["rows"]]
    assert cited and set(cited) <= real and any(loc in a["answer"] for loc in cited)

    a = ask("What is the labour cost per pillow?")
    assert "$" in a["answer"] and a["charts"]

    a = ask("Which production line has the highest downtime?", "KNV")
    assert "line_downtime" in a["sources"]

    a = ask("Which orders are waiting for picking?")
    assert "orders_waiting_for_picking" in a["sources"]

    a = ask("What is our estimated detention cost this week?")
    det = get(client, admin, "/api/dispatch/detention", factory="ALL")
    assert f"${det['incurred_estimate']:,.0f}" in a["answer"]

    a = ask("What is the best line allocation for today's orders?", "KNV")
    assert "proposal" in a["answer"].lower()

    a = ask("Tell me a joke about cats")
    assert a["sources"] == []                                        # no generic answers outside the data


def test_09_live_speed_packing_keeps_up(client, admin):
    """At live speed (1-minute ticks) every packing station, including 60-pillow Gaylords, keeps packing."""
    gaylord = {s["code"] for s in get(client, admin, "/api/quality/specs") if s["pillows_per_carton"] >= 48}

    def gaylord_cartons():
        return sum(1 for c in get(client, admin, "/api/quality/cartons", factory="ALL", limit=300) if c["sku"] in gaylord)

    before = gaylord_cartons()
    q0 = get(client, admin, "/api/quality/summary", factory="ALL")
    for _ in range(40):
        post(client, admin, "/api/sim/step", {"speed": 1})
    q1 = get(client, admin, "/api/quality/summary", factory="ALL")
    assert gaylord_cartons() > before
    produced = q1["workflow"][0]["qty"] - q0["workflow"][0]["qty"]
    assert q1["pack_queue"] - q0["pack_queue"] < max(200, produced * 0.5)   # queue does not run away
    assert all(s["qty"] >= 0 for s in q1["workflow"])
