"""Loss prevention: material pilferage and unexplained loss.

Signals that point at pilferage in a pillow plant. Each is raised as a case
(m.LossIncident) with its evidence trail, a handling playbook and an outcome.

  fg_cycle_count        finished-goods cycle count short against the book (rack location)
  exit_gate_tag         an RFID-tagged carton read at the staff exit gate with no dispatch record
  after_hours_access    a badge read at the material-store door outside store hours / role access
  consumption_variance  fibre issued to a line above output × standard fill + scrap allowance
  receiving_shortage    inbound short on arrival with a seal-number mismatch
  dispatch_mismatch     cartons counted at the dock door ≠ manifest; weighbridge variance
  scrap_reconciliation  QA rejects logged vs the scrap-cage weigh-out
  cctv_unbadged         CCTV vision: a person with no badge read inside a badge-only zone

Cases come from the simulation (its own random stream, so the production engine's
sequence is unchanged), are worked through their playbook (by the simulated security /
warehouse teams, or by a user in the app) and are closed with an outcome:
Confirmed loss, Recovered, Process error or False alarm. Values are at standard cost.

People: badge holders named in evidence are simulated employees. The CCTV footage shows
real workers, so vision only raises neutral access checks ("no badge read — verify") and
never names or flags a person in the video as a suspect; clips are referenced as evidence.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import get_settings
from .common import iso, log_event, next_number

RNG = random.Random(get_settings().sim_seed + 97)


def reseed(seed: int) -> None:
    RNG.seed(seed + 97)


STD_COST_SHARE = 0.58          # standard cost as a share of the list price (illustrative)
STORE_HOURS = (time(6, 0), time(22, 30))
SYSTEM = "S!AP loss prevention"
WINDOW_DAYS = 7                # KPI window: the last 7 calendar days including today
WINDOW_LABEL = "last 7 days incl. today"
FG_KINDS = ("fg_cycle_count", "exit_gate_tag", "dispatch_mismatch")   # a confirmed loss is written off finished goods


def window_start(now: datetime) -> datetime:
    """Midnight at the start of the KPI window (the same window for counts, trend and shrinkage)."""
    return datetime.combine(now.date() - timedelta(days=WINDOW_DAYS - 1), time(0))

KINDS = {
    "fg_cycle_count": {
        "label": "FG cycle-count shortage", "stage": "warehouse", "twin_stage": "fg", "area": "fg",
        "channel": "Cycle count · 2-person blind count", "icon": "box", "owner": "Warehouse lead + Security",
        "playbook": [
            ("recount", "Blind recount of the location by a second team"),
            ("trace", "Trace put-away, pick and move scans for the pallet"),
            ("cctv", "Review staging / aisle CCTV since the last good count"),
            ("adjacent", "Check adjacent locations and the staging lane for mis-slotted cartons"),
        ],
        "outcomes": [("Process error", 0.45), ("Confirmed loss", 0.40), ("Recovered", 0.15)],
    },
    "exit_gate_tag": {
        "label": "Tagged carton at staff exit", "stage": "warehouse", "twin_stage": "fg", "area": "gate",
        "channel": "RFID exit-gate reader", "icon": "signal", "owner": "Security · gate officer",
        "playbook": [
            ("stop", "Gate officer stops the exit and inspects bags and carts per policy"),
            ("epc", "Match the tag EPC to the carton, pallet and last scan"),
            ("badges", "List badges that passed the gate within ±60 s"),
            ("cctv", "Pull gate and staging-lane CCTV for the minute around the read"),
        ],
        "outcomes": [("Recovered", 0.45), ("False alarm", 0.40), ("Confirmed loss", 0.15)],
    },
    "after_hours_access": {
        "label": "After-hours store access", "stage": "procurement", "twin_stage": "raw", "area": "material_store",
        "channel": "Material-store door reader", "icon": "clock", "owner": "Store keeper + Security",
        "playbook": [
            ("verify", "Confirm with the shift supervisor whether the visit was an authorised task"),
            ("spot", "Spot-count high-value items in the store (shells, labels, zips)"),
            ("cctv", "Review the store-door camera for the visit"),
            ("access", "Restrict the badge to its role zones until the case is closed"),
        ],
        "outcomes": [("False alarm", 0.55), ("Process error", 0.25), ("Confirmed loss", 0.20)],
    },
    "consumption_variance": {
        "label": "Fibre consumption variance", "stage": "production", "twin_stage": "filling", "area": "line",
        "channel": "Material issues vs BOM × output", "icon": "factory", "owner": "Production controller",
        "playbook": [
            ("reconcile", "Reconcile bale issue slips against output × standard fill"),
            ("scrap", "Weigh scrap, sweepings and rework fibre on the line"),
            ("calibrate", "Check filling-head fill-weight calibration (over-fill reads as loss)"),
            ("store", "Audit bale issues at the material store for the period"),
        ],
        "outcomes": [("Process error", 0.60), ("Confirmed loss", 0.25), ("False alarm", 0.15)],
    },
    "receiving_shortage": {
        "label": "Inbound short · seal mismatch", "stage": "procurement", "twin_stage": "raw", "area": "receiving",
        "channel": "Receiving count + seal check + weighbridge", "icon": "truck", "owner": "Receiving lead",
        "playbook": [
            ("quarantine", "Quarantine the delivery; photograph the seal and trailer"),
            ("weigh", "Compare weighbridge net weight with the bill of lading"),
            ("count", "Full recount of bales / rolls against the ASN"),
            ("claim", "Raise the carrier claim / supplier debit note"),
        ],
        "outcomes": [("Confirmed loss", 0.50), ("Recovered", 0.30), ("Process error", 0.20)],
    },
    "dispatch_mismatch": {
        "label": "Load count ≠ manifest", "stage": "dispatch", "twin_stage": "dispatch", "area": "dock",
        "channel": "Dock-door carton count + weighbridge", "icon": "route", "owner": "Shipping lead + Security",
        "playbook": [
            ("hold", "Hold the truck at the gate before departure"),
            ("recount", "Scan every carton on the trailer against the manifest"),
            ("weigh", "Compare weighbridge gross weight with the expected load"),
            ("reseal", "Correct the load, re-seal and update the bill of lading"),
        ],
        "outcomes": [("Process error", 0.55), ("Recovered", 0.35), ("Confirmed loss", 0.10)],
    },
    "scrap_reconciliation": {
        "label": "Scrap not reconciled", "stage": "quality", "twin_stage": "qa", "area": "qa",
        "channel": "QA reject log vs scrap weigh-out", "icon": "shield", "owner": "Quality supervisor",
        "playbook": [
            ("count", "Count the rework / scrap cage against the QA reject log"),
            ("requeue", "Check re-queued rework against production records"),
            ("cctv", "Review QA hold-cage CCTV"),
            ("lock", "Lock the scrap cage; two-person sign-off on scrap removal"),
        ],
        "outcomes": [("Process error", 0.50), ("Confirmed loss", 0.30), ("Recovered", 0.20)],
    },
    "cctv_unbadged": {
        "label": "Unbadged entry · badge-only zone", "stage": "warehouse", "twin_stage": "pallet", "area": "staging",
        "channel": "CCTV vision + RFID zone reader", "icon": "camera", "owner": "Security · floor officer",
        "playbook": [
            ("verify", "Security verifies the person's identity on the floor"),
            ("log", "Check the visitor and contractor log"),
            ("badge", "Issue a temporary badge or escort out of the zone"),
        ],
        "outcomes": [("False alarm", 0.75), ("Process error", 0.25)],
    },
}
OUTCOMES = ["Confirmed loss", "Recovered", "Process error", "False alarm"]
WEIGHTS = {"fg_cycle_count": 0.20, "exit_gate_tag": 0.11, "consumption_variance": 0.15, "receiving_shortage": 0.12,
           "dispatch_mismatch": 0.15, "scrap_reconciliation": 0.10, "cctv_unbadged": 0.17}


# ─── helpers ──────────────────────────────────────────────────────────
def std_cost(sku: m.Sku | None) -> float:
    return round((sku.unit_price if sku and sku.unit_price else 8.0) * STD_COST_SHARE, 2)


def _ref(db: Session, f: m.Factory) -> str:
    return next_number(db, m.LossIncident, f"LP-{f.code}-", 400)


def _cam(f: m.Factory, n: str) -> str:
    return f"CAM-{f.code}-{n}"


def _pick_outcome(kind: str, rng: random.Random) -> str:
    r, acc = rng.random(), 0.0
    for o, w in KINDS[kind]["outcomes"]:
        acc += w
        if r <= acc:
            return o
    return KINDS[kind]["outcomes"][-1][0]


def _ev(source: str, ts: datetime, text: str) -> dict:
    return {"source": source, "ts": iso(ts), "text": text}


def _emps(db: Session, f: m.Factory, roles=None, n=1, rng=RNG, shift=None) -> list[m.Employee]:
    q = select(m.Employee).where(m.Employee.factory_id == f.id)
    if roles:
        q = q.where(m.Employee.role.in_(roles))
    if shift:
        q = q.where(m.Employee.shift == shift)
    rows = list(db.scalars(q.order_by(m.Employee.id)))
    return rng.sample(rows, min(n, len(rows))) if rows else []


def _data(inc: m.LossIncident) -> dict:
    """Record references kept on the case (pallet, receipt, truck flavour …), stored on its 'open' timeline entry."""
    return next((t.get("data") or {} for t in inc.timeline or [] if t.get("step") == "open"), {})


def _taken(db: Session, col) -> set:
    """Record ids already referenced by a loss case (one case per receipt / truck)."""
    return set(db.scalars(select(col).where(col.is_not(None))))


def _open_pallets(db: Session, f: m.Factory) -> set[int]:
    """Pallets that already have an open finished-goods case."""
    out = set()
    for inc in db.scalars(select(m.LossIncident).where(m.LossIncident.factory_id == f.id, m.LossIncident.status != "Resolved",
                                                       m.LossIncident.kind.in_(FG_KINDS))):
        if _data(inc).get("pallet_id"):
            out.add(_data(inc)["pallet_id"])
    return out


def _last_picks(db: Session, pallet_ids: list[int]) -> dict[int, datetime]:
    rows = db.execute(select(m.Allocation.pallet_id, func.max(m.Allocation.picked_at)).where(
        m.Allocation.pallet_id.in_(pallet_ids or [0]), m.Allocation.picked_at.is_not(None)).group_by(m.Allocation.pallet_id))
    return {pid: ts for pid, ts in rows if ts}


def _who(name: str | None) -> str:
    return f" by {name}" if name and not name.startswith("sim:") else ""


def _last_scan(db: Session, p: m.Pallet, loc: m.StorageLocation) -> tuple[datetime, str]:
    """The pallet's last WMS movement: its put-away, or a later pick from it."""
    a = db.scalar(select(m.Allocation).where(m.Allocation.pallet_id == p.id, m.Allocation.picked_at.is_not(None))
                  .order_by(m.Allocation.picked_at.desc()).limit(1))
    if a and (p.stored_at is None or a.picked_at > p.stored_at):
        o = db.get(m.CustomerOrder, a.order_id)
        return a.picked_at, f"pick of {a.cartons} carton{'s' if a.cartons != 1 else ''} for {o.number if o else 'an order'}{_who(a.picked_by)}"
    return p.stored_at, f"put-away of {p.code} to {loc.code}{_who(p.stored_by)}"


def _cctv_events(f: m.Factory) -> list[dict]:
    """Badge-only zone entries the CCTV vision pipeline found on this plant's cameras."""
    from . import cctv
    out = []
    for fd in cctv.catalog():
        if fd["factory"] != f.code:
            continue
        for e in (fd.get("summary") or {}).get("lp_events") or []:
            out.append({**e, "camera": fd["camera"], "feed": fd["id"], "title": fd["title"]})
    return out


# ─── case builders (one per kind) ─────────────────────────────────────
def _fg_cycle_count(db, f, when, rng, historic):
    """A stored pallet counted short. Only pallets that were already in the rack before the count and have had
    no WMS movement since, so the book count in the evidence is the pallet's real carton count."""
    busy = _open_pallets(db, f)
    pallets = [p for p in db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == f.id, m.Pallet.status == "Stored", m.Pallet.location_id.is_not(None), m.Pallet.cartons >= 6,
        m.Pallet.stored_at <= when - timedelta(hours=2)).order_by(m.Pallet.id)) if p.id not in busy]
    picks = _last_picks(db, [p.id for p in pallets])
    pallets = [p for p in pallets if picks.get(p.id, p.stored_at) <= when - timedelta(hours=1)]
    if not pallets:
        return None
    p = rng.choice(pallets)
    loc = db.get(m.StorageLocation, p.location_id)
    sku = db.get(m.Sku, p.sku_id)
    short = rng.randint(1, min(4, max(1, p.cartons // 6)))
    ppc = sku.pillows_per_carton if sku else 6
    qty = short * ppc
    nb = rng.randint(3, 7)
    last_ts, last_txt = _last_scan(db, p, loc)
    t0 = max(last_ts, when - timedelta(hours=rng.randint(10, 30)))  # last good count of the location
    zone = f"FG floor lane · {loc.code}" if loc.kind == "Floor" else f"FG racks · {loc.code}"
    return dict(
        kind="fg_cycle_count", severity="critical" if qty * std_cost(sku) >= 250 else "warning",
        title=f"{short} carton{'s' if short > 1 else ''} of {sku.code if sku else 'FG'} missing at {loc.code}",
        summary=(f"Cycle count found {p.cartons - short} of {p.cartons} cartons on pallet {p.code} at {loc.code} "
                 f"({p.allocated_cartons} of them reserved for orders); {qty} pillows are unaccounted for against the book."),
        zone=zone, area_ref=loc.code, location_id=loc.id,
        item=f"{sku.code} · {sku.description}" if sku else "Finished goods", item_kind="fg", qty=qty, unit="pillows",
        value_usd=round(qty * std_cost(sku), 2), camera_code=_cam(f, "06"),
        data={"pallet_id": p.id, "pallet": p.code, "cartons": short, "ppc": ppc, "book_cartons": p.cartons, "sku_id": p.sku_id},
        evidence=[
            _ev("Cycle count", when, f"Blind count {p.cartons - short}/{p.cartons} cartons by two counters; the scale "
                                     f"weight of the pallet is {short * ppc * (sku.fill_kg if sku else 0.7) + short * 0.4:.1f} kg light."),
            _ev("WMS scans", last_ts, f"Last scan on {p.code}: {last_txt}; no pick or move scan since."),
            _ev("RFID", when, f"{nb} warehouse / forklift badges were read in zone {loc.zone} since the last good count."),
            _ev("CCTV", when, f"Staging-lane camera {_cam(f, '06')}: footage {t0:%a %H:%M}–{when:%a %H:%M} retained for review."),
        ])


def _exit_gate_tag(db, f, when, rng, historic):
    """A carton that is still on the book (on a pallet in the warehouse, not reserved) read at the staff exit."""
    busy = _open_pallets(db, f)
    rows = [(c, p) for c, p in db.execute(
        select(m.Carton, m.Pallet).join(m.Pallet, m.Carton.pallet_id == m.Pallet.id).where(
            m.Carton.factory_id == f.id, m.Carton.status == "Passed", m.Carton.carton_code.is_not(None),
            m.Carton.approved_at <= when, m.Pallet.status.in_(("Stored", "Pending Put-away")),
            m.Pallet.cartons > m.Pallet.allocated_cartons).order_by(m.Carton.id.desc()).limit(400)) if p.id not in busy]
    if not rows:
        return None
    wmt = db.scalar(select(m.Customer.id).where(m.Customer.code == "WMT"))
    tagged = [r for r in rows if r[0].customer_id == wmt]  # Walmart cartons carry an RFID label (Dept. #22)
    c, pal = rng.choice(tagged or rows)
    sku = db.get(m.Sku, c.sku_id)
    badges = _emps(db, f, n=rng.randint(2, 3), rng=rng)
    epc = f"urn:epc:id:sgtin:0614141.{(sku.id if sku else 0):05d}.{c.id}"
    ppc = c.required_count or (sku.pillows_per_carton if sku else 6)
    return dict(
        kind="exit_gate_tag", severity="critical",
        title=f"Tagged carton {c.carton_code} read at the staff exit gate",
        summary=(f"Gate reader RD-{f.code}-GATE read the RFID tag of a {sku.code if sku else ''} carton "
                 f"({ppc} pillows, booked on pallet {pal.code}) moving outward at {when:%H:%M}; "
                 "no dispatch, truck or move order covers it."),
        zone="Staff exit gate", area_ref="GATE",
        item=f"{sku.code} · {sku.description}" if sku else "Finished carton", item_kind="fg",
        qty=ppc, unit="pillows", value_usd=round(ppc * std_cost(sku), 2),
        camera_code=_cam(f, "06"),
        data={"pallet_id": pal.id, "pallet": pal.code, "cartons": 1, "ppc": ppc, "carton": c.carton_code, "sku_id": c.sku_id},
        evidence=[
            _ev("RFID exit gate", when, f"EPC {epc}: 3 reads in 1.8 s, RSSI −51 → −47 dBm (moving toward the exit)."),
            _ev("WMS scans", c.approved_at or when, f"Carton approved at packing and palletised on {pal.code} "
                                                    f"({pal.status.lower()}); no pick, load or move scan for the carton after that."),
            _ev("RFID badges", when, "Badges through the gate within ±60 s: "
                                     + ", ".join(f"{e.code} ({e.role})" for e in badges) + "."),
            _ev("CCTV", when, f"Gate and staging-lane footage ({_cam(f, '06')}) {when - timedelta(minutes=2):%H:%M}–"
                              f"{when + timedelta(minutes=2):%H:%M} retained."),
        ])


def _after_hours_access(db, f, when, rng, historic):
    emp = (_emps(db, f, roles=["Sewer", "Bagger", "Filler", "Packaging operator"], rng=rng) or [None])[0]
    if emp is None:
        return None
    close = datetime.combine(when.date(), STORE_HOURS[1])
    # history: a late-evening read; live: the read that just happened (never in the future)
    t = datetime.combine(when.date(), time(22, rng.randint(34, 56))) if historic else max(when, close + timedelta(minutes=1))
    dur = rng.randint(3, 9)
    late = int((t - close).total_seconds() // 60)
    return dict(
        kind="after_hours_access", severity="warning", detected_at=t,
        title=f"Badge {emp.code} opened the material store after closing",
        summary=(f"{emp.name} ({emp.role}, shift {emp.shift}) badged into the material store at {t:%H:%M}, "
                 f"{late} min after the 22:30 close; {emp.role} badges are not on the store access list."),
        zone="Material store", area_ref="STORE", employee_id=emp.id,
        item="Pillow shells · labels · zips", item_kind="packaging", qty=0, unit="units", value_usd=0,
        camera_code=_cam(f, "07") if f.code == "KNV" else None,
        evidence=[
            _ev("Door controller", t, f"Badge {emp.code} granted by the shift-2 override list; door open {dur} min {rng.randint(5, 55)} s."),
            _ev("RFID", t, f"Interval in zone 'Material store' {t:%H:%M}–{t + timedelta(minutes=dur):%H:%M}; no store-keeper badge present."),
            _ev("Stock", t, "Store system: no material issue booked during the visit."),
        ] + ([_ev("CCTV", t, f"Store-side camera {_cam(f, '07')}: footage of the visit retained.")] if f.code == "KNV" else []))


def _consumption_variance(db, f, when, rng, historic):
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id == f.id).order_by(m.ProductionLine.id)))
    if not lines:
        return None

    def output(day, before_hour=None) -> dict[int, int]:
        q = select(m.ProductionHourly.line_id, func.coalesce(func.sum(m.ProductionHourly.actual), 0)).where(
            m.ProductionHourly.factory_id == f.id, m.ProductionHourly.date == day)
        if before_hour is not None:
            q = q.where(m.ProductionHourly.hour < before_hour)  # completed hours only (history: not after the case)
        return {lid: int(v) for lid, v in db.execute(q.group_by(m.ProductionHourly.line_id))}
    # the line counter in the evidence is the line's real output: today so far, or yesterday early in the morning
    period, outs = "today", output(when.date(), when.hour)
    if not any(v >= 300 for v in outs.values()):
        period, outs = "yesterday", output(when.date() - timedelta(days=1))
    cands = [ln for ln in lines if outs.get(ln.id, 0) >= 300]
    if not cands:
        return None
    ln = rng.choice(cands)
    out = outs[ln.id]
    sku = db.get(m.Sku, ln.current_sku_id) if ln.current_sku_id else None
    fill = sku.fill_kg if sku else 0.7
    fibre = db.get(m.Material, sku.fill_material_id) if sku and sku.fill_material_id else None
    std = out * fill * 1.015
    var = rng.uniform(3.2, 6.8)
    kg = std * var / 100
    cost = fibre.unit_cost if fibre and fibre.unit_cost else 2.4
    bales = int((std + kg) // 250) + 1
    mean_fill = fill * (1 + rng.uniform(0.0, var / 200))
    allowance = out * fill * 0.015
    return dict(
        kind="consumption_variance", severity="critical" if kg * cost >= 400 else "warning",
        title=f"{ln.name}: fibre issued {var:.1f}% above standard",
        summary=(f"{std + kg:,.0f} kg of {fibre.name if fibre else 'fibre'} issued vs {std:,.0f} kg for {out:,} pillows × "
                 f"{fill:.2f} kg (+1.5% scrap allowance): {kg:,.0f} kg unaccounted for {period}."),
        zone=ln.name, area_ref=str(ln.id), line_id=ln.id,
        item=fibre.name if fibre else "Polyester fibre", item_kind="raw", qty=round(kg, 1), unit="kg",
        value_usd=round(kg * cost, 2),
        data={"period": period, "output": out},
        evidence=[
            _ev("Material issues", when, f"{bales} bale issue slips to {ln.name} {period} from the material store."),
            _ev("Line counter", when, f"{out:,} pillows filled {period} ({sku.code if sku else 'current SKU'}, {fill:.2f} kg standard fill)."),
            _ev("Scrap log", when, f"{allowance * rng.uniform(0.5, 0.95):.1f} kg of fibre scrap and sweepings logged — "
                                   f"within the {allowance:.1f} kg allowance."),
            _ev("Fill-weight check", when, f"Hourly check-weigher mean {mean_fill * 1000:.0f} g vs {fill * 1000:.0f} g spec."),
        ])


def _seal_pair(rng) -> tuple[int, int]:
    seal = rng.randint(4400000, 4499999)
    seal2 = int(str(seal)[:-2] + str(seal)[-1] + str(seal)[-2]) if seal % 100 // 10 != seal % 10 else seal + 7
    return seal, seal2


def _receiving_shortage(db, f, when, rng, historic):
    """A goods receipt that was really booked short (GoodsReceipt.shortage_qty > 0). The case quotes the receipt's own
    figures. History may fall back to a synthetic ASN reference that matches no shipment record."""
    taken = _taken(db, m.LossIncident.shipment_id)
    grs = [g for g in db.scalars(select(m.GoodsReceipt).where(
        m.GoodsReceipt.factory_id == f.id, m.GoodsReceipt.shortage_qty > 0.5, m.GoodsReceipt.shipment_id.is_not(None),
        m.GoodsReceipt.confirmed_at > when - timedelta(hours=24 if historic else 48), m.GoodsReceipt.confirmed_at <= when)
        .order_by(m.GoodsReceipt.id)) if g.shipment_id not in taken]
    seal, seal2 = _seal_pair(rng)
    if grs:
        g = rng.choice(grs)
        s = db.get(m.InboundShipment, g.shipment_id)
        mat = db.get(m.Material, g.material_id)
        ref, expected, received, short, accepted = s.ref, g.expected_qty, g.received_qty, g.shortage_qty, g.accepted_qty
        dock, carrier, t_count = s.dock or "R1", s.carrier, g.confirmed_at or when
        booked = f"Goods receipt {g.number}"
        data = {"receipt_id": g.id, "receipt": g.number, "expected": expected, "received": received, "shortage": short}
    elif historic:
        mats = list(db.scalars(select(m.Material).where(m.Material.category.in_(("Fibre", "Shell"))).order_by(m.Material.id)))
        if not mats:
            return None
        mat, s = rng.choice(mats), None
        expected = rng.choice([8000, 10000, 12000])
        short = round(expected * rng.uniform(0.018, 0.045), -1) or 10
        received = accepted = expected - short
        while True:  # a reference that is not any real shipment
            ref = f"ASN-{f.code}-{rng.randint(5100, 5900)}"
            if not db.scalar(select(m.InboundShipment.id).where(m.InboundShipment.ref == ref)):
                break
        dock, carrier, t_count = rng.choice(["R1", "R2"]), "Carrier", when
        booked = "Receipt"
        data = {"synthetic_ref": True, "expected": expected, "received": received, "shortage": short}
    else:
        return None  # nothing was received short: no case
    unit = mat.uom if mat and mat.uom else "kg"
    return dict(
        kind="receiving_shortage", severity="warning",
        title=f"{ref}: {short:,.0f} {unit} short — seal number mismatch",
        summary=(f"{booked}: received {received:,.0f} of {expected:,.0f} {unit} of {mat.name if mat else 'material'} "
                 f"({short:,.0f} short, {accepted:,.0f} accepted to stock); the trailer seal ({seal2}) does not match "
                 f"the bill of lading ({seal})."),
        zone=f"Receiving dock {dock}", area_ref=dock, shipment_id=s.id if s else None,
        item=mat.name if mat else "Raw material", item_kind="raw", qty=short, unit=unit,
        value_usd=round(short * (mat.unit_cost if mat and mat.unit_cost else 2.0), 2),
        data=data,
        evidence=[
            _ev("Receiving count", t_count, f"Counted {received:,.0f} {unit} against the ASN quantity {expected:,.0f} {unit}."),
            _ev("Seal check", t_count, f"Seal on the trailer {seal2}; seal on the BOL {seal}. Photo attached to the receipt."),
            _ev("Weighbridge", t_count, f"Net weight {short * (1 if unit == 'kg' else 0.35):,.0f} kg below the BOL weight."),
            _ev("Carrier", when, f"{carrier or 'Carrier'} driver statement taken at the dock."),
        ])


def _carton_kg(sku: m.Sku | None, ppc: int) -> float:
    return ppc * ((sku.fill_kg if sku else 0.7) + 0.12) + 0.6


def _dispatch_mismatch(db, f, when, rng, historic):
    """Two consistent forms:
    count  — a truck still loading: the dock-door portal counts cartons beyond the manifest (= the order quantity the
             truck will be loaded with), so the truck is held before departure;
    weigh  — a truck that just left: the exit weighbridge reads heavier than its loaded quantity, while the dock-door
             count matched the manifest (= the truck's loaded_qty)."""
    taken = _taken(db, m.LossIncident.truck_id)
    loading = [] if historic else [t for t in db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id == f.id, m.TruckAppointment.status == "Loading",
        m.TruckAppointment.order_id.is_not(None)).order_by(m.TruckAppointment.id)) if t.id not in taken]
    departed = [t for t in db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id == f.id, m.TruckAppointment.status == "Departed", m.TruckAppointment.order_id.is_not(None),
        m.TruckAppointment.loaded_qty > 0, m.TruckAppointment.departed_at > when - timedelta(hours=6 if historic else 2),
        m.TruckAppointment.departed_at <= when).order_by(m.TruckAppointment.id)) if t.id not in taken]
    cam = _cam(f, "06")
    if loading:
        t = rng.choice(loading)
        o = db.get(m.CustomerOrder, t.order_id)
        sku = db.get(m.Sku, o.sku_id)
        ppc = sku.pillows_per_carton
        manifest = -(-o.qty // ppc)
        picked = db.scalar(select(func.coalesce(func.sum(m.Allocation.cartons), 0)).where(
            m.Allocation.order_id == o.id, m.Allocation.status.in_(("Picked", "Loaded")))) or 0
        extra = rng.randint(1, 4)
        counted = manifest + extra
        at = max(when, t.loading_start or when)
        return dict(
            kind="dispatch_mismatch", severity="critical", truck_id=t.id, detected_at=at,
            title=f"{t.ref} at {t.dock}: {counted} cartons counted vs {manifest} on the manifest",
            summary=(f"The dock-door counter saw {extra} more carton{'s' if extra > 1 else ''} go onto {t.ref} ({o.number}) than "
                     f"the {manifest} on the manifest while loading; extra cartons leaving the site are a pilferage signal. "
                     "The load is to be recounted before the truck leaves."),
            zone=f"Dock {t.dock}", area_ref=t.dock,
            item=f"{sku.code} · {sku.description}", item_kind="fg",
            qty=extra * ppc, unit="pillows", value_usd=round(extra * ppc * std_cost(sku), 2), camera_code=cam,
            data={"flavor": "count", "manifest_cartons": manifest, "counted": counted, "order": o.number,
                  "cartons": extra, "ppc": ppc, "sku_id": sku.id},
            evidence=[
                _ev("Dock-door counter", at, f"Vision + RFID portal counted {counted} cartons through dock {t.dock} for {t.ref}; "
                                             f"{extra} tag{'s' if extra > 1 else ''} not on {o.number}'s pick list."),
                _ev("Manifest", at, f"Bill of lading lists {manifest} cartons ({o.qty:,} pillows of {sku.code}); "
                                    f"{picked} cartons picked for {o.number}."),
                _ev("Dock hold", at, f"{t.ref} flagged at {t.dock}: recount the trailer against the manifest before departure."),
                _ev("CCTV", at, f"Dock / staging camera {cam} footage of the loading retained."),
            ])
    if departed or historic:
        if departed:
            t = rng.choice(departed)
            o = db.get(m.CustomerOrder, t.order_id)
            sku = db.get(m.Sku, o.sku_id)
            ref, dock, loaded, order_no = t.ref, t.dock or "D1", t.loaded_qty, o.number
            at = min(when, t.departed_at + timedelta(minutes=rng.randint(4, 12)))
        else:  # history with no truck leaving in the window: a reference that is not any real truck
            sku = db.scalar(select(m.Sku).join(m.Pallet, m.Pallet.sku_id == m.Sku.id).where(
                m.Pallet.factory_id == f.id).order_by(m.Sku.id).limit(1)) or db.scalar(select(m.Sku).limit(1))
            while True:
                ref = f"TRK-{rng.randint(5000, 5899)}"
                if not db.scalar(select(m.TruckAppointment.id).where(m.TruckAppointment.ref == ref)):
                    break
            t, dock, order_no, at = None, rng.choice(["D1", "D2", "D3", "D4"]), "customer order", when
            loaded = rng.randint(40, 90) * sku.pillows_per_carton
        ppc = sku.pillows_per_carton
        manifest = -(-loaded // ppc)
        ckg = _carton_kg(sku, ppc)
        expected = round(14800 + manifest * ckg)
        tol = max(20.0, 0.0025 * expected)  # weighbridge tolerance: only a variance well above it raises a case
        est = max(rng.randint(1, 3), int(-(-tol * rng.uniform(1.15, 1.6) // ckg)))
        over = round(est * ckg * rng.uniform(0.97, 1.03))
        return dict(
            kind="dispatch_mismatch", severity="warning", truck_id=t.id if t else None, detected_at=at,
            title=f"{ref}: exit weight {over} kg over the {manifest}-carton manifest",
            summary=(f"The exit weighbridge read {ref} ({order_no}) {over} kg heavier than expected for the {manifest} cartons "
                     f"({loaded:,} pillows) on the bill of lading — about {est} carton{'s' if est > 1 else ''} of {sku.code}. "
                     "The dock-door count matched the manifest, so the extra weight is unmanifested goods or dunnage."),
            zone=f"Dock {dock}", area_ref=dock,
            item=f"{sku.code} · {sku.description}", item_kind="fg",
            qty=est * ppc, unit="pillows", value_usd=round(est * ppc * std_cost(sku), 2), camera_code=cam,
            data={"flavor": "weigh", "manifest_cartons": manifest, "loaded_qty": loaded, "synthetic_ref": t is None,
                  "cartons": est, "ppc": ppc, "sku_id": sku.id},
            evidence=[
                _ev("Weighbridge", at, f"Exit gross {expected + over:,} kg vs {expected:,} kg expected (tare + {manifest} cartons); "
                                       f"tolerance ±{tol:,.0f} kg."),
                _ev("Dock-door counter", at, f"{manifest} cartons counted through dock {dock} for {ref} — matches the manifest "
                                             f"and the {loaded:,} pillows loaded."),
                _ev("Manifest", at, f"Bill of lading lists {manifest} cartons ({loaded:,} pillows)."),
                _ev("CCTV", at, f"Dock / staging camera {cam} footage of the loading retained."),
            ])
    return None


def _scrap_reconciliation(db, f, when, rng, historic):
    """Pillows from cartons rejected at QA (status Rejected; their detected_count is scrapped) since the last scrap
    weigh-out, against the cage weight. Reworked cartons are recounted and approved, so they are not scrap.
    No rejected cartons → nothing to reconcile → no case."""
    last = db.scalar(select(func.max(m.LossIncident.detected_at)).where(
        m.LossIncident.factory_id == f.id, m.LossIncident.kind == "scrap_reconciliation", m.LossIncident.detected_at <= when))
    since = max(when - timedelta(hours=24), last) if last else when - timedelta(hours=24)
    rej = list(db.scalars(select(m.Carton).where(m.Carton.factory_id == f.id, m.Carton.status == "Rejected",
                                                 m.Carton.created_at > since, m.Carton.created_at <= when).order_by(m.Carton.id)))
    logged = sum(c.detected_count or 0 for c in rej)
    if logged <= 0:
        return None
    skus = {s.id: s for s in db.scalars(select(m.Sku).where(m.Sku.id.in_({c.sku_id for c in rej})))}
    unit_cost = sum((c.detected_count or 0) * std_cost(skus.get(c.sku_id)) for c in rej) / logged  # the rejected SKU mix
    fill = sum((c.detected_count or 0) * (skus[c.sku_id].fill_kg if c.sku_id in skus else 0.7) for c in rej) / logged
    missing = rng.randint(1, max(1, min(12, logged // 3)))
    reworked = db.scalar(select(func.count(m.Carton.id)).where(
        m.Carton.factory_id == f.id, m.Carton.status == "Reworked", m.Carton.created_at > since, m.Carton.created_at <= when)) or 0
    codes = ", ".join(c.hold_code or c.carton_code or str(c.id) for c in rej[:3]) + (" …" if len(rej) > 3 else "")
    sku_txt = ", ".join(sorted({skus[c.sku_id].code for c in rej if c.sku_id in skus}))
    return dict(
        kind="scrap_reconciliation", severity="warning",
        title=f"{missing} rejected pillow{'s' if missing > 1 else ''} missing from the scrap reconciliation",
        summary=(f"The QA reject log shows {logged} pillows scrapped from {len(rej)} rejected carton{'s' if len(rej) > 1 else ''} "
                 f"since the last weigh-out; the scrap cage weighs out at {logged - missing} pillows' worth."),
        zone="QA hold & scrap cage", area_ref="QA",
        item=f"Rejected pillows (scrap) · {sku_txt}", item_kind="fg", qty=missing, unit="pillows",
        value_usd=round(missing * unit_cost, 2), camera_code=_cam(f, "04"),
        data={"carton_ids": [c.id for c in rej], "logged": logged, "since": iso(since)},
        evidence=[
            _ev("QA reject log", when, f"{logged} pillows in {len(rej)} rejected carton{'s' if len(rej) > 1 else ''} ({codes}) "
                                       f"since the last weigh-out at {since:%a %H:%M}."),
            _ev("Scrap weigh-out", when, f"Cage net weight {(logged - missing) * fill:.1f} kg ≈ {logged - missing} pillows at "
                                         f"{fill:.2f} kg average fill."),
            _ev("Rework", when, f"{reworked} carton{'s' if reworked != 1 else ''} reworked at packing in the same period — "
                                "recounted and approved, not scrap."),
            _ev("CCTV", when, f"Packing camera {_cam(f, '04')} overlooking the QA cage: footage retained."),
        ])


def _cctv_unbadged(db, f, when, rng, historic):
    evs = _cctv_events(f)
    if not evs:
        return None
    e = rng.choice(evs)
    return dict(
        kind="cctv_unbadged", severity="info", camera_code=e["camera"], feed_t=e["t"],
        title=f"No badge read for a person in the {e['zone_name']} ({e['camera']})",
        summary=(f"Vision tracked a person entering the {e['zone_name']} {e['t']:.1f} s into the {e['camera']} clip while the "
                 "zone reader logged no badge. Security asked to verify on the floor — an access check, not an accusation."),
        zone=e["zone_name"], area_ref=e["camera"], item="Finished goods in staging", item_kind="fg", qty=0, unit="",
        value_usd=0,
        evidence=[
            _ev("CCTV vision", when, f"{e['camera']} ({e['title']}): person track entered the badge-only zone and stayed "
                                     f"{e.get('dwell', 2.0):.1f} s."),
            _ev("RFID zone reader", when, "No badge read in the zone within ±10 s of the entry."),
        ])


BUILDERS = {"fg_cycle_count": _fg_cycle_count, "exit_gate_tag": _exit_gate_tag, "after_hours_access": _after_hours_access,
            "consumption_variance": _consumption_variance, "receiving_shortage": _receiving_shortage,
            "dispatch_mismatch": _dispatch_mismatch, "scrap_reconciliation": _scrap_reconciliation,
            "cctv_unbadged": _cctv_unbadged}

CLOSE_NOTES = {
    ("fg_cycle_count", "Process error"): "Cartons found mis-slotted in the next bay; location corrected and put-away retrained.",
    ("fg_cycle_count", "Recovered"): "Cartons found in the staging lane without a move scan; re-scanned to stock.",
    ("fg_cycle_count", "Confirmed loss"): "Shortage confirmed after recount and scan trace; written off finished-goods stock at standard cost; footage passed to HR and security.",
    ("exit_gate_tag", "Recovered"): "Carton recovered at the gate and returned to its pallet; incident report filed with HR.",
    ("exit_gate_tag", "False alarm"): "Stray read: the carton sat on the staging lane 3 m from the gate antenna; reader power reduced.",
    ("exit_gate_tag", "Confirmed loss"): "Carton not recovered and written off its pallet; badge holders interviewed; escalated to HR and local police.",
    ("after_hours_access", "False alarm"): "Authorised task: the shift-2 supervisor sent the operator for shell top-up; access list updated.",
    ("after_hours_access", "Process error"): "Store left on the override list after 22:30; closing checklist and door schedule fixed.",
    ("after_hours_access", "Confirmed loss"): "Spot count short of 40 shells; badge access revoked pending HR review.",
    ("consumption_variance", "Process error"): "Filling head over-filling; recalibrated and fill weight back on spec.",
    ("consumption_variance", "Confirmed loss"): "Issues above output after the scrap weigh; bale issues now need two signatures.",
    ("consumption_variance", "False alarm"): "Late issue-slip entry from shift 1; variance cleared after posting.",
    ("receiving_shortage", "Confirmed loss"): "Short delivery accepted with a carrier claim filed; seal photos attached.",
    ("receiving_shortage", "Recovered"): "Supplier issued a credit note for the shortage.",
    ("receiving_shortage", "Process error"): "Supplier keyed the ASN wrong; corrected ASN matches the count.",
    ("dispatch_mismatch", "Process error"): "Extra cartons belonged to the next order on the lane; unloaded and re-staged before departure.",
    ("dispatch_mismatch", "Recovered"): "Extra cartons taken off before departure; trailer re-sealed against the manifest.",
    ("dispatch_mismatch", "Confirmed loss"): "Truck had left; consignee confirmed cartons beyond the manifest; written off finished-goods stock and the carrier notified.",
    ("dispatch_mismatch:weigh", "Process error"): "Weight variance traced to extra dunnage and a heavier pallet type; tare table corrected.",
    ("dispatch_mismatch:weigh", "Recovered"): "Carrier returned the unmanifested cartons; re-scanned to stock.",
    ("dispatch_mismatch:weigh", "False alarm"): "Weighbridge re-zeroed; the re-weigh matched the manifest.",
    ("scrap_reconciliation", "Process error"): "Rework re-queued without a scan; records corrected.",
    ("scrap_reconciliation", "Confirmed loss"): "Scrap pillows unaccounted for; cage locked and two-person removal introduced.",
    ("scrap_reconciliation", "Recovered"): "Pillows found in the rework rack; reconciled.",
    ("cctv_unbadged", "False alarm"): "Employee wearing the badge under a jacket; reminded of the badge policy.",
    ("cctv_unbadged", "Process error"): "Contractor without a visitor badge; escorted and badged at reception.",
}
STEP_RESULTS = {
    "recount": "count confirmed", "trace": "scan trail exported", "cctv": "footage reviewed", "adjacent": "adjacent bays checked",
    "stop": "exit stopped at the gate", "epc": "EPC matched to the carton", "badges": "badge list attached",
    "verify": "supervisor contacted", "spot": "spot count recorded", "access": "badge restricted",
    "reconcile": "issues reconciled", "scrap": "scrap weighed", "calibrate": "check-weigher verified", "store": "issue slips audited",
    "quarantine": "delivery quarantined", "weigh": "weights compared", "count": "recount recorded", "claim": "claim raised",
    "hold": "truck held at the gate", "reseal": "trailer re-sealed", "requeue": "rework checked", "lock": "cage locked",
    "log": "visitor log checked", "badge": "badge issued / escorted",
}


def _create(db: Session, f: m.Factory, kind: str, when: datetime, rng: random.Random, historic=False) -> m.LossIncident | None:
    spec = BUILDERS[kind](db, f, when, rng, historic)
    if spec is None:
        return None
    spec.pop("kind", None)
    k = KINDS[kind]
    detected = spec.pop("detected_at", when)
    data = spec.pop("data", None) or {}
    inc = m.LossIncident(
        ref=_ref(db, f), factory_id=f.id, kind=kind, stage=k["stage"], status="Open", area=k["area"],
        channel=k["channel"], detected_at=detected, updated_at=detected, recovered_qty=0,
        assigned_to=k["owner"],
        timeline=[{"ts": iso(detected), "by": SYSTEM, "step": "open", "data": data,
                   "text": f"Case opened from {k['channel'].lower()}; assigned to {k['owner']}."}],
        **spec)
    db.add(inc)
    db.flush()
    log_event(db, detected, f.id, k["stage"], "loss_case", f"{inc.ref} opened: {inc.title}", "loss_incident", inc.id, SYSTEM)
    return inc


def _done_steps(inc: m.LossIncident) -> list[str]:
    return [t["step"] for t in inc.timeline or [] if t.get("step") not in ("open", "resolve", "note", "reopen", "assign")]


def next_step(inc: m.LossIncident):
    done = set(_done_steps(inc))
    return next(((k, t) for k, t in KINDS[inc.kind]["playbook"] if k not in done), None)


def _append(inc: m.LossIncident, entry: dict) -> None:
    inc.timeline = [*(inc.timeline or []), entry]


def _do_step(inc: m.LossIncident, key: str, at: datetime, by: str, user=False, note: str | None = None) -> None:
    text = dict(KINDS[inc.kind]["playbook"]).get(key, key)
    _append(inc, {"ts": iso(at), "by": by, "step": key, "user": user,
                  "text": f"{text} — {note or STEP_RESULTS.get(key, 'done')}."})
    if inc.status == "Open":
        inc.status = "Investigating"
    inc.updated_at = at


# ─── stock bookings: a confirmed loss leaves the book ─────────────────
def _booked(inc: m.LossIncident) -> dict | None:
    """The stock adjustment currently standing for this case (None if none, or if it was reversed)."""
    out = None
    for t in inc.timeline or []:
        if t.get("reversal"):
            out = None
        if t.get("adjustment"):
            out = t["adjustment"]
    return out


def _release(db: Session, p: m.Pallet, cartons: int, ppc: int, at: datetime, by: str, ref: str) -> int:
    """Release `cartons` of the pallet's reservations (newest first), keeping the Allocation rows,
    pallet.allocated_cartons and order.allocated_qty in step. Returns the cartons released."""
    done = 0
    for a in db.scalars(select(m.Allocation).where(m.Allocation.pallet_id == p.id, m.Allocation.status == "Reserved")
                        .order_by(m.Allocation.id.desc())):
        if done >= cartons:
            break
        k = min(cartons - done, a.cartons)
        new_c = a.cartons - k
        new_p = min(a.pillows, new_c * ppc)
        o = db.get(m.CustomerOrder, a.order_id)
        o.allocated_qty -= a.pillows - new_p
        p.allocated_cartons -= k
        done += k
        log_event(db, at, p.factory_id, "warehouse", "allocation",
                  f"{k} carton{'s' if k > 1 else ''} of {o.number}'s reservation on {p.code} released: written off by {ref}; "
                  f"{o.number} is re-allocated from free stock", "order", o.id, by)
        if new_c == 0:
            db.delete(a)
        else:
            a.cartons, a.pillows = new_c, new_p
    db.flush()
    return done


def _write_off_fg(db: Session, inc: m.LossIncident, cartons: int, at: datetime, by: str) -> dict | None:
    """Take `cartons` off the case's pallet (or, without one, off stored pallets of the case's SKU, free stock first).
    Cartons never drop below the pallet's reservations: if reserved cartons are lost, that reservation is released."""
    d = _data(inc)
    if d.get("pallet_id"):
        p = db.get(m.Pallet, d["pallet_id"])
        pallets = [p] if p and p.status in ("Stored", "Pending Put-away") else []
    elif d.get("sku_id"):
        pallets = sorted(db.scalars(select(m.Pallet).where(m.Pallet.factory_id == inc.factory_id, m.Pallet.sku_id == d["sku_id"],
                                                          m.Pallet.status == "Stored", m.Pallet.cartons > 0)),
                         key=lambda p: (p.cartons <= p.allocated_cartons, p.stored_at or at, p.id))
    else:
        pallets = []
    left, done = cartons, []
    for p in pallets:
        if left <= 0:
            break
        take = min(left, p.cartons)
        if take <= 0:
            continue
        sku = db.get(m.Sku, p.sku_id)
        ppc = sku.pillows_per_carton if sku else d.get("ppc", 6)
        over = take - (p.cartons - p.allocated_cartons)
        released = _release(db, p, over, ppc, at, by, inc.ref) if over > 0 else 0
        loc = db.get(m.StorageLocation, p.location_id) if p.location_id else None
        p.cartons -= take
        if p.cartons <= 0:
            p.cartons, p.status, p.location_id = 0, "Depleted", None
        left -= take
        done.append({"pallet_id": p.id, "pallet": p.code, "cartons": take, "pillows": take * ppc, "released": released})
        log_event(db, at, inc.factory_id, "warehouse", "inventory_adjustment",
                  f"{inc.ref}: {take} carton{'s' if take > 1 else ''} ({take * ppc} pillows {sku.code if sku else ''}) written off "
                  f"pallet {p.code}{f' at {loc.code}' if loc else ''} — confirmed loss; {p.cartons} cartons left on the book",
                  "pallet", p.id, by)
    return {"type": "fg", "pallets": done} if done else None


def _shell_stock(db: Session, fid: int) -> m.MaterialStock | None:
    return db.scalar(select(m.MaterialStock).join(m.Material, m.Material.id == m.MaterialStock.material_id).where(
        m.MaterialStock.factory_id == fid, m.Material.category == "Shell").order_by(m.MaterialStock.on_hand.desc()).limit(1))


def _book_loss(db: Session, inc: m.LossIncident, lost_qty: float, at: datetime, by: str) -> dict | None:
    d = _data(inc)
    if inc.kind in FG_KINDS and d.get("cartons"):
        ppc = d.get("ppc") or 6
        cartons = min(d["cartons"], int(round(lost_qty / ppc)))
        return _write_off_fg(db, inc, cartons, at, by) if cartons > 0 else None
    if inc.kind == "after_hours_access" and inc.unit == "shells":
        st = _shell_stock(db, inc.factory_id)
        if st is None:
            return None
        take = min(float(lost_qty), st.on_hand)
        st.on_hand -= take
        mat = db.get(m.Material, st.material_id)
        log_event(db, at, inc.factory_id, "procurement", "inventory_adjustment",
                  f"{inc.ref}: {take:,.0f} {mat.code} shells written off the material store — confirmed loss", "material", mat.id, by)
        return {"type": "material", "stock_id": st.id, "qty": take}
    return None  # raw-material variances are already out of stock (issued / received short); scrap is already scrapped


def _reverse(db: Session, inc: m.LossIncident, adj: dict, at: datetime, by: str) -> dict:
    """Undo a write-off when a reopened case is closed with another outcome (reservations are re-made by allocation)."""
    if adj.get("type") == "fg":
        for x in adj.get("pallets", []):
            p = db.get(m.Pallet, x["pallet_id"])
            if p and p.status in ("Stored", "Pending Put-away"):
                p.cartons += x["cartons"]
                log_event(db, at, inc.factory_id, "warehouse", "inventory_adjustment",
                          f"{inc.ref}: write-off of {x['cartons']} carton(s) on {p.code} reversed", "pallet", p.id, by)
    elif adj.get("type") == "material":
        st = db.get(m.MaterialStock, adj["stock_id"])
        if st:
            st.on_hand += adj["qty"]
            log_event(db, at, inc.factory_id, "procurement", "inventory_adjustment",
                      f"{inc.ref}: write-off of {adj['qty']:,.0f} shells reversed", "material", st.material_id, by)
    return adj


def _resolve(db: Session, inc: m.LossIncident, outcome: str, at: datetime, by: str, user=False, note=None, recovered=None):
    inc.status, inc.outcome, inc.resolved_at, inc.updated_at = "Resolved", outcome, at, at
    if outcome == "Recovered":
        inc.recovered_qty = inc.qty if recovered is None else recovered
    elif outcome == "Confirmed loss" and inc.kind == "after_hours_access" and not inc.qty:
        st = _shell_stock(db, inc.factory_id)
        mat = db.get(m.Material, st.material_id) if st else None
        inc.qty, inc.unit = 40, "shells"
        inc.value_usd = round(40 * (mat.unit_cost if mat and mat.unit_cost else 1.15), 2)
        if mat:
            inc.item = mat.name
    prior = _booked(inc)
    rev = adj = None
    if prior is not None and outcome != "Confirmed loss":
        rev = _reverse(db, inc, prior, at, by)
        prior = None
    lost = inc.qty if outcome == "Confirmed loss" else (
        (inc.qty or 0) - (inc.recovered_qty or 0) if outcome == "Recovered" else 0)
    if lost and lost > 0 and prior is None:
        adj = _book_loss(db, inc, lost, at, by)
    key = (f"{inc.kind}:{_data(inc).get('flavor')}", outcome)
    text = note or CLOSE_NOTES.get(key) or CLOSE_NOTES.get((inc.kind, outcome), outcome)
    inc.note = text
    entry = {"ts": iso(at), "by": by, "step": "resolve", "user": user, "outcome": outcome, "text": f"Closed — {outcome}: {text}"}
    if rev:
        entry["reversal"] = rev
    if adj:
        entry["adjustment"] = adj
    _append(inc, entry)
    log_event(db, at, inc.factory_id, inc.stage, "loss_case", f"{inc.ref} closed ({outcome}) by {by}", "loss_incident", inc.id, by)


def _play_through(db, inc: m.LossIncident, rng: random.Random, until: datetime, resolve: bool) -> None:
    """Simulated handling: the owner works the playbook, then closes with an outcome."""
    at = inc.detected_at + timedelta(minutes=rng.randint(6, 20))
    for key, _ in KINDS[inc.kind]["playbook"]:
        if at > until:
            return
        _do_step(inc, key, at, inc.assigned_to)
        at += timedelta(minutes=rng.randint(18, 70))
    if resolve and at <= until:
        _resolve(db, inc, _pick_outcome(inc.kind, rng), at, inc.assigned_to)


# ─── simulation hooks ─────────────────────────────────────────────────
def simulate(db: Session, a: datetime, b: datetime, factories) -> None:
    """New cases at a low rate per plant, and simulated handling of open ones."""
    minutes = (b - a).total_seconds() / 60
    if minutes <= 0:
        return
    for f in factories:
        if RNG.random() < minutes / 330:
            kinds = list(WEIGHTS)
            kind = RNG.choices(kinds, weights=[WEIGHTS[k] for k in kinds])[0]
            _create(db, f, kind, b, RNG)
        if b.time() >= time(22, 30) and a.time() < time(22, 30) and RNG.random() < 0.35:
            _create(db, f, "after_hours_access", b, RNG)
    # handling: untouched cases advance one step every ~1–3 hours of plant time (investigations span a shift or two)
    for inc in db.scalars(select(m.LossIncident).where(m.LossIncident.status != "Resolved")):
        if any(t.get("user") for t in inc.timeline or []):
            continue  # a person is working this case in the app
        idle = (b - (inc.updated_at or inc.detected_at)).total_seconds() / 60
        if idle < RNG.uniform(60, 180):
            continue
        nxt = next_step(inc)
        if nxt:
            _do_step(inc, nxt[0], b, inc.assigned_to)
        elif idle > 120:
            _resolve(db, inc, _pick_outcome(inc.kind, RNG), b, inc.assigned_to)


HISTORY = [  # (kind, days ago, hour) — a week of closed cases for each plant
    ("fg_cycle_count", 6, 9.5), ("receiving_shortage", 6, 13.2), ("exit_gate_tag", 5, 22.6),
    ("consumption_variance", 4, 15.4), ("dispatch_mismatch", 4, 11.0), ("scrap_reconciliation", 3, 14.3),
    ("after_hours_access", 3, 22.7), ("fg_cycle_count", 2, 8.8), ("exit_gate_tag", 2, 15.1), ("cctv_unbadged", 2, 16.4),
    ("consumption_variance", 1, 10.2), ("receiving_shortage", 1, 14.6), ("dispatch_mismatch", 1, 17.9),
]
TODAY = {  # live cases at the start of the demo, different per plant
    "KNV": ["exit_gate_tag", "fg_cycle_count", "consumption_variance", "cctv_unbadged"],
    "PHX": ["dispatch_mismatch", "fg_cycle_count", "scrap_reconciliation", "cctv_unbadged"],
    "CMH": ["consumption_variance", "dispatch_mismatch", "receiving_shortage", "cctv_unbadged"],
}
KEEP_LIVE = {"KNV": "exit_gate_tag", "PHX": "dispatch_mismatch", "CMH": "consumption_variance"}


def ensure_baseline(db: Session, now: datetime) -> int:
    """A week of case history per plant (once per database), so the workflow has examples."""
    from .common import get_setting, set_setting
    if get_setting(db, "loss_baseline"):
        return 0
    set_setting(db, "loss_baseline", iso(now))
    made = 0
    for f in db.scalars(select(m.Factory).order_by(m.Factory.id)):
        rng = random.Random(f"loss-{f.code}")
        for kind, days, hour in HISTORY:
            when = datetime.combine(now.date() - timedelta(days=days), time(0)) + timedelta(hours=hour)
            inc = _create(db, f, kind, when, rng, historic=True)
            if inc is not None:
                made += 1
                _play_through(db, inc, rng, when + timedelta(hours=8), resolve=True)
        today = TODAY.get(f.code, TODAY["KNV"])
        slots = sorted(rng.sample(range(-200, -15, 7), len(today)))
        live = []
        for kind, mins in zip(today, slots):
            when = max(now + timedelta(minutes=mins), datetime.combine(now.date(), time(6, 10)))
            inc = _create(db, f, kind, min(when, now), rng)
            if inc is None:
                continue  # no record supports this signal right now (e.g. nothing received short): no case
            made += 1
            _play_through(db, inc, rng, now, resolve=False)
            live.append(inc)
        # one case per plant stays at its first step so the demo can work it live (the planned kind, else the most severe)
        sev = {"critical": 0, "warning": 1, "info": 2}
        keep = next((i for i in live if i.kind == KEEP_LIVE.get(f.code)), None) or \
            min(live, key=lambda i: (sev.get(i.severity, 3), i.id), default=None)
        if keep is not None:
            keep.timeline = keep.timeline[:2]
            keep.status = "Investigating" if len(keep.timeline) > 1 else "Open"
    db.flush()
    return made


# ─── user actions ─────────────────────────────────────────────────────
class LossError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def act(db: Session, inc: m.LossIncident, action: str, now: datetime, user: str, *, step=None, outcome=None,
        note=None, recovered_qty=None, assignee=None) -> m.LossIncident:
    if action == "reopen":
        if inc.status != "Resolved":
            raise LossError(409, f"{inc.ref} is not closed")
        inc.status, inc.outcome, inc.resolved_at, inc.updated_at = "Investigating", None, None, now
        _append(inc, {"ts": iso(now), "by": user, "step": "reopen", "user": True, "text": note or "Case reopened."})
        return inc
    if inc.status == "Resolved":
        raise LossError(409, f"{inc.ref} is already closed ({inc.outcome})")
    if action == "assign":
        inc.assigned_to = (assignee or user)[:60]
        inc.status = "Investigating"
        inc.updated_at = now
        _append(inc, {"ts": iso(now), "by": user, "step": "assign", "user": True,
                      "text": f"Assigned to {inc.assigned_to}." + (f" {note}" if note else "")})
    elif action == "step":
        keys = [k for k, _ in KINDS[inc.kind]["playbook"]]
        if step not in keys:
            raise LossError(400, f"Unknown step '{step}' for {inc.kind}")
        if step in _done_steps(inc):
            raise LossError(409, "Step already recorded")
        _do_step(inc, step, now, user, user=True, note=note)
    elif action == "note":
        if not note:
            raise LossError(400, "A note is required")
        _append(inc, {"ts": iso(now), "by": user, "step": "note", "user": True, "text": note})
        inc.updated_at = now
    elif action == "resolve":
        if outcome not in OUTCOMES:
            raise LossError(400, f"Outcome must be one of {', '.join(OUTCOMES)}")
        if outcome == "Confirmed loss" and not (note or "").strip():
            raise LossError(400, "Confirming a loss needs a note (what was found, who was informed)")
        if recovered_qty is not None and (recovered_qty < 0 or recovered_qty > (inc.qty or 0) + 1e-9):
            raise LossError(400, "Recovered quantity must be between 0 and the case quantity")
        _resolve(db, inc, outcome, now, user, user=True, note=note, recovered=recovered_qty)
    else:
        raise LossError(400, f"Unknown action '{action}'")
    log_event(db, now, inc.factory_id, inc.stage, "loss_case", f"{inc.ref}: {action} by {user}", "loss_incident", inc.id, user)
    return inc


# ─── read models ──────────────────────────────────────────────────────
def _lookups(db: Session) -> dict:
    from . import cctv
    return {
        "factories": {f.id: f for f in db.scalars(select(m.Factory))},
        "feeds": {fd["camera"]: fd["id"] for fd in cctv.catalog()},
    }


def incident_dict(db: Session, inc: m.LossIncident, L: dict | None = None, detail: bool = False) -> dict:
    L = L or _lookups(db)
    k = KINDS[inc.kind]
    f = L["factories"].get(inc.factory_id)
    done = set(_done_steps(inc))
    nxt = next_step(inc) if inc.status != "Resolved" else None
    out = {
        "id": inc.id, "ref": inc.ref, "kind": inc.kind, "kind_label": k["label"], "icon": k["icon"],
        "stage": inc.stage, "twin_stage": k["twin_stage"], "severity": inc.severity, "status": inc.status,
        "outcome": inc.outcome, "title": inc.title, "summary": inc.summary, "zone": inc.zone, "area": inc.area,
        "area_ref": inc.area_ref, "channel": inc.channel, "item": inc.item, "item_kind": inc.item_kind,
        "qty": inc.qty, "unit": inc.unit, "value_usd": round(inc.value_usd or 0, 2),
        "recovered_qty": inc.recovered_qty or 0,
        "detected_at": iso(inc.detected_at), "updated_at": iso(inc.updated_at), "resolved_at": iso(inc.resolved_at),
        "assigned_to": inc.assigned_to, "factory": f.code if f else None, "factory_name": f.name if f else None,
        "camera": inc.camera_code, "feed_id": L["feeds"].get(inc.camera_code), "feed_t": inc.feed_t,
        "next_step": ({"key": nxt[0], "text": nxt[1]} if nxt else
                      {"key": "resolve", "text": "All checks done — close the case with an outcome"}
                      if inc.status != "Resolved" else None),
        "progress": [len(done), len(k["playbook"])],
        "employee_id": inc.employee_id, "line_id": inc.line_id, "location_id": inc.location_id,
        "truck_id": inc.truck_id, "shipment_id": inc.shipment_id,
    }
    if detail:
        emp = db.get(m.Employee, inc.employee_id) if inc.employee_id else None
        ln = db.get(m.ProductionLine, inc.line_id) if inc.line_id else None
        loc = db.get(m.StorageLocation, inc.location_id) if inc.location_id else None
        trk = db.get(m.TruckAppointment, inc.truck_id) if inc.truck_id else None
        shp = db.get(m.InboundShipment, inc.shipment_id) if inc.shipment_id else None
        data = _data(inc)
        pal = db.get(m.Pallet, data["pallet_id"]) if data.get("pallet_id") else None
        gr = db.get(m.GoodsReceipt, data["receipt_id"]) if data.get("receipt_id") else None
        out.update({
            "evidence": inc.evidence or [], "timeline": inc.timeline or [], "note": inc.note,
            "playbook": [{"key": key, "text": text, "done": key in done} for key, text in k["playbook"]],
            "outcomes": [o for o, _ in k["outcomes"]] + [o for o in OUTCOMES if o not in dict(k["outcomes"])],
            "links": {
                "employee": {"id": emp.id, "code": emp.code, "name": emp.name, "role": emp.role, "shift": emp.shift} if emp else None,
                "line": {"id": ln.id, "name": ln.name} if ln else None,
                "location": {"id": loc.id, "code": loc.code, "zone": loc.zone} if loc else None,
                "truck": {"id": trk.id, "ref": trk.ref, "dock": trk.dock, "status": trk.status} if trk else None,
                "shipment": {"id": shp.id, "ref": shp.ref, "status": shp.status} if shp else None,
                "pallet": {"id": pal.id, "code": pal.code, "status": pal.status, "cartons": pal.cartons,
                           "reserved_cartons": pal.allocated_cartons} if pal else None,
                "receipt": {"id": gr.id, "number": gr.number, "expected": gr.expected_qty, "received": gr.received_qty,
                            "shortage": gr.shortage_qty} if gr else None,
            },
            "stock_adjustment": _booked(inc),
        })
    return out


def incidents(db: Session, fids, *, status=None, stage=None, kind=None, camera=None, limit=None) -> list[m.LossIncident]:
    q = select(m.LossIncident).where(m.LossIncident.factory_id.in_(fids))
    if status == "active":
        q = q.where(m.LossIncident.status != "Resolved")
    elif status:
        q = q.where(m.LossIncident.status == status)
    if stage:
        q = q.where(m.LossIncident.stage == stage)
    if kind:
        q = q.where(m.LossIncident.kind == kind)
    if camera:
        q = q.where(m.LossIncident.camera_code == camera)
    rows = list(db.scalars(q))
    sev = {"critical": 0, "warning": 1, "info": 2}
    rows.sort(key=lambda r: (r.status == "Resolved", sev.get(r.severity, 3), -(r.detected_at or datetime.min).timestamp()))
    return rows[:limit] if limit else rows


def output_unit_cost(db: Session, fids, start: datetime, end: datetime) -> float:
    """Standard cost per pillow of the plant's output mix in the window: the SKUs of the cartons approved in it,
    each at std_cost() — the same basis as the value of every finished-goods case."""
    rows = db.execute(select(m.Carton.sku_id, func.sum(m.Carton.required_count)).where(
        m.Carton.factory_id.in_(fids), m.Carton.status.in_(("Passed", "Reworked", "Overridden")),
        m.Carton.approved_at >= start, m.Carton.approved_at <= end).group_by(m.Carton.sku_id)).all()
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    n = sum(q or 0 for _, q in rows)
    if n:
        return sum((q or 0) * std_cost(skus.get(sid)) for sid, q in rows) / n
    return sum(std_cost(s) for s in skus.values()) / len(skus) if skus else std_cost(None)


def overview(db: Session, fids, now: datetime) -> dict:
    rows = incidents(db, fids)
    start = window_start(now)
    recent = [r for r in rows if r.detected_at and start <= r.detected_at <= now]
    active = [r for r in rows if r.status != "Resolved"]
    closed = [r for r in recent if r.status == "Resolved"]
    conf = [r for r in closed if r.outcome == "Confirmed loss"]
    rec = [r for r in closed if r.outcome == "Recovered"]
    # shrinkage: confirmed loss in the window over the value of the output made in the same window, both at standard cost
    out_pillows = float(db.scalar(select(func.coalesce(func.sum(m.ProductionHourly.actual), 0)).where(
        m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date >= start.date(),
        m.ProductionHourly.date <= now.date())) or 0)
    out_cost = output_unit_cost(db, fids, start, now)
    out_value = out_pillows * out_cost
    hours = [(r.resolved_at - r.detected_at).total_seconds() / 3600 for r in closed if r.resolved_at and r.detected_at]
    by_kind = []
    for key, k in KINDS.items():
        rs = [r for r in recent if r.kind == key]
        by_kind.append({"kind": key, "label": k["label"], "count": len(rs), "active": sum(1 for r in rs if r.status != "Resolved"),
                        "confirmed_value": round(sum(r.value_usd or 0 for r in rs if r.outcome == "Confirmed loss"), 2),
                        "value": round(sum(r.value_usd or 0 for r in rs), 2)})
    zones = defaultdict(lambda: {"count": 0, "value": 0.0, "active": 0})
    for r in recent:
        z = zones[r.zone or "—"]
        z["count"] += 1
        z["value"] += r.value_usd or 0
        z["active"] += r.status != "Resolved"
    trend = []  # the same cases as cases_7d, by day
    for d in range(WINDOW_DAYS - 1, -1, -1):
        day = now.date() - timedelta(days=d)
        rs = [r for r in recent if r.detected_at.date() == day]
        trend.append({"date": day.isoformat(), "cases": len(rs),
                      "confirmed_value": round(sum(r.value_usd or 0 for r in rs if r.outcome == "Confirmed loss"), 2)})
    outcomes = Counter(r.outcome for r in closed)
    L = _lookups(db)
    feeds_online = sum(1 for fd in L["feeds"] if any(fd.startswith(f"CAM-{L['factories'][i].code}") for i in fids))
    controls = [
        {"name": "RFID exit-gate readers", "status": "Online", "detail": f"{2 * len(fids)} portals · staff and goods exits"},
        {"name": "Material-store door control", "status": "Online", "detail": "Role access list · auto-lock 22:30"},
        {"name": "CCTV vision analytics", "status": "Online", "detail": f"{feeds_online} cameras with badge-only zones and clip retention"},
        {"name": "Dock-door carton counters", "status": "Online", "detail": f"{4 * len(fids)} dock doors · vision + RFID portal"},
        {"name": "Weighbridge", "status": "Calibrated", "detail": "In/out weighing of every trailer"},
        {"name": "Seal register", "status": "Active", "detail": "Inbound and outbound seal numbers checked against the BOL"},
        {"name": "Cycle counting", "status": "Active", "detail": "ABC counts · zone with cases moved to daily"},
        {"name": "Scrap cage", "status": "Locked", "detail": "Two-person sign-off for scrap removal"},
    ]
    return {
        "sim_time": iso(now),
        "window": WINDOW_LABEL, "window_start": iso(start),
        "kpis": {
            "window": WINDOW_LABEL,
            "active": len(active), "open": sum(1 for r in active if r.status == "Open"),
            "investigating": sum(1 for r in active if r.status == "Investigating"),
            "critical_active": sum(1 for r in active if r.severity == "critical"),
            "value_at_risk": round(sum(r.value_usd or 0 for r in active), 2),
            "cases_7d": len(recent), "closed_7d": len(closed),
            "confirmed_loss_7d": round(sum(r.value_usd or 0 for r in conf), 2),
            "recovered_value_7d": round(sum((r.recovered_qty or 0) / (r.qty or 1) * (r.value_usd or 0) for r in rec), 2),
            "false_alarm_pct": round(100 * outcomes.get("False alarm", 0) / len(closed), 1) if closed else None,
            "mean_hours_to_close": round(sum(hours) / len(hours), 1) if hours else None,
            "shrinkage_pct": round(100 * sum(r.value_usd or 0 for r in conf) / out_value, 3) if out_value else None,
            "output_pillows_7d": int(out_pillows), "output_std_cost": round(out_cost, 4),
            "output_value_7d": round(out_value, 2),
        },
        "by_kind": by_kind,
        "by_zone": sorted(({"zone": z, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}}
                           for z, d in zones.items()), key=lambda z: (-z["count"], -z["value"]))[:10],
        "trend": trend,
        "outcomes": {o: outcomes.get(o, 0) for o in OUTCOMES},
        "channels": [{"channel": k["channel"], "kind": key, "cases": sum(1 for r in recent if r.kind == key)} for key, k in KINDS.items()],
        "controls": controls,
        "kinds": {key: {"label": k["label"], "stage": k["stage"], "channel": k["channel"], "owner": k["owner"], "icon": k["icon"],
                        "playbook": [{"key": a, "text": b} for a, b in k["playbook"]],
                        "outcomes": [o for o, _ in k["outcomes"]]} for key, k in KINDS.items()},
        "disclaimer": "CCTV vision raises access checks; "
                      "it does not identify or accuse people in the footage.",
    }


def facility_cases(db: Session, f: m.Factory) -> list[dict]:
    """Active cases for the twins (layout area + stage)."""
    L = _lookups(db)
    return [incident_dict(db, r, L) for r in incidents(db, [f.id], status="active")]


# ─── alerts and S!AP advisories ───────────────────────────────────────
def alert_rows(db: Session) -> list[dict]:
    out = []
    for r in db.scalars(select(m.LossIncident).where(m.LossIncident.status != "Resolved",
                                                     m.LossIncident.severity.in_(("critical", "warning")))):
        nxt = next_step(r)
        out.append(dict(key=f"loss:{r.id}", severity=r.severity, factory_id=r.factory_id, stage=r.stage,
                        title=f"Loss prevention · {r.title}", description=r.summary,
                        recommended_action=(nxt[1] if nxt else "Close the case with an outcome") + f" ({r.ref}).",
                        entity_type="loss_incident", entity_id=r.id, impact=r.value_usd or 0))
    return out


def advisories(db: Session, fids, now: datetime, factories: dict) -> list[dict]:
    """Patterns across cases: hotspots and repeat signals (not a duplicate of each alert)."""
    from .predictive import advisory
    out = []
    start = window_start(now)
    rows = [r for r in incidents(db, fids) if r.detected_at and start <= r.detected_at <= now]
    for fid in fids:
        f = factories.get(fid)
        mine = [r for r in rows if r.factory_id == fid]
        if not mine:
            continue
        by_kind = Counter(r.kind for r in mine if r.outcome != "False alarm")
        kind, n = by_kind.most_common(1)[0] if by_kind else (None, 0)
        if kind and n >= 2:
            k = KINDS[kind]
            rs = [r for r in mine if r.kind == kind]
            conf = [r for r in rs if r.outcome == "Confirmed loss"]
            val = sum(r.value_usd or 0 for r in rs)
            rec = {
                "fg_cycle_count": "Move the affected rack zone to daily cycle counts, add a PTZ preset over the aisle at shift change and require a move scan for every relocation.",
                "exit_gate_tag": "Run random bag checks at the staff exit at shift end and lower the gate antenna power to stop stray reads from the staging lane.",
                "consumption_variance": "Put the filling heads on hourly check-weighing and make bale issues two-signature until issues and output reconcile.",
                "receiving_shortage": "Weigh every inbound trailer in and out and reject deliveries whose seal does not match the BOL.",
                "dispatch_mismatch": "Make the dock-door count a departure gate: trucks leave only when the count equals the manifest.",
                "scrap_reconciliation": "Lock the scrap cage and weigh out scrap daily with two signatures.",
                "cctv_unbadged": "Badge-policy briefing at the start of each shift; visitor badges at reception for contractors.",
                "after_hours_access": "Tighten the store access list and auto-lock the store at 22:30 with no override for line roles.",
            }[kind]
            out.append(advisory(
                f"loss:{f.code}:{kind}", "Loss prevention", "warning" if conf or val >= 300 else "info",
                f"Repeat {k['label'].lower()} signals at {f.name} — {n} cases in the last 7 days",
                f"{n} {k['label'].lower()} cases (${val:,.0f} at standard cost, {len(conf)} confirmed). "
                f"Most recent: {rs[0].title}.", f,
                impact={"cost_usd": round(val, 2)}, recommendation=rec, confidence=min(0.9, 0.45 + 0.1 * n),
                evidence=[f"{r.ref} · {r.detected_at:%a %H:%M} · {r.outcome or r.status} · {r.title}" for r in rs[:5]],
                link={"page": "loss-prevention", "entity_type": "loss_kind", "entity_id": kind, "label": "Open cases"}))
        zones = Counter(r.zone for r in mine if r.outcome != "False alarm" and r.zone)
        if zones:
            z, zn = zones.most_common(1)[0]
            if zn >= 2:
                zr = [r for r in mine if r.zone == z]
                out.append(advisory(
                    f"loss:{f.code}:zone:{z}", "Loss prevention", "info",
                    f"Shrinkage hotspot: {z} at {f.name}", f"{zn} cases in the last 7 days at {z}.", f,
                    impact={"cost_usd": round(sum(r.value_usd or 0 for r in zr), 2)},
                    recommendation="Add the zone to the security patrol route and the daily cycle count until a week passes without a case.",
                    confidence=0.55, evidence=[f"{r.ref} · {KINDS[r.kind]['label']} · {r.outcome or r.status}" for r in zr[:5]],
                    link={"page": "loss-prevention", "entity_type": None, "entity_id": None, "label": "Open cases"}))
    return out
