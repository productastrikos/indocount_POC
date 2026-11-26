"""Computer-vision inference interface.

Providers
  * OpenCVContourCounter - real OpenCV pipeline (grayscale -> threshold ->
    morphology -> contours) that counts pillows in a frame.
  * SimulatedCounter     - returns the simulated ground truth.

No plant cameras are connected in the POC, so frames are generated
(`render_frame`) and every frame is watermarked SIMULATED. Pointing
CV_VIDEO_SOURCE at an RTSP stream or video file makes `capture_frame` read
real frames instead; the same counter runs on them."""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

import numpy as np

from ..config import get_settings

try:  # OpenCV is optional; the simulated provider is the fallback
    import cv2  # type: ignore
    HAVE_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    HAVE_CV2 = False

W, H = 480, 270


@dataclass
class Detection:
    count: int
    boxes: list = field(default_factory=list)
    confidence: float = 0.0
    provider: str = "simulated"


class SimulatedCounter:
    name = "simulated"

    def infer(self, frame, truth: int | None = None) -> Detection:
        return Detection(count=truth or 0, confidence=0.97, provider=self.name)


class OpenCVContourCounter:
    name = "opencv"

    def infer(self, frame, truth: int | None = None) -> Detection:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = [cv2.boundingRect(c) for c in contours if 60 < cv2.contourArea(c) < 20000]
        areas = [w * h for _, _, w, h in boxes]
        conf = 0.99 - (float(np.std(areas) / np.mean(areas)) * 0.1 if len(areas) > 1 else 0.02)
        return Detection(count=len(boxes), boxes=boxes, confidence=round(max(0.5, min(0.995, conf)), 3), provider=self.name)


def provider():
    s = get_settings()
    if s.cv_provider == "opencv" and HAVE_CV2:
        return OpenCVContourCounter()
    return SimulatedCounter()


# ─── synthetic frames ────────────────────────────────────────────────────
def _pillow_slots(n: int, box):
    x0, y0, x1, y1 = box
    cols = max(1, math.ceil(math.sqrt(n * (x1 - x0) / max(1, (y1 - y0)))))
    rows = max(1, math.ceil(n / cols))
    cw, ch = (x1 - x0) / cols, (y1 - y0) / rows
    slots = []
    for i in range(n):
        r, c = divmod(i, cols)
        slots.append((int(x0 + c * cw + cw * 0.12), int(y0 + r * ch + ch * 0.14),
                      int(x0 + (c + 1) * cw - cw * 0.12), int(y0 + (r + 1) * ch - ch * 0.14)))
    return slots


def _base(station: str):
    img = np.full((H, W, 3), (38, 38, 42), np.uint8)
    for x in range(-H, W, 26):  # conveyor belt texture
        cv2.line(img, (x, H), (x + H, 0), (46, 46, 52), 6)
    cv2.rectangle(img, (0, 0), (W, 22), (20, 20, 24), -1)
    return img


def render_carton(n_inside: int, required: int, rng: random.Random | None = None):
    """Top view of an open carton with `n_inside` pillows."""
    img = _base("carton")
    cv2.rectangle(img, (110, 42), (370, 250), (40, 90, 140), -1)     # carton (BGR brown)
    cv2.rectangle(img, (120, 52), (360, 240), (30, 70, 115), -1)     # inner floor
    slots = _pillow_slots(max(required, n_inside, 1), (124, 56, 356, 236))
    for (x0, y0, x1, y1) in slots[:n_inside]:
        cv2.rectangle(img, (x0, y0), (x1, y1), (235, 238, 240), -1)
        cv2.rectangle(img, (x0 + 3, y0 + 3), (x1 - 3, y1 - 3), (250, 252, 252), 1)
    return img


def render_line(station: str, t: float):
    """Filling / sewing / compression stations: pillows moving on a conveyor."""
    img = _base(station)
    speed = 60
    for k in range(5):
        x = int((t * speed + k * 110) % (W + 120)) - 100
        y0, y1 = (110, 190) if station != "Compression" else (130, 175)
        cv2.rectangle(img, (x, y0), (x + 80, y1), (236, 238, 240), -1)
        if station == "Sewing":
            cv2.line(img, (x + 4, y0 + 6), (x + 76, y0 + 6), (90, 90, 200), 1)
    if station == "Compression":
        cv2.rectangle(img, (200, 40), (290, 125), (120, 120, 130), -1)
    return img


def overlay(img, det: Detection, required: int | None, label: str, ok: bool | None):
    color = (80, 200, 90) if ok in (True, None) else (60, 60, 235)
    for (x, y, w, h) in det.boxes:
        cv2.rectangle(img, (x - 2, y - 2), (x + w + 2, y + h + 2), color, 1)
    cv2.putText(img, label, (8, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 230), 1, cv2.LINE_AA)
    txt = f"COUNT {det.count}" + (f" / {required}" if required else "")
    cv2.putText(img, txt, (8, H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    cv2.putText(img, f"{det.provider.upper()} conf {det.confidence:.2f}", (W - 175, H - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (200, 200, 210), 1, cv2.LINE_AA)
    cv2.putText(img, "SIMULATED", (W - 96, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 190, 255), 1, cv2.LINE_AA)
    return img


def count_pillows(n: int, rng: random.Random) -> tuple[int, float, str]:
    """Run the configured counter on a generated carton frame holding n pillows."""
    p = provider()
    if isinstance(p, OpenCVContourCounter):
        det = p.infer(render_carton(n, n, rng))
        return det.count, det.confidence, "opencv"
    return n, round(rng.uniform(0.93, 0.995), 3), "edge-vision"


def capture_frame():
    """Read one frame from CV_VIDEO_SOURCE if configured (future real cameras)."""
    src = get_settings().cv_video_source
    if not (src and HAVE_CV2):
        return None
    cap = cv2.VideoCapture(src)
    ok, frame = cap.read()
    cap.release()
    return cv2.resize(frame, (W, H)) if ok else None


def camera_frame(station: str, carton: dict | None, live: bool = True) -> tuple[bytes, dict]:
    """JPEG frame plus detection metadata for the camera viewer."""
    if not HAVE_CV2:
        return b"", {"provider": "simulated", "count": carton.get("detected") if carton else None}
    t = time.time()
    real = capture_frame()
    p = provider()
    if station in ("Carton packing", "Final inspection") and carton:
        req, det_n = carton["required"], carton["detected"]
        cycle = 4.0
        phase = (t % cycle) / cycle
        shown = det_n if (station == "Final inspection" or phase > 0.75) else min(det_n, int(phase / 0.75 * (det_n + 1)))
        img = real if real is not None else render_carton(shown, req)
        det = p.infer(img, shown)
        complete = shown == det_n
        ok = None if not complete else det.count == req
        label = f"{carton.get('sku', '')}  {carton.get('code', '')}"
        overlay(img, det, req, label, ok)
        if complete:
            cv2.putText(img, "PASS" if ok else ("UNDERPACKED" if det.count < req else "OVERPACKED"), (130, 36),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 200, 90) if ok else (60, 60, 235), 2, cv2.LINE_AA)
        meta = {"provider": det.provider, "count": det.count, "required": req, "complete": complete, "ok": ok}
    else:
        img = real if real is not None else render_line(station, t)
        det = p.infer(img, None)
        overlay(img, det, None, station.upper(), None)
        meta = {"provider": det.provider, "count": det.count}
    ok_, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
    return (buf.tobytes() if ok_ else b""), meta
