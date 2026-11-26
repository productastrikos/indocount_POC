"""Computer-vision analysis of the rendered CCTV feeds.

For every feed (public/cctv/<id>.mp4) this writes public/cctv/<id>.json with
per-frame results the browser replays in sync with the video, and the feed
catalogue (backend/app/cv/cctv_catalog.json).

Pipeline
  people    YOLO11m person detection + ByteTrack multi-object tracking,
            YOLO11s-pose keypoints matched to the tracks
  activity  per-person state from body and wrist motion (handling, lifting,
            walking, idle), smoothed over time
  pillows   fixed cameras: motion segmentation (MOG2) x colour model, tracked
            blobs; a pillow whose track ends inside a carton opening is a
            pillow placed in that carton. PTZ cameras: YOLOE open-vocabulary
            detection ("pillow", "white plastic bag", "cardboard box")
  OCR       PP-OCR (RapidOCR) on the carton print / labels, fuzzy-matched to the
            label vocabulary -> customer recognition
  flow      Farneback optical flow on the conveyor -> belt speed; pillows
            crossing the count line
  zones     idle at station, left station, restricted-zone entry, heatmap

    .cvenv/Scripts/python tools/cctv/analyze.py [--no-catalog] [feed-id ...]
    .cvenv/Scripts/python tools/cctv/analyze.py --catalog-only
    .cvenv/Scripts/python tools/cctv/analyze.py --lp     (badge-only zones from stored tracks)

Line and area cameras track people with the pose model itself (one pass per
frame, keypoints on every box); packing cameras use YOLO11m + ByteTrack with
pose matched to the tracks for the best recall at the carton stations.
CV_THREADS sets the torch CPU threads per process (default 4).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from feeds import FEEDS, OUT_DIR, OUT_FPS, OUT_H, OUT_W, ROOT  # noqa: E402

MODELS = ROOT / ".cvenv" / "models"
CATALOG = ROOT / "backend" / "app" / "cv" / "cctv_catalog.json"
ACT = {"idle": 0, "handling": 1, "lifting": 2, "walking": 3, "present": 4}
OBJ = {"pillow": 0, "carton": 1, "pallet": 2}

_models = {}


def model(name):
    """Load a model once per process. 'file#tag' gives a separate instance of the same weights
    (a tracking predictor must not be shared with plain predict calls)."""
    if name not in _models:
        import torch
        from ultralytics import YOLO, YOLOE
        torch.set_num_threads(int(os.environ.get("CV_THREADS", "4")))
        file = name.split("#")[0]
        if file.startswith("yoloe"):
            m = YOLOE(str(MODELS / file))
            names = ["pillow", "white plastic bag", "cardboard box", "pallet"]
            m.set_classes(names, m.get_text_pe(names))
            _models[name] = m
        else:
            _models[name] = YOLO(str(MODELS / file))
    return _models[name]


def read_frames(path: Path):
    cap = cv2.VideoCapture(str(path))
    out = []
    while True:
        ok, im = cap.read()
        if not ok:
            break
        out.append(im)
    return out


def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def inside(pt, poly):
    return cv2.pointPolygonTest(np.array(poly, np.float32), (float(pt[0]), float(pt[1])), False) >= 0


# ── people ─────────────────────────────────────────────────────────────
def detect_people(frames, step, imgsz=960, fast=False):
    raw = {}
    if fast:  # pose model as detector + ByteTrack: one network pass per frame
        trk = model("yolo11s-pose.pt#track")
        trk.predictor = None  # fresh tracker state per feed
        for i in range(0, len(frames), step):
            r = trk.track(frames[i], persist=True, tracker="bytetrack.yaml", imgsz=imgsz, conf=0.2, iou=0.5,
                          verbose=False)[0]
            boxes = []
            if r.boxes is not None and r.boxes.id is not None:
                kps = r.keypoints.data.tolist() if r.keypoints is not None else [None] * len(r.boxes)
                for tid, b, c, kp in zip(r.boxes.id.int().tolist(), r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), kps):
                    boxes.append([tid, *b, c, kp])
            raw[i] = boxes
        return raw
    det, pose = model("yolo11m.pt"), model("yolo11s-pose.pt")
    det.predictor = None  # fresh tracker state per feed
    for i in range(0, len(frames), step):
        r = det.track(frames[i], persist=True, tracker="bytetrack.yaml", classes=[0], imgsz=imgsz, conf=0.18,
                      iou=0.5, verbose=False)[0]
        boxes = []
        if r.boxes is not None and r.boxes.id is not None:
            for tid, b, c in zip(r.boxes.id.int().tolist(), r.boxes.xyxy.tolist(), r.boxes.conf.tolist()):
                boxes.append([tid, *b, c, None])
        p = pose.predict(frames[i], imgsz=imgsz, conf=0.3, verbose=False)[0]
        if p.keypoints is not None and len(p.boxes):
            kps = p.keypoints.data.tolist()
            for pb, kp in zip(p.boxes.xyxy.tolist(), kps):
                best = max(boxes, key=lambda bb: iou(bb[1:5], pb), default=None)
                if best is not None and iou(best[1:5], pb) > 0.45:
                    best[6] = kp
        raw[i] = boxes
    return raw


def interpolate_people(raw, n, step):
    """Per-frame boxes/keypoints for every track; drop flickering tracks."""
    by_tid = defaultdict(dict)
    for i, boxes in raw.items():
        for tid, x0, y0, x1, y1, c, kp in boxes:
            by_tid[tid][i] = (np.array([x0, y0, x1, y1]), c, None if kp is None else np.array(kp))
    tracks = {}
    for tid, obs in by_tid.items():
        if len(obs) < 3:
            continue
        idx = sorted(obs)
        per = {}
        for a, b in zip(idx, idx[1:] + [None]):
            per[a] = obs[a]
            if b is None or b - a > step * 4:
                for j in range(a + 1, min(n, a + step)):
                    per[j] = obs[a]
                continue
            for j in range(a + 1, b):
                w = (j - a) / (b - a)
                ba, ca, ka = obs[a]
                bb, cb, kb = obs[b]
                kp = None
                if ka is not None and kb is not None:
                    kp = ka * (1 - w) + kb * w
                elif ka is not None:
                    kp = ka
                per[j] = (ba * (1 - w) + bb * w, ca * (1 - w) + cb * w, kp)
        tracks[tid] = per
    # compact ids in order of first appearance
    order = sorted(tracks, key=lambda t: min(tracks[t]))
    return {k + 1: tracks[t] for k, t in enumerate(order)}


def merge_station_tracks(tracks, stations):
    """ByteTrack loses a person who ducks behind a carton and gives them a new id.
    Fragments that live in the same station zone one after another, close together,
    are the same person: join them (track re-association)."""
    if not stations:
        return tracks

    def foot(per, j):
        b = per[j][0]
        return ((b[0] + b[2]) / 2, b[3])

    def zone_of(per):
        hits = Counter()
        for j in per:
            for zid, zz in stations.items():
                if inside(foot(per, j), zz["pts"]):
                    hits[zid] += 1
        if not hits:
            return None
        zid, k = hits.most_common(1)[0]
        return zid if k >= 0.5 * len(per) else None

    zones = {tid: zone_of(per) for tid, per in tracks.items()}
    merged = {tid: dict(per) for tid, per in tracks.items()}
    for zid in stations:
        ids = sorted([t for t in merged if zones.get(t) == zid], key=lambda t: min(merged[t]))
        head = None
        for t in ids:
            if head is None or t not in merged:
                head = t
                continue
            a, b = merged[head], merged[t]
            if min(b) <= max(a) - 3:  # alive at the same time: a different person
                if len(b) > len(a):
                    head = t
                continue
            la, fb = a[max(a)][0], b[min(b)][0]
            w = max(20.0, float(np.median([a[j][0][2] - a[j][0][0] for j in a])))
            d = np.hypot((la[0] + la[2]) / 2 - (fb[0] + fb[2]) / 2, (la[1] + la[3]) / 2 - (fb[1] + fb[3]) / 2)
            if d <= 3.0 * w and min(b) - max(a) <= 6 * OUT_FPS:
                a.update(b)
                del merged[t]
    order = sorted(merged, key=lambda t: min(merged[t]))
    return {k + 1: merged[t] for k, t in enumerate(order)}


def activity(tracks, n, cartons):
    """Per-frame activity code for every track."""
    acts = {}
    for tid, per in tracks.items():
        idx = sorted(per)
        a = {}
        for j in idx:
            box, _, kp = per[j]
            h = max(box[3] - box[1], 1.0)
            c = ((box[0] + box[2]) / 2, box[3])
            jb = max(idx[0], j - OUT_FPS)  # ~1 s back
            pb = per.get(jb, per[idx[0]])[0]
            v = np.hypot(c[0] - (pb[0] + pb[2]) / 2, c[1] - pb[3]) / h * OUT_FPS / max(1, j - jb)
            if v > 0.55 and j - jb >= 5:
                a[j] = ACT["walking"]
                continue
            if kp is None or h < 90:
                a[j] = ACT["present"]
                continue
            wr = [kp[k] for k in (9, 10) if kp[k][2] > 0.3]
            sh = [kp[k] for k in (5, 6) if kp[k][2] > 0.3]
            if not wr:
                a[j] = ACT["present"]
                continue
            if sh and min(w[1] for w in wr) < min(s[1] for s in sh) - 0.03 * h:
                a[j] = ACT["lifting"]
                continue
            near = any(inside(w[:2], z) for w in wr for z in cartons)
            jw = max(idx[0], j - OUT_FPS // 2)
            kpb = per.get(jw, per[idx[0]])[2]
            ws = 0.0
            if kpb is not None:
                ws = max((np.hypot(kp[k][0] - kpb[k][0], kp[k][1] - kpb[k][1]) for k in (9, 10)
                          if kp[k][2] > 0.3 and kpb[k][2] > 0.3), default=0.0) / h * OUT_FPS / max(1, j - jw)
            a[j] = ACT["handling"] if (near or ws > 0.28) else ACT["idle"]
        # smooth: majority over ±0.5 s; idle only counts after 2 s
        keys = sorted(a)
        sm = {}
        for j in keys:
            win = [a[k] for k in range(j - 7, j + 8) if k in a]
            sm[j] = Counter(win).most_common(1)[0][0]
        run = []
        for j in keys + [None]:
            if j is not None and sm[j] == ACT["idle"]:
                run.append(j)
                continue
            if 0 < len(run) < 2 * OUT_FPS:
                for k in run:
                    sm[k] = ACT["handling"]
            run = []
        acts[tid] = sm
    return acts


# ── pillows (fixed cameras) ────────────────────────────────────────────
class PillowTracker:
    """Moving bright objects (bagged pillows) from MOG2 foreground x colour model."""

    def __init__(self, frames, sat_max=55, val_min=178, min_area=900, roi=None):
        self.bg = cv2.createBackgroundSubtractorMOG2(history=240, varThreshold=20, detectShadows=False)
        for f in frames[::3]:                      # learn the empty-ish background first
            self.bg.apply(cv2.resize(f, (OUT_W // 2, OUT_H // 2)), learningRate=0.04)
        self.sat_max, self.val_min, self.min_area = sat_max, val_min, min_area
        self.roi = roi
        self.tracks, self.next_id, self.done = {}, 1, []

    def step(self, i, frame, body_mask=None):
        small = cv2.resize(frame, (OUT_W // 2, OUT_H // 2))
        fg = self.bg.apply(small, learningRate=0.003)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        m = cv2.bitwise_and(fg, cv2.inRange(hsv, (0, 0, self.val_min), (180, self.sat_max, 255)))
        if body_mask is not None:
            m[body_mask > 0] = 0
        if self.roi is not None:
            m = cv2.bitwise_and(m, self.roi)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        n, _, st, cen = cv2.connectedComponentsWithStats(m)
        dets = []
        for k in range(1, n):
            x, y, w, h, a = st[k]
            if a * 4 < self.min_area:
                continue
            dets.append((np.array([x * 2, y * 2, (x + w) * 2, (y + h) * 2], float), a * 4))
        # greedy association by distance
        used = set()
        for tid, t in list(self.tracks.items()):
            best, bd = None, 1e9
            lc = t["boxes"][-1][1]
            cx, cy = (lc[0] + lc[2]) / 2, (lc[1] + lc[3]) / 2
            for k, (b, a) in enumerate(dets):
                if k in used:
                    continue
                d = np.hypot((b[0] + b[2]) / 2 - cx, (b[1] + b[3]) / 2 - cy)
                if d < bd:
                    best, bd = k, d
            if best is not None and bd < 110:
                used.add(best)
                t["boxes"].append((i, dets[best][0]))
                t["miss"] = 0
            else:
                t["miss"] += 1
                if t["miss"] > 3:
                    self.done.append(self.tracks.pop(tid))
        for k, (b, a) in enumerate(dets):
            if k not in used:
                self.tracks[self.next_id] = {"id": self.next_id, "boxes": [(i, b)], "miss": 0}
                self.next_id += 1

    def finish(self):
        self.done.extend(self.tracks.values())
        self.tracks = {}
        return [t for t in self.done if len(t["boxes"]) >= 4]


def body_mask_for(tracks, j):
    """Torso and legs of every posed person (half resolution), so clothing is not
    mistaken for pillows. Hands/forearms stay visible: that is where pillows are."""
    mask = np.zeros((OUT_H // 2, OUT_W // 2), np.uint8)
    for per in tracks.values():
        if j not in per or per[j][2] is None:
            continue
        kp = per[j][2] / 2
        ok = lambda k: kp[k][2] > 0.3  # noqa: E731
        torso = [k for k in (5, 6, 12, 11) if ok(k)]
        if len(torso) == 4:
            cv2.fillConvexPoly(mask, np.int32([kp[k][:2] for k in torso]), 255)
        for a, b in ((11, 13), (13, 15), (12, 14), (14, 16), (5, 7), (6, 8)):
            if ok(a) and ok(b):
                cv2.line(mask, tuple(np.int32(kp[a][:2])), tuple(np.int32(kp[b][:2])), 255, 9)
    return cv2.dilate(mask, np.ones((5, 5), np.uint8))


# ── OCR ────────────────────────────────────────────────────────────────
_ocr = None


def ocr_frame(frame):
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    res, _ = _ocr(frame)
    out = []
    for box, text, conf in res or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append((text, float(conf), [int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))]))
    return out


def norm(s):
    return re.sub(r"[^A-Z0-9#]", "", s.upper())


def match_vocab(text, vocab):
    best, score = None, 0.0
    t = norm(text)
    for v in vocab:
        r = difflib.SequenceMatcher(None, t, norm(v)).ratio()
        if norm(v) in t or t in norm(v) and len(t) >= 4:
            r = max(r, 0.8)
        if r > score:
            best, score = v, r
    return (best, score) if score >= 0.62 else (None, score)


# ── analysis ───────────────────────────────────────────────────────────
def nearest_person(tracks, j, pt, limit=260):
    """Station person whose wrists (else box centre) are closest to pt at frame j."""
    who, wd = None, 1e9
    for tid, per in tracks.items():
        if j not in per:
            continue
        box, _, kp = per[j]
        pts = [kp[k][:2] for k in (9, 10) if kp is not None and kp[k][2] > 0.3] or \
              [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)]
        d = min(np.hypot(p[0] - pt[0], p[1] - pt[1]) for p in pts)
        if d < wd:
            who, wd = tid, d
    return who if wd <= limit else None


def white_near(frame, cx, cy, sat_max, val_min, rx=38, up=40, down=12):
    patch = frame[max(0, int(cy) - up):int(cy) + down, max(0, int(cx) - rx):int(cx) + rx]
    if patch.size == 0:
        return 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    return float(((hsv[..., 1] < sat_max + 5) & (hsv[..., 2] > val_min - 10)).mean())


def hand_placements(frames, tracks, pillow_tracks, z, pp):
    """Retail cartons on a packing table: pillows are white like the pile behind them, so a
    blob track alone is ambiguous. Two cues, merged per carton:
      hands   the packer's wrists lift above the carton rim holding something white and
              plunge below the rim within 0.8 s (pose keypoints, smoothed);
      track   a white blob track that descends into the drop region above the carton.
    Candidates closer than the debounce are one placement."""
    n = len(frames)
    sat, val = pp.get("sat_max", 45), pp.get("val_min", 185)
    p = np.array(z["pts"])
    x0, y0 = p.min(0)
    x1, y1 = p.max(0)
    cand = []
    # the packer working this carton: most wrist time inside it
    packer, hits = None, 0
    for tid, per in tracks.items():
        c = sum(1 for _, _, kp in per.values() if kp is not None for k in (9, 10)
                if kp[k][2] > 0.3 and x0 - 12 <= kp[k][0] <= x1 + 12 and y0 - 15 <= kp[k][1] <= y1)
        if c > hits:
            packer, hits = tid, c
    if packer is not None and hits >= OUT_FPS:
        wx, wy = np.full(n, np.nan), np.full(n, np.nan)
        for j, (_, _, kp) in tracks[packer].items():
            if kp is None:
                continue
            ws = [kp[k][:2] for k in (9, 10) if kp[k][2] > 0.3]
            if ws:
                wx[j], wy[j] = np.mean(ws, axis=0)
        ok = ~np.isnan(wy)
        if ok.sum() > 5:
            idx = np.arange(n)
            wy = np.interp(idx, idx[ok], wy[ok])
            wx = np.interp(idx, idx[ok], wx[ok])
            wy = np.convolve(np.pad(wy, 1, mode="edge"), np.ones(3) / 3, mode="valid")
            f = 4
            while f < n - 2:
                if wy[f] <= wy[max(0, f - 4):f + 5].min() + 0.01 and wy[f] <= y0 - 4 and x0 - 30 <= wx[f] <= x1 + 30:
                    hit = next((g for g in range(f + 1, min(n, f + 13))
                                if wy[g] >= y0 + 12 and x0 - 15 <= wx[g] <= x1 + 15), None)
                    if hit is not None:
                        carry = max(white_near(frames[g], wx[g], wy[g], sat, val) for g in range(max(0, f - 2), min(n, f + 3)))
                        if carry >= 0.22:
                            cand.append((hit, packer, "hands"))
                        f = hit
                f += 1
    drop_up = z.get("drop_up", 95)
    for t in pillow_tracks:
        jf, bf = t["boxes"][-1]
        j0, b0 = t["boxes"][0]
        ce = ((bf[0] + bf[2]) / 2, (bf[1] + bf[3]) / 2)
        cs = ((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
        if not (x0 - 15 <= ce[0] <= x1 + 15 and y0 - drop_up <= ce[1] <= y1):
            continue
        top = min((b[1] + b[3]) / 2 for _, b in t["boxes"])
        if np.hypot(ce[0] - cs[0], ce[1] - cs[1]) < z.get("min_travel", 30) or jf - j0 < 4 or ce[1] - top < 12:
            continue
        cand.append((jf, nearest_person(tracks, jf, ce), "track"))
    out = []
    for j, who, src in sorted(cand, key=lambda c: (c[0], c[2] != "hands")):
        if out and j - out[-1][0] < z.get("debounce", 18):
            if src == "hands" or out[-1][1] is None:  # the packer's own hands name the worker
                out[-1] = (out[-1][0], who, out[-1][2])
            if src not in out[-1][2]:
                out[-1] = (out[-1][0], out[-1][1], out[-1][2] + "+" + src)
            continue
        out.append((j, who, src))
    # a blob alone is not a placement here: on this table it is as often a tape roll, a carton
    # flap or a carton being carried away; it only corroborates a hand placement
    return [o for o in out if "hands" in o[2]]


def lp_zone_events(d: dict) -> list[dict]:
    """Badge-only zones (loss prevention). An entry is a person who was outside the zone for
    at least 0.4 s, then inside for at least 1 s. 'badged' mirrors the browser's identity
    fusion: station workers and large foreground people have a badge read nearby; anyone else
    is 'no badge read' — an access check for security to verify, never an accusation."""
    fps = d["fps"]
    zones = [z for z in d["zones"] if z["kind"] == "restricted"]
    if not zones:
        return []
    feet = defaultdict(dict)
    for f, ps in enumerate(d["p"]):
        for p in ps:
            feet[p[0]][f] = (p[1] + p[3] / 2, p[2] + p[4])
    out = []
    for z in zones:
        for tid, per in feet.items():
            pp = d["people"].get(str(tid)) or d["people"].get(tid) or {}
            badged = bool(pp.get("zone")) or (pp.get("h", 0) >= 150 and pp.get("last", 0) - pp.get("first", 0) >= 3 * fps)
            out_run, entered, in_run = 0, None, 0

            def emit():
                out.append({"type": "restricted", "f": entered, "t": round(entered / fps, 2), "zone": z["id"],
                            "by": tid, "badged": badged, "dwell": round(in_run / fps, 1)})
            for f in sorted(per):
                if inside(per[f], z["pts"]):
                    if entered is None and out_run >= 0.4 * fps:
                        entered, in_run = f, 0
                    if entered is not None:
                        in_run += 1
                    out_run = 0
                    continue
                if entered is not None and in_run >= fps:
                    emit()
                entered, in_run = None, 0
                out_run += 1
            if entered is not None and in_run >= fps:
                emit()
    return sorted(out, key=lambda e: e["f"])


def apply_lp(d: dict, fd: dict) -> dict:
    """(Re)compute badge-only zone events on an analysed feed from its stored tracks."""
    lp = [z for z in fd.get("zones", []) if z["kind"] == "restricted"]
    d["zones"] = [z for z in d["zones"] if z["kind"] != "restricted"] + lp
    d["ev"] = sorted([e for e in d["ev"] if e["type"] != "restricted"] + lp_zone_events(d), key=lambda e: e["f"])
    names = {z["id"]: z["name"] for z in lp}
    ents = [e for e in d["ev"] if e["type"] == "restricted"]
    d["summary"]["badge_checks"] = len(ents)
    d["summary"]["lp_events"] = [{"t": e["t"], "zone": e["zone"], "zone_name": names.get(e["zone"], e["zone"]),
                                  "dwell": e["dwell"]} for e in ents if not e["badged"]]
    return d


def analyze(fd):
    t_start = time.time()
    path = OUT_DIR / f"{fd['id']}.mp4"
    frames = read_frames(path)
    n = len(frames)
    fixed = not fd.get("ptz")
    step = 2 if fd["kind"] == "packing" else 3
    zones = fd.get("zones", [])
    cartons = {z["id"]: z for z in zones if z["kind"] == "carton"}
    stations = {z["id"]: z for z in zones if z["kind"] == "station"}

    packing = fd["kind"] == "packing"
    raw = detect_people(frames, step, imgsz=960 if packing else 640, fast=not packing)
    tracks = merge_station_tracks(interpolate_people(raw, n, step), stations)
    acts = activity(tracks, n, [z["pts"] for z in cartons.values()])

    # objects
    objects = defaultdict(list)
    events = []
    pillow_tracks = []
    if fixed and (cartons or fd.get("count_line")):
        roi = None
        if fd.get("roi"):
            roi = np.zeros((OUT_H // 2, OUT_W // 2), np.uint8)
            for poly_ in fd["roi"]:
                cv2.fillPoly(roi, [np.int32(np.array(poly_) / 2)], 255)
        pt = PillowTracker(frames, roi=roi, **fd.get("pillow_params", {}))
        for j, f in enumerate(frames):
            pt.step(j, f, body_mask_for(tracks, j))
        pillow_tracks = pt.finish()
        for t in pillow_tracks:
            for j, b in t["boxes"]:
                objects[j].append([1000 + t["id"], OBJ["pillow"], *[int(v) for v in (b[0], b[1], b[2] - b[0], b[3] - b[1])], 80])
    if not fixed or fd.get("yoloe"):
        ye = model("yoloe-11s-seg.pt")
        cls_map = {0: OBJ["pillow"], 1: OBJ["pillow"], 2: OBJ["carton"], 3: OBJ["pallet"]}
        for j in range(0, n, 3):
            r = ye.predict(frames[j], imgsz=768, conf=0.12, verbose=False)[0]
            dets = [[2000 + k, cls_map[int(c)], int(b[0]), int(b[1]), int(b[2] - b[0]), int(b[3] - b[1]), int(cf * 100)]
                    for k, (b, c, cf) in enumerate(zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()))]
            for jj in range(j, min(n, j + 3)):
                objects[jj].extend(dets)

    # pillow placed in carton: a track that ends inside a carton opening, coming from above/outside
    worker_of = {}
    counts = defaultdict(int)
    for t in sorted(pillow_tracks, key=lambda t: t["boxes"][-1][0]):
        jf, bf = t["boxes"][-1]
        j0, b0 = t["boxes"][0]
        c_end = ((bf[0] + bf[2]) / 2, (bf[1] + bf[3]) / 2)
        c_start = ((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
        for zid, z in cartons.items():
            if z.get("hands") or not inside(c_end, z["pts"]):
                continue
            travel = np.hypot(c_end[0] - c_start[0], c_end[1] - c_start[1])
            if travel < z.get("min_travel", 25) or (jf - j0) < 4:
                continue
            if z.get("descend"):  # it must come down into the opening, not just sit there
                top = min((b[1] + b[3]) / 2 for _, b in t["boxes"])
                if c_end[1] - top < z["descend"]:
                    continue
            last = [e for e in events if e["type"] == "pillow_in" and e["zone"] == zid]
            if last and jf - last[-1]["f"] < z.get("debounce", 15):
                continue
            # who placed it: nearest station person (wrists, else box) at that moment
            who, wd = None, 1e9
            for tid, per in tracks.items():
                if jf not in per:
                    continue
                box, _, kp = per[jf]
                pts = [kp[k][:2] for k in (9, 10) if kp is not None and kp[k][2] > 0.3] or \
                      [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)]
                d = min(np.hypot(p[0] - c_end[0], p[1] - c_end[1]) for p in pts)
                if d < wd:
                    who, wd = tid, d
            if wd > 260:
                who = None
            per_event = z.get("per_event", 1)
            counts[zid] += per_event
            worker_of[t["id"]] = who
            events.append({"type": "pillow_in", "f": int(jf), "t": round(jf / OUT_FPS, 2), "zone": zid, "by": who,
                           "n": per_event, "count": counts[zid], "pillow": 1000 + t["id"]})
            spec = z.get("spec")
            if spec and counts[zid] % spec == 0:
                events.append({"type": "carton_complete", "f": int(jf) + 4, "t": round((jf + 4) / OUT_FPS, 2),
                               "zone": zid, "count": spec, "by": who})
            break
    for zid, z in cartons.items():
        if not z.get("hands"):
            continue
        for jf, who, src in hand_placements(frames, tracks, pillow_tracks, z, fd.get("pillow_params", {})):
            counts[zid] += 1
            events.append({"type": "pillow_in", "f": int(jf), "t": round(jf / OUT_FPS, 2), "zone": zid, "by": who,
                           "n": 1, "count": counts[zid], "cue": src})
            spec = z.get("spec")
            if spec and (counts[zid] + z.get("offset", 0)) % spec == 0:
                events.append({"type": "carton_complete", "f": int(jf) + 4, "t": round((jf + 4) / OUT_FPS, 2),
                               "zone": zid, "count": spec, "by": who})
    events.sort(key=lambda e: e["f"])

    # conveyor: belt speed and pillows crossing the count line
    flow_series = []
    if fixed and fd.get("conveyor"):
        cz = np.array(fd["conveyor"]["pts"], np.int32)
        mask = np.zeros((OUT_H, OUT_W), np.uint8)
        cv2.fillPoly(mask, [cz], 255)
        mask_s = cv2.resize(mask, (OUT_W // 2, OUT_H // 2)) > 0
        prev = None
        for j, f in enumerate(frames):
            g = cv2.cvtColor(cv2.resize(f, (OUT_W // 2, OUT_H // 2)), cv2.COLOR_BGR2GRAY)
            if prev is not None and j % 3 == 0:
                fl = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 21, 3, 5, 1.1, 0)
                vx = fl[..., 0][mask_s]
                mv = vx[np.abs(vx) > 0.4]
                flow_series.append(round(float(np.median(mv)) * 2 * OUT_FPS if len(mv) > 50 else 0.0, 1))
            prev = g
        line = fd.get("count_line")
        if line:
            # virtual line sensor: share of pixels at the count line moving downstream (optical
            # flow, half resolution). The sewing table behind the belt is white too, so colour
            # alone cannot see a pillow pass; motion can. One rising edge = one pillow.
            lx = int(line[0][0]) // 2
            ly0, ly1 = sorted((int(line[0][1]) // 2, int(line[1][1]) // 2))
            direction = fd["conveyor"].get("direction", 1)
            sig = [0.0]
            prev = cv2.cvtColor(cv2.resize(frames[0], (OUT_W // 2, OUT_H // 2)), cv2.COLOR_BGR2GRAY)[ly0:ly1]
            for f in frames[1:]:
                g = cv2.cvtColor(cv2.resize(f, (OUT_W // 2, OUT_H // 2)), cv2.COLOR_BGR2GRAY)[ly0:ly1]
                fl = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 15, 3, 5, 1.1, 0)
                sig.append(float((fl[:, max(0, lx - 10):lx + 10, 0] * direction > 0.8).mean()))
                prev = g
            sig = np.convolve(np.pad(sig, 1, mode="edge"), np.ones(3) / 3, mode="valid")
            on, start = False, -99
            for j, v in enumerate(sig):
                if not on and v > 0.22 and j - start > 8:
                    on, start = True, j
                elif on and v < 0.08:
                    on = False
                    peak = start + int(np.argmax(sig[start:j + 1]))
                    counts["line"] += 1
                    events.append({"type": "line_cross", "f": peak, "t": round(peak / OUT_FPS, 2), "count": counts["line"]})

    # station presence, idle and left-station events
    people_summary = {}
    heat = np.zeros((18, 32), np.float32)
    for tid, per in tracks.items():
        idx = sorted(per)
        feet = {j: ((per[j][0][0] + per[j][0][2]) / 2, per[j][0][3]) for j in idx}
        for j in idx[::2]:
            x, y = feet[j]
            if 0 <= x < OUT_W and 0 <= y < OUT_H:
                heat[int(y / OUT_H * 18), int(x / OUT_W * 32)] += 1
        zone_hits = Counter()
        for j in idx:
            for zid, z in stations.items():
                if inside(feet[j], z["pts"]):
                    zone_hits[zid] += 1
        zone = None
        if zone_hits:
            zid, hits = zone_hits.most_common(1)[0]
            if hits >= 0.35 * len(idx) and hits >= 2 * OUT_FPS:
                zone = zid
        a = acts.get(tid, {})
        cnt = Counter(a.values())
        tot = max(1, sum(cnt.values()))
        h_med = float(np.median([per[j][0][3] - per[j][0][1] for j in idx]))
        people_summary[tid] = {
            "first": idx[0], "last": idx[-1], "zone": zone, "h": round(h_med),
            "pct": {k: round(100 * cnt.get(v, 0) / tot) for k, v in ACT.items()},
            "placed": sum(1 for e in events if e["type"] == "pillow_in" and e.get("by") == tid),
        }
        if zone:
            run = []
            for j in idx + [None]:
                if j is not None and a.get(j) == ACT["idle"]:
                    run.append(j)
                    continue
                if len(run) >= 4 * OUT_FPS:
                    events.append({"type": "idle", "f": run[0], "t": round(run[0] / OUT_FPS, 2),
                                   "end": round(run[-1] / OUT_FPS, 2), "by": tid, "zone": zone,
                                   "seconds": round(len(run) / OUT_FPS, 1)})
                run = []
            out_run = []
            for j in idx + [None]:
                if j is not None and not inside(feet[j], stations[zone]["pts"]):
                    out_run.append(j)
                    continue
                if len(out_run) >= 6 * OUT_FPS:
                    events.append({"type": "left_station", "f": out_run[0], "t": round(out_run[0] / OUT_FPS, 2),
                                   "end": round(out_run[-1] / OUT_FPS, 2), "by": tid, "zone": zone,
                                   "seconds": round(len(out_run) / OUT_FPS, 1)})
                out_run = []

    # OCR on the carton print / labels
    ocr = []
    rec = fd.get("recognition")
    hits = defaultdict(list)
    if rec and rec.get("vocab"):
        every = int(fd.get("ocr_every", 2.0) * OUT_FPS)
        for j in range(0, n, every):
            for text, conf, box in ocr_frame(frames[j]):
                if conf < 0.5:
                    continue
                m, score = match_vocab(text, rec["vocab"])
                if m is None:
                    continue
                ocr.append({"f": j, "t": round(j / OUT_FPS, 2), "raw": text, "text": m, "conf": round(conf, 2),
                            "score": round(score, 2), "box": box})
                hits[m].append(conf)
    recognition = None
    if rec:
        evidence = [{"text": k, "reads": len(v), "conf": round(float(np.mean(v)), 2)} for k, v in hits.items()]
        evidence.sort(key=lambda e: -e["reads"])
        recognition = {k: rec[k] for k in ("customer_code", "customer", "brand", "product", "rule", "label") if k in rec}
        recognition["evidence"] = evidence
        recognition["confidence"] = round(float(np.mean([e["conf"] for e in evidence])), 2) if evidence else None
        if evidence:
            first = min(o["t"] for o in ocr)
            events.append({"type": "label_read", "f": int(first * OUT_FPS), "t": first,
                           "text": " · ".join(e["text"] for e in evidence[:3]), "customer": rec.get("customer")})
        else:  # nothing legible on camera: no recognition claim
            recognition = None

    events.sort(key=lambda e: e["f"])

    # compact per-frame arrays
    p_frames, k_frames = [], []
    for j in range(n):
        ps, ks = [], []
        for tid, per in tracks.items():
            if j not in per:
                continue
            box, c, kp = per[j]
            x0, y0, x1, y1 = (int(round(v)) for v in box)
            ps.append([tid, x0, y0, x1 - x0, y1 - y0, int(round(c * 100)), int(acts.get(tid, {}).get(j, ACT["present"]))])
            if kp is not None and (y1 - y0) >= 110:
                flat = []
                for k in range(17):
                    flat += [int(kp[k][0]), int(kp[k][1])] if kp[k][2] > 0.3 else [-1, -1]
                ks.append([tid, *flat])
        p_frames.append(ps)
        k_frames.append(ks)
    o_frames = [objects.get(j, []) for j in range(n)]

    per_sec = []
    for s in range(int(np.ceil(n / OUT_FPS))):
        js = range(s * OUT_FPS, min(n, (s + 1) * OUT_FPS))
        people = np.mean([len(p_frames[j]) for j in js])
        working = np.mean([sum(1 for p in p_frames[j] if p[6] in (1, 2)) for j in js])
        per_sec.append([round(float(people), 1), round(float(working), 1)])

    placed = [e for e in events if e["type"] == "pillow_in"]
    dur = n / OUT_FPS
    station_people = [p for p in people_summary.values() if p["zone"]]
    summary = {
        "people_peak": max((len(p) for p in p_frames), default=0),
        "people_avg": round(float(np.mean([len(p) for p in p_frames])), 1),
        "tracks": len(tracks),
        "pillows_placed": sum(e["n"] for e in placed),
        "placements": len(placed),
        "pillows_per_min": round(sum(e["n"] for e in placed) / dur * 60, 1) if placed else None,
        "cartons_completed": sum(1 for e in events if e["type"] == "carton_complete"),
        "line_crossings": counts.get("line", 0),
        "belt_speed_px_s": round(float(np.median([v for v in flow_series if v])), 1) if any(flow_series) else None,
        "station_active_pct": round(float(np.mean([100 - p["pct"]["idle"] for p in station_people])), 1) if station_people else None,
        "idle_events": sum(1 for e in events if e["type"] == "idle"),
        "ocr_reads": len(ocr),
    }
    data = {
        "v": 1, "id": fd["id"], "w": OUT_W, "h": OUT_H, "fps": OUT_FPS, "n": n, "dur": round(dur, 2),
        "models": model_info(fd, fixed),
        "zones": zones + ([{"id": "line", "kind": "count_line", "name": "Count line", "pts": fd["count_line"]}] if fd.get("count_line") else [])
                 + ([{"id": "belt", **fd["conveyor"], "kind": "conveyor"}] if fd.get("conveyor") else []),
        "p": p_frames, "k": k_frames, "o": o_frames, "ev": events, "ocr": ocr,
        "people": people_summary, "series": per_sec, "flow": flow_series,
        "heat": (heat / max(1.0, heat.max()) * 100).round().astype(int).tolist(),
        "summary": summary, "recognition": recognition,
        "processing_s": round(time.time() - t_start, 1),
    }
    apply_lp(data, fd)
    (OUT_DIR / f"{fd['id']}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data


def model_info(fd, fixed):
    if fd["kind"] == "packing":
        out = {"people": "YOLO11m + ByteTrack", "pose": "YOLO11s-pose (17 keypoints)"}
    else:
        out = {"people": "YOLO11s-pose + ByteTrack", "pose": "YOLO11s-pose (17 keypoints)"}
    out["activity"] = "Keypoint & motion classifier"
    if fixed and (fd.get("zones") or fd.get("count_line")):
        out["pillows"] = "MOG2 motion × colour segmentation + blob tracking"
    if not fixed or fd.get("yoloe"):
        out["objects"] = "YOLOE-11s open-vocabulary (pillow, carton, pallet)"
    if fd.get("recognition", {}).get("vocab"):
        out["ocr"] = "PP-OCRv4 (RapidOCR)"
    if any(z.get("hands") for z in fd.get("zones", [])):
        out["placement"] = "Hand-pose lift & plunge + blob track"
    if fd.get("conveyor"):
        out["flow"] = "Farnebäck optical flow"
    if fd.get("count_line"):
        out["count"] = "Optical-flow line sensor"
    return out


def write_catalog():
    feeds = []
    for fd in FEEDS:
        p = OUT_DIR / f"{fd['id']}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        feeds.append({
            "id": fd["id"], "factory": fd["factory"], "camera": fd["camera"], "title": fd["title"], "kind": fd["kind"],
            "station": fd.get("station"), "area": fd.get("area"), "ptz": bool(fd.get("ptz")),
            "duration": d["dur"], "fps": d["fps"], "width": d["w"], "height": d["h"],
            "video": f"/cctv/{fd['id']}.mp4", "poster": f"/cctv/{fd['id']}.jpg", "analytics": f"/cctv/{fd['id']}.json",
            "models": d["models"], "summary": d["summary"], "recognition": d["recognition"],
        })
    CATALOG.write_text(json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "feeds": feeds}, indent=1), encoding="utf-8")
    return len(feeds)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--catalog-only" in args:
        print("catalog feeds:", write_catalog())
        sys.exit(0)
    if "--lp" in args:  # badge-only zone events from stored tracks, no re-detection
        for fd in FEEDS:
            p = OUT_DIR / f"{fd['id']}.json"
            if p.exists():
                d = apply_lp(json.loads(p.read_text(encoding="utf-8")), fd)
                p.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
                print(fd["id"], "badge checks", d["summary"]["badge_checks"], "no-badge", len(d["summary"]["lp_events"]))
        print("catalog feeds:", write_catalog())
        sys.exit(0)
    want = {a for a in args if not a.startswith("--")}
    for fd in FEEDS:
        if want and fd["id"] not in want:
            continue
        d = analyze(fd)
        s = d["summary"]
        print(fd["id"], f"{d['processing_s']}s", json.dumps(s), flush=True)
    if "--no-catalog" not in args:
        print("catalog feeds:", write_catalog())
