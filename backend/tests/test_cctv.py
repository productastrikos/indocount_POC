"""CCTV feeds: catalogue, live context (RFID identities, customer order) and published assets."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import cctv

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_feed_list_covers_every_cv_camera(client, h):
    r = client.get("/api/cctv/feeds", headers=h)
    assert r.status_code == 200, r.text
    feeds = r.json()["feeds"]
    assert {f["factory"] for f in feeds} == {"KNV", "PHX", "CMH"}
    cams = {f["camera"] for f in feeds}
    for code in ("KNV", "PHX", "CMH"):
        for n in range(1, 7):  # every seeded CV camera has footage
            assert f"CAM-{code}-0{n}" in cams
    one = client.get("/api/cctv/feeds", headers=h, params={"factory": "PHX"}).json()["feeds"]
    assert one and all(f["factory"] == "PHX" for f in one)
    assert client.get("/api/cctv/feeds", headers=h, params={"factory": "XYZ"}).status_code == 404


def test_packing_feed_identifies_customer_and_workers(client, h):
    feeds = client.get("/api/cctv/feeds", headers=h, params={"factory": "KNV", "kind": "packing"}).json()["feeds"]
    assert feeds
    walmart = next(f for f in feeds if (f.get("recognition") or {}).get("customer_code") == "WMT")
    d = client.get(f"/api/cctv/feeds/{walmart['id']}", headers=h).json()
    assert d["station"] == "Carton packing" and d["line_id"]
    assert d["recognition"]["evidence"], "the carton label should be read by OCR"
    assert d["order"]["customer"] == "Walmart"
    roles = {w["role"] for w in d["workers"]}
    assert "Packaging operator" in roles
    assert all(w["state"] in ("Station", "Away", "Break") for w in d["workers"])
    assert client.get("/api/cctv/feeds/nope", headers=h).status_code == 404


def test_published_assets_match_the_catalogue():
    for f in cctv.catalog():
        for key in ("video", "poster", "analytics"):
            p = ROOT / "public" / f[key].lstrip("/")
            assert p.exists() and p.stat().st_size > 1000, p
        d = json.loads((ROOT / "public" / f["analytics"].lstrip("/")).read_text(encoding="utf-8"))
        assert d["n"] == len(d["p"]) == len(d["o"]) == len(d["k"])
        assert d["fps"] == f["fps"] and abs(d["dur"] - f["duration"]) < 0.01
        if f["kind"] == "packing":
            assert any(e["type"] == "pillow_in" for e in d["ev"]), f["id"]
