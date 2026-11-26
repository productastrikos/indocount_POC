"""JWT authentication and role-based access control."""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import get_settings
from .db import get_db

ROLES = {
    "admin": {"label": "Administrator", "perms": {"*"}},
    "executive": {"label": "Executive", "perms": set()},
    "plant_manager": {"label": "Plant Manager", "perms": {
        "production.write", "optimize", "maintenance.write", "quality.write", "quality.override",
        "warehouse.write", "dispatch.write", "procurement.write", "receiving.write", "security.write"}},
    "maintenance_manager": {"label": "Maintenance Manager", "perms": {"maintenance.write"}},
    "quality_supervisor": {"label": "Quality Supervisor", "perms": {"quality.write", "quality.override"}},
    "warehouse_operator": {"label": "Warehouse Operator", "perms": {"warehouse.write", "receiving.write"}},
    "procurement": {"label": "Procurement Officer", "perms": {"procurement.write", "receiving.write"}},
    "logistics": {"label": "Logistics Coordinator", "perms": {"dispatch.write", "warehouse.write"}},
}

DEMO_USERS = [
    ("admin", "Alex Morgan", "admin", "admin123"),
    ("exec", "Chairman's Office", "executive", "exec123"),
    ("plant", "Priya Nair", "plant_manager", "plant123"),
    ("maint", "Marcus Lee", "maintenance_manager", "maint123"),
    ("quality", "Dana Brooks", "quality_supervisor", "quality123"),
    ("warehouse", "Luis Ortega", "warehouse_operator", "wh123"),
    ("procure", "Sarah Kim", "procurement", "proc123"),
    ("logistics", "Tom Becker", "logistics", "log123"),
]

_bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except ValueError:
        return False


def create_token(user: m.User) -> str:
    s = get_settings()
    payload = {"sub": user.username, "role": user.role,
               "exp": datetime.now(timezone.utc) + timedelta(minutes=s.jwt_expire_minutes)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def user_dict(u: m.User) -> dict:
    r = ROLES.get(u.role, {"label": u.role, "perms": set()})
    return {"username": u.username, "name": u.full_name, "role": u.role, "role_label": r["label"],
            "permissions": sorted(r["perms"])}


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> m.User:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.scalar(select(m.User).where(m.User.username == payload.get("sub")))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


def has_perm(user: m.User, perm: str) -> bool:
    perms = ROLES.get(user.role, {}).get("perms", set())
    return "*" in perms or perm in perms


def require(perm: str):
    def dep(user: m.User = Depends(current_user)) -> m.User:
        if not has_perm(user, perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Your role ({ROLES.get(user.role, {}).get('label', user.role)}) cannot perform this action")
        return user
    return dep
