"""Demo data for Indocount's US operations (all simulated).

The seed builds the state at 06:00 on the demo day, then runs the
simulation engine through the morning so that today's production, cartons,
telemetry, workforce activity, put-away, picking and truck events are
produced by the same logic the live simulation uses."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m
from .auth import DEMO_USERS, hash_password
from .config import get_settings
from .db import Base
from .services import calendar as cal
from .services import inventory
from .services.machine_health import BASELINES, reading
from .simulation import engine

FACTORIES = [
    # code, name, state, tz, garnett rate, blow rate, pick rate
    ("KNV", "Kernersville", "NC", "America/New_York", 170, 150, 420),
    ("PHX", "Phoenix", "AZ", "America/Phoenix", 125, 105, 330),
    ("CMH", "Columbus", "OH", "America/New_York", 105, 95, 300),
]
CUSTOMERS = [
    ("WMT", "Walmart", "Mass retail", 1), ("CST", "Costco", "Club", 1), ("TGT", "Target", "Mass retail", 2),
    ("AMZ", "Amazon", "E-commerce", 1), ("SAM", "Sam's Club", "Club", 2), ("KOH", "Kohl's", "Department store", 3),
    ("MCY", "Macy's", "Department store", 3),
]
MATERIALS = [
    ("F-101", "Polyester fibre 7D conjugated", "Fibre", "kg", 2.10),
    ("F-104", "Polyester fibre 15D hollow siliconized", "Fibre", "kg", 2.45),
    ("FM-201", "Shredded memory foam", "Foam", "kg", 3.80),
    ("SH-301", "Pillow shell — Standard/Queen", "Shell", "pcs", 1.15),
    ("SH-302", "Pillow shell — King", "Shell", "pcs", 1.40),
    ("PB-401", "Polybag, printed", "Bag", "pcs", 0.06),
    ("CB-501", "Carton box, multi-pack", "Carton", "pcs", 1.10),
    ("CB-502", "Gaylord carton 48×40×48", "Carton", "pcs", 14.50),
    ("LB-601", "GS1-128 carton label", "Label", "pcs", 0.03),
]
REORDER_DAYS = {"Fibre": 4, "Foam": 5, "Shell": 12, "Bag": 4, "Carton": 4, "Label": 3}
# code, customer, description, size, fill, kg, shell, ppc, packaging, carton, labels, cartons/pallet, price
SKUS = [
    ("WMT-MS-Q", "WMT", "Mainstays Standard/Queen pillow", "Queen", "F-101", 0.62, "SH-301", 7, "Polybag, compressed", "CB-501",
     "GS1-128 carton label + Walmart item no.; retail UPC per pillow", 40, 6.40),
    ("WMT-HM-K", "WMT", "Hotel Style King pillow", "King", "F-104", 0.95, "SH-302", 6, "Polybag, compressed", "CB-501",
     "GS1-128 carton label + Walmart item no.", 36, 9.80),
    ("CST-KS-Q", "CST", "Kirkland Signature down-alternative Queen", "Queen", "F-104", 0.85, "SH-301", 60, "Polybag, vacuum compressed",
     "CB-502", "Costco item # + pallet tag on Gaylord", 2, 8.90),
    ("CST-MF-Q", "CST", "Shredded memory foam Queen", "Queen", "FM-201", 1.10, "SH-301", 60, "Vacuum roll-pack", "CB-502",
     "Costco item # + pallet tag on Gaylord", 2, 12.50),
    ("TGT-TH-S", "TGT", "Threshold Standard pillow", "Standard", "F-101", 0.60, "SH-301", 4, "Polybag", "CB-501",
     "Target DPCI carton label; UPC per pillow", 48, 7.10),
    ("TGT-TH-K", "TGT", "Threshold King pillow", "King", "F-104", 0.90, "SH-302", 4, "Polybag", "CB-501",
     "Target DPCI carton label; UPC per pillow", 40, 9.20),
    ("AMZ-BAS-Q2", "AMZ", "Amazon Basics Queen 2-pack", "Queen", "F-101", 0.65, "SH-301", 2, "Vacuum compressed 2-pack", "CB-501",
     "FNSKU label + carton content label", 60, 11.40),
    ("AMZ-BAS-K2", "AMZ", "Amazon Basics King 2-pack", "King", "F-104", 0.92, "SH-302", 2, "Vacuum compressed 2-pack", "CB-501",
     "FNSKU label + carton content label", 54, 13.90),
    ("SAM-MM-Q", "SAM", "Member's Mark Queen", "Queen", "F-101", 0.68, "SH-301", 48, "Polybag, compressed", "CB-502",
     "Sam's item # + pallet tag on Gaylord", 2, 7.60),
    ("KOH-SON-S", "KOH", "Sonoma Standard pillow", "Standard", "F-101", 0.60, "SH-301", 6, "Polybag", "CB-501",
     "Kohl's carton label; UPC per pillow", 40, 7.30),
    ("MCY-HTL-Q", "MCY", "Hotel Collection Queen", "Queen", "F-104", 0.90, "SH-301", 4, "Zippered bag", "CB-501",
     "Macy's carton label; RFID tag per pillow", 48, 14.20),
    ("MCY-HTL-K", "MCY", "Hotel Collection King", "King", "F-104", 1.00, "SH-302", 4, "Zippered bag", "CB-501",
     "Macy's carton label; RFID tag per pillow", 40, 16.40),
]
SUPPLIERS = [
    ("SUP-01", "Carolina Fibre Corp", "Greensboro, NC", 4, ["F-101", "F-104"]),
    ("SUP-02", "Southwest Polymers", "Mesa, AZ", 5, ["F-101", "F-104"]),
    ("SUP-03", "Midwest Foam Products", "Dayton, OH", 6, ["FM-201"]),
    ("SUP-04", "Indocount Textiles (India)", "Kolhapur, IN", 35, ["SH-301", "SH-302"]),
    ("SUP-05", "PackRight Packaging", "Charlotte, NC", 3, ["PB-401", "CB-501", "CB-502"]),
    ("SUP-06", "Desert Corrugated", "Phoenix, AZ", 3, ["CB-501", "CB-502", "PB-401"]),
    ("SUP-07", "LabelTech Inc", "Columbus, OH", 2, ["LB-601", "PB-401"]),
]
SUPPLIER_FOR = {  # (factory, material) -> supplier
    ("KNV", "F-101"): "SUP-01", ("KNV", "F-104"): "SUP-02", ("PHX", "F-101"): "SUP-02", ("PHX", "F-104"): "SUP-02",
    ("CMH", "F-101"): "SUP-01", ("CMH", "F-104"): "SUP-01", ("KNV", "PB-401"): "SUP-05", ("PHX", "PB-401"): "SUP-06",
    ("CMH", "PB-401"): "SUP-07", ("PHX", "CB-501"): "SUP-06", ("PHX", "CB-502"): "SUP-06",
}
LATE_PROFILE = {"SUP-02": (0.5, 1.0, 3.0), "SUP-04": (0.35, 2.0, 6.0)}
FIRST = ["Maria", "James", "Ana", "Luis", "Keisha", "David", "Rosa", "Kevin", "Linh", "Carlos", "Tasha", "Juan", "Emily",
         "Andre", "Priya", "Miguel", "Grace", "Omar", "Sofia", "Darnell", "Mei", "Victor", "Hannah", "Jose", "Aaliyah",
         "Ricardo", "Nicole", "Tyrone", "Elena", "Brandon", "Fatima", "Diego", "Jasmine", "Hector", "Kim", "Marcus",
         "Yolanda", "Raj", "Teresa", "Samuel"]
ROLE_WAGE = {"Filler": (17.5, 19.0), "Sewer": (16.0, 18.5), "Bagger": (15.5, 16.75), "Packaging operator": (16.0, 17.25),
             "Supervisor": (27.0, 32.0), "Maintenance technician": (26.0, 31.0), "Warehouse associate": (16.5, 18.5),
             "Forklift operator": (18.0, 20.0)}
CARRIERS = engine.CARRIERS

# Order book per factory: (sku, qty, ship day offset, ship hour, stage, line index, completed at 06:00)
KNV_ORDERS = [
    ("WMT-MS-Q", 4200, -5, 10.0, "delivered", None, 0), ("CST-KS-Q", 3600, -3, 14.0, "delivered", None, 0),
    ("TGT-TH-S", 2400, -1, 9.0, "dispatched", None, 0), ("AMZ-BAS-Q2", 3000, 0, 7.5, "ready", None, 0),
    ("MCY-HTL-Q", 2000, 0, 13.0, "in_inventory", None, 0),
    ("WMT-MS-Q", 4200, 0, 14.0, "producing", 3, 3010),       # ORD-501: Garnett Line 03 (underperforming)
    ("WMT-HM-K", 3600, 1, 10.0, "producing", 1, 2400),       # Garnett Line 01 (M-102 degrading)
    ("CST-KS-Q", 4800, 2, 9.0, "producing", 2, 1200), ("KOH-SON-S", 2400, 2, 15.0, "producing", 6, 600),
    ("TGT-TH-S", 3200, 3, 11.0, "producing", 4, 800), ("AMZ-BAS-Q2", 3600, 3, 14.0, "producing", 7, 3000),
    ("MCY-HTL-Q", 2400, 4, 10.0, "producing", 5, 200), ("WMT-MS-Q", 4900, 5, 10.0, "producing", 8, 0),
    ("CST-KS-Q", 3000, 6, 13.0, "queued", 2, 0), ("TGT-TH-S", 2400, 7, 9.0, "queued", 4, 0),
    ("WMT-HM-K", 3000, 8, 15.0, "queued", 1, 0), ("KOH-SON-S", 2400, 9, 11.0, "queued", 7, 0),
    ("KOH-SON-S", 2400, 10, 14.0, "queued", 3, 0),
    ("KOH-SON-S", 1800, 2, 10.0, "queued", 8, 0),          # urgent order queued behind a long run (optimizer demo)
]
PHX_ORDERS = [
    ("AMZ-BAS-Q2", 2400, -4, 11.0, "delivered", None, 0), ("CST-MF-Q", 3000, -2, 9.0, "delivered", None, 0),
    ("TGT-TH-K", 2000, -1, 15.0, "dispatched", None, 0), ("WMT-MS-Q", 2800, 0, 9.5, "ready", None, 0),
    ("SAM-MM-Q", 2880, 0, 16.0, "in_inventory", None, 0),
    ("AMZ-BAS-K2", 2400, 1, 8.0, "producing", 1, 1500), ("CST-MF-Q", 3600, 1, 15.0, "producing", 2, 1800),
    ("TGT-TH-K", 2800, 2, 10.0, "producing", 3, 900), ("WMT-MS-Q", 3500, 3, 9.0, "producing", 4, 700),
    ("AMZ-BAS-Q2", 3000, 3, 13.0, "producing", 5, 300), ("SAM-MM-Q", 2880, 4, 11.0, "producing", 6, 480),
    ("CST-MF-Q", 3000, 5, 10.0, "producing", 7, 0), ("TGT-TH-K", 2400, 6, 14.0, "producing", 8, 0),
    ("AMZ-BAS-K2", 2400, 7, 9.0, "queued", 1, 0), ("WMT-MS-Q", 2800, 8, 10.0, "queued", 4, 0),
]
CMH_ORDERS = [
    ("KOH-SON-S", 2400, -3, 10.0, "delivered", None, 0), ("MCY-HTL-K", 1600, -2, 13.0, "dispatched", None, 0),
    ("WMT-HM-K", 2400, 0, 10.0, "in_inventory", None, 0), ("SAM-MM-Q", 1920, 0, 15.5, "in_inventory", None, 0),
    ("WMT-MS-Q", 2800, 1, 11.0, "producing", 1, 1400), ("TGT-TH-S", 2400, 1, 16.0, "producing", 2, 1300),
    ("MCY-HTL-K", 2000, 2, 9.0, "producing", 3, 600), ("KOH-SON-S", 2400, 2, 14.0, "producing", 4, 500),
    ("SAM-MM-Q", 2880, 3, 10.0, "producing", 5, 480), ("WMT-HM-K", 2400, 4, 13.0, "producing", 6, 240),
    ("TGT-TH-S", 2000, 5, 9.0, "producing", 7, 0), ("WMT-MS-Q", 2800, 6, 11.0, "producing", 8, 0),
    ("MCY-HTL-K", 1600, 7, 15.0, "queued", 3, 0), ("KOH-SON-S", 2400, 8, 10.0, "queued", 4, 0),
]
ORDER_BOOK = {"KNV": KNV_ORDERS, "PHX": PHX_ORDERS, "CMH": CMH_ORDERS}
STOCK_SKUS = {"KNV": ["TGT-TH-K", "AMZ-BAS-K2", "SAM-MM-Q", "MCY-HTL-K", "CST-MF-Q"],
              "PHX": ["KOH-SON-S", "MCY-HTL-Q", "WMT-HM-K", "TGT-TH-S"],
              "CMH": ["AMZ-BAS-Q2", "TGT-TH-K", "MCY-HTL-Q", "CST-KS-Q"]}
STOCK_PALLETS = {"KNV": 60, "PHX": 50, "CMH": 45}


def reset_all(db: Session) -> None:
    """Delete all rows (schema is managed by Alembic)."""
    db.execute(text("UPDATE production_lines SET current_po_id = NULL"))
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()


def at(day: date, hours: float) -> datetime:
    return datetime.combine(day, time(0, 0)) + timedelta(hours=hours)


def seed(db: Session, base_date: date | None = None) -> None:
    s = get_settings()
    rng = random.Random(s.sim_seed)
    engine.reseed(s.sim_seed)
    today = base_date or date.today()
    day_start = datetime.combine(today, cal.DAY_START)

    for username, name, role, pw in DEMO_USERS:
        db.add(m.User(username=username, full_name=name, role=role, password_hash=hash_password(pw)))

    customers = {}
    for code, name, channel, tier in CUSTOMERS:
        customers[code] = m.Customer(code=code, name=name, channel=channel, priority_tier=tier)
        db.add(customers[code])
    materials = {}
    for code, name, cat, uom, cost in MATERIALS:
        materials[code] = m.Material(code=code, name=name, category=cat, uom=uom, unit_cost=cost)
        db.add(materials[code])
    db.flush()
    skus = {}
    for (code, cust, desc, size, fill, kg, shell, ppc, pack, carton, labels, cpp, price) in SKUS:
        skus[code] = m.Sku(code=code, customer_id=customers[cust].id, description=desc, size=size,
                           fill_material_id=materials[fill].id, fill_kg=kg, shell_material_id=materials[shell].id,
                           bag_material_id=materials["PB-401"].id, carton_material_id=materials[carton].id,
                           label_material_id=materials["LB-601"].id, pillows_per_carton=ppc, packaging_type=pack,
                           carton_type="Gaylord 48×40×48" if carton == "CB-502" else f"Corrugated {ppc}-pack carton",
                           label_requirements=labels, cartons_per_pallet=cpp, unit_price=price)
        db.add(skus[code])
    suppliers = {}
    for code, name, loc, lead, mats in SUPPLIERS:
        suppliers[code] = m.Supplier(code=code, name=name, location=loc, lead_time_days=lead, material_codes=mats,
                                     contact=f"orders@{name.split()[0].lower()}.example")
        db.add(suppliers[code])
    db.flush()

    factories, lines_by_f = {}, {}
    machine_seq = 0
    for fi, (code, name, st, tz, g_rate, b_rate, pick) in enumerate(FACTORIES, start=1):
        f = m.Factory(code=code, name=name, city=name, state=st, timezone=tz, pick_rate_cartons_hr=pick, wh_productivity=1.0)
        db.add(f)
        db.flush()
        factories[code] = f
        lines = []
        for i in range(8):
            garnett = i < 5
            num = i + 1 if garnett else i - 4
            ln = m.ProductionLine(code=f"{code}-{'GL' if garnett else 'BL'}{num:02d}",
                                  name=f"{'Garnett' if garnett else 'Blow'} Line {num:02d}", factory_id=f.id,
                                  line_type="Garnett" if garnett else "Blow", planned_rate=g_rate if garnett else b_rate,
                                  base_efficiency=round(rng.uniform(0.965, 1.02), 3), perf_modifier=1.0,
                                  changeover_factor=round(rng.uniform(0.9, 1.15), 2), status="Running", status_since=day_start)
            db.add(ln)
            lines.append(ln)
        lines_by_f[code] = lines
        db.flush()
        # special line behaviour
        if code == "KNV":
            lines[2].base_efficiency = 0.97         # Garnett Line 03 (sewing constrained, see employees)
            lines[6].changeover_factor = 2.2        # Blow Line 02: slow changeovers
        if code == "PHX":
            lines[3].changeover_factor = 1.9
        f.daily_target = int(sum(ln.planned_rate for ln in lines) * cal.productive_minutes_per_shift() * 2 / 60)
        # machines
        n = 0
        for ln in lines:
            types = ["Fibre Opener", "Garnett Main Motor" if ln.line_type == "Garnett" else "Blow Fan Motor",
                     "Flapper Unit", "Filling Head"]
            for t in types:
                n += 1
                b = dict(BASELINES[t])
                hsm = rng.uniform(120, 1400)
                mc = m.Machine(code=f"M-{fi}{n:02d}", name=t, machine_type=t, stage=engine.mh.STAGE_OF[t], line_id=ln.id,
                               factory_id=f.id, status="Running", baseline=b, operating_hours=round(rng.uniform(8000, 30000)),
                               hours_since_maintenance=hsm, degradation=rng.uniform(0.00002, 0.00008),
                               last_maintenance_at=day_start - timedelta(hours=hsm * 2.2), health_score=95)
                db.add(mc)
        machine_seq += n
        # cameras
        cams = [("01", "Pillow filling", 0), ("02", "Sewing", 2), ("03", "Compression", 1),
                ("04", "Carton packing", 0), ("05", "Carton packing", 2), ("06", "Final inspection", None)]
        for suffix, station, li in cams:
            db.add(m.Camera(code=f"CAM-{code}-{suffix}", name=f"{station} — {lines[li].name if li is not None else 'Dispatch lane'}",
                            station=station, factory_id=f.id, line_id=lines[li].id if li is not None else None,
                            status="Online", mode="simulated", stream_url=None))
        # storage locations
        for zone in "ABC":
            for bay in range(1, 5):
                for rack in range(1, 4):
                    for level in range(1, 5):
                        db.add(m.StorageLocation(code=f"{code}-{zone}{bay:02d}-R{rack}-L{level}", factory_id=f.id, zone=zone,
                                                 bay=bay, rack=rack, level=level, capacity_pallets=1, kind="Rack"))
        for bay in range(1, 5):
            for lane in range(1, 4):
                db.add(m.StorageLocation(code=f"{code}-D{bay:02d}-LN{lane}", factory_id=f.id, zone="D", bay=bay, rack=lane,
                                         level=1, capacity_pallets=12, kind="Floor"))
    db.flush()

    # special machine conditions
    def machine(code):
        return db.scalar(select(m.Machine).where(m.Machine.code == code))
    mc = machine("M-102")  # KNV Garnett Line 01 main motor: drifting vibration/temperature
    mc.hours_since_maintenance, mc.degradation = 137.0, 0.004
    mc = machine("M-226")  # PHX Blow Line 02 fan motor: early warning
    mc.hours_since_maintenance, mc.degradation = 700.0, 0.0004
    mc = machine("M-315")  # CMH Garnett Line 04 flapper: early warning
    mc.hours_since_maintenance, mc.degradation = 900.0, 0.00034
    for mc in db.scalars(select(m.Machine)):
        r = reading(mc, True, rng)
        for k, v in r.items():
            setattr(mc, k, v)

    _employees(db, factories, lines_by_f, rng, today)
    _materials_and_procurement(db, factories, materials, suppliers, skus, rng, today)
    _orders(db, factories, lines_by_f, skus, customers, rng, today)
    _history(db, factories, lines_by_f, skus, rng, today)

    db.add(m.SimState(id=1, sim_time=day_start, running=s.sim_autostart, speed=s.sim_default_speed, tick=0,
                      seed_date=today, scenario_log=[]))
    db.add(m.Setting(key="detention_charge", value=s.detention_charge_usd))
    db.add(m.Setting(key="detention_free_min", value=s.detention_free_minutes))
    db.add(m.Setting(key="changeover_standard_min", value=s.changeover_standard_minutes))
    db.add(m.Setting(key="shift2_differential", value=s.shift2_differential_usd))
    db.flush()
    # Run the morning through the engine so today's history is consistent.
    engine.FAST_MODE = True
    try:
        engine.advance_to(db, datetime.combine(today, time(s.sim_start_hour, 0)), step_min=10)
    finally:
        engine.FAST_MODE = False
    engine.PICK_BUDGET.clear()
    engine.PUTAWAY_BUDGET.clear()
    engine.PACK_BUDGET.clear()
    from .services import alerts
    alerts.evaluate(db, datetime.combine(today, time(s.sim_start_hour, 0)))
    db.commit()


def _employees(db, factories, lines_by_f, rng, today):
    n = 1000
    names_used = 0

    def add(f, role, shift, line=None, skill=None):
        nonlocal n, names_used
        n += 1
        names_used += 1
        lo, hi = ROLE_WAGE[role]
        r = rng.random()
        e = m.Employee(code=f"EMP-{n}", name=f"{rng.choice(FIRST)} {chr(65 + rng.randrange(26))}.", role=role,
                       factory_id=f.id, line_id=line.id if line else None, shift=shift,
                       employment_type="Full-time" if r < 0.75 else "Temporary (agency)" if r < 0.95 else "Part-time",
                       hourly_wage=round(rng.uniform(lo, hi) * 4) / 4,
                       attendance="Present" if rng.random() < 0.95 else rng.choice(["Absent", "Late"]),
                       skill=skill if skill is not None else round(min(1.12, max(0.92, rng.gauss(1.03, 0.04))), 3),
                       productivity_modifier=1.0)
        db.add(e)
        return e

    for code, f in factories.items():
        lines = lines_by_f[code]
        for sh in (1, 2):
            for i, ln in enumerate(lines):
                add(f, "Filler", sh, ln)
                s1 = add(f, "Sewer", sh, ln)
                s2 = add(f, "Sewer", sh, ln)
                add(f, "Bagger", sh, ln)
                s1.attendance = "Present"
                if code == "KNV" and sh == 1 and i in (2, 4, 5):
                    s1.skill, s2.skill, s2.attendance = 1.0, {2: 0.70, 4: 0.80, 5: 0.78}[i], "Present"
                if i % 2 == 0:
                    add(f, "Packaging operator", sh, ln)
            for _ in range(2):
                add(f, "Supervisor", sh)
                add(f, "Maintenance technician", sh)
            for _ in range(3):
                add(f, "Warehouse associate", sh).attendance = "Present"
            for _ in range(2):
                add(f, "Forklift operator", sh).attendance = "Present"
    db.flush()


def _materials_and_procurement(db, factories, materials, suppliers, skus, rng, today):
    now = datetime.combine(today, cal.DAY_START)
    po_n, ship_n, gr_n = 45000, 8000, 60000
    for code, f in factories.items():
        book = ORDER_BOOK[code]
        sku_list = [skus[x[0]] for x in book]
        cons = {}
        for sku in sku_list:
            share = f.daily_target / len(sku_list)
            cons[sku.fill_material_id] = cons.get(sku.fill_material_id, 0) + share * sku.fill_kg
            cons[sku.shell_material_id] = cons.get(sku.shell_material_id, 0) + share
            cons[sku.bag_material_id] = cons.get(sku.bag_material_id, 0) + share
            cons[sku.carton_material_id] = cons.get(sku.carton_material_id, 0) + share / sku.pillows_per_carton
            cons[sku.label_material_id] = cons.get(sku.label_material_id, 0) + share / sku.pillows_per_carton
        for mcode, mat in materials.items():
            c = round(cons.get(mat.id, 0), 1)
            if c <= 0:
                continue
            cover = rng.uniform(5.5, 11) if mat.category != "Shell" else rng.uniform(16, 26)
            if (code, mcode) == ("KNV", "F-104"):
                cover = 1.35
            if (code, mcode) == ("PHX", "CB-502"):
                cover = 2.7
            db.add(m.MaterialStock(factory_id=f.id, material_id=mat.id, on_hand=round(c * cover, 1), damaged=0,
                                   reorder_days=REORDER_DAYS[mat.category], avg_daily_consumption=c, consumed_today=0))
            sup = suppliers[SUPPLIER_FOR.get((code, mcode)) or next(s for s in SUPPLIERS if mcode in s[4])[0]]
            lead = sup.lead_time_days
            lot = round(c * (8 if mat.category != "Shell" else 30), -2) or 100
            # received history (supplier performance)
            for k in range(3):
                order_d = now - timedelta(days=rng.uniform(12, 40) if mat.category != "Shell" else rng.uniform(45, 80))
                due = order_d + timedelta(days=lead)
                p_late, lo, hi = LATE_PROFILE.get(sup.code, (0.08, 0.3, 1.0))
                arrived = due + timedelta(days=rng.uniform(lo, hi) if rng.random() < p_late else rng.uniform(-0.3, 0.1))
                if arrived > now - timedelta(days=1):
                    arrived = now - timedelta(days=rng.uniform(1, 3))
                po_n += 1
                ship_n += 1
                gr_n += 1
                recv = lot
                short = 0
                if (code, mcode, k) == ("KNV", "F-101", 0):
                    recv, short = lot - 200, 200
                po = m.PurchaseOrder(number=f"PO-{po_n}", factory_id=f.id, supplier_id=sup.id, material_id=mat.id,
                                     ordered_qty=lot, received_qty=recv, unit_price=mat.unit_cost, order_date=order_d,
                                     expected_date=due, status="Received" if not short else "Partially Received",
                                     created_by="procure", history=[
                                         {"ts": order_d.isoformat(timespec="minutes"), "event": f"PO created for {lot:,.0f} {mat.uom}", "by": "procure"},
                                         {"ts": arrived.isoformat(timespec="minutes"), "event": f"Received {recv:,.0f} {mat.uom}", "by": "warehouse"}])
                db.add(po)
                db.flush()
                sh = m.InboundShipment(ref=f"INB-{ship_n}", bol_number=f"BOL{order_d:%y%m}{po.id:05d}", carrier=rng.choice(CARRIERS),
                                       po_id=po.id, factory_id=f.id, supplier_id=sup.id, material_id=mat.id, qty=lot,
                                       expected_arrival=due, original_expected_arrival=due, actual_arrival=arrived,
                                       dock=rng.choice(["R1", "R2"]), status="Discrepancy" if short else "Received")
                db.add(sh)
                db.flush()
                gr = m.GoodsReceipt(number=f"GR-{gr_n}", shipment_id=sh.id, po_id=po.id, factory_id=f.id, material_id=mat.id,
                                    expected_qty=lot, received_qty=recv, damaged_qty=0, shortage_qty=short, accepted_qty=recv,
                                    confirmed_at=arrived + timedelta(hours=1), confirmed_by="warehouse")
                db.add(gr)
                db.flush()
                if short:
                    db.add(m.MaterialDiscrepancy(receipt_id=gr.id, po_id=po.id, factory_id=f.id, material_id=mat.id,
                                                 kind="Shortage", qty=short, status="Open", created_at=arrived))
            # open purchase orders
            opens = [rng.uniform(1.5, 6)] if mat.category != "Shell" else [rng.uniform(6, 20)]
            if (code, mcode) == ("KNV", "F-101"):
                opens = [4.67 / 24]  # arrives 10:40 today -> goods receipt demo
            if (code, mcode) == ("KNV", "F-104"):
                opens = [-0.62]     # was due yesterday afternoon; now delayed
            for off in opens:
                po_n += 1
                ship_n += 1
                due = now + timedelta(days=off)
                qty = 10000.0 if (code, mcode) == ("KNV", "F-101") else lot
                po = m.PurchaseOrder(number=f"PO-{po_n}", factory_id=f.id, supplier_id=sup.id, material_id=mat.id,
                                     ordered_qty=qty, received_qty=0, unit_price=mat.unit_cost,
                                     order_date=due - timedelta(days=lead + 1), expected_date=due, status="Open",
                                     created_by="procure", history=[{"ts": (due - timedelta(days=lead + 1)).isoformat(timespec="minutes"),
                                                                     "event": f"PO created for {qty:,.0f} {mat.uom}", "by": "procure"}])
                db.add(po)
                db.flush()
                sh = m.InboundShipment(ref=f"INB-{ship_n}", bol_number=f"BOL{due:%y%m}{po.id:05d}", carrier=rng.choice(CARRIERS),
                                       po_id=po.id, factory_id=f.id, supplier_id=sup.id, material_id=mat.id, qty=qty,
                                       expected_arrival=due, original_expected_arrival=due,
                                       status="In Transit" if off < 1 else "Scheduled",
                                       dock="R1" if (code, mcode) == ("KNV", "F-101") else None)
                if (code, mcode) == ("KNV", "F-104"):
                    sh.expected_arrival = now + timedelta(days=1, hours=8)
                    sh.delay_reason = "Carrier capacity shortage — load not tendered"
                    po.status = "Delayed"
                    po.history = [*po.history, {"ts": now.isoformat(timespec="minutes"),
                                                "event": "Supplier revised ETA +1.6 days (carrier capacity)", "by": "SUP-02"}]
                db.add(sh)
    db.flush()


def _make_pallets(db, f, po, sku, cartons, when, store=True):
    cpp = sku.cartons_per_pallet
    made = []
    while cartons > 0:
        take = min(cpp, cartons)
        p = m.Pallet(code=f"PLT-{50000 + (db.scalar(select(m.Pallet.id).order_by(m.Pallet.id.desc()).limit(1)) or 0) + 1}",
                     factory_id=f.id, po_id=po.id if po else None, sku_id=sku.id, customer_id=sku.customer_id,
                     batch_no=f"B{when:%y%m%d}-{po.number[-4:] if po else 'STK'}", cartons=take, capacity=cpp,
                     allocated_cartons=0, status="Pending Put-away", created_at=when, closed_at=when)
        db.add(p)
        db.flush()
        if store and take == cpp:
            loc = inventory.suggest_location(db, p)
            if loc:
                p.status, p.location_id, p.stored_at, p.stored_by = "Stored", loc.id, when, "warehouse"
        elif take < cpp:
            p.status = "Open"
        made.append(p)
        cartons -= take
    return made


def _orders(db, factories, lines_by_f, skus, customers, rng, today):
    day_start = datetime.combine(today, cal.DAY_START)
    num = 496
    seq = {}
    for code, f in factories.items():
        lines = lines_by_f[code]
        for (sku_code, qty, off, hour, stage, li, done) in ORDER_BOOK[code]:
            sku = skus[sku_code]
            ppc = sku.pillows_per_carton
            ship = at(today + timedelta(days=off), hour)
            tier = next(c for c in CUSTOMERS if c[0] == sku_code.split("-")[0])[3]
            prio = "High" if off <= 1 or tier == 1 and off <= 2 else ("Medium" if off <= 5 else "Low")
            o = m.CustomerOrder(number=f"ORD-{num}", customer_id=sku.customer_id, factory_id=f.id, sku_id=sku.id, qty=qty,
                                order_date=ship - timedelta(days=28), ship_window_start=ship - timedelta(days=21),
                                ship_window_end=ship + timedelta(days=7), scheduled_dispatch=ship, priority=prio, status="Open")
            num += 1
            db.add(o)
            db.flush()
            line = lines[li - 1] if li else rng.choice(lines)
            seq[line.id] = seq.get(line.id, 0) + 1
            po = m.ProductionOrder(number=f"PRD-{24000 + o.id}", customer_order_id=o.id, factory_id=f.id, sku_id=sku.id,
                                   line_id=line.id, sequence=seq[line.id], required_qty=qty, completed_qty=0,
                                   start_date=ship - timedelta(days=4), required_completion=ship - timedelta(hours=18),
                                   priority=prio, status="Planned")
            db.add(po)
            db.flush()
            finished = stage in ("delivered", "dispatched", "ready", "in_inventory")
            if finished:
                po.completed_qty = po.packed_qty = po.stored_qty = qty
                po.status = "Completed"
                po.started_at = ship - timedelta(days=3)
                po.completed_at = po.estimated_completion = ship - timedelta(days=1, hours=6)
                po.sequence = 0
            if stage in ("delivered", "dispatched"):
                o.allocated_qty = o.picked_qty = o.loaded_qty = qty
                o.status = "Delivered" if stage == "delivered" else "Dispatched"
                o.dispatched_at = ship + timedelta(minutes=80)
                if stage == "delivered":
                    o.delivered_at = ship + timedelta(hours=30)
                wait = rng.choice([20, 35, 45, 150, 25, 185]) if code != "PHX" else rng.choice([15, 30, 40])
                t = m.TruckAppointment(ref=f"TRK-{6900 + o.id}", carrier=rng.choice(CARRIERS), factory_id=f.id,
                                       customer_id=o.customer_id, order_id=o.id, dock=rng.choice(engine.DOCKS),
                                       scheduled_arrival=ship, expected_loading_min=75, actual_arrival=ship,
                                       loading_start=ship + timedelta(minutes=wait),
                                       loading_end=ship + timedelta(minutes=wait + 70),
                                       departed_at=ship + timedelta(minutes=wait + 70), loaded_qty=qty, status="Departed",
                                       wait_min=wait, detention_charge=350.0 if wait > 120 else 0.0,
                                       delay_reason=("Order not picked when truck arrived" if wait > 120 else None))
                db.add(t)
                continue
            if finished:
                pallets = _make_pallets(db, f, po, sku, qty // ppc, ship - timedelta(days=1))
                for p in pallets:
                    if p.status != "Stored":
                        loc = inventory.suggest_location(db, p)
                        p.status, p.location_id, p.stored_at = "Stored", loc.id, ship - timedelta(days=1)
                db.flush()
                inventory.allocate(db, o, day_start - timedelta(hours=8), "warehouse")
                db.flush()
                if stage == "ready":
                    for a in db.scalars(select(m.Allocation).where(m.Allocation.order_id == o.id)):
                        inventory.pick(db, a, day_start - timedelta(hours=1), "warehouse")
                t = m.TruckAppointment(ref=f"TRK-{6900 + o.id}", carrier=rng.choice(CARRIERS), factory_id=f.id,
                                       customer_id=o.customer_id, order_id=o.id, dock=engine.DOCKS[o.id % 3],
                                       scheduled_arrival=ship, expected_loading_min=75, status="Scheduled",
                                       late_by_min=rng.choice([0, 0, 5, 15]))
                db.add(t)
                continue
            if stage == "producing":
                po.started_at = day_start - timedelta(hours=rng.uniform(4, 30)) if done else None
                po.completed_qty = done
                po.status = "In Progress" if done else "Ready"
                po.sequence = 0
                queue = min(done, ppc * rng.randint(1, 3)) if done else 0
                packed_cartons = (done - queue) // ppc
                po.pack_queue = done - packed_cartons * ppc
                po.packed_qty = packed_cartons * ppc
                if packed_cartons:
                    pallets = _make_pallets(db, f, po, sku, packed_cartons, day_start - timedelta(hours=2))
                    po.stored_qty = sum(p.cartons for p in pallets if p.status == "Stored") * ppc
                line.current_po_id = po.id
                line.current_sku_id = sku.id
                line.status = "Running"
            else:
                po.status = "Ready"
            if off <= 1 and stage == "producing" and ship.date() == today:
                db.add(m.TruckAppointment(ref=f"TRK-{6900 + o.id}", carrier=rng.choice(CARRIERS), factory_id=f.id,
                                          customer_id=o.customer_id, order_id=o.id, dock="D2", scheduled_arrival=ship,
                                          expected_loading_min=75, status="Scheduled"))
        # standing stock (make-to-stock replenishment) for other SKUs
        for i in range(STOCK_PALLETS[code]):
            sku = skus[STOCK_SKUS[code][i % len(STOCK_SKUS[code])]]
            _make_pallets(db, f, None, sku, sku.cartons_per_pallet, day_start - timedelta(days=rng.uniform(1, 9)))
    db.flush()


def _history(db, factories, lines_by_f, skus, rng, today):
    """Closed shifts for trends plus yesterday's line-level history."""
    yday = today - timedelta(days=1)
    sku_list = list(skus.values())
    for code, f in factories.items():
        lines = lines_by_f[code]
        perf = {"KNV": 0.93, "PHX": 0.9, "CMH": 0.88}[code]
        for d in range(14, 0, -1):
            day = today - timedelta(days=d)
            for sh in (1, 2):
                planned = f.daily_target / 2
                actual = planned * rng.uniform(perf - 0.05, perf + 0.04)
                insp = int(actual / 10.5)
                bad = int(insp * rng.uniform(0.002, 0.009))
                workers = 41 + rng.randint(0, 4)
                wage = 18.1 + (1 if sh == 2 else 0)
                db.add(m.ShiftSummary(factory_id=f.id, date=day, shift=sh, planned=round(planned), actual=round(actual),
                                      planned_min=460 * 8, run_min=460 * 8 * rng.uniform(0.86, 0.94),
                                      downtime_min=rng.uniform(40, 120), cartons_inspected=insp, cartons_passed=insp - bad,
                                      labour_cost=round(workers * 8 * wage, 2), workers_deployed=workers, workers_required=40,
                                      idle_min=rng.uniform(120, 320), changeovers=rng.randint(3, 8),
                                      changeover_lost_min=rng.uniform(3, 25),
                                      defects={"Underpacked": max(0, bad // 2), "Overpacked": bad // 5,
                                               "Sewing defect": bad // 6, "Label mismatch": bad // 8}))
        for ln in lines:
            for sh in (1, 2):
                for hour in cal.hours_for_shift(sh):
                    pm = cal.planned_minutes_in_hour(yday, hour, sh)
                    if pm <= 0:
                        continue
                    planned = ln.planned_rate * pm / 60
                    eff = rng.uniform(0.86, 1.0) * (0.84 if ln.code == "KNV-GL03" else 1.0)
                    db.add(m.ProductionHourly(line_id=ln.id, factory_id=f.id, date=yday, hour=hour, shift=sh,
                                              planned=round(planned, 1), actual=round(planned * eff), run_min=pm * min(1, eff + 0.05),
                                              downtime_min=pm * max(0, 0.95 - eff)))
            for k in range(rng.randint(1, 3)):
                start = at(yday, rng.uniform(7, 21.5))
                std = 3.0
                dur = std * ln.changeover_factor * rng.uniform(0.85, 1.4)
                a, b = rng.sample(sku_list, 2)
                db.add(m.ChangeoverEvent(line_id=ln.id, factory_id=f.id, prev_sku_id=a.id, new_sku_id=b.id,
                                         customer_id=b.customer_id, start=start, end=start + timedelta(minutes=dur),
                                         duration_min=round(dur, 1), standard_min=std, lost_min=round(max(0, dur - std), 1),
                                         flagged=dur > std))
                db.add(m.DowntimeEvent(line_id=ln.id, factory_id=f.id, category="Changeover", reason=f"{a.code} → {b.code}",
                                       start=start, end=start + timedelta(minutes=dur), minutes=round(dur, 1)))
    # telemetry for yesterday's productive windows (10-minute resolution)
    day_start = datetime.combine(today, cal.DAY_START)
    for mc in db.scalars(select(m.Machine)):
        hsm_now = mc.hours_since_maintenance
        t = datetime.combine(yday, cal.DAY_START)
        end = datetime.combine(yday, cal.DAY_END)
        while t < end:
            if cal.is_production_time(t):
                back = cal.productive_minutes(t, day_start) / 60
                mc.hours_since_maintenance = max(0.0, hsm_now - back)
                r = reading(mc, True, rng)
                db.add(m.TelemetryPoint(machine_id=mc.id, ts=t, **r))
            t += timedelta(minutes=10)
        mc.hours_since_maintenance = hsm_now
    db.flush()
