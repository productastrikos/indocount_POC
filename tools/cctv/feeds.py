"""CCTV feed catalogue: which camera shows which part of which source clip, and how
that camera is mounted and tuned. Shared by render.py and analyze.py; the backend
reads the generated public/cctv/catalog.json."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "public" / "cctv"
OUT_W, OUT_H, OUT_FPS = 960, 540, 15

# Plant-floor footage supplied for the POC (phone recordings).
SOURCES = {
    "walk_a": ROOT / "WhatsApp Video 2026-11-24 at 18.16.11.mp4",       # filling → sewing conveyor → Serta packing
    "walk_b": ROOT / "WhatsApp Video 2026-11-24 at 18.16.12.mp4",       # fibre bins → opener → carding → bagging → rollers
    "display": ROOT / "WhatsApp Video 2026-11-24 at 18.16.13 (1).mp4",  # Beautyrest BLACK club display carton
    "cartons": ROOT / "WhatsApp Video 2026-11-24 at 18.16.13.mp4",      # Serta bed pillows · Dept. #22 RFID cartons
}

# Camera looks: each camera model / mounting has its own sensor response.
LOOK = {
    "neutral": {"wb": (1.00, 1.00, 1.00), "sat": 0.86, "contrast": 1.04, "gamma": 1.0, "noise": 0.016, "vignette": 0.22, "lens": -0.05, "sharpen": 0.35},
    "cool":    {"wb": (1.07, 1.01, 0.94), "sat": 0.78, "contrast": 1.08, "gamma": 1.04, "noise": 0.020, "vignette": 0.28, "lens": -0.08, "sharpen": 0.45},
    "warm":    {"wb": (0.92, 0.99, 1.07), "sat": 0.82, "contrast": 1.02, "gamma": 0.97, "lift": 0.01, "noise": 0.022, "vignette": 0.25, "lens": -0.06, "sharpen": 0.30},
    "flat":    {"wb": (1.02, 1.03, 0.98), "sat": 0.70, "contrast": 0.94, "gamma": 1.02, "lift": 0.02, "noise": 0.024, "vignette": 0.30, "lens": -0.10, "soft": 0.5, "sharpen": 0.55},
    "dome":    {"wb": (1.03, 1.00, 0.97), "sat": 0.80, "contrast": 1.06, "gamma": 1.0, "noise": 0.018, "vignette": 0.34, "lens": -0.14, "sharpen": 0.4},
}


def feed(id, factory, camera, title, kind, src, seg, stab="lock", **kw):
    return {"id": id, "factory": factory, "camera": camera, "title": title, "kind": kind, "src": src, "seg": seg,
            "stab": stab, **kw}


FEEDS = [
    # ── Kernersville, NC ───────────────────────────────────────────────
    feed("knv-04", "KNV", "CAM-KNV-04", "Carton packing · club display pallet", "packing", "display", (0.0, 29.45),
         look=LOOK["neutral"]),
    feed("knv-05", "KNV", "CAM-KNV-05", "Carton packing · retail cartons", "packing", "cartons", (0.0, 25.6),
         look=LOOK["neutral"]),
    feed("knv-02", "KNV", "CAM-KNV-02", "Sewing line · outfeed conveyor", "line", "walk_a", (31.0, 40.9),
         look=LOOK["cool"]),
    feed("knv-01", "KNV", "CAM-KNV-01", "Pillow filling · machine hall", "area", "walk_a", (0.0, 11.0), stab="smooth",
         smooth_s=2.2, look=LOOK["dome"], ptz=True),
    feed("knv-03", "KNV", "CAM-KNV-03", "Compression & bagging", "line", "walk_b", (44.0, 58.0), stab="smooth",
         smooth_s=2.0, portrait_y=0.62, look=LOOK["warm"], ptz=True),
    feed("knv-06", "KNV", "CAM-KNV-06", "Final inspection · dispatch lane", "area", "walk_a", (72.0, 83.3), stab="smooth",
         smooth_s=2.4, look=LOOK["neutral"], ptz=True),
    feed("knv-07", "KNV", "CAM-KNV-07", "Fibre opening · feed bins", "area", "walk_b", (0.0, 11.0), stab="smooth",
         smooth_s=2.0, portrait_y=0.6, look=LOOK["flat"], ptz=True),
    feed("knv-08", "KNV", "CAM-KNV-08", "Carding & bun formation", "area", "walk_b", (18.0, 34.0), stab="smooth",
         smooth_s=2.4, portrait_y=0.55, look=LOOK["cool"], ptz=True),

    # ── Phoenix, AZ ────────────────────────────────────────────────────
    feed("phx-04", "PHX", "CAM-PHX-04", "Carton packing · retail cartons", "packing", "cartons", (0.0, 25.6),
         crop=(0.40, 0.08, 1.0, 0.92), tilt=0.03, look=LOOK["warm"]),
    feed("phx-05", "PHX", "CAM-PHX-05", "Carton packing · club display pallet", "packing", "display", (0.0, 29.45),
         crop=(0.0, 0.06, 0.70, 0.94), roll=-1.2, look=LOOK["warm"]),
    feed("phx-02", "PHX", "CAM-PHX-02", "Sewing line · conveyor tour", "line", "walk_a", (41.0, 57.0), stab="smooth",
         smooth_s=2.6, look=LOOK["warm"], ptz=True),
    feed("phx-01", "PHX", "CAM-PHX-01", "Pillow filling · shell loading", "area", "walk_a", (17.5, 24.5), stab="smooth",
         smooth_s=2.0, look=LOOK["warm"], ptz=True),
    feed("phx-03", "PHX", "CAM-PHX-03", "Compression · roller outfeed", "line", "walk_b", (52.0, 68.0), stab="smooth",
         smooth_s=2.2, portrait_y=0.66, look=LOOK["dome"], ptz=True),
    feed("phx-06", "PHX", "CAM-PHX-06", "Final inspection · carton staging", "area", "walk_a", (63.5, 72.0), stab="smooth",
         smooth_s=2.4, look=LOOK["warm"], ptz=True),

    # ── Columbus, OH ───────────────────────────────────────────────────
    feed("cmh-04", "CMH", "CAM-CMH-04", "Carton packing · retail cartons", "packing", "cartons", (28.2, 38.9),
         look=LOOK["cool"]),
    feed("cmh-05", "CMH", "CAM-CMH-05", "Carton packing · retail cartons (west)", "packing", "cartons", (0.0, 25.6),
         crop=(0.0, 0.10, 0.62, 0.95), tilt=0.035, roll=0.8, look=LOOK["cool"]),
    feed("cmh-02", "CMH", "CAM-CMH-02", "Sewing line · outfeed conveyor", "line", "walk_a", (31.0, 40.9),
         crop=(0.22, 0.08, 1.0, 0.92), tilt=0.02, look=LOOK["flat"]),
    feed("cmh-01", "CMH", "CAM-CMH-01", "Pillow filling · chute & display box", "area", "walk_b", (34.0, 44.0), stab="smooth",
         smooth_s=2.0, portrait_y=0.6, look=LOOK["cool"], ptz=True),
    feed("cmh-03", "CMH", "CAM-CMH-03", "Compression & bagging", "line", "walk_b", (44.0, 58.0), stab="smooth",
         smooth_s=2.0, portrait_y=0.70, look=LOOK["flat"], ptz=True),
    feed("cmh-06", "CMH", "CAM-CMH-06", "Final inspection · dispatch lane", "area", "walk_a", (72.0, 83.3), stab="smooth",
         smooth_s=2.4, crop=(0.15, 0.06, 1.0, 0.94), look=LOOK["cool"], ptz=True),
]


# -- CV configuration per camera (output-frame coordinates, 960x540) -----
DISPLAY_REC = {
    "customer_code": "CST", "customer": "Costco", "brand": "Beautyrest BLACK",
    "product": "Cooling memory-foam + gel-fibre 2-pack bed pillows", "label": "Club display pallet carton",
    "rule": "Club full-pallet display print (Beautyrest BLACK, 2-pack) -> club-channel order",
    "vocab": ["Beautyrest", "BLACK", "COOLING", "2 PACK"],
}
CARTON_REC = {
    "customer_code": "WMT", "customer": "Walmart", "brand": "Serta",
    "product": "Serta bed pillows", "label": "Department-coded RFID carton label",
    "rule": "Retail carton label 'Dept. #22 / Bedding' with RFID mark -> Walmart department-coded label",
    "vocab": ["Dept. #22", "Bedding", "SERTA BED PILLOWS", "RFID"],
}
SERTA_REC = {"brand": "Serta", "product": "Serta bed pillows (cartoned)", "label": "Carton print",
             "rule": "Carton print read for staging verification", "vocab": ["Serta"]}
BEAUTYREST_REC = {"brand": "Beautyrest", "product": "Beautyrest memory-rest pillows", "label": "Retail box print",
                  "rule": "Retail box print read at the filling chute", "vocab": ["Beautyrest", "MEMORY REST"]}

WHITE_BAGS = {"sat_max": 55, "val_min": 178, "min_area": 900}
WHITE_PILLOWS = {"sat_max": 42, "val_min": 192, "min_area": 700}


def z(id, kind, name, pts, **kw):
    return {"id": id, "kind": kind, "name": name, "pts": pts, **kw}


CV = {
    "knv-04": {
        "zones": [
            z("cA", "carton", "Display carton", [[45, 150], [410, 140], [410, 250], [290, 262], [45, 258]],
              per_event=2, capacity=48, base=22, descend=30, min_travel=30, debounce=10),
            z("sB", "station", "Packer B (behind display)", [[50, 140], [280, 140], [280, 275], [50, 275]]),
            z("sA", "station", "Packer A", [[290, 380], [720, 380], [720, 540], [290, 540]]),
            z("lp1", "restricted", "FG aisle · badge-only", [[600, 190], [800, 190], [800, 262], [600, 262]]),
        ],
        "roi": [[[20, 0], [620, 0], [620, 300], [20, 300]]],
        "pillow_params": WHITE_BAGS, "recognition": DISPLAY_REC, "ocr_every": 2.0,
    },
    "phx-05": {
        "zones": [
            z("cA", "carton", "Display carton", [[80, 165], [440, 160], [440, 252], [80, 258]],
              per_event=2, capacity=48, base=10, descend=30, min_travel=30, debounce=10),
            z("sB", "station", "Packer B (behind display)", [[90, 120], [330, 120], [330, 270], [90, 270]]),
            z("sA", "station", "Packer A", [[400, 380], [800, 380], [800, 540], [400, 540]]),
        ],
        "roi": [[[40, 0], [700, 0], [700, 300], [40, 300]]],
        "pillow_params": WHITE_BAGS, "recognition": DISPLAY_REC, "ocr_every": 2.0,
    },
    "knv-05": {
        "zones": [
            z("cA", "carton", "Carton A", [[338, 285], [430, 285], [430, 355], [338, 355]], spec=4, hands=True, min_travel=30, debounce=18),
            z("cB", "carton", "Carton B", [[528, 285], [622, 285], [622, 355], [528, 355]], spec=4, hands=True, min_travel=30, debounce=18),
            z("sA", "station", "Packer A", [[250, 380], [430, 380], [430, 540], [250, 540]]),
            z("sB", "station", "Packer B", [[580, 400], [750, 400], [750, 540], [580, 540]]),
        ],
        "roi": [[[230, 170], [770, 170], [770, 380], [230, 380]]],
        "pillow_params": WHITE_PILLOWS, "recognition": CARTON_REC, "ocr_every": 2.0,
    },
    "phx-04": {
        "zones": [
            z("cB", "carton", "Carton B", [[262, 290], [405, 290], [405, 362], [262, 362]], spec=4, hands=True, min_travel=30, debounce=18),
            z("sB", "station", "Packer B", [[330, 420], [570, 420], [570, 540], [330, 540]]),
            z("lp1", "restricted", "Staging lane · badge-only", [[0, 262], [205, 262], [205, 380], [0, 380]]),
        ],
        "roi": [[[150, 170], [620, 170], [620, 400], [150, 400]]],
        "pillow_params": WHITE_PILLOWS, "recognition": CARTON_REC, "ocr_every": 2.0,
    },
    "cmh-04": {
        "zones": [
            z("cB", "carton", "Carton B", [[495, 320], [600, 320], [600, 398], [495, 398]], spec=4, hands=True, min_travel=30, debounce=18),
            z("sB", "station", "Packer B", [[540, 420], [770, 420], [770, 540], [540, 540]]),
            z("lp1", "restricted", "Pillow staging · badge-only", [[120, 250], [340, 250], [340, 322], [120, 322]]),
        ],
        "roi": [[[380, 180], [760, 180], [760, 420], [380, 420]]],
        "pillow_params": WHITE_PILLOWS, "recognition": CARTON_REC, "ocr_every": 1.5,
    },
    "cmh-05": {
        "zones": [
            z("cA", "carton", "Carton A", [[538, 270], [662, 270], [662, 342], [538, 342]], spec=4, hands=True, min_travel=30, debounce=18),
            z("sA", "station", "Packer A", [[430, 400], [630, 400], [630, 540], [430, 540]]),
            z("lp1", "restricted", "Carton staging · badge-only", [[810, 225], [960, 225], [960, 352], [810, 352]]),
        ],
        "roi": [[[400, 150], [840, 150], [840, 370], [400, 370]]],
        "pillow_params": WHITE_PILLOWS, "recognition": CARTON_REC, "ocr_every": 2.0,
    },
    "knv-02": {
        "zones": [
            z("s1", "station", "Sewer 1", [[330, 280], [520, 280], [520, 400], [330, 400]]),
            z("s2", "station", "Sewer 2", [[680, 260], [820, 260], [820, 400], [680, 400]]),
        ],
        "conveyor": {"name": "Outfeed belt", "pts": [[40, 372], [960, 372], [960, 422], [40, 425]], "m_per_px": 0.0048},
        "count_line": [[600, 290], [600, 360]],
        "roi": [[[0, 290], [960, 290], [960, 430], [0, 430]]],
        "pillow_params": {"sat_max": 50, "val_min": 175, "min_area": 900},
    },
    "cmh-02": {
        "zones": [
            z("s1", "station", "Sewer 1", [[200, 260], [380, 260], [380, 410], [200, 410]]),
            z("s2", "station", "Sewer 2", [[620, 250], [790, 250], [790, 410], [620, 410]]),
        ],
        "conveyor": {"name": "Outfeed belt", "pts": [[0, 385], [960, 382], [960, 440], [0, 445]], "m_per_px": 0.0036},
        "count_line": [[500, 290], [500, 370]],
        "roi": [[[0, 290], [960, 290], [960, 450], [0, 450]]],
        "pillow_params": {"sat_max": 50, "val_min": 175, "min_area": 1100},
    },
    "knv-06": {"recognition": SERTA_REC, "ocr_every": 2.0},
    "cmh-06": {"recognition": SERTA_REC, "ocr_every": 2.0},
    "phx-06": {"recognition": SERTA_REC, "ocr_every": 2.0},
    "cmh-01": {"recognition": BEAUTYREST_REC, "ocr_every": 2.0},
}
for _f in FEEDS:
    _f.update(CV.get(_f["id"], {}))
