"""Relational data model shared by every stage of the platform.

Procurement -> Production -> Quality & Packaging -> Warehouse -> Dispatch all
read and write these same tables; nothing is page-specific."""
from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


# ─── Master data ──────────────────────────────────────────────────────────
class Factory(Base):
    __tablename__ = "factories"
    id = Column(Integer, primary_key=True)
    code = Column(String(8), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    city = Column(String(60))
    state = Column(String(4))
    timezone = Column(String(40))
    daily_target = Column(Integer, default=0)
    pick_rate_cartons_hr = Column(Integer, default=400)
    wh_productivity = Column(Float, default=1.0)  # scenario multiplier for warehouse crews


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    channel = Column(String(40))
    priority_tier = Column(Integer, default=2)  # 1 = strategic


class Material(Base):
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)  # F-104
    name = Column(String(80), nullable=False)
    category = Column(String(30))  # Fibre, Foam, Shell, Bag, Carton, Label
    uom = Column(String(8))
    unit_cost = Column(Float, default=0)


class Sku(Base):
    __tablename__ = "skus"
    id = Column(Integer, primary_key=True)
    code = Column(String(24), unique=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    description = Column(String(120))
    size = Column(String(20))
    fill_material_id = Column(Integer, ForeignKey("materials.id"))
    fill_kg = Column(Float, default=0.7)
    shell_material_id = Column(Integer, ForeignKey("materials.id"))
    bag_material_id = Column(Integer, ForeignKey("materials.id"))
    carton_material_id = Column(Integer, ForeignKey("materials.id"))
    label_material_id = Column(Integer, ForeignKey("materials.id"))
    pillows_per_carton = Column(Integer, nullable=False)
    packaging_type = Column(String(60))
    carton_type = Column(String(60))
    label_requirements = Column(String(200))
    cartons_per_pallet = Column(Integer, default=36)
    unit_price = Column(Float, default=8.0)
    customer = relationship("Customer")
    fill_material = relationship("Material", foreign_keys=[fill_material_id])


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    location = Column(String(80))
    lead_time_days = Column(Integer, default=5)
    contact = Column(String(80))
    material_codes = Column(JSON, default=list)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(40), unique=True, nullable=False)
    full_name = Column(String(80))
    role = Column(String(30), nullable=False)
    password_hash = Column(String(200), nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(JSON)


# ─── Procurement & inbound ───────────────────────────────────────────────
class MaterialStock(Base):
    __tablename__ = "material_stock"
    __table_args__ = (UniqueConstraint("factory_id", "material_id"),)
    id = Column(Integer, primary_key=True)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    on_hand = Column(Float, default=0)          # usable quantity
    damaged = Column(Float, default=0)          # quarantined, not usable
    reorder_days = Column(Float, default=4)     # alert threshold in days of cover
    avg_daily_consumption = Column(Float, default=0)
    consumed_today = Column(Float, default=0)
    material = relationship("Material")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True)
    number = Column(String(20), unique=True, nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    ordered_qty = Column(Float, nullable=False)
    received_qty = Column(Float, default=0)
    damaged_qty = Column(Float, default=0)
    unit_price = Column(Float, default=0)
    order_date = Column(DateTime)
    expected_date = Column(DateTime)
    status = Column(String(24), default="Open")  # Open, Partially Received, Received, Delayed, Cancelled
    created_by = Column(String(40))
    notes = Column(Text)
    history = Column(JSON, default=list)
    supplier = relationship("Supplier")
    material = relationship("Material")


class InboundShipment(Base):
    __tablename__ = "inbound_shipments"
    id = Column(Integer, primary_key=True)
    ref = Column(String(24), unique=True, nullable=False)
    bol_number = Column(String(24))
    carrier = Column(String(60))
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    material_id = Column(Integer, ForeignKey("materials.id"))
    qty = Column(Float, nullable=False)
    expected_arrival = Column(DateTime)
    original_expected_arrival = Column(DateTime)
    actual_arrival = Column(DateTime)
    dock = Column(String(8))
    status = Column(String(20), default="Scheduled")  # Scheduled, In Transit, Arrived, Receiving, Received, Discrepancy
    delay_reason = Column(String(120))
    scanned_qty = Column(Float, default=0)     # draft count during receiving
    scanned_damaged = Column(Float, default=0)
    po = relationship("PurchaseOrder")


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"
    id = Column(Integer, primary_key=True)
    number = Column(String(20), unique=True, nullable=False)
    shipment_id = Column(Integer, ForeignKey("inbound_shipments.id"))
    po_id = Column(Integer, ForeignKey("purchase_orders.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    material_id = Column(Integer, ForeignKey("materials.id"))
    expected_qty = Column(Float)
    received_qty = Column(Float)
    damaged_qty = Column(Float, default=0)
    shortage_qty = Column(Float, default=0)
    accepted_qty = Column(Float, default=0)
    confirmed_at = Column(DateTime)
    confirmed_by = Column(String(40))
    notes = Column(Text)


class MaterialDiscrepancy(Base):
    __tablename__ = "material_discrepancies"
    id = Column(Integer, primary_key=True)
    receipt_id = Column(Integer, ForeignKey("goods_receipts.id"))
    po_id = Column(Integer, ForeignKey("purchase_orders.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    material_id = Column(Integer, ForeignKey("materials.id"))
    kind = Column(String(12))  # Shortage, Damage
    qty = Column(Float)
    status = Column(String(16), default="Open")
    created_at = Column(DateTime)


# ─── Production ──────────────────────────────────────────────────────────
class ProductionLine(Base):
    __tablename__ = "production_lines"
    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, nullable=False)
    name = Column(String(40), nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    line_type = Column(String(12))  # Garnett, Blow
    planned_rate = Column(Float)    # target pillows / productive hour
    base_efficiency = Column(Float, default=0.95)
    perf_modifier = Column(Float, default=1.0)  # scenario multiplier
    changeover_factor = Column(Float, default=1.0)
    status = Column(String(16), default="Running")  # Running, Idle, Changeover, Breakdown, Maintenance
    status_reason = Column(String(80))
    status_since = Column(DateTime)
    changeover_until = Column(DateTime)
    current_po_id = Column(Integer, ForeignKey("production_orders.id", use_alter=True, name="fk_line_current_po"))
    current_sku_id = Column(Integer, ForeignKey("skus.id"))
    output_carry = Column(Float, default=0)
    factory = relationship("Factory")


class Machine(Base):
    __tablename__ = "machines"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)  # M-102
    name = Column(String(60))
    machine_type = Column(String(40))
    stage = Column(String(40))
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    status = Column(String(16), default="Running")  # Running, Idle, Breakdown, Maintenance
    baseline = Column(JSON)   # {rpm, temperature, vibration, pressure, power}
    rpm = Column(Float)
    temperature = Column(Float)
    vibration = Column(Float)
    pressure = Column(Float)
    power = Column(Float)
    operating_hours = Column(Float, default=0)
    hours_since_maintenance = Column(Float, default=0)
    degradation = Column(Float, default=0)  # drift per operating hour (0 = healthy)
    health_score = Column(Float, default=95)
    last_maintenance_at = Column(DateTime)
    unavailable_until = Column(DateTime)
    line = relationship("ProductionLine")


class TelemetryPoint(Base):
    __tablename__ = "telemetry"
    __table_args__ = (Index("ix_telemetry_machine_ts", "machine_id", "ts"),)
    id = Column(Integer, primary_key=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    ts = Column(DateTime, nullable=False)
    rpm = Column(Float)
    temperature = Column(Float)
    vibration = Column(Float)
    pressure = Column(Float)
    power = Column(Float)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id = Column(Integer, primary_key=True)
    number = Column(String(16), unique=True, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    wo_type = Column(String(16))   # Predictive, Corrective, Preventive
    status = Column(String(16), default="Open")  # Open, Acknowledged, Scheduled, In Progress, Completed
    priority = Column(String(10))
    description = Column(Text)
    created_at = Column(DateTime)
    acknowledged_at = Column(DateTime)
    acknowledged_by = Column(String(40))
    scheduled_start = Column(DateTime)
    scheduled_end = Column(DateTime)
    started_at = Column(DateTime)
    expected_completion = Column(DateTime)
    completed_at = Column(DateTime)
    machine = relationship("Machine")


class DowntimeEvent(Base):
    __tablename__ = "downtime_events"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    machine_id = Column(Integer, ForeignKey("machines.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    category = Column(String(30))  # Mechanical breakdown, Changeover, Material shortage, Operator-related delay, Other
    reason = Column(String(120))
    start = Column(DateTime)
    end = Column(DateTime)
    minutes = Column(Float, default=0)  # productive minutes lost (live)


class ChangeoverEvent(Base):
    __tablename__ = "changeovers"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    prev_sku_id = Column(Integer, ForeignKey("skus.id"))
    new_sku_id = Column(Integer, ForeignKey("skus.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    po_id = Column(Integer, ForeignKey("production_orders.id"))
    start = Column(DateTime)
    end = Column(DateTime)
    duration_min = Column(Float)
    standard_min = Column(Float, default=3)
    lost_min = Column(Float, default=0)
    flagged = Column(Boolean, default=False)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)
    name = Column(String(60))
    role = Column(String(30))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    shift = Column(Integer)
    employment_type = Column(String(20))
    hourly_wage = Column(Float)
    attendance = Column(String(12), default="Present")
    skill = Column(Float, default=1.0)
    productivity_modifier = Column(Float, default=1.0)


class EmployeeShiftStat(Base):
    __tablename__ = "employee_shift_stats"
    __table_args__ = (UniqueConstraint("employee_id", "date", "shift"),)
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    shift = Column(Integer, nullable=False)
    units = Column(Float, default=0)
    productive_min = Column(Float, default=0)
    idle_min = Column(Float, default=0)
    paid_min = Column(Float, default=0)


class RfidInterval(Base):
    """RFID badge presence: one row per continuous stay of a badge in a reader
    zone (workstation, break room, restroom, material store, …). An open row
    (end IS NULL) is where the badge is right now. Simulated UHF badge reads."""
    __tablename__ = "rfid_intervals"
    __table_args__ = (Index("ix_rfid_emp_date", "employee_id", "date"),
                      Index("ix_rfid_factory_date", "factory_id", "date"),
                      Index("ix_rfid_open", "end"))
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    date = Column(Date, nullable=False)
    shift = Column(Integer, nullable=False)
    state = Column(String(10), nullable=False)  # Station, Away, Break
    zone = Column(String(48))
    reader = Column(String(24))
    reason = Column(String(40))
    start = Column(DateTime, nullable=False)
    end = Column(DateTime)
    planned_end = Column(DateTime)  # simulation only: when the current trip away ends


class LineWip(Base):
    """Pillows in process between the stations of a line:
    filling -> [sewing buffer] -> sewing -> [bagging buffer] -> bagging/compression."""
    __tablename__ = "line_wip"
    line_id = Column(Integer, ForeignKey("production_lines.id"), primary_key=True)
    date = Column(Date)
    sewing = Column(Float, default=0)   # filled pillows at the sewing table
    bagging = Column(Float, default=0)  # sewn pillows at bagging / compression
    updated_at = Column(DateTime)


# ─── Orders ──────────────────────────────────────────────────────────────
class CustomerOrder(Base):
    __tablename__ = "customer_orders"
    id = Column(Integer, primary_key=True)
    number = Column(String(16), unique=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("skus.id"), nullable=False)
    qty = Column(Integer, nullable=False)  # pillows
    order_date = Column(DateTime)
    ship_window_start = Column(DateTime)
    ship_window_end = Column(DateTime)
    scheduled_dispatch = Column(DateTime)
    priority = Column(String(8), default="Medium")
    status = Column(String(16), default="Open")  # Open, Dispatched, Delivered, Cancelled
    allocated_qty = Column(Integer, default=0)
    picked_qty = Column(Integer, default=0)
    loaded_qty = Column(Integer, default=0)
    dispatched_at = Column(DateTime)
    delivered_at = Column(DateTime)
    expedite_picking = Column(Boolean, default=False)
    customer = relationship("Customer")
    sku = relationship("Sku")


class ProductionOrder(Base):
    __tablename__ = "production_orders"
    id = Column(Integer, primary_key=True)
    number = Column(String(16), unique=True, nullable=False)
    customer_order_id = Column(Integer, ForeignKey("customer_orders.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("skus.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    sequence = Column(Integer, default=0)
    required_qty = Column(Integer, nullable=False)
    completed_qty = Column(Integer, default=0)   # pillows produced (filled + sewn)
    pack_queue = Column(Integer, default=0)      # produced, waiting at the packing station
    packed_qty = Column(Integer, default=0)      # pillows in approved cartons
    held_qty = Column(Integer, default=0)        # pillows in blocked cartons
    stored_qty = Column(Integer, default=0)      # pillows put away in finished goods
    start_date = Column(DateTime)
    required_completion = Column(DateTime)
    estimated_completion = Column(DateTime)
    priority = Column(String(8), default="Medium")
    status = Column(String(16), default="Planned")  # Planned, Ready, In Progress, Delayed, Completed
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    material_risk = Column(Boolean, default=False)
    risk_reason = Column(String(160))
    sku = relationship("Sku")
    customer_order = relationship("CustomerOrder", foreign_keys=[customer_order_id])


class ProductionHourly(Base):
    __tablename__ = "production_hourly"
    __table_args__ = (UniqueConstraint("line_id", "date", "hour", "shift"),)
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("production_lines.id"), nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    date = Column(Date, nullable=False)
    hour = Column(Integer, nullable=False)
    shift = Column(Integer, nullable=False)
    planned = Column(Float, default=0)
    actual = Column(Float, default=0)
    run_min = Column(Float, default=0)
    downtime_min = Column(Float, default=0)


class ShiftSummary(Base):
    """Closed shifts (history). Today's figures are computed live."""
    __tablename__ = "shift_summaries"
    __table_args__ = (UniqueConstraint("factory_id", "date", "shift"),)
    id = Column(Integer, primary_key=True)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    date = Column(Date, nullable=False)
    shift = Column(Integer, nullable=False)
    planned = Column(Float, default=0)
    actual = Column(Float, default=0)
    planned_min = Column(Float, default=0)
    run_min = Column(Float, default=0)
    downtime_min = Column(Float, default=0)
    cartons_inspected = Column(Integer, default=0)
    cartons_passed = Column(Integer, default=0)
    labour_cost = Column(Float, default=0)
    workers_deployed = Column(Integer, default=0)
    workers_required = Column(Integer, default=0)
    idle_min = Column(Float, default=0)
    changeovers = Column(Integer, default=0)
    changeover_lost_min = Column(Float, default=0)
    defects = Column(JSON, default=dict)


# ─── Quality & packaging ─────────────────────────────────────────────────
class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, nullable=False)
    name = Column(String(60))
    station = Column(String(30))  # Pillow filling, Sewing, Compression, Carton packing, Final inspection
    factory_id = Column(Integer, ForeignKey("factories.id"))
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    status = Column(String(12), default="Online")
    mode = Column(String(12), default="simulated")
    stream_url = Column(String(200))
    inspections_today = Column(Integer, default=0)
    detections_today = Column(Integer, default=0)
    last_event_at = Column(DateTime)


class DetectionEvent(Base):
    __tablename__ = "detection_events"
    __table_args__ = (Index("ix_detection_camera_ts", "camera_id", "ts"),)
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"))
    ts = Column(DateTime)
    event_type = Column(String(24))
    label = Column(String(120))
    confidence = Column(Float)
    carton_id = Column(Integer, ForeignKey("cartons.id"))
    count = Column(Integer)
    severity = Column(String(10), default="info")


class Carton(Base):
    __tablename__ = "cartons"
    __table_args__ = (Index("ix_carton_factory_status", "factory_id", "status"),)
    id = Column(Integer, primary_key=True)
    carton_code = Column(String(24), unique=True)  # assigned when the carton is approved
    hold_code = Column(String(24))                 # assigned while blocked
    factory_id = Column(Integer, ForeignKey("factories.id"))
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    po_id = Column(Integer, ForeignKey("production_orders.id"))
    sku_id = Column(Integer, ForeignKey("skus.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    required_count = Column(Integer)
    detected_count = Column(Integer)
    status = Column(String(14))   # Passed, Blocked, Reworked, Overridden, Rejected
    result = Column(String(14))   # Correct, Underpacked, Overpacked, Defect
    defect_category = Column(String(40))
    first_pass = Column(Boolean, default=True)
    created_at = Column(DateTime)
    approved_at = Column(DateTime)
    approved_by = Column(String(40))
    override_reason = Column(String(200))
    pallet_id = Column(Integer, ForeignKey("pallets.id"))
    inference_source = Column(String(12), default="simulated")
    confidence = Column(Float)


class DefectRecord(Base):
    __tablename__ = "defects"
    id = Column(Integer, primary_key=True)
    carton_id = Column(Integer, ForeignKey("cartons.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    line_id = Column(Integer, ForeignKey("production_lines.id"))
    po_id = Column(Integer, ForeignKey("production_orders.id"))
    sku_id = Column(Integer, ForeignKey("skus.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    category = Column(String(40))
    qty = Column(Integer, default=1)
    disposition = Column(String(16), default="Rework")  # Rework, Reject, Override
    status = Column(String(12), default="Open")
    created_at = Column(DateTime)
    resolved_at = Column(DateTime)
    resolved_by = Column(String(40))
    notes = Column(String(200))


# ─── Warehouse ───────────────────────────────────────────────────────────
class StorageLocation(Base):
    __tablename__ = "storage_locations"
    id = Column(Integer, primary_key=True)
    code = Column(String(24), unique=True, nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    zone = Column(String(4))
    bay = Column(Integer)
    rack = Column(Integer)
    level = Column(Integer)
    capacity_pallets = Column(Integer, default=1)
    kind = Column(String(12), default="Rack")


class Pallet(Base):
    """Handling unit of approved cartons; doubles as the inventory lot."""
    __tablename__ = "pallets"
    __table_args__ = (Index("ix_pallet_factory_status", "factory_id", "status"),)
    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False)
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    po_id = Column(Integer, ForeignKey("production_orders.id"))
    sku_id = Column(Integer, ForeignKey("skus.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    batch_no = Column(String(20))
    cartons = Column(Integer, default=0)
    capacity = Column(Integer, default=36)
    allocated_cartons = Column(Integer, default=0)
    status = Column(String(18), default="Open")  # Open, Pending Put-away, Stored, Depleted
    location_id = Column(Integer, ForeignKey("storage_locations.id"))
    created_at = Column(DateTime)
    closed_at = Column(DateTime)
    stored_at = Column(DateTime)
    stored_by = Column(String(40))
    sku = relationship("Sku")
    location = relationship("StorageLocation")


class Allocation(Base):
    """Finished goods reserved for a customer order; also the pick task."""
    __tablename__ = "allocations"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("customer_orders.id"), nullable=False)
    pallet_id = Column(Integer, ForeignKey("pallets.id"), nullable=False)
    cartons = Column(Integer, nullable=False)
    pillows = Column(Integer, nullable=False)
    status = Column(String(12), default="Reserved")  # Reserved, Picked, Loaded
    created_at = Column(DateTime)
    picked_at = Column(DateTime)
    picked_by = Column(String(40))
    pallet = relationship("Pallet")


# ─── Dispatch ────────────────────────────────────────────────────────────
class TruckAppointment(Base):
    __tablename__ = "truck_appointments"
    id = Column(Integer, primary_key=True)
    ref = Column(String(20), unique=True, nullable=False)
    carrier = Column(String(60))
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    order_id = Column(Integer, ForeignKey("customer_orders.id"))
    dock = Column(String(8))
    scheduled_arrival = Column(DateTime)
    expected_loading_min = Column(Integer, default=60)
    actual_arrival = Column(DateTime)
    loading_start = Column(DateTime)
    loading_end = Column(DateTime)
    departed_at = Column(DateTime)
    loaded_qty = Column(Integer, default=0)
    status = Column(String(14), default="Scheduled")  # Scheduled, Arrived, Loading, Loaded, Departed, Cancelled
    wait_min = Column(Float, default=0)
    detention_charge = Column(Float, default=0)
    delay_reason = Column(String(120))
    late_by_min = Column(Integer, default=0)  # simulated carrier lateness
    order = relationship("CustomerOrder")


# ─── Cross-cutting ───────────────────────────────────────────────────────
class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    key = Column(String(120), unique=True, nullable=False)
    severity = Column(String(10))  # critical, warning, info
    factory_id = Column(Integer, ForeignKey("factories.id"))
    stage = Column(String(20))     # procurement, production, quality, warehouse, dispatch
    title = Column(String(200))
    description = Column(Text)
    recommended_action = Column(Text)
    entity_type = Column(String(24))
    entity_id = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    status = Column(String(14), default="Open")  # Open, Acknowledged, Resolved
    acknowledged_by = Column(String(40))
    resolved_at = Column(DateTime)
    impact = Column(Float, default=0)


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (Index("ix_event_entity", "entity_type", "entity_id"),)
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime)
    factory_id = Column(Integer, ForeignKey("factories.id"))
    stage = Column(String(20))
    event_type = Column(String(40))
    message = Column(String(300))
    entity_type = Column(String(24))
    entity_id = Column(Integer)
    user = Column(String(40))


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"
    id = Column(Integer, primary_key=True)
    factory_id = Column(Integer, ForeignKey("factories.id"))
    created_at = Column(DateTime)
    created_by = Column(String(40))
    status = Column(String(12), default="Proposed")
    before = Column(JSON)
    after = Column(JSON)
    metrics = Column(JSON)


class SimState(Base):
    __tablename__ = "sim_state"
    id = Column(Integer, primary_key=True)
    sim_time = Column(DateTime, nullable=False)
    running = Column(Boolean, default=True)
    speed = Column(Integer, default=1)
    tick = Column(Integer, default=0)
    seed_date = Column(Date)
    scenario_log = Column(JSON, default=list)
