"""Predictive maintenance analytics and S!AP advisories (illustrative model on simulated telemetry).

Runs against the seeded demo database through the public REST API. The seed
plants a degrading Garnett Line 01 main motor at Kernersville (M-102); the
rest of the fleet is healthy."""
import statistics
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADVISORY_KEYS = {"id", "category", "severity", "title", "summary", "factory", "factory_id", "machine", "prediction",
                 "impact", "recommendation", "window", "confidence", "evidence", "actions", "link"}
PREDICTION_KEYS = {"failure_prob_24h", "failure_prob_7d", "rul_hours", "rul_low_hours", "rul_high_hours",
                   "failure_mode", "method"}
SEEDED_DEGRADED = {"M-102", "M-226", "M-315"}


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


def pm_advisory(client, h, code, factory="ALL"):
    data = get(client, h, "/api/predictive/advisories", factory=factory)
    return next((a for a in data["advisories"] if a["id"] == f"pm:{code}"), None)


def test_01_machines_endpoint_contract(client, admin):
    d = get(client, admin, "/api/predictive/machines", factory="ALL")
    assert {"sim_time", "disclaimer", "kpis", "machines", "fleet", "related", "model"} <= set(d)
    assert d["disclaimer"] and "simulated" not in d["disclaimer"].lower()
    assert len(d["machines"]) == 96
    fleet = d["fleet"]
    assert {"risk_matrix", "forecast", "lowest_rul", "reliability", "plan", "unavailable"} <= set(fleet)
    assert len(fleet["forecast"]) == 14
    assert all(f["expected_failures_planned"] <= f["expected_failures"] + 1e-6 for f in fleet["forecast"])
    assert {r["type"] for r in fleet["reliability"]} == set(d["model"]["weibull"])
    for r in fleet["reliability"]:
        assert 1.4 <= r["beta"] <= 2.2 and r["model_mtbf_h"] > 0 and r["model_mttr_unplanned_h"] > r["model_mttr_planned_h"]
    assert {"fill_weight", "energy", "spares", "technicians"} <= set(d["related"])
    assert len(d["related"]["fill_weight"]) == 24                      # one Filling Head per line
    assert d["related"]["technicians"]["shifts"]
    assert all(s["expected_qty"] >= s["planned_qty"] for s in d["related"]["spares"])
    k = d["kpis"]
    assert k["machines"] == 96 and k["at_risk_7d"] >= 1 and k["expected_failures_7d"] > 0
    # factory scoping
    knv = get(client, admin, "/api/predictive/machines", factory="KNV")
    assert len(knv["machines"]) == 32 and {r["factory"] for r in knv["machines"]} == {"KNV"}


def test_02_m102_is_the_top_predictive_advisory(client, admin):
    data = get(client, admin, "/api/predictive/advisories", factory="ALL")
    assert {"sim_time", "advisories", "disclaimer", "counts", "total"} <= set(data)
    items = data["advisories"]
    for a in items:
        assert ADVISORY_KEYS <= set(a), a["id"]
        assert a["severity"] in ("critical", "warning", "info")
        assert 0 <= a["confidence"] <= 1
        if a["prediction"]:
            assert PREDICTION_KEYS <= set(a["prediction"])
    ranks = [{"critical": 0, "warning": 1, "info": 2}[a["severity"]] for a in items]
    assert ranks == sorted(ranks)                                      # ranked by severity first

    pm = [a for a in items if a["category"] == "Predictive maintenance"]
    top = pm[0]
    assert top["id"] == "pm:M-102"
    assert top in items[:3]
    assert top["severity"] == "critical" and top["factory"] == "KNV"
    assert top["machine"]["code"] == "M-102" and top["machine"]["line"] == "Garnett Line 01"
    p = top["prediction"]
    assert p["failure_prob_7d"] >= 0.5
    assert p["failure_prob_24h"] <= p["failure_prob_7d"]
    assert p["rul_low_hours"] <= p["rul_hours"] <= p["rul_high_hours"]
    assert "bearing" in p["failure_mode"].lower()
    assert top["impact"]["pillows_at_risk"] > 0 and top["impact"]["downtime_hours"] > 0 and top["impact"]["cost_usd"] > 0
    assert top["window"]["start"] and top["actions"] and top["actions"][0]["type"] == "schedule_work_order"
    assert top["link"] == {"page": "production", "entity_type": "machine", "entity_id": top["machine"]["id"]}
    assert top["evidence"] and top["work_order"] is None

    # limit + category filter
    lim = get(client, admin, "/api/predictive/advisories", factory="KNV", limit=2)
    assert len(lim["advisories"]) <= 2 and all(a["factory"] == "KNV" for a in lim["advisories"])
    only = get(client, admin, "/api/predictive/advisories", category="Predictive maintenance")
    assert only["advisories"] and {a["category"] for a in only["advisories"]} == {"Predictive maintenance"}


def test_03_healthy_machines_get_low_probabilities(client, admin):
    rows = get(client, admin, "/api/predictive/machines", factory="ALL")["machines"]
    healthy = [r for r in rows if r["available"] and r["code"] not in SEEDED_DEGRADED and (r["health_score"] or 0) >= 90]
    assert len(healthy) >= 60
    p7 = [r["failure_prob_7d"] for r in healthy]
    assert max(p7) < 0.10 and statistics.median(p7) < 0.03
    assert all(r["severity"] in (None, "info") for r in healthy)
    m102 = next(r for r in rows if r["code"] == "M-102")
    assert m102["failure_prob_7d"] > 10 * statistics.median(p7)
    assert m102["rul_hours"] < min(r["rul_hours"] for r in healthy)


def test_04_machine_detail_curve_and_options(client, admin):
    rows = get(client, admin, "/api/predictive/machines", factory="KNV")["machines"]
    mid = next(r["machine_id"] for r in rows if r["code"] == "M-102")
    d = get(client, admin, f"/api/predictive/machines/{mid}")
    assert d["advisory_id"] == "pm:M-102"
    mc = d["machine"]
    assert mc["covariates"] and mc["hazard_multiplier"] >= 1 and mc["repair"]["part"]
    curve = d["curve"]
    assert len(curve) == 57
    probs = [c["p_fail"] for c in curve]
    assert probs[0] == 0 and probs == sorted(probs) and probs[-1] >= mc["failure_prob_7d"]
    tel = d["telemetry"]
    assert tel["points"] and tel["thresholds"]["vibration_alarm"] == pytest.approx(2.8 * 1.9, abs=0.01)
    opts = {o["key"]: o for o in mc["options"]}
    assert {"rtf", "now", "night"} <= set(opts)
    assert opts["night"]["pillows_lost"] == 0                          # 23:00-06:00 costs no production
    assert sum(1 for o in mc["options"] if o["recommended"]) == 1
    assert client.get("/api/predictive/machines/999999", headers=admin).status_code == 404


def test_05_schedule_creates_work_order_and_advisory_shows_it(client, admin):
    adv = pm_advisory(client, admin, "M-102")
    action = adv["actions"][0]
    url = f"/api/predictive/advisories/{quote(adv['id'], safe='')}/schedule"

    quality = login(client, "quality", "quality123")                  # no maintenance.write
    post(client, quality, url, expect=403)

    maint = login(client, "maint", "maint123")
    wo = post(client, maint, url, {"start": action["start"], "end": action["end"]})
    assert wo["number"].startswith("WO-") and wo["type"] == "Predictive" and wo["status"] == "Scheduled"
    assert wo["machine"] == "M-102" and wo["scheduled_start"] == action["start"] and wo["advisory_id"] == "pm:M-102"

    after = pm_advisory(client, admin, "M-102")
    assert after["work_order"]["number"] == wo["number"]
    assert after["actions"] == []
    assert wo["number"] in after["recommendation"]
    assert after["severity"] in ("warning", "info")                    # mitigated by the scheduled job

    # Visible to the existing maintenance views and the plan; a second schedule is refused.
    assert any(w["number"] == wo["number"] for w in get(client, admin, "/api/production/maintenance", factory="KNV")["work_orders"])
    plan = get(client, admin, "/api/predictive/machines", factory="KNV")["fleet"]["plan"]
    assert any(j["work_order"] and j["work_order"]["number"] == wo["number"] for j in plan["jobs"])
    post(client, maint, url, expect=409)
    post(client, maint, "/api/predictive/advisories/spares%3AKNV/schedule", expect=400)
