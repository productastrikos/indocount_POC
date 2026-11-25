"""Digital twin and RFID badge tracking."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import calendar as cal


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def get(c, h, url, **params):
    r = c.get(url, headers=h, params=params)
    assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:300]}"
    return r.json()


def step(c, h, minutes):
    r = c.post("/api/sim/step", headers=h, json={"speed": minutes})
    assert r.status_code == 200, r.text


def sim_time(c, h):
    return datetime.fromisoformat(get(c, h, "/api/twin/network")["sim_time"])


def test_network_map(client, h):
    net = get(client, h, "/api/twin/network")
    assert {f["code"] for f in net["factories"]} == {"KNV", "PHX", "CMH"}
    for f in net["factories"]:
        assert f["lat"] and f["lon"] and f["status"] in ("Healthy", "Watch", "Critical")
        assert f["lines_total"] == 8 and f["target"] > 0
        assert f["badges"]["on_floor"] > 0, "badges should be read on the floor during a shift"
    for lane in net["lanes"]:
        assert 0 < lane["progress"] < 1 and lane["destination"]


def test_facility_flow_is_consistent(client, h):
    d = get(client, h, "/api/twin/facility/KNV")
    assert len(d["lines"]) == 8 and len(d["flow"]) == 10
    hourly = {r["id"]: r for r in get(client, h, "/api/production/lines", factory="KNV")}
    for ln in d["lines"]:
        s = ln["stages"]
        # every station only moves forward: filled >= sewn >= bagged, and bagged is the line's output
        assert s["filling"]["today"] >= s["sewing"]["today"] >= s["bagging"]["today"] >= 0
        assert s["sewing"]["today"] - s["bagging"]["today"] == s["bagging"]["wip"]
        assert s["filling"]["today"] - s["sewing"]["today"] == s["sewing"]["wip"]
        assert s["bagging"]["today"] == ln["output_today"] == hourly[ln["id"]]["actual"]
        assert len(s["sewing"]["workers"]) <= 2
    flow = {f["key"]: f for f in d["flow"]}
    assert flow["filling"]["today"] == sum(ln["stages"]["filling"]["today"] for ln in d["lines"])
    assert flow["bagging"]["today"] == sum(ln["output_today"] for ln in d["lines"])
    assert flow["packing"]["now"] == sum(ln["stages"]["packing"]["queue"] for ln in d["lines"])
    assert flow["sewing"]["workers"] > 0 and flow["sewing"]["per_worker_hour"]
    r = d["rfid"]
    assert r["badges_on_floor"] == r["at_station"] + r["away"] + r["on_break"]
    assert 50 < r["availability_pct"] <= 100


def test_breaks_are_not_time_away(client, h):
    """Move into the next scheduled break: badges read in the break room / canteen,
    and the break minutes are never added to time away."""
    now = sim_time(client, h)
    starts = []
    for sh in (1, 2):
        s = cal.SHIFTS[sh]
        for a, _b in s["breaks"] + [s["lunch"]]:
            t = datetime.combine(now.date(), a)
            if t > now:
                starts.append(t)
    if not starts:
        pytest.skip("no break left today")
    target = min(starts) + timedelta(minutes=3)
    while sim_time(client, h) < target:
        step(client, h, min(15, (target - sim_time(client, h)).total_seconds() / 60))
    d = get(client, h, "/api/twin/facility/KNV")
    on_break = [w for w in d["workers"] if w["state"] == "Break"]
    assert len(on_break) >= 10
    assert all(w["zone"] in ("Break room", "Canteen") for w in on_break)
    w = on_break[0]
    detail = get(client, h, f"/api/twin/workers/{w['id']}")
    tl = detail["timeline"]
    assert tl and tl[-1]["state"] == "Break" and tl[-1]["open"]
    away = sum(iv["minutes"] for iv in tl if iv["state"] == "Away")
    brk = sum(iv["minutes"] for iv in tl if iv["state"] == "Break")
    assert detail["worker"]["away_min"] == pytest.approx(away, abs=0.6)
    assert detail["worker"]["break_min"] == pytest.approx(brk, abs=0.6)
    # intervals never overlap
    for a, b in zip(tl, tl[1:]):
        assert a["end"] is not None and a["end"] <= b["start"]


def test_technician_dispatched_to_breakdown(client, h):
    lines = get(client, h, "/api/production/lines", factory="PHX")
    ln = next(x for x in lines if x["status"] == "Running")
    r = client.post("/api/sim/scenario/run", headers=h, json={"kind": "machine_failure", "line_id": ln["id"]})
    assert r.status_code == 200, r.text
    step(client, h, 2)
    d = get(client, h, "/api/twin/facility/PHX")
    techs = [w for w in d["workers"] if w["role"] == "Maintenance technician" and w["state"] in ("Station", "Away", "Break")]
    if not techs:
        pytest.skip("no technician on shift")
    broken = {mc["code"] for x in d["lines"] for mc in x["machines"] if mc["status"] in ("Breakdown", "Maintenance")}
    assert broken
    stationed = [w for w in techs if w["state"] == "Station"]
    assert any((w["zone"] or "").split(" ")[0] in broken for w in stationed) or not stationed


def test_workforce_advisories_shape(client, h):
    res = get(client, h, "/api/twin/advisories", factory="ALL")
    for a in res["advisories"]:
        assert {"id", "category", "severity", "title", "summary", "recommendation", "evidence", "factory"} <= set(a)
        assert a["severity"] in ("critical", "warning", "info")
