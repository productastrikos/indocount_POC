"""Integration layer for future enterprise and shop-floor systems.

Each adapter has the interface the platform needs and a simulated
implementation used by the POC. Real connectors are enabled by setting the
corresponding environment variable; no credentials are assumed."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import get_settings


class Adapter(ABC):
    name: str = ""
    system: str = ""
    env_var: str = ""

    def configured(self) -> bool:
        return bool(getattr(get_settings(), self.env_var.lower(), None)) if self.env_var else False

    def status(self) -> dict:
        return {"name": self.name, "system": self.system, "mode": "live" if self.configured() else "simulated",
                "env": self.env_var, "capabilities": self.capabilities()}

    @abstractmethod
    def capabilities(self) -> list[str]: ...


class ERPAdapter(Adapter):
    """SAP Business One Service Layer: purchase orders, goods receipts, sales orders, deliveries."""
    name, system, env_var = "ERP", "SAP Business One (Service Layer)", "SAP_B1_URL"

    def capabilities(self):
        return ["Purchase orders (OPOR)", "Goods receipt PO (OPDN)", "Sales orders (ORDR)", "Deliveries (ODLN)", "Item master (OITM)"]

    def push_goods_receipt(self, receipt: dict) -> dict:
        return {"status": "queued", "target": self.system, "payload": receipt, "mode": "simulated"}


class HRMSAdapter(Adapter):
    """Attendance, shift rosters and wage rates."""
    name, system, env_var = "HRMS", "HRMS / time & attendance", "HRMS_URL"

    def capabilities(self):
        return ["Shift roster", "Clock-in / clock-out events", "Wage rates & differentials"]


class IoTAdapter(Adapter):
    """Machine telemetry from PLCs via OPC UA / MQTT."""
    name, system, env_var = "IoT / PLC", "OPC UA / MQTT broker", "IOT_BROKER_URL"

    def capabilities(self):
        return ["RPM, temperature, vibration, pressure, power", "Machine state (run/idle/fault)", "Cycle counters"]


class CCTVAdapter(Adapter):
    """Camera streams for packing-station inference (RTSP)."""
    name, system, env_var = "CCTV", "RTSP camera streams", "CV_VIDEO_SOURCE"

    def capabilities(self):
        return ["Frame capture", "Pillow counting inference", "Detection events"]


class BarcodeRFIDAdapter(Adapter):
    """Handheld scanners and RFID portals for cartons, pallets and locations."""
    name, system, env_var = "Barcode / RFID", "Scanners & RFID portals", ""

    def capabilities(self):
        return ["GS1-128 carton labels", "Pallet LPN", "Location barcodes", "RFID dock portals"]


ADAPTERS = [ERPAdapter(), HRMSAdapter(), IoTAdapter(), CCTVAdapter(), BarcodeRFIDAdapter()]


def status() -> list[dict]:
    return [a.status() for a in ADAPTERS]
