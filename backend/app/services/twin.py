"""Digital twin read model.

network()   US map: the three plants, live status and in-transit loads.
facility()  One plant: every line station with its pillows and operators,
            packing, QA hold, palletizing, finished goods racks, docks, the
            factory pillow flow and RFID workforce analytics.
worker()    One badge: today's zone timeline and effectiveness.

Everything is read from the same tables the simulation writes; nothing here
is generated for display only."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..simulation import rfid
from . import alerts as alert_svc, calendar as cal, inventory, metrics, workforce
from .common import iso, pct

# Plant coordinates (lat, lon). Destination DCs are illustrative.
FACTORY_GEO = {"KNV": (36.1199, -80.0737), "PHX": (33.4484, -112.0740), "CMH": (39.9612, -82.9988)}
CUSTOMER_DC = {
    "WMT": ("Walmart DC · Bentonville, AR", 36.3729, -94.2088),
    "CST": ("Costco depot · Mira Loma, CA", 33.9845, -117.5159),
    "TGT": ("Target DC · Minneapolis, MN", 44.9778, -93.2650),
    "AMZ": ("Amazon FC · Fort Worth, TX", 32.7555, -97.3308),
    "SAM": ("Sam's Club DC · Kansas City, MO", 39.0997, -94.5786),
    "KOH": ("Kohl's DC · Menomonee Falls, WI", 43.1789, -88.1173),
    "MCY": ("Macy's DC · Joliet, IL", 41.5250, -88.0817),
}
TRANSIT_HOURS = {  # rough linehaul durations used to animate in-transit loads
    "KNV": 14, "PHX": 18, "CMH": 10,
}
LINE_ROLES = ("Filler", "Sewer", "Bagger")
ROLE_STAGE = {"Filler": "filling", "Sewer": "sewing", "Bagger": "bagging", "Packaging operator": "packing",
              "Supervisor": "floor", "Maintenance technician": "maintenance", "Warehouse associate": "warehouse",
              "Forklift operator": "palletizing"}
STAGE_DEFS = [
    # key, label, unit, roles, description
    ("raw", "Receiving & raw material", "kg", ("Forklift operator",), "Fibre bales and pillow shells received and staged line-side"),
    ("fibre", "Fibre opening & carding", "kg", ("Filler",), "Fibre opener → flapper → Garnett carding / blow unit build the fibre bun"),
    ("filling", "Pillow filling", "pillows", ("Filler",), "Filling head blows the fibre into the shell"),
    ("sewing", "Sewing & closing", "pillows", ("Sewer",), "Two sewers close the shell and finish the seam"),
    ("bagging", "Bagging & compression", "pillows", ("Bagger",), "Polybag, compress, label"),
    ("packing", "Carton packing (CV count)", "pillows", ("Packaging operator",), "Camera-verified pillow count per carton"),
    ("qa", "QA hold & rework", "pillows", ("Supervisor",), "Blocked cartons waiting for recount or rework"),
    ("pallet", "Palletizing & put-away", "pillows", ("Forklift operator",), "Approved cartons on pallets moving to racks"),
    ("fg", "Finished goods racks", "pillows", ("Warehouse associate",), "Stored pallets available or reserved for orders"),
    ("dispatch", "Picking, staging & loading", "pillows", ("Warehouse associate", "Forklift operator"), "Picked for orders and loaded on trucks"),
]


def current_shift(now: datetime) -> int:
    return cal.shift_of(now) or (2 if now.hour >= 23 else 1)


def _mins(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60)


# ─── RFID aggregates ─────────────────────────────────────────────────────
def badge_stats(db: Session, factory_ids: list[int], day: date, now: datetime, shift: int | None = None,
                employee_id: int | None = None) -> dict[int, dict]:
    """Per-employee totals for the day (or one shift) from the badge intervals."""
    q = select(m.RfidInterval).where(m.RfidInterval.factory_id.in_(factory_ids), m.RfidInterval.date == day)
    if shift:
        q = q.where(m.RfidInterval.shift == shift)
    if employee_id:
        q = q.where(m.RfidInterval.employee_id == employee_id)
    out: dict[int, dict] = {}
    for iv in db.scalars(q.order_by(m.RfidInterval.start)):
        s = out.get(iv.employee_id)
        if s is None:
            s = out[iv.employee_id] = {"station_min": 0.0, "away_min": 0.0, "break_min": 0.0, "away_trips": 0,
                                       "longest_away_min": 0.0, "overrun_min": 0.0, "no_read_min": 0.0,
                                       "first_read": iv.start, "current": None, "zones": defaultdict(float),
                                       "hourly_away": defaultdict(float)}
        end = iv.end or now
        mins = _mins(iv.start, min(end, now))
        if iv.state == "Station":
            s["station_min"] += mins
        elif iv.state == "Break":
            s["break_min"] += mins
        else:
            s["away_min"] += mins
            s["away_trips"] += 1
            s["longest_away_min"] = max(s["longest_away_min"], mins)
            base = (iv.zone or "").replace(" (overrun)", "")
            s["zones"][base if iv.reason != "Break overrun" else "Break overrun"] += mins
            if iv.reason == "Break overrun":
                s["overrun_min"] += mins
            if (iv.zone or "").startswith("No read"):
                s["no_read_min"] += mins
            t = iv.start
            while t < min(end, now):  # split by clock hour for the trend chart
                nxt = min(min(end, now), t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                s["hourly_away"][t.hour] += _mins(t, nxt)
                t = nxt
        if iv.end is None:
            s["current"] = iv
    return out


def _worker_row(e: m.Employee, st: dict | None, prod: dict | None, line_names: dict, now: datetime,
                shift: int) -> dict:
    cur = st["current"] if st else None
    started = cal.shift_bounds(now.date(), e.shift)[0] <= now
    if e.attendance not in ("Present", "Late"):
        state, zone = "Absent", "No badge read today"
    elif cur is not None:
        state, zone = cur.state, cur.zone
    elif not started:
        state, zone = "Scheduled", "Shift not started"
    elif e.attendance == "Late" and not st:
        state, zone = "Not arrived", "Late — no badge read yet"
    else:
        state, zone = "Off", "Shift ended"
    station = st["station_min"] if st else 0.0
    away = st["away_min"] if st else 0.0
    avail = round(100 * station / (station + away), 1) if station + away > 0 else None
    perf = prod.get("productivity_pct") if prod else None
    perf_c = min(perf, 115.0) if perf is not None else None
    eff = round(avail * perf_c / 100, 1) if (avail is not None and perf_c is not None) else avail
    since = cur.start if cur is not None else None
    in_state = round(_mins(since, now), 1) if since else None
    flag = None
    if state == "Away" and in_state is not None and in_state >= rfid.AWAY_ALERT_MIN:
        flag = f"Away {in_state:.0f} min ({zone})"
    elif avail is not None and station + away >= 60 and avail < 90:
        flag = f"Only {avail:.0f}% of working time at station"
    return {
        "id": e.id, "code": e.code, "name": e.name, "role": e.role, "stage": ROLE_STAGE.get(e.role),
        "line_id": e.line_id, "line": line_names.get(e.line_id), "shift": e.shift, "badge": rfid.badge_id(e),
        "employment_type": e.employment_type, "attendance": e.attendance,
        "state": state, "zone": zone, "reader": cur.reader if cur is not None else None,
        "reason": cur.reason if cur is not None else None, "since": iso(since), "minutes_in_state": in_state,
        "first_read": iso(st["first_read"]) if st else None,
        "station_min": round(station, 1), "away_min": round(away, 1),
        "break_min": round(st["break_min"], 1) if st else 0.0, "away_trips": st["away_trips"] if st else 0,
        "longest_away_min": round(st["longest_away_min"], 1) if st else 0.0,
        "overrun_min": round(st["overrun_min"], 1) if st else 0.0,
        "availability_pct": avail, "performance_pct": perf, "effectiveness_pct": eff,
        "units": prod.get("units") if prod else None,
        "pillows_per_hour": round(prod["units"] / (prod["productive_min"] / 60), 1)
        if prod and prod.get("units") and prod.get("productive_min", 0) >= 15 else None,
        "skill": e.skill, "flag": flag,
    }


# ─── network map ─────────────────────────────────────────────────────────
def network(db: Session, now: datetime) -> dict:
    day = now.date()
    shift = current_shift(now)
    comp = {r["factory_id"]: r for r in metrics.factory_comparison(db, day, now)}
    factories = list(db.scalars(select(m.Factory).order_by(m.Factory.id)))
    fids = [f.id for f in factories]
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))))
    open_alerts = alert_svc.open_alerts(db, fids, 500)
    machines = list(db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(fids))))
    stats = badge_stats(db, fids, day, now, shift)
    emps = list(db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(fids), m.Employee.shift == shift)))
    customers = {c.id: c for c in db.scalars(select(m.Customer))}
    out = []
    for f in factories:
        c = comp.get(f.id, {})
        fl = [ln for ln in lines if ln.factory_id == f.id]
        fa = [a for a in open_alerts if a.factory_id == f.id]
        fm = [mc for mc in machines if mc.factory_id == f.id]
        badges = {"on_floor": 0, "at_station": 0, "away": 0, "on_break": 0}
        station = away = 0.0
        for e in emps:
            if e.factory_id != f.id:
                continue
            s = stats.get(e.id)
            if not s:
                continue
            station += s["station_min"]
            away += s["away_min"]
            cur = s["current"]
            if cur is not None:
                badges["on_floor"] += 1
                badges[{"Station": "at_station", "Away": "away", "Break": "on_break"}[cur.state]] += 1
        crit = sum(1 for a in fa if a.severity == "critical")
        down = [ln for ln in fl if ln.status in ("Breakdown", "Maintenance")]
        status = "Critical" if (crit or down) else ("Watch" if fa or any(mc.health_score < 65 for mc in fm) else "Healthy")
        lat, lon = FACTORY_GEO.get(f.code, (None, None))
        out.append({
            "id": f.id, "code": f.code, "name": f.name, "state": f.state, "city": f"{f.city}, {f.state}",
            "lat": lat, "lon": lon, "status": status,
            "output": c.get("output"), "target": c.get("target"),
            "achievement_pct": pct(c.get("output") or 0, c.get("target") or 0),
            "efficiency_pct": c.get("efficiency_pct"), "packaging_accuracy_pct": c.get("packaging_accuracy_pct"),
            "shipment_readiness_pct": c.get("shipment_readiness_pct"), "downtime_min": c.get("downtime_min"),
            "lines_total": len(fl), "lines_running": sum(1 for ln in fl if ln.status == "Running"),
            "lines_down": [{"code": ln.code, "name": ln.name, "status": ln.status, "reason": ln.status_reason} for ln in down],
            "machines_at_risk": sum(1 for mc in fm if mc.health_score < 85),
            "alerts": {"critical": crit, "warning": sum(1 for a in fa if a.severity == "warning"), "total": len(fa)},
            "top_alert": fa[0].title if fa else None,
            "badges": badges,
            "rfid_availability_pct": round(100 * station / (station + away), 1) if station + away else None,
        })
    # loads on the road: departed trucks not yet delivered
    lanes = []
    q = (select(m.TruckAppointment, m.CustomerOrder).join(m.CustomerOrder, m.TruckAppointment.order_id == m.CustomerOrder.id)
         .where(m.TruckAppointment.status == "Departed", m.CustomerOrder.status == "Dispatched"))
    fmap = {f.id: f for f in factories}
    for t, o in db.execute(q):
        cust = customers.get(o.customer_id)
        dc = CUSTOMER_DC.get(cust.code if cust else "")
        f = fmap.get(t.factory_id)
        if not dc or not f or not t.departed_at:
            continue
        hours = TRANSIT_HOURS.get(f.code, 12)
        progress = min(0.97, _mins(t.departed_at, now) / 60 / hours)
        lanes.append({"truck": t.ref, "carrier": t.carrier, "factory": f.code, "customer": cust.name, "order": o.number,
                      "destination": dc[0], "lat": dc[1], "lon": dc[2], "pillows": t.loaded_qty or o.loaded_qty,
                      "departed_at": iso(t.departed_at), "progress": round(max(0.03, progress), 3)})
    lanes.sort(key=lambda x: x["departed_at"] or "", reverse=True)
    return {"sim_time": iso(now), "shift": shift, "factories": out,
            "destinations": [{"code": k, "name": v[0], "lat": v[1], "lon": v[2]} for k, v in CUSTOMER_DC.items()],
            "lanes": lanes[:24]}


# ─── facility twin ───────────────────────────────────────────────────────
def facility(db: Session, f: m.Factory, now: datetime) -> dict:
    day = now.date()
    shift = current_shift(now)
    fids = [f.id]
    s_start, s_end = cal.shift_bounds(day, shift)
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    customers = {c.id: c for c in db.scalars(select(m.Customer))}
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id == f.id).order_by(m.ProductionLine.id)))
    line_names = {ln.id: ln.name for ln in lines}
    wip = {w.line_id: w for w in db.scalars(select(m.LineWip).where(m.LineWip.line_id.in_([ln.id for ln in lines] or [0])))}
    hourly = defaultdict(lambda: {"day": 0.0, "shift": 0.0, "last_hour": 0.0, "planned": 0.0, "down": 0.0})
    for r in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.factory_id == f.id, m.ProductionHourly.date == day)):
        h = hourly[r.line_id]
        h["day"] += r.actual
        h["down"] += r.downtime_min
        if r.shift == shift:
            h["shift"] += r.actual
        if r.hour == now.hour - 1:
            h["last_hour"] += r.actual
    elapsed = cal.productive_minutes(datetime.combine(day, cal.DAY_START), now)
    machines = defaultdict(list)
    for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id == f.id).order_by(m.Machine.id)):
        machines[mc.line_id].append(mc)
    pos = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id == f.id)))
    po_by_id = {p.id: p for p in pos}
    pack_queue = defaultdict(int)
    for p in pos:
        if p.pack_queue:
            pack_queue[p.line_id] += p.pack_queue
    corders = {o.id: o for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id == f.id))}

    # people on this shift
    emps = list(db.scalars(select(m.Employee).where(m.Employee.factory_id == f.id, m.Employee.shift == shift)
                           .order_by(m.Employee.line_id, m.Employee.role, m.Employee.id)))
    stats = badge_stats(db, fids, day, now, shift)
    prod_rows = {r["id"]: r for r in workforce.employee_rows(db, fids, day, now, shift=shift)}
    workers = [_worker_row(e, stats.get(e.id), prod_rows.get(e.id), line_names, now, shift) for e in emps]
    wmap = {w["id"]: w for w in workers}

    def machine_row(mc):
        return {"id": mc.id, "code": mc.code, "name": mc.name, "type": mc.machine_type, "stage": mc.stage,
                "status": mc.status, "health": round(mc.health_score or 0, 1),
                "health_status": "Healthy" if (mc.health_score or 0) >= 85 else "Warning" if (mc.health_score or 0) >= 65 else "Critical",
                "vibration": mc.vibration, "temperature": mc.temperature}

    line_rows = []
    for ln in lines:
        h = hourly[ln.id]
        w = wip.get(ln.id)
        sew_wip = round(w.sewing) if w and w.date == day else 0
        bag_wip = round(w.bagging) if w and w.date == day else 0
        bagged = int(h["day"])
        sewn = bagged + bag_wip
        filled = sewn + sew_wip
        po = po_by_id.get(ln.current_po_id) if ln.current_po_id else None
        sku = skus.get(ln.current_sku_id) or (skus.get(po.sku_id) if po else None)
        fill_kg = sku.fill_kg if sku else 0.7
        co = corders.get(po.customer_order_id) if po and po.customer_order_id else None
        planned = ln.planned_rate * elapsed / 60
        crew = [x for x in workers if x["line_id"] == ln.id]
        mcs = machines.get(ln.id, [])
        stage_mc = {mc.stage: mc for mc in mcs}

        def crew_of(role):
            return [x["id"] for x in crew if x["role"] == role]
        sewers = [wmap[i] for i in crew_of("Sewer")]
        sewer_away = sum(x["away_min"] for x in sewers)
        line_rows.append({
            "id": ln.id, "code": ln.code, "name": ln.name, "type": ln.line_type, "status": ln.status,
            "status_reason": ln.status_reason, "since": iso(ln.status_since), "changeover_until": iso(ln.changeover_until),
            "rate_target": ln.planned_rate, "last_hour": int(h["last_hour"]), "output_today": bagged,
            "output_shift": int(h["shift"]), "planned_to_date": round(planned), "efficiency_pct": pct(bagged, planned),
            "downtime_min": round(h["down"]),
            "sku": sku.code if sku else None, "sku_name": sku.description if sku else None,
            "customer": customers[sku.customer_id].name if sku else None,
            "po": {"id": po.id, "number": po.number, "required": po.required_qty, "completed": po.completed_qty,
                   "progress_pct": pct(po.completed_qty, po.required_qty), "due": iso(po.required_completion),
                   "eta": iso(po.estimated_completion), "status": po.status,
                   "order": co.number if co else None} if po else None,
            "stages": {
                "fibre": {"kg_today": round(filled * fill_kg), "kg_per_hour": round(ln.planned_rate * fill_kg),
                          "pillow_equivalent": filled, "machines": [stage_mc[s].id for s in ("Mixing", "Flapper Processing", "Bun Formation") if s in stage_mc],
                          "workers": crew_of("Filler")},
                "filling": {"today": filled, "wip": 0, "workers": crew_of("Filler"),
                            "machines": [stage_mc["Pillow Filling"].id] if "Pillow Filling" in stage_mc else []},
                "sewing": {"today": sewn, "wip": sew_wip, "workers": crew_of("Sewer"), "machines": []},
                "bagging": {"today": bagged, "wip": bag_wip, "workers": crew_of("Bagger"), "machines": []},
                "packing": {"queue": pack_queue.get(ln.id, 0), "workers": crew_of("Packaging operator")},
            },
            "fill_kg": fill_kg,
            "machines": [machine_row(mc) for mc in mcs],
            "workers": [x["id"] for x in crew],
            "sewer_away_min": round(sewer_away, 1),
            "est_pillows_lost_to_away": round(sewer_away * ln.planned_rate / 60 * 0.5 * (ln.base_efficiency or 1)),
        })

    # downstream of the lines
    day0 = datetime.combine(day, datetime.min.time())
    cartons_today = list(db.scalars(select(m.Carton).where(m.Carton.factory_id == f.id, m.Carton.created_at >= day0)))
    packed_ok = [c for c in cartons_today if c.status in ("Passed", "Reworked", "Overridden")]
    blocked = list(db.scalars(select(m.Carton).where(m.Carton.factory_id == f.id, m.Carton.status == "Blocked")))
    shift_cartons = [c for c in packed_ok if c.created_at >= s_start]
    pallets = list(db.scalars(select(m.Pallet).where(m.Pallet.factory_id == f.id, m.Pallet.status.in_(("Open", "Pending Put-away")))))
    ppc = lambda p: skus[p.sku_id].pillows_per_carton if p.sku_id in skus else 1  # noqa: E731
    open_p = [p for p in pallets if p.status == "Open"]
    pend_p = [p for p in pallets if p.status == "Pending Put-away"]
    fg = inventory.fg_summary(db, fids)
    stored_shift = sum(p.cartons * ppc(p) for p in db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == f.id, m.Pallet.stored_at >= s_start)))
    picked = db.scalar(select(func.coalesce(func.sum(m.Allocation.pillows), 0)).join(m.CustomerOrder).where(
        m.CustomerOrder.factory_id == f.id, m.Allocation.status == "Picked")) or 0
    trucks = list(db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.factory_id == f.id,
                                                              m.TruckAppointment.scheduled_arrival >= day0 - timedelta(days=1))))
    departed_today = [t for t in trucks if t.departed_at and t.departed_at >= day0]
    loaded_today = sum(t.loaded_qty or 0 for t in departed_today)
    docks = []
    for d in ("D1", "D2", "D3", "D4"):
        t = next((x for x in trucks if x.dock == d and x.status in ("Arrived", "Loading", "Loaded")), None)
        nxt = min((x for x in trucks if x.dock == d and x.status == "Scheduled" and x.scheduled_arrival and x.scheduled_arrival >= now),
                  key=lambda x: x.scheduled_arrival, default=None)
        o = corders.get(t.order_id) if t else None
        docks.append({"dock": d, "truck": t.ref if t else None, "carrier": t.carrier if t else None, "status": t.status if t else "Free",
                      "order": o.number if o else None, "customer": customers[o.customer_id].name if o else None,
                      "order_qty": o.qty if o else None, "loaded_qty": (o.loaded_qty if o else 0),
                      "loaded_pct": pct(o.loaded_qty, o.qty) if o else None, "wait_min": round(t.wait_min or 0) if t else None,
                      "next": {"truck": nxt.ref, "at": iso(nxt.scheduled_arrival),
                               "customer": customers[nxt.customer_id].name if nxt.customer_id in customers else None} if nxt else None})
    inbound = list(db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id == f.id,
                                                              m.InboundShipment.status.in_(("Arrived", "Receiving")))))
    stock = list(db.scalars(select(m.MaterialStock).where(m.MaterialStock.factory_id == f.id)))
    mats = {x.id: x for x in db.scalars(select(m.Material))}
    fibre_kg = sum(s.on_hand for s in stock if mats.get(s.material_id) and mats[s.material_id].category in ("Fibre", "Foam"))
    shells = sum(s.on_hand for s in stock if mats.get(s.material_id) and mats[s.material_id].category == "Shell")

    # racks: occupied slots by zone/bay/rack/level
    occ = inventory.location_occupancy(db, fids)
    racks = defaultdict(list)
    for loc in db.scalars(select(m.StorageLocation).where(m.StorageLocation.factory_id == f.id).order_by(m.StorageLocation.id)):
        if occ.get(loc.id):
            racks[loc.zone].append([loc.bay, loc.rack, loc.level])

    # factory pillow flow with the people at each step
    tot = lambda key: sum(r["stages"][key]["today"] for r in line_rows)  # noqa: E731
    wip_sum = lambda key: sum(r["stages"][key]["wip"] for r in line_rows)  # noqa: E731
    pallet_pillows = sum(p.cartons * ppc(p) for p in open_p) + sum(p.cartons * ppc(p) for p in pend_p)
    held = sum(p.held_qty or 0 for p in pos)
    shift_hours = max(0.25, _mins(s_start, min(now, s_end)) / 60)
    flow_vals = {
        "raw": {"now": round(fibre_kg), "today": None, "unit": "kg fibre", "extra": f"{round(shells):,} shells on hand"},
        "fibre": {"now": None, "today": round(sum(r["stages"]["fibre"]["kg_today"] for r in line_rows)), "unit": "kg opened",
                  "extra": f"≈ {tot('filling'):,} pillows of fibre"},
        "filling": {"now": 0, "today": tot("filling")},
        "sewing": {"now": wip_sum("sewing"), "today": tot("sewing")},
        "bagging": {"now": wip_sum("bagging"), "today": tot("bagging")},
        "packing": {"now": sum(pack_queue.values()), "today": sum(c.required_count or 0 for c in packed_ok),
                    "extra": f"{len(packed_ok):,} cartons approved today"},
        "qa": {"now": held, "today": None, "extra": f"{len(blocked)} blocked cartons"},
        "pallet": {"now": pallet_pillows, "today": None, "extra": f"{len(open_p)} open · {len(pend_p)} to put away"},
        "fg": {"now": fg["total_pillows"], "today": None, "extra": f"{fg['stored_pallets']} pallets · {fg['utilization_pct']:.0f}% full"},
        "dispatch": {"now": int(picked), "today": loaded_today, "extra": f"{len(departed_today)} trucks departed today"},
    }
    shift_tp = {  # throughput this shift for pillows-per-worker-hour
        "filling": sum(r["output_shift"] for r in line_rows) + wip_sum("sewing") + wip_sum("bagging"),
        "sewing": sum(r["output_shift"] for r in line_rows) + wip_sum("bagging"),
        "bagging": sum(r["output_shift"] for r in line_rows),
        "packing": sum(c.required_count or 0 for c in shift_cartons),
        "pallet": sum(c.required_count or 0 for c in shift_cartons),
        "fg": stored_shift,
        "dispatch": sum(t.loaded_qty or 0 for t in departed_today if t.departed_at >= s_start),
    }
    flow = []
    for key, label, unit, roles, desc in STAGE_DEFS:
        crew = [w for w in workers if w["role"] in roles and w["state"] not in ("Absent", "Scheduled")]
        if key == "fibre":
            crew = []  # fillers are counted once, at filling
        at = sum(1 for w in crew if w["state"] == "Station")
        st_h = sum(w["station_min"] for w in crew) / 60
        aw = sum(w["away_min"] for w in crew)
        tp = shift_tp.get(key)
        v = flow_vals[key]
        flow.append({"key": key, "label": label, "unit": v.get("unit", unit), "description": desc,
                     "now": v.get("now"), "today": v.get("today"), "extra": v.get("extra"),
                     "roles": list(roles), "workers": len(crew), "at_station": at,
                     "away": sum(1 for w in crew if w["state"] == "Away"),
                     "on_break": sum(1 for w in crew if w["state"] == "Break"),
                     "away_min": round(aw), "availability_pct": round(100 * st_h * 60 / (st_h * 60 + aw), 1) if st_h * 60 + aw else None,
                     "per_worker_hour": round(tp / st_h, 1) if tp and st_h >= 0.25 else None})

    return {
        "sim_time": iso(now), "shift": shift, "shift_window": [iso(s_start), iso(s_end)],
        "shift_hours_elapsed": round(shift_hours, 2),
        "factory": {"id": f.id, "code": f.code, "name": f.name, "state": f.state, "city": f"{f.city}, {f.state}",
                    "daily_target": f.daily_target},
        "lines": line_rows,
        "packing": {"queue": sum(pack_queue.values()), "cartons_today": len(packed_ok),
                    "pillows_today": flow_vals["packing"]["today"], "blocked_cartons": len(blocked), "held_pillows": held,
                    "accuracy_pct": round(100 * sum(1 for c in cartons_today if c.first_pass and c.status == "Passed")
                                          / len(cartons_today), 2) if cartons_today else 100.0,
                    "blocked": [{"id": c.id, "code": c.hold_code, "line_id": c.line_id, "result": c.result,
                                 "detected": c.detected_count, "required": c.required_count} for c in blocked[:12]]},
        "palletizing": {"open_pallets": len(open_p), "pending_putaway": len(pend_p), "pillows": pallet_pillows},
        "warehouse": {**fg, "racks": racks, "zones": ["A", "B", "C"], "bays": 4, "racks_per_bay": 3, "levels": 4},
        "docks": docks,
        "receiving": [{"ref": s.ref, "carrier": s.carrier, "material": mats[s.material_id].code if s.material_id in mats else None,
                       "qty": s.qty, "status": s.status, "dock": s.dock} for s in inbound],
        "raw": {"fibre_kg": round(fibre_kg), "shells": round(shells)},
        "flow": flow,
        "workers": workers,
        "rfid": rfid_summary(workers, line_rows, stats, shift, now),
        "disclaimer": "RFID badge reads and all operations are simulated for this demo.",
    }


def rfid_summary(workers: list[dict], line_rows: list[dict], stats: dict, shift: int, now: datetime) -> dict:
    on = [w for w in workers if w["state"] in ("Station", "Away", "Break")]
    worked = [w for w in workers if w["station_min"] + w["away_min"] > 0]
    zones = defaultdict(float)
    hourly = defaultdict(float)
    for w in worked:
        s = stats.get(w["id"])
        if not s:
            continue
        for z, v in s["zones"].items():
            zones[z] += v
        for h, v in s["hourly_away"].items():
            hourly[h] += v
    by_role = defaultdict(lambda: {"workers": 0, "station_min": 0.0, "away_min": 0.0, "trips": 0, "eff": []})
    for w in worked:
        r = by_role[w["role"]]
        r["workers"] += 1
        r["station_min"] += w["station_min"]
        r["away_min"] += w["away_min"]
        r["trips"] += w["away_trips"]
        if w["effectiveness_pct"] is not None:
            r["eff"].append(w["effectiveness_pct"])
    station = sum(w["station_min"] for w in worked)
    away = sum(w["away_min"] for w in worked)
    readers = sum(1 + 2 + 1 for _ in line_rows) + sum(1 for w in workers if w["role"] == "Packaging operator") + 11
    hours = sorted(hourly)
    return {
        "readers": readers, "badges_on_floor": len(on),
        "at_station": sum(1 for w in on if w["state"] == "Station"),
        "away": sum(1 for w in on if w["state"] == "Away"),
        "on_break": sum(1 for w in on if w["state"] == "Break"),
        "absent": sum(1 for w in workers if w["state"] in ("Absent", "Not arrived")),
        "availability_pct": round(100 * station / (station + away), 1) if station + away else None,
        "away_hours": round(away / 60, 1), "station_hours": round(station / 60, 1),
        "avg_away_min_per_worker": round(away / len(worked), 1) if worked else 0,
        "trips": sum(w["away_trips"] for w in worked),
        "long_trips_now": [w["id"] for w in on if w["state"] == "Away" and (w["minutes_in_state"] or 0) >= rfid.AWAY_ALERT_MIN],
        "est_pillows_lost_to_away": sum(r["est_pillows_lost_to_away"] for r in line_rows),
        "zones": sorted([{"zone": z, "minutes": round(v)} for z, v in zones.items()], key=lambda x: -x["minutes"]),
        "hourly": [{"hour": f"{h:02d}:00", "away_min": round(hourly[h])} for h in hours],
        "by_role": sorted([{"role": k, "workers": v["workers"], "away_min": round(v["away_min"]), "trips": v["trips"],
                            "availability_pct": round(100 * v["station_min"] / (v["station_min"] + v["away_min"]), 1)
                            if v["station_min"] + v["away_min"] else None,
                            "avg_away_min": round(v["away_min"] / v["workers"], 1) if v["workers"] else 0,
                            "effectiveness_pct": round(sum(v["eff"]) / len(v["eff"]), 1) if v["eff"] else None}
                           for k, v in by_role.items()], key=lambda x: -x["away_min"]),
        "top_away": [w["id"] for w in sorted(worked, key=lambda w: -w["away_min"])[:8]],
        "alert_threshold_min": rfid.AWAY_ALERT_MIN,
    }


# ─── one badge ───────────────────────────────────────────────────────────
def worker(db: Session, e: m.Employee, now: datetime) -> dict:
    day = now.date()
    line = db.get(m.ProductionLine, e.line_id) if e.line_id else None
    ivs = list(db.scalars(select(m.RfidInterval).where(m.RfidInterval.employee_id == e.id, m.RfidInterval.date == day)
                          .order_by(m.RfidInterval.start)))
    st = badge_stats(db, [e.factory_id], day, now, employee_id=e.id).get(e.id)
    prod = next((r for r in workforce.employee_rows(db, [e.factory_id], day, now, shift=e.shift) if r["id"] == e.id), None)
    row = _worker_row(e, st, prod, {line.id: line.name} if line else {}, now, e.shift)
    # role peers on the same shift for comparison
    peers = list(db.scalars(select(m.Employee).where(m.Employee.factory_id == e.factory_id, m.Employee.role == e.role,
                                                     m.Employee.shift == e.shift)))
    pst = badge_stats(db, [e.factory_id], day, now, e.shift)
    peer_away = [pst[p.id]["away_min"] for p in peers if p.id in pst]
    hist = []
    for back in range(5, 0, -1):
        d = day - timedelta(days=back)
        s = badge_stats(db, [e.factory_id], d, now, employee_id=e.id).get(e.id)
        if s:
            hist.append({"date": d.isoformat(), "away_min": round(s["away_min"], 1), "trips": s["away_trips"],
                         "availability_pct": round(100 * s["station_min"] / (s["station_min"] + s["away_min"]), 1)
                         if s["station_min"] + s["away_min"] else None})
    s_start, s_end = cal.shift_bounds(day, e.shift)
    return {
        "sim_time": iso(now), "worker": row, "line": line.name if line else None,
        "shift_window": [iso(s_start), iso(s_end)],
        "propensity_note": "Badge reads simulated; scheduled breaks and lunch are never counted as time away.",
        "timeline": [{"state": iv.state, "zone": iv.zone, "reader": iv.reader, "reason": iv.reason,
                      "start": iso(iv.start), "end": iso(iv.end), "open": iv.end is None,
                      "minutes": round(_mins(iv.start, min(iv.end or now, now)), 1)} for iv in ivs],
        "zones": sorted([{"zone": z, "minutes": round(v, 1)} for z, v in (st["zones"].items() if st else [])],
                        key=lambda x: -x["minutes"]),
        "peer_avg_away_min": round(sum(peer_away) / len(peer_away), 1) if peer_away else None,
        "peer_count": len(peer_away),
        "history": hist,
    }


# ─── S!AP advisories from badge data ─────────────────────────────────────
def workforce_advisories(db: Session, factory_ids: list[int], now: datetime, limit: int | None = None) -> list[dict]:
    """RFID-based workforce advisories in the S!AP advisory format."""
    day = now.date()
    shift = current_shift(now)
    out = []
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(factory_ids)))}
    fmap = {f.id: f for f in db.scalars(select(m.Factory).where(m.Factory.id.in_(factory_ids)))}
    stats = badge_stats(db, factory_ids, day, now, shift)
    emps = {e.id: e for e in db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(factory_ids), m.Employee.shift == shift))}
    rows = []
    for eid, s in stats.items():
        e = emps.get(eid)
        if not e:
            continue
        worked = s["station_min"] + s["away_min"]
        rows.append((e, s, worked))
    by_f = defaultdict(list)
    for e, s, worked in rows:
        by_f[e.factory_id].append((e, s, worked))
    for fid, items in by_f.items():
        f = fmap[fid]
        tot_station = sum(s["station_min"] for _, s, _ in items)
        tot_away = sum(s["away_min"] for _, s, _ in items)
        avg_away = tot_away / len(items) if items else 0
        # 1) people away right now beyond the threshold
        for e, s, _ in items:
            cur = s["current"]
            if cur is not None and cur.state == "Away":
                mins = _mins(cur.start, now)
                if mins >= rfid.AWAY_ALERT_MIN and e.role in ("Sewer", "Filler", "Bagger", "Packaging operator"):
                    ln = lines.get(e.line_id)
                    sev = "warning" if e.role in ("Sewer", "Filler") else "info"
                    lost = round(mins * ln.planned_rate / 60 * 0.5) if (ln and e.role == "Sewer") else 0
                    out.append(_adv(f"rfid:away:{e.code}", "Workforce · RFID", sev, f,
                                    f"{e.name} ({e.role}, {ln.name if ln else 'plant'}) away from station {mins:.0f} min",
                                    f"Badge last read at {cur.zone}. Station unattended since {cur.start:%H:%M}.",
                                    "Supervisor to check on the operator and cover the station with a floater.",
                                    evidence=[f"Reader {cur.reader or 'no read'} · zone {cur.zone}",
                                              f"{s['away_trips']} trips, {s['away_min']:.0f} min away this shift"],
                                    impact={"pillows_at_risk": lost, "downtime_hours": round(mins / 60, 2), "cost_usd": None, "orders": []},
                                    link=("production", "line", e.line_id) if e.line_id else None, confidence=0.95))
        # 2) chronic time away this shift (>= 2x plant average and >= 25 min)
        for e, s, worked in sorted(items, key=lambda x: -x[1]["away_min"])[:2]:
            if worked >= 90 and s["away_min"] >= max(25, 2 * avg_away):
                ln = lines.get(e.line_id)
                avail = 100 * s["station_min"] / worked
                top_zone = max(s["zones"].items(), key=lambda kv: kv[1])[0] if s["zones"] else "—"
                out.append(_adv(f"rfid:chronic:{e.code}", "Workforce · RFID", "info", f,
                                f"{e.name} ({e.role}) away {s['away_min']:.0f} min this shift — {s['away_min'] / max(avg_away, 1):.1f}× plant average",
                                f"{s['away_trips']} trips, longest {s['longest_away_min']:.0f} min; at station {avail:.0f}% of working time. "
                                f"Most time away: {top_zone}.",
                                "Review with the operator; if trips are to the material store, move line-side stock closer.",
                                evidence=[f"Plant average {avg_away:.0f} min away per person this shift"],
                                impact={"pillows_at_risk": round(s['away_min'] * ln.planned_rate / 60 * 0.5) if (ln and e.role == 'Sewer') else 0,
                                        "downtime_hours": round(s["away_min"] / 60, 2), "cost_usd": None, "orders": []},
                                link=("production", "line", e.line_id) if e.line_id else None, confidence=0.9))
        # 3) material-store trips → line-side replenishment
        store = sum(s["zones"].get("Material store", 0) for _, s, _ in items)
        if tot_away > 0 and store / tot_away >= 0.22 and store >= 30:
            out.append(_adv(f"rfid:store:{f.code}", "Workforce · RFID", "info", f,
                            f"{f.name}: {store:.0f} min of operator time spent walking to the material store",
                            f"{100 * store / tot_away:.0f}% of all unscheduled time away this shift is material-store trips.",
                            "Introduce line-side kanban for shells, polybags and labels so operators stay at the station.",
                            evidence=[f"RFID store reader dwell {store:.0f} min across {len(items)} badges"],
                            impact={"pillows_at_risk": None, "downtime_hours": round(store / 60, 1), "cost_usd": None, "orders": []},
                            link=None, confidence=0.85))
        # 4) break overruns
        over = sum(s["overrun_min"] for _, s, _ in items)
        if over >= 20:
            out.append(_adv(f"rfid:overrun:{f.code}", "Workforce · RFID", "info", f,
                            f"{f.name}: break overruns cost {over:.0f} min this shift",
                            f"{sum(1 for _, s, _ in items if s['overrun_min'] > 0)} people returned late from breaks or lunch.",
                            "Stagger break returns by line and announce the end of break on the floor.",
                            evidence=[f"Plant availability {100 * tot_station / (tot_station + tot_away):.1f}% this shift" if tot_station + tot_away else "—"],
                            impact={"pillows_at_risk": None, "downtime_hours": round(over / 60, 1), "cost_usd": None, "orders": []},
                            link=None, confidence=0.85))
    rank = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (rank.get(a["severity"], 3), -((a.get("impact") or {}).get("pillows_at_risk") or 0)))
    return out[:limit] if limit else out


def _adv(id_, category, severity, f, title, summary, recommendation, evidence, impact, link, confidence):
    return {"id": id_, "category": category, "severity": severity, "title": title, "summary": summary,
            "factory": f.code, "factory_id": f.id, "machine": None, "prediction": None, "impact": impact,
            "recommendation": recommendation, "window": None, "confidence": confidence, "evidence": evidence,
            "actions": [], "link": {"page": link[0], "entity_type": link[1], "entity_id": link[2]} if link else None}
