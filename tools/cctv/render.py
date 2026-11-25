"""Render CCTV camera feeds from the plant-floor source clips.

Each feed in feeds.FEEDS is one camera: a segment of a source clip, stabilized
(locked to a reference frame for fixed cameras, or smoothed for PTZ tours),
re-framed (crop / mounting angle / lens), given that camera's sensor look
(white balance, contrast, noise, vignette) and encoded as a seamlessly looping
15 fps H.264 MP4 for the browser.

    .cvenv/Scripts/python tools/cctv/render.py [feed-id ...]
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from feeds import FEEDS, OUT_DIR, OUT_FPS, OUT_H, OUT_W, SOURCES  # noqa: E402

XFADE_S = 0.6


# ── reading ────────────────────────────────────────────────────────────
def read_segment(src: str, t0: float, t1: float, portrait_y: float | None = None):
    """Frames of [t0, t1) resampled to OUT_FPS. Portrait clips are cut to a 16:9 window
    centred at portrait_y (fraction of the height)."""
    cap = cv2.VideoCapture(str(SOURCES[src]))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t0 * fps)))
    frames, i, want = [], int(round(t0 * fps)), t0
    while want < t1:
        ok, im = cap.read()
        if not ok:
            break
        t = i / fps
        i += 1
        if t + 0.5 / fps < want:
            continue
        want += 1.0 / OUT_FPS
        if im.shape[0] > im.shape[1]:  # portrait phone clip -> landscape window
            h = int(im.shape[1] * 9 / 16)
            cy = int((portrait_y or 0.5) * im.shape[0])
            y0 = max(0, min(im.shape[0] - h, cy - h // 2))
            im = im[y0:y0 + h]
        frames.append(im)
    return frames


# ── stabilization ──────────────────────────────────────────────────────
def _pair_motion(a, b):
    """Similarity transform (dx, dy, da, ds) taking frame a to frame b."""
    pts = cv2.goodFeaturesToTrack(a, maxCorners=400, qualityLevel=0.01, minDistance=12, blockSize=7)
    if pts is None or len(pts) < 12:
        return 0.0, 0.0, 0.0, 0.0
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(21, 21), maxLevel=3)
    good = st.ravel() == 1
    if good.sum() < 10:
        return 0.0, 0.0, 0.0, 0.0
    m, _ = cv2.estimateAffinePartial2D(pts[good], nxt[good], method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None:
        return 0.0, 0.0, 0.0, 0.0
    s = float(np.hypot(m[0, 0], m[1, 0]))
    return float(m[0, 2]), float(m[1, 2]), float(np.arctan2(m[1, 0], m[0, 0])), float(np.log(max(s, 1e-6)))


def _compose(m, tx, ty, a, s):
    return np.array([[s * np.cos(a), -s * np.sin(a), tx], [s * np.sin(a), s * np.cos(a), ty]], dtype=np.float64)


def lock_to_reference(frames):
    """Fixed camera: register every frame directly to the middle frame (ORB features +
    RANSAC similarity), so the result cannot drift the way chained estimates do."""
    sc = 0.5
    gray = [cv2.cvtColor(cv2.resize(f, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY) for f in frames]
    orb = cv2.ORB_create(3000, fastThreshold=8)
    kr, dr = orb.detectAndCompute(gray[len(gray) // 2], None)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    params, last = [], (0.0, 0.0, 0.0, 1.0)
    for g in gray:
        k, d = orb.detectAndCompute(g, None)
        est = None
        if d is not None and len(k) > 30:
            good = [p[0] for p in bf.knnMatch(d, dr, k=2) if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
            if len(good) >= 25:
                src = np.float32([k[x.queryIdx].pt for x in good])
                dst = np.float32([kr[x.trainIdx].pt for x in good])
                m, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=1.5,
                                                     maxIters=5000, confidence=0.997)
                if m is not None and inl is not None and inl.sum() >= 20:
                    est = (m[0, 2] / sc, m[1, 2] / sc, float(np.arctan2(m[1, 0], m[0, 0])), float(np.hypot(m[0, 0], m[1, 0])))
        last = est or last
        params.append(last)
    p = np.array(params)
    ker = cv2.getGaussianKernel(7, 1.4).ravel()          # remove estimation jitter, keep the lock
    pad = np.pad(p, ((3, 3), (0, 0)), mode="edge")
    p = np.stack([np.convolve(pad[:, j], ker, mode="valid") for j in range(4)], axis=1)
    h, w = frames[0].shape[:2]
    shx = np.abs(p[:, 0]) + np.abs(np.sin(p[:, 2])) * h / 2 + np.abs(p[:, 3] - 1) * w / 2
    shy = np.abs(p[:, 1]) + np.abs(np.sin(p[:, 2])) * w / 2 + np.abs(p[:, 3] - 1) * h / 2
    zoom = float(min(1.0 + 2.0 * max(shx.max() / w, shy.max() / h) + 0.015, 1.35))
    z = np.array([[zoom, 0, (1 - zoom) * w / 2], [0, zoom, (1 - zoom) * h / 2], [0, 0, 1]])
    out = []
    for f, (tx, ty, a, s) in zip(frames, p):
        m = z @ np.vstack([_compose(None, tx, ty, a, s), [0, 0, 1]])
        out.append(cv2.warpAffine(f, m[:2], (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
    return out, zoom


def stabilize(frames, mode: str, smooth_s: float = 1.6):
    """'lock': cancel all camera motion (fixed camera). 'smooth': keep the slow pan,
    remove the hand-held shake (PTZ tour). Returns warped frames and the zoom used to
    hide the borders."""
    if mode == "none":
        return frames, 1.0
    if mode == "lock":
        return lock_to_reference(frames)
    sc = 0.5
    gray = [cv2.cvtColor(cv2.resize(f, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY) for f in frames]
    steps = [(0.0, 0.0, 0.0, 0.0)] + [_pair_motion(gray[i - 1], gray[i]) for i in range(1, len(gray))]
    traj = np.cumsum(np.array(steps), axis=0)
    traj[:, :2] /= sc
    if mode == "lock":
        target = np.repeat(traj[len(traj) // 2][None, :], len(traj), axis=0)  # lock to the middle frame
    else:
        k = max(3, int(smooth_s * OUT_FPS))
        ker = cv2.getGaussianKernel(2 * k + 1, k / 2).ravel()
        pad = np.pad(traj, ((k, k), (0, 0)), mode="edge")
        target = np.stack([np.convolve(pad[:, j], ker, mode="valid") for j in range(4)], axis=1)
    corr = target - traj
    h, w = frames[0].shape[:2]
    max_shift = np.abs(corr[:, :2]).max(axis=0)
    zoom = 1.0 + 2.0 * max(max_shift[0] / w, max_shift[1] / h) + 0.02
    zoom = float(min(zoom, 1.35))
    out = []
    for f, (dx, dy, da, ds) in zip(frames, corr):
        s = np.exp(ds) * zoom
        m = cv2.getRotationMatrix2D((w / 2, h / 2), np.degrees(da), s)
        m[0, 2] += dx * zoom
        m[1, 2] += dy * zoom
        out.append(cv2.warpAffine(f, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
    return out, zoom


# ── camera look ────────────────────────────────────────────────────────
def _lens_maps(w, h, k1):
    """Barrel distortion (wide-angle CCTV lens)."""
    yy, xx = np.indices((h, w), dtype=np.float32)
    nx, ny = (xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)
    r2 = nx * nx + ny * ny
    f = 1 + k1 * r2
    norm = 1 + k1 * 1.0  # keep the corners inside the frame
    mx = (nx * f / norm) * (w / 2) + w / 2
    my = (ny * f / norm) * (h / 2) + h / 2
    return mx.astype(np.float32), my.astype(np.float32)


class Look:
    def __init__(self, spec: dict, w: int, h: int, seed: int):
        self.s = spec
        self.rng = np.random.default_rng(seed)
        self.maps = _lens_maps(w, h, spec.get("lens", 0.0)) if spec.get("lens") else None
        yy, xx = np.indices((h, w), dtype=np.float32)
        d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / np.sqrt(2)
        self.vig = (1 - spec.get("vignette", 0.25) * d ** 2.2).astype(np.float32)[..., None]
        wb = spec.get("wb", (1.0, 1.0, 1.0))  # B, G, R gains
        self.wb = np.array(wb, dtype=np.float32)[None, None, :]

    def __call__(self, im):
        s = self.s
        if self.maps is not None:
            im = cv2.remap(im, self.maps[0], self.maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        if s.get("soft", 0):
            im = cv2.GaussianBlur(im, (0, 0), s["soft"])
        f = im.astype(np.float32) / 255.0
        f = f * self.wb
        lum = f.mean(axis=2, keepdims=True)
        f = lum + (f - lum) * s.get("sat", 0.85)                      # CCTV sensors are less saturated
        f = (f - 0.5) * s.get("contrast", 1.0) + 0.5 + s.get("lift", 0.0)
        f = np.clip(f, 0, 1) ** s.get("gamma", 1.0)
        f = f * self.vig
        n = s.get("noise", 0.018)
        if n:
            grain = self.rng.normal(0, n, f.shape[:2]).astype(np.float32)[..., None]
            f = f + grain * (0.35 + 0.65 * (1 - f))                    # more noise in the shadows
        out = np.clip(f * 255, 0, 255).astype(np.uint8)
        if s.get("sharpen", 0.35):                                      # in-camera sharpening halo
            bl = cv2.GaussianBlur(out, (0, 0), 1.2)
            out = cv2.addWeighted(out, 1 + s.get("sharpen", 0.35), bl, -s.get("sharpen", 0.35), 0)
        return out


def reframe(im, crop, tilt: float = 0.0, rot: float = 0.0):
    """Crop (x0, y0, x1, y1 as fractions), optional keystone (a different mounting
    angle) and roll, then scale to the output size."""
    h, w = im.shape[:2]
    x0, y0, x1, y1 = crop
    src = np.float32([[x0 * w, y0 * h], [x1 * w, y0 * h], [x1 * w, y1 * h], [x0 * w, y1 * h]])
    if tilt:  # positive tilt: camera mounted higher, looking down (top edge wider)
        dx = tilt * (x1 - x0) * w
        src[0, 0] -= dx
        src[1, 0] += dx
    if rot:
        c = src.mean(axis=0)
        a = np.radians(rot)
        r = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]], dtype=np.float32)
        src = (src - c) @ r.T + c
    dst = np.float32([[0, 0], [OUT_W, 0], [OUT_W, OUT_H], [0, OUT_H]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(im, m, (OUT_W, OUT_H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


def loop_crossfade(frames):
    """Blend the last XFADE_S seconds into the first so the clip loops without a jump."""
    k = int(XFADE_S * OUT_FPS)
    if len(frames) < 4 * k:
        return frames
    body = frames[k:len(frames) - k]
    tail = []
    for i in range(k):
        a = (i + 1) / (k + 1)
        tail.append(cv2.addWeighted(frames[len(frames) - k + i], 1 - a, frames[i], a, 0))
    return body + tail


def encode(frames, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    gen = imageio_ffmpeg.write_frames(
        str(path), (OUT_W, OUT_H), fps=OUT_FPS, codec="libx264", pix_fmt_in="bgr24", macro_block_size=1,
        quality=None, bitrate=None,
        output_params=["-crf", "31", "-preset", "slow", "-g", str(OUT_FPS * 2),
                       "-movflags", "+faststart", "-tune", "film", "-an"])
    gen.send(None)
    for f in frames:
        gen.send(np.ascontiguousarray(f).tobytes())
    gen.close()


def render(feed: dict):
    src = feed["src"]
    t0, t1 = feed["seg"]
    frames = read_segment(src, t0, t1, feed.get("portrait_y"))
    frames, zoom = stabilize(frames, feed.get("stab", "lock"), feed.get("smooth_s", 1.6))
    look = Look(feed.get("look", {}), OUT_W, OUT_H, seed=zlib.crc32(feed["id"].encode()))
    out = [look(reframe(f, feed.get("crop", (0, 0, 1, 1)), feed.get("tilt", 0.0), feed.get("roll", 0.0))) for f in frames]
    out = loop_crossfade(out)
    path = OUT_DIR / f"{feed['id']}.mp4"
    encode(out, path)
    cv2.imwrite(str(OUT_DIR / f"{feed['id']}.jpg"), out[len(out) // 3], [cv2.IMWRITE_JPEG_QUALITY, 80])
    return {"id": feed["id"], "frames": len(out), "seconds": round(len(out) / OUT_FPS, 2), "zoom": round(zoom, 3),
            "kb": path.stat().st_size // 1024}


if __name__ == "__main__":
    want = set(sys.argv[1:])
    for fd in FEEDS:
        if want and fd["id"] not in want:
            continue
        print(render(fd), flush=True)
