"""Production / overview / maintenance metrics add up: every screen derives the same numbers from the same
records, and the identities between them hold (downtime by cause = hourly downtime, A x P = efficiency,
A x P x Q = OEE, factory rows sum to the plant KPI, plan to date agrees across the monitoring views, the
7-day expected failures equal the first 7 forecast days, ...).

Runs against the seeded demo database through the public REST API (simulation paused)."""
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.db import SessionLocal
from app.main import app
from app.services import calendar as cal
from app.services import metrics, workforce
from app.services.common import factory_ids, sim_now


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def get(c, h, url, **params):
    r = c.get(url, headers=h, params={k: v for k, v in params.items() if v is not None})
    assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:300]}"
    return r.json()


def scopes(db):
    """(date, shift) scopes that have production: today (both / each started shift) and yesterday."""
    now = sim_now(db)
    out = [(now.date(), None), (now.date() - timedelta(days=1), None), (now.date() - timedelta(days=1), 2)]
    out += [(now.date(), sh) for sh in (1, 2) if cal.shift_bounds(now.date(), sh)[0] < now]
    return now, out


def test_downtime_by_cause_equals_hourly_records(client, db):
    """Events overlapping the window (incl. ones that started earlier / are still open) count only their minutes
    inside it, so causes + "No orders" idle = the hourly downtime, per line and in total."""
    now, scs = scopes(db)
    fids = factory_ids(db, "ALL")
    for day, shift in scs:
        p = metrics.production(db, fids, day, shift, now)
        dd = p["downtime"]
        q = select(m.ProductionHourly.line_id, func.sum(m.ProductionHourly.downtime_min)).where(m.ProductionHourly.date == day)
        if shift:
            q = q.where(m.ProductionHourly.shift == shift)
        hourly = {lid: float(v or 0) for lid, v in db.execute(q.group_by(m.ProductionHourly.line_id))}
        total = sum(dd["by_category"].values()) + dd["no_orders_min"]
        assert abs(total - sum(hourly.values())) <= 1.0, (day, shift, total, sum(hourly.values()))
        for ln in p["lines"]:
            x = p["by_line"][ln.id]
            assert abs(x["down"] + x["no_orders"] - hourly.get(ln.id, 0.0)) <= 0.6, (day, shift, ln.name)
            # run + downtime + no-orders idle = elapsed productive time of the scope
            assert abs(x["run"] + x["down"] + x["no_orders"] - p["elapsed_min"]) <= 1.0, (day, shift, ln.name)
        assert "No orders" not in dd["by_category"], "starvation is reported apart from machine/process downtime"


def test_no_orders_idle_is_not_planned_time(client, db):
    """A line starved of work (open "No orders" event that started yesterday) loses planned time, not availability."""
    now = sim_now(db)
    fids = factory_ids(db, "ALL")
    before = metrics.production(db, fids, now.date(), None, now)
    ln = before["lines"][0]
    a, b = metrics.scope_window(now.date(), None, now)
    elapsed = cal.productive_minutes(a, b)
    if elapsed < 60:
        pytest.skip("not enough of the day elapsed")
    start = datetime.combine(now.date() - timedelta(days=1), time(22, 0))
    db.add(m.DowntimeEvent(line_id=ln.id, factory_id=ln.factory_id, category="Other", reason="No production order queued",
                           start=start, end=None, minutes=0))  # legacy category, recognised by its reason
    db.flush()
    after = metrics.production(db, fids, now.date(), None, now)
    x0, x1 = before["by_line"][ln.id], after["by_line"][ln.id]
    assert x1["no_orders"] == pytest.approx(elapsed, abs=0.5)
    assert x1["planned_min"] == pytest.approx(0, abs=0.5) and x1["planned"] == pytest.approx(0, abs=0.5)
    assert after["planned_to_date"] == pytest.approx(before["planned_to_date"] - x0["planned"], abs=1)
    dd = metrics.downtime_detail(db, fids, a, b, now)
    assert dd["no_orders_min"] >= elapsed - 0.5
    # the line's recorded downtime is now explained by the starvation, so it leaves the machine/process total
    assert sum(dd["by_category"].values()) == pytest.approx(sum(before["downtime"]["by_category"].values()) - x0["down"], abs=0.5)
    assert after["availability"] == pytest.approx(
        sum(ln_.planned_rate * after["by_line"][ln_.id]["run"] for ln_ in after["lines"])
        / sum(ln_.planned_rate * after["by_line"][ln_.id]["planned_min"] for ln_ in after["lines"]), rel=1e-9)
    db.rollback()


def test_oee_identities(client, db):
    """Availability is rate-weighted, so A x P = efficiency and A x P x Q = OEE at plant level."""
    now, scs = scopes(db)
    for code in ("ALL", "KNV", "PHX", "CMH"):
        fids = factory_ids(db, code)
        for day, shift in scs:
            p = metrics.production(db, fids, day, shift, now)
            if p["availability"] is None:
                continue
            eff = p["actual"] / sum(p["by_line"][ln.id]["planned"] for ln in p["lines"])
            assert p["availability"] * p["performance"] == pytest.approx(eff, rel=1e-9), (code, day, shift)
            assert p["availability"] <= 1.0 + 1e-9
            a, b = metrics.scope_window(day, shift, now)
            from app.services import quality
            q = quality.quality_counts(db, fids, a, metrics.quality_until(b, now))
            o = metrics.oee(p, q)
            if p["performance"] <= 1:
                assert abs(o["availability_pct"] * o["performance_pct"] / 100 - p["efficiency_pct"]) <= 0.15
            Q = q["passed_first"] / q["inspected"] if q["inspected"] else 1.0
            assert o["oee_pct"] == pytest.approx(round(min(1, p["availability"]) * min(1, p["performance"]) * Q * 100, 1))


def test_overview_matches_production_page(client, admin, db):
    now, scs = scopes(db)
    for day, shift in scs:
        params = {"date": day.isoformat(), "shift": shift}
        ov = get(client, admin, "/api/overview", **params)["kpis"]
        pr = get(client, admin, "/api/production/summary", **params)
        assert ov["production"]["actual"] == pr["production"]["actual"]
        assert ov["production"]["planned_to_date"] == pr["production"]["planned_to_date"]
        assert ov["production"]["target"] == pr["production"]["target"]
        assert ov["efficiency"]["pct"] == pr["production"]["efficiency_pct"]
        assert ov["oee"] == pr["oee"], (day, shift)
        assert ov["downtime"]["total_min"] == pr["downtime_total"]
        assert ov["downtime"]["by_category"] == pr["downtime"]
        assert ov["downtime"]["idle_no_orders_min"] == pr["idle_no_orders_min"]


def test_factory_comparison_follows_scope_and_sums_to_kpi(client, admin, db):
    now, scs = scopes(db)
    for day, shift in scs:
        o = get(client, admin, "/api/overview", date=day.isoformat(), shift=shift)
        k, cmp = o["kpis"], o["comparison"]
        assert sum(f["output"] for f in cmp) == k["production"]["actual"], (day, shift)
        assert abs(sum(f["target"] for f in cmp) - k["production"]["target"]) <= 2
        assert abs(sum(f["planned_to_date"] for f in cmp) - k["production"]["planned_to_date"]) <= 2
        assert abs(sum(f["downtime_min"] for f in cmp) - k["downtime"]["total_min"]) <= 2
        for f in cmp:
            single = get(client, admin, "/api/production/summary", factory=f["code"], date=day.isoformat(), shift=shift)
            assert f["output"] == single["production"]["actual"] and f["efficiency_pct"] == single["production"]["efficiency_pct"]


def test_quality_counts_stay_inside_the_scope(client, admin, db):
    now = sim_now(db)
    if cal.shift_bounds(now.date(), 2)[0] >= now:
        pytest.skip("shift 2 not started")
    day = get(client, admin, "/api/overview")["kpis"]["packaging"]["inspected"]
    s1 = get(client, admin, "/api/overview", shift=1)["kpis"]["packaging"]["inspected"]
    s2 = get(client, admin, "/api/overview", shift=2)["kpis"]["packaging"]["inspected"]
    assert s1 + s2 == day
    yd = (now.date() - timedelta(days=1)).isoformat()
    ys1 = get(client, admin, "/api/overview", date=yd, shift=1)["kpis"]["packaging"]["inspected"]
    ys2 = get(client, admin, "/api/overview", date=yd, shift=2)["kpis"]["packaging"]["inspected"]
    assert ys1 + ys2 == get(client, admin, "/api/overview", date=yd)["kpis"]["packaging"]["inspected"]


def test_customer_filter_scopes_packaging_and_keeps_oee_plant_wide(client, admin, db):
    cust = db.scalar(select(m.Customer).order_by(m.Customer.id))
    plant = get(client, admin, "/api/overview")["kpis"]
    k = get(client, admin, "/api/overview", customer=cust.id)["kpis"]
    assert k["oee"] == plant["oee"], "OEE quality must have the same (plant-wide) scope as availability and performance"
    assert k["packaging"]["customer_scoped"] and set(k["customer_scoped"]) == {"packaging", "shipment"}
    now = sim_now(db)
    a, b = metrics.scope_window(now.date(), None, now)
    blocked = db.scalar(select(func.count()).select_from(m.Carton).where(
        m.Carton.customer_id == cust.id, m.Carton.status == "Blocked", m.Carton.created_at >= a,
        m.Carton.created_at < metrics.quality_until(b, now))) or 0
    assert k["packaging"]["blocked"] == blocked
    assert k["packaging"]["inspected"] <= plant["packaging"]["inspected"]


def test_output_per_worker_hour_uses_worker_hours(client, admin, db):
    now = sim_now(db)
    fids = factory_ids(db, "ALL")
    for day, shift in ((now.date(), None), (now.date() - timedelta(days=1), None)):
        k = get(client, admin, "/api/overview", date=day.isoformat(), shift=shift)["kpis"]
        wf = workforce.summary(db, fids, day, now, k["production"]["actual"], shift=shift)
        wh = sum(n * workforce.productive_hours_elapsed(day, sh, now) for sh, n in wf["deployed_by_shift"].items())
        assert wf["worker_hours"] == pytest.approx(wh, abs=0.01)
        assert sum(wf["deployed_by_shift"].values()) == wf["workers_deployed"] == k["workforce"]["deployed"]
        assert k["workforce"]["output_per_worker_hour"] == pytest.approx(k["production"]["actual"] / wh, abs=0.06)


def test_past_day_workforce_counts_support_staff(client, admin, db):
    now = sim_now(db)
    yd = now.date() - timedelta(days=1)
    rows = list(db.scalars(select(m.ShiftSummary).where(m.ShiftSummary.date == yd)))
    if len(rows) != 2 * len(factory_ids(db, "ALL")):
        pytest.skip("no closed shift records for yesterday")
    wf = get(client, admin, "/api/production/workforce", date=yd.isoformat())
    assert wf["workers_deployed"] == sum(r.workers_deployed for r in rows)
    assert wf["labour_cost"] == pytest.approx(sum(r.labour_cost for r in rows), abs=0.05)
    rostered = sum(1 for e in wf["employees"] if e["role"] not in workforce.WAREHOUSE_ROLES)
    assert wf["absent"] == max(0, rostered - wf["workers_deployed"])
    assert wf["source"] == "shift summaries"


def test_labour_cost_per_hour_is_one_crew_rate(client, admin, db):
    now = sim_now(db)
    for shift in (1, 2):
        if cal.shift_bounds(now.date(), shift)[0] >= now:
            continue
        wf = get(client, admin, "/api/production/workforce", shift=shift)
        crew_rate = sum(e["hourly_wage"] for e in wf["employees"]
                        if e["attendance"] in ("Present", "Late") and e["role"] not in workforce.WAREHOUSE_ROLES)
        assert wf["labour_cost_per_hour"] == pytest.approx(crew_rate, rel=0.002), "one shift = that crew's hourly bill"
    both = get(client, admin, "/api/production/workforce")
    assert both["labour_cost_per_hour"] == pytest.approx(both["labour_cost"] / both["paid_hours"], rel=0.001)  # paid_hours is rounded


def test_line_drawer_counts_same_roles_and_current_shift_output(client, admin, db):
    now = sim_now(db)
    shift = metrics.current_shift(now.date(), now) or 1
    for ln in db.scalars(select(m.ProductionLine).order_by(m.ProductionLine.id).limit(6)):
        d = get(client, admin, f"/api/production/lines/{ln.id}")
        wf = d["workforce"]
        assert wf["workers_required"] == sum(workforce.LINE_ROLE_REQUIREMENT.values())
        crew = [e for e in wf["employees"] if e["attendance"] in ("Present", "Late") and e["role"] in workforce.LINE_ROLE_REQUIREMENT]
        assert wf["workers_deployed"] == len(crew)
        out = db.scalar(select(func.sum(m.ProductionHourly.actual)).where(
            m.ProductionHourly.line_id == ln.id, m.ProductionHourly.date == now.date(), m.ProductionHourly.shift == shift)) or 0
        assert wf["pillows"] == int(out)
        if out:
            assert wf["labour_cost_per_pillow"] == pytest.approx(wf["labour_cost"] / out, abs=0.001)


def test_monitoring_plan_to_date_agrees(client, admin, db):
    now, scs = scopes(db)
    for day, shift in scs:
        params = {"date": day.isoformat(), "shift": shift}
        s = get(client, admin, "/api/production/summary", **params)["production"]
        h = get(client, admin, "/api/production/hourly", **params)
        assert h["totals"]["planned_to_date"] == s["planned_to_date"]
        assert h["totals"]["actual"] == s["actual"]
        if h["lines"]:
            assert abs(sum(r["target"] for r in h["lines"]) - s["planned_to_date"]) <= len(h["lines"]) / 2 + 1
            assert sum(r["actual"] for r in h["lines"]) == pytest.approx(s["actual"], abs=len(h["lines"]))
        assert abs(sum(x["planned_to_date"] for x in h["chart"]) - s["planned_to_date"]) <= 2
        assert abs(sum(x["planned"] for x in h["chart"]) - s["target"]) <= len(h["chart"]) / 2 + 1
        if not shift and day == now.date():
            lines = get(client, admin, "/api/production/lines")
            assert abs(sum(r["planned_to_date"] for r in lines) - s["planned_to_date"]) <= len(lines) / 2 + 1
            assert abs(sum(r["target_today"] for r in lines) - s["target"]) <= len(lines) / 2 + 1
            assert sum(r["downtime_min"] for r in lines) == pytest.approx(h["downtime_total"], abs=len(lines) / 2 + 1)


def test_lines_last_hour_compares_with_that_hours_plan(client, admin, db):
    now = sim_now(db)
    last_h = now.hour - 1
    planned = defaultdict(float)
    for r in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.date == now.date(), m.ProductionHourly.hour == last_h)):
        planned[r.line_id] += r.planned
    for ln in get(client, admin, "/api/production/lines"):
        if ln["planned_last_hour"] is None:
            continue
        assert ln["planned_last_hour"] <= round(planned[ln["id"]]) + 1, "never more than the hour's productive-minute plan"


def test_shift_not_started_reports_null(client, db):
    now = sim_now(db)
    fids = factory_ids(db, "ALL")
    early = datetime.combine(now.date(), time(10, 0))
    k = metrics.kpis(db, fids, now.date(), 2, None, early)
    assert k["production"]["status"] == "Not started" and not k["production"]["started"]
    assert k["efficiency"]["pct"] is None and k["efficiency"]["delta"] is None
    assert k["oee"]["oee_pct"] is None and k["oee"]["availability_pct"] is None


def test_previous_shift_compares_like_with_like(client, admin, db):
    now = sim_now(db)
    cur = metrics.current_shift(now.date(), now)
    if not cur:
        pytest.skip("before the first shift")
    k = get(client, admin, "/api/overview")["kpis"]["efficiency"]
    s_cur = get(client, admin, "/api/production/summary", shift=cur)["production"]["efficiency_pct"]
    pday, psh = metrics.previous_shift(now.date(), cur)
    s_prev = get(client, admin, "/api/production/summary", date=pday.isoformat(), shift=psh)["production"]["efficiency_pct"]
    assert k["current_shift_pct"] == s_cur and k["previous_shift_pct"] == s_prev
    if s_cur is not None and s_prev is not None:
        assert k["delta"] == pytest.approx(s_cur - s_prev, abs=0.11)
    lc = get(client, admin, "/api/overview")["kpis"]["labour_cost"]
    wf_cur = get(client, admin, "/api/production/workforce", shift=cur)
    assert lc["current_per_pillow"] == wf_cur["labour_cost_per_pillow"]


def test_expected_failures_equal_first_7_forecast_days(client, admin, db):
    d = get(client, admin, "/api/predictive/machines")
    k, fc = d["kpis"], d["fleet"]["forecast"]
    assert k["expected_failures_7d"] == pytest.approx(sum(f["expected_failures"] for f in fc[:7]), abs=0.011)
    assert k["expected_failures_7d_planned"] == pytest.approx(sum(f["expected_failures_planned"] for f in fc[:7]), abs=0.011)
    assert k["expected_pillows_lost_7d"] == pytest.approx(sum(f["expected_pillows_lost"] for f in fc[:7]), abs=8)
    status = {ln.id: ln.status for ln in db.scalars(select(m.ProductionLine))}
    avail = {mc["machine_id"] for mc in d["machines"] if mc["available"]}
    assert k["available"] == len(avail)
    assert k["available_on_idle_lines"] == sum(1 for mc in db.scalars(select(m.Machine))
                                               if mc.id in avail and status.get(mc.line_id) != "Running")


def test_reliability_observed_figures(client, admin, db):
    d = get(client, admin, "/api/predictive/machines")
    now = sim_now(db)
    st = db.scalar(select(m.SimState))
    since = datetime.combine(st.seed_date, cal.DAY_START)
    run = {lid: float(v or 0) / 60 for lid, v in db.execute(select(m.ProductionHourly.line_id, func.sum(m.ProductionHourly.run_min))
                                                           .where(m.ProductionHourly.date >= since.date())
                                                           .group_by(m.ProductionHourly.line_id))}
    machines = list(db.scalars(select(m.Machine)))
    for r in d["fleet"]["reliability"]:
        mcs = [mc for mc in machines if mc.machine_type == r["type"]]
        assert r["observed_op_hours"] == round(sum(run.get(mc.line_id, 0) for mc in mcs)), r["type"]
        evs = list(db.scalars(select(m.DowntimeEvent).where(
            m.DowntimeEvent.category == "Mechanical breakdown", m.DowntimeEvent.start >= since,
            m.DowntimeEvent.machine_id.in_([mc.id for mc in mcs]))))
        assert r["observed_failures"] == len(evs)
        done = [e.minutes / 60 for e in evs if e.end]
        assert r["observed_mttr_h"] == (round(sum(done) / len(done), 2) if done else None)
    assert now >= since


def test_machine_health_window_only_when_siap_recommends_one(client, admin):
    recs = get(client, admin, "/api/production/maintenance")["recommendations"]
    fleet = {mc["machine_id"]: mc for mc in get(client, admin, "/api/predictive/machines")["machines"]}
    for r in recs:
        p = fleet.get(r["machine_id"])
        if not p or r.get("model") != "S!AP reliability model":
            continue
        if p["recommended"]:
            assert not r["suggested_window"].startswith("Monitor") and r["window_start"]
        else:
            assert r["suggested_window"].startswith("Monitor") and r["window_start"] is None
        assert r["est_hours_to_maintenance"] == pytest.approx(p["rul_hours"], abs=0.05)


def test_pipeline_past_date_uses_that_days_figures(client, admin, db):
    now = sim_now(db)
    yd = (now.date() - timedelta(days=1)).isoformat()
    s = get(client, admin, "/api/production/summary", date=yd)
    stages = {x["stage"]: x for x in s["pipeline"]["stages"]}
    assert s["pipeline"]["note"]
    assert stages["Pillow Filling"]["qty"] == s["production"]["actual"] == stages["Sewing"]["qty"], "no live WIP on a past date"
    assert stages["Transfer to Packaging"]["qty"] == s["production"]["actual"]
