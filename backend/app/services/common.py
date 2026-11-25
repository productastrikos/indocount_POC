"""Small helpers shared by services, the simulation engine and routers."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import get_settings


def state(db: Session) -> m.SimState:
    return db.get(m.SimState, 1)


def sim_now(db: Session) -> datetime:
    st = state(db)
    return st.sim_time if st else datetime.now().replace(microsecond=0)


def factory_ids(db: Session, code: str | None) -> list[int]:
    """'ALL' / None -> every factory; otherwise the matching factory."""
    if not code or code.upper() == "ALL":
        return [f.id for f in db.scalars(select(m.Factory).order_by(m.Factory.id))]
    f = db.scalar(select(m.Factory).where(m.Factory.code == code.upper()))
    return [f.id] if f else []


def factory_map(db: Session) -> dict[int, m.Factory]:
    return {f.id: f for f in db.scalars(select(m.Factory))}


def log_event(db: Session, ts, factory_id, stage, event_type, message, entity_type=None, entity_id=None, user=None):
    db.add(m.EventLog(ts=ts, factory_id=factory_id, stage=stage, event_type=event_type,
                      message=message[:300], entity_type=entity_type, entity_id=entity_id, user=user))


def next_number(db: Session, model, prefix: str, start: int = 1000, width: int = 0) -> str:
    n = (db.scalar(select(func.max(model.id))) or 0) + start + 1
    return f"{prefix}{str(n).zfill(width) if width else n}"


def get_setting(db: Session, key: str, default=None):
    row = db.get(m.Setting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value):
    row = db.get(m.Setting, key)
    if row:
        row.value = value
    else:
        db.add(m.Setting(key=key, value=value))


def business_rules(db: Session) -> dict:
    s = get_settings()
    return {
        "detention_charge": float(get_setting(db, "detention_charge", s.detention_charge_usd)),
        "detention_free_min": float(get_setting(db, "detention_free_min", s.detention_free_minutes)),
        "changeover_standard_min": float(get_setting(db, "changeover_standard_min", s.changeover_standard_minutes)),
        "shift2_differential": float(get_setting(db, "shift2_differential", s.shift2_differential_usd)),
    }


def iso(dt):
    return dt.isoformat(timespec="minutes") if dt else None


def pct(a, b, digits=1):
    return round(100.0 * a / b, digits) if b else 0.0


PRIORITY_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}
